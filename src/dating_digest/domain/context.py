"""Validated, reproducible product context; no news scoring or LLM calls."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from hashlib import sha256
import json


CLAIM_STATUSES = {"observed_in_test", "planned", "hypothesis", "unresolved"}


def json_sha256(value: dict) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def index_by_id(items: list[dict], label: str) -> dict:
    result = {}
    for item in items:
        identity = item["id"]
        if not isinstance(identity, str) or not identity.strip() or identity in result:
            raise ValueError(f"Invalid or duplicate {label} ID: {identity}")
        result[identity] = item
    if not result:
        raise ValueError(f"Missing {label}")
    return result


def build_context_bundle(context: dict) -> dict:
    """Validate references and preserve claim status. The local PDF is optional.

    A document's plans never populate known_product_capabilities. No current
    product phase, quota, ranking bonus, or effect size is inferred here.
    """
    if not context.get("version"):
        raise ValueError("Missing business context version")
    policy = context["context_policy"]
    if (policy["mode"] != "advisory" or policy["search_mode"] != "additive"
            or policy["allow_market_watch"] is not True
            or policy["no_topic_or_geography_quota"] is not True):
        raise ValueError("Context must preserve broad coverage and advisory relevance")
    sources = index_by_id(context["source_documents"], "context source")
    claims = index_by_id(context["context_claims"], "context claim")
    questions = index_by_id(context["decision_questions"], "decision question")
    for source in sources.values():
        date.fromisoformat(source["review_date"])
        fingerprint = source["sha256"]
        if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise ValueError("Invalid source SHA-256")
    for claim in claims.values():
        if claim["source_id"] not in sources or claim["status"] not in CLAIM_STATUSES:
            raise ValueError(f"Invalid source or status for claim: {claim['id']}")
        if not claim["statement"].strip() or not claim["pages"] or any(
            type(page) is not int or page < 1 for page in claim["pages"]
        ):
            raise ValueError(f"Missing statement or source pages: {claim['id']}")
    for question in questions.values():
        if not question["question"].strip() or not question["claim_ids"]:
            raise ValueError(f"Missing decision question or evidence: {question['id']}")
        if not set(question["claim_ids"]) <= claims.keys():
            raise ValueError(f"Unknown claim in decision question: {question['id']}")
        if any(not question["search_queries"].get(lang, "").strip() for lang in ("ru", "en")):
            raise ValueError(f"Missing RU/EN context query: {question['id']}")
    return {
        "schema_version": "1.0",
        "context_version": context["version"],
        "context_sha256": json_sha256(context),
        "business_context": deepcopy(context),
        "warnings": [policy["freshness"], *[
            claim["statement"] for claim in claims.values() if claim["status"] == "unresolved"
        ]],
    }


def validate_context_bundle(bundle: dict) -> None:
    """Detect accidental drift between a saved snapshot and its fingerprint."""
    expected = build_context_bundle(bundle["business_context"])
    if bundle != expected:
        raise ValueError("Context bundle differs from its snapshot; create a new run")


def validate_context_refs(bundle: dict, refs: dict) -> None:
    """Check generated references. Does not certify the meaning of a so what."""
    validate_context_bundle(bundle)
    context = bundle["business_context"]
    question_ids = {item["id"] for item in context["decision_questions"]}
    claim_ids = {item["id"] for item in context["context_claims"]}
    if refs["relevance_type"] not in {"decision_support", "market_watch"}:
        raise ValueError("Unknown context relevance type")
    for key, known in (("question_ids", question_ids), ("claim_ids", claim_ids)):
        values = refs[key]
        if (not isinstance(values, list) or any(not isinstance(value, str) for value in values)
                or len(set(values)) != len(values) or not set(values) <= known):
            raise ValueError("Unknown, duplicate or malformed context question or claim reference")
    if refs["relevance_type"] == "decision_support" and not refs["question_ids"]:
        raise ValueError("Decision support requires a concrete question")
