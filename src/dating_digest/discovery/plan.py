"""Provider-independent search plans; does not perform network requests."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import re
from urllib.parse import urlsplit, urlunsplit

from dating_digest.domain.window import validate_window
from dating_digest.domain.context import validate_context_bundle
from dating_digest.storage.archive import normalize_url
from dating_digest.discovery.queries import build_followup_query, company_search_name, require_fast_policy


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
        "schema_version": "1.1", "strategy": "fast", "run_id": manifest["run_id"],
        "window_start": start.isoformat(), "window_end_exclusive": end.isoformat(),
        "timezone": "Europe/Moscow", "status": "planned_not_executed",
        "business_context_usage": "editorial_only_no_revalidation",
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


def build_plan(catalog: dict, sources: dict, rules: dict, manifest: dict,
               context_bundle: dict | None = None) -> dict:
    """Build the source-first plan used by collect-news-fast.md; no network."""
    require_fast_policy(rules)
    result = _base_plan(catalog, sources, rules, manifest, context_bundle)
    focus = rules["research_policy"]
    directions = focus["primary_directions"]
    vocabulary = focus["directions"]
    companies = [c for c in catalog["companies"] if c["enabled"]]
    by_id = {c["id"]: c for c in companies}
    queries = []

    def add(lane, language, family, text, *, phase="if_gap", company_ids=(),
            source_id=None, model_id=None, direction_id=None, priority=4, trigger=None,
            query_spec=None):
        if query_spec is None and not text.startswith("site:"):
            domains = rules["retrieval_policy"].get("excluded_domains", [])
            if domains:
                query_spec = {"intent": family, "exclude_domains": domains,
                              "query_without_domain_exclusions": text}
                text += "".join(f" -site:{domain}" for domain in domains)
        identity = [lane, language, family, text, list(company_ids), source_id, model_id, direction_id]
        qid = sha256(repr(identity).encode("utf-8")).hexdigest()[:20]
        queries.append({"query_id": qid, "lane": lane, "language": language,
                        "family": family, "query": text, "company_ids": list(company_ids),
                        "source_id": source_id, "model_id": model_id, "decision_id": None,
                        "status": "planned", "search_priority": priority, "phase": phase,
                        "trigger": trigger, "direction_id": direction_id})
        if query_spec:
            queries[-1].update(query_intent=query_spec["intent"],
                               exclude_domains=query_spec["exclude_domains"],
                               query_without_domain_exclusions=query_spec["query_without_domain_exclusions"])

    def primary_language(company):
        language = company.get("primary_search_language", "ru" if company["tier"] == "russia" else "en")
        return language if language in company["search_languages"] else company["search_languages"][0]

    def company_name(company, language):
        name = company_search_name(company, language)
        context = company.get("disambiguation_terms", {}).get(language, [])
        if context:
            name += " " + context[0]
        elif company["tier"] == "golden_adjacent":
            name += " " + rules["discovery"]["context"][language][0]
        return name

    # Source reviews can satisfy several brands and directions at once. None
    # of these fallbacks is an instruction to execute a Cartesian product.
    external = rules["external_search"]
    for c in companies:
        primary = primary_language(c)
        for lang in sorted(c["search_languages"], key=lambda value: value != primary):
            query_spec = build_followup_query(result, language=lang, intent="product_change", company_id=c["id"])
            add("company", lang, "event_lookup",
                query_spec["query"], query_spec=query_spec,
                company_ids=[c["id"]],
                trigger="company_not_checked" if lang == primary else "language_not_checked")
        query_spec = build_followup_query(result, language=primary, intent="interview", company_id=c["id"])
        add("external_company", primary, "external_lookup", query_spec["query"],
            company_ids=[c["id"]], query_spec=query_spec,
            trigger="external_source_gap_or_interview_result_signal")
        queries[-1]["intent_options"] = external["intents"]

    policy = sources.get("source_policy", {})
    media_ids = policy.get("key_media_ids", [])
    indexes = {}
    for source in sources["sources"]:
        if not source["enabled"]:
            continue
        parts = urlsplit(source["url"])
        ids = [cid for cid in source["company_ids"] if cid in by_id]
        official = bool(ids) and (source["kind"].startswith("official_") or source["kind"] == "investor_relations")
        media = source["id"] in media_ids
        if official or media:
            priority = (1 if source["kind"] in {"official_newsroom", "investor_relations"} else 2) if official else 3
            # Some catalog URLs are known articles. They are reference seeds,
            # never discoveries. Navigate from a neutral section instead.
            path = parts.path
            reference = bool(re.search(r"/(?:articles|announcements)/[^/]+", path))
            entry_path = "/articles/" if "/articles/" in path else "/blog/"
            url = urlunsplit((parts.scheme, parts.netloc, entry_path, "", "")) if reference else source["url"]
            key = normalize_url(url)
            entry = indexes.setdefault(key, {"url": key, "origin": "source_index", "source_ids": [],
                                             "company_ids": [], "search_priority": priority,
                                             "direction_ids": list(directions),
                                             "index_kind": source.get("index_kind", "entry_point"),
                                             "review": {"status": "planned", "feed_available": None,
                                                        "checked_through": None, "end_of_feed": None,
                                                        "next_page_url": None, "evidence_urls": []}})
            entry["source_ids"].append(source["id"])
            entry["company_ids"] = sorted(set(entry["company_ids"]) | set(ids))
            entry["search_priority"] = min(entry["search_priority"], priority)
            if source.get("index_kind") == "publication_feed":
                entry["index_kind"] = "publication_feed"
        if official:
            primary_company = by_id[ids[0]]
            scope = parts.hostname + (parts.path if source.get("index_kind") == "publication_feed" else "")
            lookup = f'site:{scope} {company_name(primary_company, source["language"])}'
            add("official_source", source["language"], "source_lookup",
                lookup,
                company_ids=ids, source_id=source["id"], priority=priority,
                trigger="index_failed_or_window_not_reached")
        if media:
            lang = source["language"]
            for direction in directions:
                add("media_review", lang, "event_lookup",
                    f'site:{parts.hostname} {rules["discovery"]["context"][lang][0]} {vocabulary[direction]["terms"][lang][0]}',
                    source_id=source["id"], direction_id=direction, priority=3,
                    trigger="source_direction_or_window_not_checked")

    discovery = rules["discovery"]
    if discovery["enabled"]:
        for lang in rules["languages"]:
            context = discovery["context"][lang][0]
            for direction in directions:
                add("discovery", lang, "event_discovery",
                    f'{context} {vocabulary[direction]["terms"][lang][0]}', phase="first_pass",
                    direction_id=direction, model_id="open_ended")
            # Model phrases and golden hints stay in the snapshot vocabulary;
            # the session instantiates them only after observing a relevant gap.
        for item in discovery["native_language_queries"]:
            for direction in directions:
                add("discovery", item["language"], "event_discovery", item["direction_queries"][direction],
                    phase="first_pass", direction_id=direction, model_id=item["id"])
        for language in external["first_pass_languages"]:
            for intent in external["first_pass_intents"]:
                query_spec = build_followup_query(result, language=language, intent=intent)
                add("external_discovery", language, intent, query_spec["query"],
                    phase="first_pass", query_spec=query_spec, model_id="open_ended")
        for item in discovery.get("risk_queries", []):
            words = re.findall(r'"([^"\n]+)"', item["query"])
            text = " ".join([words[0], words[2]]) if len(words) > 2 else item["query"]
            add("risk_monitoring", item["language"], "trust_safety", text, model_id=item["id"],
                trigger="secondary_after_primary")
    for section in focus["secondary_sections"]:
        signals = rules["category_signals"][section]
        for lang in rules["languages"]:
            add("secondary_review", lang, section,
                f'{discovery["context"][lang][0]} {signals[lang][0]}', trigger="secondary_after_primary")

    queries.sort(key=lambda q: (q["search_priority"], media_ids.index(q["source_id"]) if q["source_id"] in media_ids else 0))
    tasks = {}
    for q in queries:
        key = (q["query"], q["phase"])
        task = tasks.setdefault(key, {"task_id": sha256(
            f'{result["window_start"]}|{result["window_end_exclusive"]}|{key}'.encode("utf-8")
        ).hexdigest()[:20], "query": q["query"], "phase": q["phase"],
            "search_priority": q["search_priority"], "language_variants": {}})
        task["language_variants"].setdefault(q["language"], []).append(q["query_id"])
    result.update(queries=queries, query_count=len(queries),
                  search_tasks=list(tasks.values()), task_count=len(tasks),
                  language_request_count=sum(len(t["language_variants"]) for t in tasks.values()),
                  initial_query_count=sum(q["phase"] == "first_pass" for q in queries),
                  source_indexes=sorted(indexes.values(), key=lambda x: (
                      x["search_priority"], min((media_ids.index(sid) for sid in x["source_ids"]
                                                 if sid in media_ids), default=-1))))
    for c in result["coverage"]:
        company = by_id[c["company_id"]]
        c.update(query_ids=[q["query_id"] for q in queries if c["company_id"] in q["company_ids"]],
                 languages=company["search_languages"], primary_language=primary_language(company),
                 aliases=company["aliases"])
    result["coverage_targets"] = {"mode": focus["coverage_mode"], "primary_directions": list(directions),
                                  "secondary_topics": focus["secondary_sections"],
                                  "topics": list(rules["category_signals"]),
                                  "models": [p["id"] for p in discovery["business_model_patterns"]],
                                  "risks": discovery.get("risk_queries", []),
                                  "native_languages": [x["language"] for x in discovery["native_language_queries"]],
                                  "models_required_each_run": False}
    result["execution_policy"].update(
        passes="Review each source_index once; run unbranded first_pass discovery and external interviews/results; use if_gap only for evidenced company/language/source gaps or a concrete signal. Empty topics do not trigger additional searches.",
        source_order="Review source_indexes by search_priority once; official newsroom/IR, other official sources, key media; then unbranded event discovery and measured fallbacks.",
        languages="For an executed task, verify all language_variants; keep unverified company/language cells as explicit gaps.",
        coverage=focus["coverage_rule"], secondary=focus["secondary_rule"],
        candidate_filter=rules["retrieval_policy"]["candidate_filter"],
        followup_templates={
            "company": "{name} {necessary_qualifier} {direction_term_or_company_event_hint}",
            "model": "{dating_context} {model_phrase} {launch_term}",
            "selector": "dating_digest.discovery.queries.build_followup_query(plan, language=..., intent=..., company_id=... or company_name=..., term_index=...)",
            "rule": "Choose search_intents and term_index from the observed gap/signal through the selector. Use company_event_hints or model phrases when relevant. Record the actual query, filters, language, trigger and related query_ids/model_id. Empty topics do not trigger searches.",
        },
        event_notes="raw/event_candidates.jsonl: actor, company_ids, what_changed, mechanism, markets_or_audience, launch_stage, published_at, source_url, evidence, status; unconfirmed fields stay null/unknown and needs_review.",
        budget={"total_seconds": 900, "save_reserve_seconds": 60, "search_request_target": [40, 60],
                "search_request_reserve": 80, "max_inflight": 4, "max_per_domain": 2,
                "request_timeout_seconds": 25, "transient_retries": 1, "cache_max_age_seconds": 86400},
        language_evidence="No hard filter: record actual phrase and observed result languages; a shared brand/group query never proves every member or language.",
        completion="Conditional tasks are not executed coverage. Save pending targets and reasons at deadline; a request target is not a quality quota.")
    result["execution_policy"].update(
        external_search=external,
        source_review=sources["source_policy"]["index_review"],
        source_review_log="raw/source_reviews.jsonl",
        new_companies="Keep newly found names, mechanics and sources in discovery_candidates using the snapshot candidate_fields; company_name follow-ups work before catalog promotion.",
    )
    if rules["retrieval_policy"].get("excluded_domains"):
        result["execution_policy"]["source_order"] = policy["media_search"]
    return result
