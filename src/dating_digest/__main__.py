"""Local planning, saved-HTML extraction and archiving without network calls."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from .domain.context import build_context_bundle, index_by_id, json_sha256
from .domain.window import date_window
from .discovery.plan import build_plan
from .discovery.queries import require_fast_policy
from .io import read_json, write_json
from .orchestration.period import choose_period
from .storage import archive_run

CONFIG_NAMES = ("business_context", "editorial_policy", "pipeline", "companies", "sources", "queries", "golden_set")


def validate_configs(configs: dict, schema_dir: Path = Path("schemas")) -> None:
    """Check config links and scoring contracts before creating artifacts."""
    missing = set(CONFIG_NAMES) - configs.keys()
    if missing:
        raise ValueError("Missing run configurations: " + ", ".join(sorted(missing)))
    catalog, rules = configs["companies"], configs["queries"]
    require_fast_policy(rules)
    companies = index_by_id(catalog["companies"], "company")
    sources = index_by_id(configs["sources"]["sources"], "source")
    if any(not isinstance(rules[key], dict) for key in ("company_profiles", "category_signals")):
        raise ValueError("Query profiles and category signals must be objects")

    def references(values, known, label):
        if (not isinstance(values, list) or any(not isinstance(value, str) for value in values)
                or not set(values) <= set(known)):
            raise ValueError(f"Invalid {label} references")

    required = set(catalog["required_always_company_ids"] + catalog["golden_set_company_ids"]
                   + catalog["golden_adjacent_company_ids"])
    if not required <= {cid for cid, company in companies.items() if company["enabled"] is True}:
        raise ValueError("Required companies must exist and be enabled")
    for company in companies.values():
        references(company["source_ids"], sources, f"company {company['id']} source")
        if company["tier"] not in rules["company_profiles"] or type(company["enabled"]) is not bool:
            raise ValueError(f"Invalid company profile or enabled flag: {company['id']}")
        languages = company["search_languages"]
        if not isinstance(languages, list) or not languages or any(not isinstance(lang, str) for lang in languages):
            raise ValueError(f"Invalid search languages: {company['id']}")
        if company.get("primary_search_language", languages[0]) not in languages:
            raise ValueError(f"Primary search language is not enabled: {company['id']}")
        if company["id"] in required and not {"ru", "en"} <= set(languages):
            raise ValueError(f"Required company needs RU/EN queries: {company['id']}")
    for source in sources.values():
        references(source["company_ids"], companies, f"source {source['id']} company")
        if type(source["enabled"]) is not bool:
            raise ValueError(f"Invalid source enabled flag: {source['id']}")
    for profile in rules["company_profiles"].values():
        references(profile["thematic_sections"], rules["category_signals"], "query section")
    references(rules["discovery"]["source_seed_ids"], sources, "discovery seed")
    source_policy = configs["sources"].get("source_policy", {})
    media_ids = source_policy.get("key_media_ids", [])
    references(media_ids, sources, "key media")
    if len(media_ids) != len(set(media_ids)):
        raise ValueError("Duplicate key media references")
    for source_id in media_ids:
        source = sources[source_id]
        if not source["enabled"] or source["kind"] not in {"industry_media", "general_media"}:
            raise ValueError(f"Key media must be enabled media: {source_id}")
        language = source["language"]
        keywords = source_policy.get("media_keywords", {}).get(language)
        if (language not in rules["discovery"]["context"] or not isinstance(keywords, list)
                or not keywords or any(not isinstance(word, str) or not word.strip() for word in keywords)):
            raise ValueError(f"Missing key media keywords or dating context: {source_id}")
    focus = rules["research_policy"]
    directions = focus.get("primary_directions", [])
    vocabulary = focus.get("directions", {})
    references(directions, vocabulary, "primary direction")
    if not directions or len(directions) != len(set(directions)):
        raise ValueError("Primary directions must be nonempty and unique")
    references(focus.get("secondary_sections"), rules["category_signals"], "secondary section")
    languages = (set(rules["languages"]) | {lang for c in companies.values() for lang in c["search_languages"]}
                 | {sources[sid]["language"] for sid in media_ids})
    for language in languages:
        terms = rules["company_query_policy"].get("event_terms", {}).get(language)
        if (not isinstance(terms, list) or not terms
                or any(not isinstance(term, str) or not term.strip() for term in terms)):
            raise ValueError(f"Missing company event terms: {language}")
    intents = rules["search_intents"]
    external = rules["external_search"]
    if not isinstance(intents, dict) or "product_change" not in intents or not isinstance(external, dict):
        raise ValueError("Search intents and external search policy must be configured together")
    references(external.get("intents"), intents, "external search intent")
    references(external.get("first_pass_intents"), external["intents"], "external first-pass intent")
    references(external.get("first_pass_languages"), external.get("contexts", {}), "external search language")
    if not external["intents"] or not external["first_pass_languages"]:
        raise ValueError("External search needs intents and first-pass languages")
    if any(not isinstance(definition, dict) for definition in intents.values()):
        raise ValueError("Search intent definitions must be objects")
    for language in languages | set(external["first_pass_languages"]):
        context = external.get("contexts", {}).get(language)
        if not isinstance(context, str) or not context.strip():
            raise ValueError(f"Missing external dating context: {language}")
        for intent, definition in intents.items():
            terms = definition.get("terms", {}).get(language)
            if (not isinstance(terms, list) or not terms
                    or any(not isinstance(term, str) or not term.strip() for term in terms)):
                raise ValueError(f"Missing intent terms: {intent}/{language}")
    for intent, definition in intents.items():
        if type(definition.get("outside_official")) is not bool:
            raise ValueError(f"Invalid external-domain flag: {intent}")
    if any(not intents[intent]["outside_official"] for intent in external["intents"]):
        raise ValueError("External search intents must exclude official company domains")
    review_rule = source_policy.get("index_review")
    if not isinstance(review_rule, str) or not review_rule.strip():
        raise ValueError("Missing source index review policy")
    for direction in directions:
        definition = vocabulary[direction]
        references(definition["section_ids"], rules["category_signals"], "direction section")
        for language in languages:
            terms = definition.get("terms", {}).get(language)
            if (not isinstance(terms, list) or not terms
                    or any(not isinstance(term, str) or not term.strip() for term in terms)):
                raise ValueError(f"Missing event terms: {direction}/{language}")
    for item in rules["discovery"]["native_language_queries"]:
        for direction in directions:
            query = item.get("direction_queries", {}).get(direction)
            if not isinstance(query, str) or not query.strip():
                raise ValueError(f"Missing native event query: {item['id']}/{direction}")
    references(list(rules.get("company_event_hints", {})), companies, "event hint company")
    for cid, hints in rules.get("company_event_hints", {}).items():
        if not set(hints) <= set(companies[cid]["search_languages"]):
            raise ValueError(f"Event hint language is not enabled: {cid}")
        for phrases in hints.values():
            if (not isinstance(phrases, list) or not phrases
                    or any(not isinstance(phrase, str) or not phrase.strip() for phrase in phrases)):
                raise ValueError(f"Invalid event hints: {cid}")
    screening = rules["retrieval_policy"]["candidate_filter"]
    if not screening["excluded_types"] or not screening["title_rules"]:
        raise ValueError("Candidate filter must define types and title rules")
    for rule in screening["title_rules"]:
        references([rule["reason"]], screening["excluded_types"], "filter reason")
        try:
            re.compile(rule["pattern"], re.IGNORECASE)
        except re.error as error:
            raise ValueError(f"Invalid candidate filter pattern: {error}") from error
    storage = configs["pipeline"].get("storage", {})
    if not isinstance(storage, dict) or not isinstance(storage.get("article_archive_dir", "article"), str) or not storage.get("article_archive_dir", "article").strip():
        raise ValueError("storage.article_archive_dir must be a nonempty path string")
    policy = configs["editorial_policy"]
    if set(policy["material_filter"]["excluded_types"]) != set(screening["excluded_types"]):
        raise ValueError("Search and editorial material filters differ")
    if policy["research_focus"]["primary_directions"] != directions:
        raise ValueError("Search and editorial research directions differ")
    sections = index_by_id(policy["sections"], "editorial section")
    event, digest = [read_json(schema_dir / f"{name}.schema.json") for name in ("event", "digest")]
    if not (set(sections) == set(rules["category_signals"]) == set(event["properties"]["section_id"]["enum"])
            == set(digest["$defs"]["section_id"]["enum"])):
        raise ValueError("Editorial sections differ from query or schema sections")
    weights = configs["pipeline"]["ranking"]["weights"]
    criteria = event["properties"]["ranking"]["properties"]["criteria"]["items"]["properties"]["name"]["enum"]
    if (not isinstance(weights, dict) or set(weights) != set(criteria) or any(type(value) not in {int, float} or not 0 <= value <= 1
                                           for value in weights.values()) or abs(sum(weights.values()) - 1) > 1e-9):
        raise ValueError("Ranking weights must match the schema, be nonnegative and sum to one")
    limit = policy["max_news_total"]
    if type(limit) is not int or not 1 <= limit <= digest["properties"]["items"]["maxItems"]:
        raise ValueError("Editorial limit exceeds the digest schema limit")
    golden = configs["golden_set"]
    for case in golden["cases"] + golden.get("marketing_reference_cases", []):
        references(case["company_ids"], required, "golden-set company")
        references(case["section_ids"], sections, "golden-set section")


def aware_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected an ISO timestamp with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("Timestamp must include timezone, e.g. +03:00")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description="Dating digest: run planning and persistent article archive")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-run", help="Create a planned run; no network or publication")
    init.add_argument("--as-of", type=aware_timestamp, help="End of proposed last-week window; asks yes/no")
    init.add_argument("--start-date", type=date.fromisoformat, help="Explicit start date, YYYY-MM-DD")
    init.add_argument("--end-date", type=date.fromisoformat, help="Explicit inclusive end date, YYYY-MM-DD")
    init.add_argument("--data-dir", type=Path, default=Path("data"))
    plan = commands.add_parser("plan-search", help="Save the fast source-first plan from run snapshots; no network")
    plan.add_argument("--run-dir", type=Path, required=True)
    archive = commands.add_parser("archive-articles", help="Archive staged articles by publication date, preserving existing files")
    archive.add_argument("--run-dir", type=Path, required=True)
    archive.add_argument("--archive-dir", type=Path, help="Persistent archive; defaults to the run setting or ./article")
    for command, help_text in (
        ("qa-research", "Audit saved articles and prepare research_audit.json; no network or semantic approval"),
        ("check-research-qa", "Verify source-based research_review.json against current inputs before selection"),
    ):
        qa = commands.add_parser(command, help=help_text)
        qa.add_argument("--run-dir", type=Path, required=True)
    extract = commands.add_parser("extract-page", help="Extract saved UTF-8 HTML into a staged Article; no network")
    extract.add_argument("--html", type=Path, required=True)
    extract.add_argument("--metadata", type=Path, required=True, help="Verified url, source_id, dates and company_ids as JSON")
    extract.add_argument("--output", type=Path, required=True, help="New Article JSON, normally run_dir/articles/<id>.json")
    extract.add_argument("--tags", nargs="+", choices=("features", "monetization", "trust_safety", "ai", "new_models", "business", "marketing"))
    init.add_argument("--config-dir", type=Path, default=Path("config"),
                      help="Configuration directory to freeze in the new run")
    args = parser.parse_args()

    if args.command in {"qa-research", "check-research-qa"}:
        try:
            from .research_qa import prepare_research_qa, check_research_qa
            if args.command == "qa-research":
                result = prepare_research_qa(args.run_dir)
                print(json.dumps({key: result[key] for key in
                                  ("run_id", "input_sha256", "automatic_status", "semantic_status")},
                                 ensure_ascii=False, indent=2))
                if result["automatic_status"] == "blocked":
                    parser.exit(1, "Local checks blocked; see research_audit.json\n")
            else:
                print(json.dumps(check_research_qa(args.run_dir), ensure_ascii=False, indent=2))
        except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
            parser.error(str(exc))
        return

    if args.command == "extract-page":
        try:
            from .ingestion import extract_article
            article = extract_article(args.html.read_text(encoding="utf-8-sig"),
                                      read_json(args.metadata), tags=args.tags)
            article["raw_path"] = str(args.html)
            write_json(args.output, article)
        except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
            parser.error(str(exc))
        print(f"Saved Article: {args.output}; {article['extraction_status']}; "
              f"{len(article['text'] or '')} characters")
        return

    if args.command == "archive-articles":
        try:
            result = archive_run(args.run_dir, args.archive_dir)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            parser.error(str(exc))
        print(json.dumps({key: value for key, value in result.items() if key != "items"}, ensure_ascii=False, indent=2))
        return

    if args.command == "plan-search":
        try:
            manifest = read_json(args.run_dir / "manifest.json")
            if manifest.get("schema_version") != "0.5":
                raise ValueError("Run has no supported frozen configuration; create a new run")
            bundle = read_json(args.run_dir / "context_bundle.json")
            snapshots = {name: read_json(args.run_dir / "config_snapshot" / f"{name}.json") for name in CONFIG_NAMES}
            if manifest.get("config_sha256") != {name: json_sha256(value) for name, value in snapshots.items()}:
                raise ValueError("Configuration snapshot differs from manifest; create a new run")
            if not manifest.get("business_context") or snapshots["business_context"] != bundle["business_context"]:
                raise ValueError("Run context snapshot is missing or inconsistent; create a new run")
            validate_configs(snapshots)
            result = build_plan(snapshots["companies"], snapshots["sources"], snapshots["queries"], manifest, bundle)
            result["catalog_origin"] = "run_snapshot"
            write_json(args.run_dir / "search_plan.json", result)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            parser.error(str(exc))
        print(f"Saved {result['query_count']} coverage queries in {result['task_count']} search tasks "
              f"({result['language_request_count']} separate language requests before provider optimization); "
              "no search has run.")
        print(f"First pass: {result['initial_query_count']} short queries plus "
              f"{len(result['source_indexes'])} source indexes; remaining queries only for measured gaps.")
        return

    if (args.start_date is None) != (args.end_date is None):
        parser.error("--start-date and --end-date must be provided together")
    if args.start_date is not None and args.as_of is not None:
        parser.error("Use explicit dates or --as-of, not both")
    try:
        if args.start_date is not None:
            start, end = date_window(args.start_date, args.end_date)
            selection = "explicit_dates"
        else:
            start, end, selection = choose_period(args.as_of or datetime.now(timezone.utc))
        snapshots = {name: read_json(args.config_dir / f"{name}.json") for name in CONFIG_NAMES}
        validate_configs(snapshots)
        bundle = build_context_bundle(snapshots["business_context"])
        now = datetime.now(timezone.utc)
        run_id = f"{end:%Y%m%dT%H%M%S%z}_{uuid4().hex[:8]}"
        run_dir = args.data_dir / "runs" / run_id
        manifest = {
            "schema_version": "0.5", "run_id": run_id, "created_at": now.isoformat(),
            "timezone": "Europe/Moscow", "window_start": start.isoformat(), "window_end_exclusive": end.isoformat(),
            "window_days": (end - start).total_seconds() / 86400,
            "window_confirmation": {"status": "confirmed", "method": selection, "confirmed_at": now.isoformat()},
            "status": "planned", "stages": {name: "planned" for name in ("discovery", "ingestion", "research_qa", "editorial", "review", "publishing")},
            "publication_enabled": False,
            "article_storage": {"archive_dir": str(Path(snapshots["pipeline"].get("storage", {}).get("article_archive_dir", "article")).resolve()),
                                "layout": "YYYY/MM/DD.MM.YY", "mode": "append_only_deduplicated"},
            "business_context": {"version": bundle["context_version"], "sha256": bundle["context_sha256"], "bundle": "context_bundle.json"},
            "config_sha256": {name: json_sha256(value) for name, value in snapshots.items()},
            "note": "Scaffold only; no collection, ranking, review or delivery has run.",
        }
        run_dir.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".init-", dir=run_dir.parent) as temporary:
            staging = Path(temporary) / run_id
            staging.mkdir()
            for name in ("config_snapshot", "raw", "articles", "events", "rendered"):
                (staging / name).mkdir()
            for name, value in snapshots.items():
                write_json(staging / "config_snapshot" / f"{name}.json", value)
            write_json(staging / "context_bundle.json", bundle)
            write_json(staging / "manifest.json", manifest)
            staging.rename(run_dir)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    except (EOFError, KeyboardInterrupt):
        parser.exit(2, "\nПериод не выбран. Запуск не создан.\n")
    print(f"Created planned run: {run_dir.resolve()}")


if __name__ == "__main__":
    main()
