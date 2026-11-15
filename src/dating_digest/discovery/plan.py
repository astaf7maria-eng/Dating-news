"""Provider-independent search plans; does not perform network requests."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import re
from urllib.parse import urlsplit, urlunsplit

from dating_digest.domain.window import validate_window
from dating_digest.domain.context import validate_context_bundle
from dating_digest.storage.archive import normalize_url


def _or(terms: list[str]) -> str:
    if not terms:
        raise ValueError("Search group must contain at least one term")
    return "(" + " OR ".join('"' + term.replace('"', '') + '"' for term in dict.fromkeys(terms)) + ")"


def _base_plan(catalog: dict, sources: dict, rules: dict, manifest: dict,
               context_bundle: dict | None) -> dict:
    if manifest.get("window_confirmation", {}).get("status") != "confirmed":
        raise ValueError("Сначала выберите и подтвердите период")
    start, end = validate_window(
        datetime.fromisoformat(manifest["window_start"]),
        datetime.fromisoformat(manifest["window_end_exclusive"]),
    )
    companies = [c for c in catalog["companies"] if c["enabled"]]
    required = set(catalog["required_always_company_ids"] + catalog["golden_set_company_ids"]
                   + catalog["golden_adjacent_company_ids"])
    if not required <= {company["id"] for company in companies}:
        raise ValueError("Обязательную компанию нельзя отключить или удалить из плана")
    saved_context = manifest.get("business_context")
    if saved_context and context_bundle is None:
        raise ValueError("Run context snapshot is missing; create a new run")
    if context_bundle is not None:
        validate_context_bundle(context_bundle)
        if saved_context and (saved_context.get("sha256") != context_bundle["context_sha256"]
                              or saved_context.get("version") != context_bundle["context_version"]):
            raise ValueError("Context bundle does not match run manifest")
    for source in sources["sources"]:
        if source["enabled"] and not urlsplit(source["url"]).hostname:
            raise ValueError(f"Invalid source URL: {source['id']}")

    result = {
        "schema_version": "0.9", "run_id": manifest["run_id"],
        "window_start": start.isoformat(), "window_end_exclusive": end.isoformat(),
        "timezone": "Europe/Moscow", "status": "planned_not_executed",
        "business_context_usage": "so_what_only_no_revalidation",
        "config_versions": {"companies": catalog["version"], "sources": sources["version"], "queries": rules["version"]},
        "config_snapshot": {"companies": catalog, "sources": sources, "queries": rules},
        "collection_policy": catalog["collection_policy"],
        "coverage": [{"company_id": c["id"], "required": c["id"] in required,
                      "query_ids": [], "status": "planned", "articles_saved": None} for c in companies],
        "execution_policy": {
            "source_order": "Выполнять search_tasks по search_priority: 1 — официальные страницы для медиа/инвесторов, 2 — остальные официальные источники, 3 — ключевые СМИ по ключевым словам, 4 — широкий поиск. Найденное официальное сообщение не отменяет проверку СМИ и остальное обязательное покрытие.",
            "coverage": "Все query_id сохраняются; результат языкового варианта относится ко всем его query_id.",
            "languages": "Выполнить все language_variants. Объединять языки только при явной поддержке провайдером поиска по всем указанным языкам; иначе выполнять отдельно.",
            "request_identity": "Перед переиспользованием результата сверять провайдера, текст, языковые/региональные фильтры, окно, страницу и остальные параметры запроса. task_id не является ключом кеша провайдера.",
            "completion": "Покрытие подтверждать выполненными вариантами, страницами результатов и сохранёнными материалами; наличие задания не доказывает выполнение.",
        },
        "notes": "План не является результатом поиска. Даты передаются адаптеру провайдера; полнота и сохранение проверяются после выполнения.",
        "context_status": "available" if context_bundle is not None else "not_available",
    }
    if context_bundle is not None:
        result["config_versions"]["business_context"] = context_bundle["context_version"]
        result["context_sha256"] = context_bundle["context_sha256"]
    return result


def _build_exhaustive_plan(catalog: dict, sources: dict, rules: dict, result: dict) -> dict:
    companies = [c for c in catalog["companies"] if c["enabled"]]
    queries, seen = [], set()

    def add(lane, language, family, query, company_ids=(), source_id=None, model_id=None,
            decision_id=None):
        identity = f"{lane}|{language}|{family}|{query}|{','.join(company_ids)}|{source_id}|{model_id}|{decision_id}"
        query_id = sha256(identity.encode("utf-8")).hexdigest()[:20]
        if query_id in seen:
            return
        seen.add(query_id)
        queries.append({
            "query_id": query_id, "lane": lane, "language": language,
            "family": family, "query": query, "company_ids": list(company_ids),
            "source_id": source_id, "model_id": model_id, "status": "planned",
            "decision_id": decision_id,
        })

    for company in companies:
        for language in company["search_languages"]:
            base = _or([company["name"], *company["aliases"]])
            context = company.get("disambiguation_terms", {}).get(language, [])
            if context:
                base += " " + _or(context)
            add("company", language, "baseline", base, [company["id"]])
            profile = rules["company_profiles"][company["tier"]]
            for section in profile["thematic_sections"]:
                terms = rules["category_signals"][section].get(language)
                if terms:
                    add("company", language, section, base + " " + _or(terms), [company["id"]])

    by_id = {c["id"]: c for c in companies}
    source_policy = sources.get("source_policy", {})
    key_media = source_policy.get("key_media_ids", [])
    for source in sources["sources"]:
        if not source["enabled"]:
            continue
        ids = [c for c in source.get("company_ids", []) if c in by_id]
        host = urlsplit(source["url"]).hostname
        if ids and (source["kind"].startswith("official_") or source["kind"] == "investor_relations"):
            # No launch-only filter: reports, surveys and policies also matter.
            names = [alias for cid in ids for alias in [by_id[cid]["name"], *by_id[cid]["aliases"]]]
            add("official_source", source["language"], "source_lookup",
                f"site:{host} " + _or(list(dict.fromkeys(names))), ids, source["id"])

    discovery = rules["discovery"]
    for source in sources["sources"]:
        if source["enabled"] and source["id"] in key_media:
            language = source["language"]
            host = urlsplit(source["url"]).hostname
            add("media_review", language, "keyword_lookup",
                f"site:{host} {_or(discovery['context'][language])} "
                + _or(source_policy["media_keywords"][language]), source_id=source["id"])

    if discovery["enabled"]:
        for language in rules["languages"]:
            context = _or(discovery["context"][language])
            novelty = _or(discovery["novelty_signals"][language])
            for pattern in discovery["business_model_patterns"]:
                add("discovery", language, "new_models",
                    f"{context} {_or(pattern['phrases'][language])} {novelty}", model_id=pattern["id"])
            # Broad query finds mechanisms outside our predefined model vocabulary.
            add("discovery", language, "new_models", f"{context} {novelty}", model_id="open_ended")
        for item in discovery["native_language_queries"]:
            add("discovery", item["language"], "new_models", item["query"], model_id=item["id"])
        for source in sources["sources"]:
            if source["enabled"] and source["id"] in discovery["source_seed_ids"]:
                host = urlsplit(source["url"]).hostname
                add("discovery", source["language"], "new_models",
                    f"site:{host} " + _or(["dating startup", "new dating app", "matchmaking launch"]),
                    source_id=source["id"], model_id="industry_source")
        for item in discovery.get("risk_queries", []):
            add("risk_monitoring", item["language"], "trust_safety", item["query"])

    # Official media/IR pages first, other official pages next, then key media.
    # Broad search stays mandatory and can find facts absent from company PR.
    source_by_id = {source["id"]: source for source in sources["sources"]}
    for query in queries:
        kind = source_by_id.get(query["source_id"], {}).get("kind")
        if query["lane"] == "official_source":
            query["search_priority"] = 1 if kind in {"official_newsroom", "investor_relations"} else 2
        else:
            query["search_priority"] = 3 if query["source_id"] in key_media else 4
    media_order = {source_id: index for index, source_id in enumerate(key_media)}
    queries.sort(key=lambda query: (query["search_priority"], media_order.get(query["source_id"], 0)))

    # Keep editorial coverage identities separate from retrieval work. Identical
    # text shares a task, but different language filters remain distinct requests.
    # Providers may combine those variants only if every language is supported.
    tasks = {}
    for query in queries:
        task = tasks.setdefault(query["query"], {
            "task_id": sha256(
                f"{result['window_start']}|{result['window_end_exclusive']}|{query['query']}".encode("utf-8")
            ).hexdigest()[:20],
            "query": query["query"], "search_priority": query["search_priority"], "language_variants": {},
        })
        task["language_variants"].setdefault(query["language"], []).append(query["query_id"])

    result.update(queries=queries, query_count=len(queries), search_tasks=list(tasks.values()),
                  task_count=len(tasks),
                  language_request_count=sum(len(task["language_variants"]) for task in tasks.values()))
    for company in result["coverage"]:
        company["query_ids"] = [q["query_id"] for q in queries if company["company_id"] in q["company_ids"]]
    return result


def build_plan(catalog: dict, sources: dict, rules: dict, manifest: dict,
               context_bundle: dict | None = None, *, strategy: str = "fast") -> dict:
    """Keep coverage targets, but schedule retrieval in two passes, without I/O.

    Both strategies share snapshot/context validation. Only the selected query
    set is constructed; the exhaustive plan remains available for comparisons.
    """
    if strategy not in {"fast", "exhaustive"}:
        raise ValueError(f"Unknown search strategy: {strategy}")
    result = _base_plan(catalog, sources, rules, manifest, context_bundle)
    if strategy == "exhaustive":
        return _build_exhaustive_plan(catalog, sources, rules, result)

    companies = [c for c in catalog["companies"] if c["enabled"]]
    by_id = {c["id"]: c for c in companies}
    queries = []

    def add(lane, language, family, text, *, phase="if_gap", company_ids=(),
            source_id=None, model_id=None, priority=4, trigger=None):
        identity = [lane, language, family, text, list(company_ids), source_id, model_id]
        qid = sha256(repr(identity).encode("utf-8")).hexdigest()[:20]
        queries.append({"query_id": qid, "lane": lane, "language": language,
                        "family": family, "query": text, "company_ids": list(company_ids),
                        "source_id": source_id, "model_id": model_id, "decision_id": None,
                        "status": "planned", "search_priority": priority, "phase": phase,
                        "trigger": trigger})

    def name_for(company, language):
        aliases = company["aliases"]
        pattern = {"ru": r"[А-Яа-я]", "zh": r"[\u4e00-\u9fff]", "ja": r"[\u3040-\u30ff]"}.get(language)
        native = next((a for a in aliases if pattern and re.search(pattern, a)), None)
        if language == "en":
            native = next((a for a in aliases if re.search(r"[A-Za-z]", a)
                           and not re.search(r"[А-Яа-я\u4e00-\u9fff\u3040-\u30ff]", a)), None)
        return native or company["name"].split(" / ")[-1]

    for c in companies:
        primary = "ru" if c["tier"] == "russia" else "en"
        if primary not in c["search_languages"]:
            primary = c["search_languages"][0]
        for lang in c["search_languages"]:
            # A real language-specific phrase, even when the provider has no
            # hard language filter. Labels alone must never establish coverage.
            context = c.get("disambiguation_terms", {}).get(lang, [])
            suffix = context[0] if context else {"ru": "новости", "en": "news", "zh": "相亲", "ja": "ニュース"}.get(lang, "")
            add("company", lang, "baseline", f'{name_for(c, lang)} {suffix}'.strip(),
                phase="first_pass" if lang == primary else "if_gap", company_ids=[c["id"]],
                trigger=None if lang == primary else "language_not_checked")

    policy = sources.get("source_policy", {})
    media_ids = policy.get("key_media_ids", [])
    indexes = {}
    for source in sources["sources"]:
        if not source["enabled"]:
            continue
        parts = urlsplit(source["url"])
        ids = [cid for cid in source["company_ids"] if cid in by_id]
        if ids and (source["kind"].startswith("official_") or source["kind"] == "investor_relations"):
            priority = 1 if source["kind"] in {"official_newsroom", "investor_relations"} else 2
            # Some catalog URLs are known articles. They are reference seeds,
            # never discoveries. Navigate from a neutral section instead.
            path = parts.path
            reference = bool(re.search(r"/(?:articles|announcements)/[^/]+", path))
            entry_path = "/articles/" if "/articles/" in path else "/blog/"
            url = urlunsplit((parts.scheme, parts.netloc, entry_path, "", "")) if reference else source["url"]
            key = normalize_url(url)
            entry = indexes.setdefault(key, {"url": key, "origin": "source_index", "source_ids": [],
                                             "company_ids": [], "search_priority": priority})
            entry["source_ids"].append(source["id"])
            entry["company_ids"] = sorted(set(entry["company_ids"]) | set(ids))
            entry["search_priority"] = min(entry["search_priority"], priority)
            primary_company = by_id[ids[0]]
            context = primary_company.get("disambiguation_terms", {}).get(source["language"], [])
            lookup = f'site:{parts.hostname} {name_for(primary_company, source["language"])}'
            if context:
                lookup += " " + context[0]
            add("official_source", source["language"], "source_lookup",
                lookup,
                company_ids=ids, source_id=source["id"], priority=priority,
                trigger="index_failed_or_window_not_reached")
        if source["id"] in media_ids:
            lang = source["language"]
            add("media_review", lang, "keyword_lookup",
                f'site:{parts.hostname} {rules["discovery"]["context"][lang][0]}',
                phase="first_pass", source_id=source["id"], priority=3)

    discovery = rules["discovery"]
    if discovery["enabled"]:
        for lang in rules["languages"]:
            context = discovery["context"][lang][0]
            add("discovery", lang, "new_models", context, phase="first_pass", model_id="open_ended")
            for pattern in discovery["business_model_patterns"]:
                add("discovery", lang, "new_models", f'{context} {pattern["phrases"][lang][0]}',
                    model_id=pattern["id"], trigger="model_not_checked")
        for item in discovery["native_language_queries"]:
            # Keep a broad native search first; the full alternative vocabulary
            # remains in the frozen config for gap-driven follow-up.
            words = re.findall(r'"([^"\n]+)"', item["query"])
            add("discovery", item["language"], "new_models", words[0] if words else item["query"],
                phase="first_pass", model_id=item["id"])
        for item in discovery.get("risk_queries", []):
            words = re.findall(r'"([^"\n]+)"', item["query"])
            text = " ".join([words[0], words[2]]) if len(words) > 2 else item["query"]
            add("risk_monitoring", item["language"], "trust_safety", text, phase="first_pass", model_id=item["id"])
    for section, signals in rules["category_signals"].items():
        for lang in rules["languages"]:
            add("topic_review", lang, section,
                f'{discovery["context"][lang][0]} {signals[lang][0]}', trigger="topic_not_checked")

    queries.sort(key=lambda q: (q["search_priority"], media_ids.index(q["source_id"]) if q["source_id"] in media_ids else 0))
    tasks = {}
    for q in queries:
        key = (q["query"], q["phase"])
        task = tasks.setdefault(key, {"task_id": sha256(
            f'{result["window_start"]}|{result["window_end_exclusive"]}|{key}'.encode("utf-8")
        ).hexdigest()[:20], "query": q["query"], "phase": q["phase"],
            "search_priority": q["search_priority"], "language_variants": {}})
        task["language_variants"].setdefault(q["language"], []).append(q["query_id"])
    result.update(strategy="fast", queries=queries, query_count=len(queries),
                  search_tasks=list(tasks.values()), task_count=len(tasks),
                  language_request_count=sum(len(t["language_variants"]) for t in tasks.values()),
                  initial_query_count=sum(q["phase"] == "first_pass" for q in queries),
                  source_indexes=sorted(indexes.values(), key=lambda x: x["search_priority"]))
    for c in result["coverage"]:
        company = by_id[c["company_id"]]
        c.update(query_ids=[q["query_id"] for q in queries if c["company_id"] in q["company_ids"]],
                 languages=company["search_languages"], aliases=company["aliases"],
                 themes=rules["company_profiles"][company["tier"]]["thematic_sections"])
    result["coverage_targets"] = {"topics": list(rules["category_signals"]),
                                  "models": [p["id"] for p in discovery["business_model_patterns"]],
                                  "risks": discovery.get("risk_queries", []),
                                  "native_languages": [x["language"] for x in discovery["native_language_queries"]]}
    result["execution_policy"].update(
        passes="Navigate source_indexes once; first_pass queries; then only triggered if_gap queries. All unverified targets remain explicit gaps.",
        budget={"total_seconds": 900, "save_reserve_seconds": 60, "search_request_target": [40, 60],
                "search_request_reserve": 80, "max_inflight": 4, "max_per_domain": 2,
                "request_timeout_seconds": 25, "transient_retries": 1, "cache_max_age_seconds": 86400},
        language_evidence="No hard filter: record actual phrase and observed result languages; a shared brand/group query never proves every member or language.",
        completion="Conditional tasks are not executed coverage. Save pending targets and reasons at deadline; a request target is not a quality quota.")
    return result
