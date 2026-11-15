"""Provider-independent search plans; does not perform network requests."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from urllib.parse import urlsplit

from dating_digest.domain.window import validate_window
from dating_digest.domain.context import validate_context_bundle


def _or(terms: list[str]) -> str:
    if not terms:
        raise ValueError("Search group must contain at least one term")
    return "(" + " OR ".join('"' + term.replace('"', '') + '"' for term in dict.fromkeys(terms)) + ")"


def build_plan(catalog: dict, sources: dict, rules: dict, manifest: dict,
               context_bundle: dict | None = None) -> dict:
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
        if not host:
            raise ValueError(f"Invalid source URL: {source['id']}")
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

    if context_bundle is not None:
        validate_context_bundle(context_bundle)
        if saved_context and (saved_context.get("sha256") != context_bundle["context_sha256"]
                              or saved_context.get("version") != context_bundle["context_version"]):
            raise ValueError("Context bundle does not match run manifest")

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
                f"{start.isoformat()}|{end.isoformat()}|{query['query']}".encode("utf-8")
            ).hexdigest()[:20],
            "query": query["query"], "search_priority": query["search_priority"], "language_variants": {},
        })
        task["language_variants"].setdefault(query["language"], []).append(query["query_id"])

    result = {
        "schema_version": "0.9", "run_id": manifest["run_id"],
        "window_start": start.isoformat(), "window_end_exclusive": end.isoformat(),
        "timezone": "Europe/Moscow", "status": "planned_not_executed",
        "business_context_usage": "so_what_only_no_revalidation",
        "config_versions": {"companies": catalog["version"], "sources": sources["version"], "queries": rules["version"]},
        "config_snapshot": {"companies": catalog, "sources": sources, "queries": rules},
        "collection_policy": catalog["collection_policy"],
        "coverage": [{"company_id": cid, "required": cid in required,
                      "query_ids": [q["query_id"] for q in queries if cid in q["company_ids"]],
                      "status": "planned", "articles_saved": None} for cid in by_id],
        "query_count": len(queries), "queries": queries,
        "task_count": len(tasks), "search_tasks": list(tasks.values()),
        "language_request_count": sum(len(task["language_variants"]) for task in tasks.values()),
        "execution_policy": {
            "source_order": "Выполнять search_tasks по search_priority: 1 — официальные страницы для медиа/инвесторов, 2 — остальные официальные источники, 3 — ключевые СМИ по ключевым словам, 4 — широкий поиск. Найденное официальное сообщение не отменяет проверку СМИ и остальное обязательное покрытие.",
            "coverage": "Все query_id сохраняются; результат языкового варианта относится ко всем его query_id.",
            "languages": "Выполнить все language_variants. Объединять языки только при явной поддержке провайдером поиска по всем указанным языкам; иначе выполнять отдельно.",
            "request_identity": "Перед переиспользованием результата сверять провайдера, текст, языковые/региональные фильтры, окно, страницу и остальные параметры запроса. task_id не является ключом кеша провайдера.",
            "completion": "Покрытие подтверждать выполненными вариантами, страницами результатов и сохранёнными материалами; наличие задания не доказывает выполнение.",
        },
        "notes": "План не является результатом поиска. Даты передаются адаптеру провайдера; полнота и сохранение проверяются после выполнения.",
    }
    result["context_status"] = "available" if context_bundle is not None else "not_available"
    if context_bundle is not None:
        result["config_versions"]["business_context"] = context_bundle["context_version"]
        result["context_sha256"] = context_bundle["context_sha256"]
    return result
