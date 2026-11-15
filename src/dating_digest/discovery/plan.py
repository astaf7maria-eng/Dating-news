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
        for question in context_bundle["business_context"]["decision_questions"]:
            for language, query in question["search_queries"].items():
                add("context", language, "decision_support", query, decision_id=question["id"])

    result = {
        "schema_version": "0.6", "run_id": manifest["run_id"],
        "window_start": start.isoformat(), "window_end_exclusive": end.isoformat(),
        "timezone": "Europe/Moscow", "status": "planned_not_executed",
        "config_versions": {"companies": catalog["version"], "sources": sources["version"], "queries": rules["version"]},
        "config_snapshot": {"companies": catalog, "sources": sources, "queries": rules},
        "collection_policy": catalog["collection_policy"],
        "coverage": [{"company_id": cid, "required": cid in required,
                      "query_ids": [q["query_id"] for q in queries if cid in q["company_ids"]],
                      "status": "planned", "articles_saved": None} for cid in by_id],
        "query_count": len(queries), "queries": queries,
        "notes": "План не является результатом поиска. Даты передаются адаптеру провайдера; полнота и сохранение проверяются после выполнения.",
    }
    result["context_status"] = "available" if context_bundle is not None else "not_available"
    if context_bundle is not None:
        result["config_versions"]["business_context"] = context_bundle["context_version"]
        result["config_snapshot"]["business_context"] = context_bundle["business_context"]
        result["context_sha256"] = context_bundle["context_sha256"]
        result["context_warnings"] = context_bundle["warnings"]
    return result
