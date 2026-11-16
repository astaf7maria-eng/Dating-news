"""Short follow-up queries selected by the observed search intent."""

from __future__ import annotations

import re
from urllib.parse import urlsplit


def require_fast_policy(rules: dict) -> None:
    """Reject snapshots predating the agreed source-first research workflow."""
    for name in ("research_policy", "search_intents", "external_search"):
        if not isinstance(rules.get(name), dict) or not rules[name]:
            raise ValueError(f"Search snapshot lacks {name}; create a new run")


def company_search_name(company: dict, language: str) -> str:
    patterns = {"ru": r"[А-Яа-яЁё]", "zh": r"[\u4e00-\u9fff]", "ja": r"[\u3040-\u30ff]"}
    pattern = patterns.get(language)
    for alias in company["aliases"]:
        if pattern and re.search(pattern, alias):
            return alias
        if language == "en" and re.search(r"[A-Za-z]", alias) and not re.search(
            r"[А-Яа-яЁё\u4e00-\u9fff\u3040-\u30ff]", alias
        ):
            return alias
    return company["name"].split(" / ")[-1]


def build_followup_query(plan: dict, *, language: str, intent: str,
                         company_id: str | None = None, company_name: str | None = None,
                         term_index: int = 0) -> dict:
    """Build one query from frozen inputs, including companies discovered later.

    The session chooses intent from an observed gap or statement, then may
    choose the next synonym after an unhelpful response. No network is used.
    """
    snapshots = plan["config_snapshot"]
    catalog, sources, rules = (snapshots[name] for name in ("companies", "sources", "queries"))
    require_fast_policy(rules)
    if company_id is not None and company_name is not None:
        raise ValueError("Use company_id or a discovered company_name")
    if type(term_index) is not int or term_index < 0:
        raise ValueError("term_index must be a nonnegative integer")

    definitions = rules["search_intents"]
    directions = rules["research_policy"]["directions"]
    if intent in definitions or intent in directions:
        definition = definitions[intent] if intent in definitions else directions[intent]
        terms = definition["terms"].get(language, [])
    else:
        raise ValueError(f"Unknown search intent in this snapshot: {intent}")
    if term_index >= len(terms):
        raise ValueError(f"No query term {term_index} for {intent}/{language}")

    context = rules["external_search"]["contexts"].get(language)
    if not context:
        raise ValueError(f"Missing dating context: {language}")
    excluded_domains = set(rules["retrieval_policy"].get("excluded_domains", []))
    ids = []
    if company_id is not None:
        company = next((item for item in catalog["companies"]
                        if item["id"] == company_id and item["enabled"]), None)
        if company is None:
            raise ValueError(f"Unknown or disabled company: {company_id}")
        if language not in company["search_languages"]:
            raise ValueError(f"Language is not enabled for {company_id}: {language}")
        ids = [company_id]
        label = company_search_name(company, language)
        qualifiers = company.get("disambiguation_terms", {}).get(language, [])
        if qualifiers:
            label += " " + qualifiers[0]
        elif company["tier"] == "golden_adjacent":
            label += " " + context
        if definition.get("outside_official"):
            excluded_domains.update(domain.removeprefix("www.").lower() for domain in company["official_domains"])
            for source in sources["sources"]:
                official = source["kind"].startswith("official_") or source["kind"] == "investor_relations"
                if official and company_id in source["company_ids"]:
                    excluded_domains.add(urlsplit(source["url"]).hostname.removeprefix("www.").lower())
    elif company_name is not None:
        if not isinstance(company_name, str) or not company_name.strip():
            raise ValueError("Discovered company_name must be nonempty")
        label = company_name.strip() + " " + context
    else:
        label = context

    plain_query = f"{label} {terms[term_index]}"
    exclusions = " ".join(f"-site:{domain}" for domain in sorted(excluded_domains))
    return {
        "query": " ".join(part for part in (plain_query, exclusions) if part),
        "query_without_domain_exclusions": plain_query,
        "exclude_domains": sorted(excluded_domains),
        "language": language, "intent": intent, "company_ids": ids,
        "company_name": company_name,
        "outside_official": definition.get("outside_official", False),
        "window_start": plan["window_start"],
        "window_end_exclusive": plan["window_end_exclusive"],
    }
