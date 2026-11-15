"""Local scaffold: select a period and save a search plan without network calls."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from .domain.context import build_context_bundle, index_by_id, json_sha256
from .domain.window import date_window
from .discovery.plan import build_plan
from .orchestration.period import choose_period

CONFIG_NAMES = ("business_context", "editorial_policy", "pipeline", "companies", "sources", "queries", "golden_set")


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def write_json(path: Path, value: dict) -> None:
    # Same-filesystem hard link publishes a complete file and cannot overwrite
    # an existing destination, including one created by a concurrent process.
    with TemporaryDirectory(prefix=".write-", dir=path.parent) as temporary:
        staging = Path(temporary) / path.name
        with staging.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.link(staging, path)


def validate_configs(configs: dict, schema_dir: Path = Path("schemas")) -> None:
    """Check config links and scoring contracts before creating artifacts."""
    catalog, rules = configs["companies"], configs["queries"]
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
        if company["id"] in required and not {"ru", "en"} <= set(languages):
            raise ValueError(f"Required company needs RU/EN queries: {company['id']}")
    for source in sources.values():
        references(source["company_ids"], companies, f"source {source['id']} company")
        if type(source["enabled"]) is not bool:
            raise ValueError(f"Invalid source enabled flag: {source['id']}")
    for profile in rules["company_profiles"].values():
        references(profile["thematic_sections"], rules["category_signals"], "query section")
    references(rules["discovery"]["source_seed_ids"], sources, "discovery seed")
    if "editorial_policy" not in configs:  # Legacy plans only supplied the catalog.
        return
    policy = configs["editorial_policy"]
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
    for case in configs["golden_set"]["cases"]:
        references(case["company_ids"], required, "golden-set company")


def aware_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected an ISO timestamp with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("Timestamp must include timezone, e.g. +03:00")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description="Dating digest project scaffold")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-run", help="Create a planned run; no network or publication")
    init.add_argument("--as-of", type=aware_timestamp, help="End of proposed last-week window; asks yes/no")
    init.add_argument("--start-date", type=date.fromisoformat, help="Explicit start date, YYYY-MM-DD")
    init.add_argument("--end-date", type=date.fromisoformat, help="Explicit inclusive end date, YYYY-MM-DD")
    init.add_argument("--data-dir", type=Path, default=Path("data"))
    plan = commands.add_parser("plan-search", help="Save a plan for a confirmed run; no network")
    plan.add_argument("--run-dir", type=Path, required=True)
    for command in (init, plan):
        command.add_argument("--config-dir", type=Path, default=Path("config"),
                             help="Config directory; plan-search uses it only for legacy runs")
    args = parser.parse_args()

    if args.command == "plan-search":
        try:
            manifest = read_json(args.run_dir / "manifest.json")
            bundle_path = args.run_dir / "context_bundle.json"
            bundle = read_json(bundle_path) if bundle_path.exists() else None
            version = manifest["schema_version"]
            if version == "0.5":
                snapshots = {name: read_json(args.run_dir / "config_snapshot" / f"{name}.json") for name in CONFIG_NAMES}
                if manifest.get("config_sha256") != {name: json_sha256(value) for name, value in snapshots.items()}:
                    raise ValueError("Configuration snapshot differs from manifest; create a new run")
                if not manifest.get("business_context") or bundle is None or snapshots["business_context"] != bundle["business_context"]:
                    raise ValueError("Run context snapshot is missing or inconsistent; create a new run")
            elif version in {"0.3", "0.4"}:
                snapshots = {name: read_json(args.config_dir / f"{name}.json") for name in ("companies", "sources", "queries")}
            else:
                raise ValueError(f"Unsupported manifest version: {version}")
            validate_configs(snapshots)
            result = build_plan(snapshots["companies"], snapshots["sources"], snapshots["queries"], manifest, bundle)
            result["catalog_origin"] = "run_snapshot" if version == "0.5" else "live_legacy"
            write_json(args.run_dir / "search_plan.json", result)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            parser.error(str(exc))
        print(f"Saved {result['query_count']} planned queries; no search has run.")
        if result["catalog_origin"] == "live_legacy":
            print("Legacy run uses the current catalog. Create a new run to freeze all inputs.")
        if bundle is None:
            print("Legacy run has no product context; Lovito decision queries are unavailable.")
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
            "status": "planned", "stages": {name: "planned" for name in ("discovery", "ingestion", "editorial", "review", "publishing")},
            "publication_enabled": False,
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
