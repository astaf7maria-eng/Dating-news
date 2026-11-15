"""Local scaffold commands. Collection and publishing are not implemented yet."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from .domain.window import date_window
from .orchestration.period import choose_period


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
    args = parser.parse_args()

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
    except ValueError as exc:
        parser.error(str(exc))
    except (EOFError, KeyboardInterrupt):
        parser.exit(2, "\nПериод не выбран. Запуск не создан.\n")

    now = datetime.now(timezone.utc)
    run_id = f"{end:%Y%m%dT%H%M%S%z}_{uuid4().hex[:8]}"
    run_dir = args.data_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    for name in ("config_snapshot", "raw", "articles", "events", "rendered"):
        (run_dir / name).mkdir()
    manifest = {
        "schema_version": "0.3",
        "run_id": run_id,
        "created_at": now.isoformat(),
        "timezone": "Europe/Moscow",
        "window_start": start.isoformat(),
        "window_end_exclusive": end.isoformat(),
        "window_days": (end - start).total_seconds() / 86400,
        "window_confirmation": {
            "status": "confirmed",
            "method": selection,
            "confirmed_at": now.isoformat(),
        },
        "status": "planned",
        "stages": {name: "planned" for name in (
            "discovery", "ingestion", "editorial", "review", "publishing"
        )},
        "publication_enabled": False,
        "note": "Scaffold only; no collection, ranking, review or delivery has run.",
    }
    with (run_dir / "manifest.json").open("x", encoding="utf-8", newline="\n") as output:
        json.dump(manifest, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(f"Created planned run: {run_dir.resolve()}")


if __name__ == "__main__":
    main()
