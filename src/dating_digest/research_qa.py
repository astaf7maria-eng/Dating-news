"""Read-only corpus audit and a revision-bound gate before editorial selection.

The CLI does not perform research or certify facts. A separate source-based
assessment is required even when every local check succeeds.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import unicodedata

from jsonschema import Draft202012Validator, FormatChecker

from .domain.context import json_sha256
from .domain.window import MOSCOW, validate_window
from .discovery.plan import build_plan
from .io import read_json, write_json
from .storage.archive import normalize_url, publication_day

CHECKS = ("coverage", "dates", "sources", "filters", "extraction", "event_facts", "missed_news")
INELIGIBLE = {"unknown_date", "outside_window", "ambiguous_day", "incomplete_text", "encoding_damage", "extraction_diagnostics"}


def _validator(path: Path) -> Draft202012Validator:
    return Draft202012Validator(read_json(path), format_checker=FormatChecker())


def audit_research(run_dir: Path, project_dir: Path = Path(".")) -> dict:
    """Audit all staged articles and their archived bodies without changing them."""
    run_dir, project_dir = Path(run_dir).resolve(), Path(project_dir).resolve()
    findings, inputs, articles = [], {}, []

    def finding(code, path, detail, severity="blocking"):
        findings.append(dict(code=code, path=path, detail=detail, severity=severity))

    def capture(path, label):
        inputs[label] = sha256(path.read_bytes()).hexdigest()

    paths = [run_dir / name for name in ("manifest.json", "context_bundle.json", "search_plan.json", "article_archive.json", "article_index.json")
             if (run_dir / name).exists()]
    for directory in ("config_snapshot", "articles", "raw"):
        paths.extend(p for p in (run_dir / directory).rglob("*") if p.is_file())
    for path in sorted(set(paths)):
        label = path.relative_to(run_dir).as_posix()
        capture(path, label)
        try:
            if path.suffix == ".jsonl":
                for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
                    if line.strip() and not isinstance(json.loads(line), dict):
                        raise ValueError(f"Line {number}: expected JSON object")
            elif path.suffix == ".json":
                json.loads(path.read_text(encoding="utf-8-sig"))
        except (ValueError, UnicodeError) as exc:
            finding("invalid_json", label, str(exc))
    for name in ("schemas/article.schema.json", "schemas/research-review.schema.json",
                 "prompts/common.md", "prompts/review-research.md"):
        capture(project_dir / name, "rules/" + name)
    capture(Path(__file__), "rules/research_qa.py")

    manifest = read_json(run_dir / "manifest.json")
    start, end = validate_window(datetime.fromisoformat(manifest["window_start"]),
                                 datetime.fromisoformat(manifest["window_end_exclusive"]))
    if manifest.get("window_confirmation", {}).get("status") != "confirmed":
        finding("unconfirmed_window", "manifest.json", "Reporting window is not confirmed")
    snapshots = {p.stem: read_json(p) for p in sorted((run_dir / "config_snapshot").glob("*.json"))}
    if not snapshots or manifest.get("config_sha256") != {k: json_sha256(v) for k, v in snapshots.items()}:
        finding("snapshot_mismatch", "config_snapshot", "Snapshot hashes differ from manifest")
    plan = read_json(run_dir / "search_plan.json")
    if any(plan.get(key) != manifest.get(key) for key in ("run_id", "window_start", "window_end_exclusive")):
        finding("plan_mismatch", "search_plan.json", "Plan belongs to another run/window")
    bundle_path = run_dir / "context_bundle.json"
    expected = build_plan(snapshots["companies"], snapshots["sources"], snapshots["queries"], manifest,
                          read_json(bundle_path) if bundle_path.exists() else None)
    for key in ("coverage", "source_indexes"):
        if plan[key] != expected[key]:
            finding("coverage_plan_mismatch", "search_plan.json", "Coverage differs from frozen catalog plan: " + key)

    targets = sorted({f"company:{row['company_id']}:{lang}" for row in plan["coverage"] for lang in row["languages"]}
                     | {f"source:{sid}:{direction}" for row in plan["source_indexes"]
                        for sid in row["source_ids"] for direction in row["direction_ids"]})
    if not targets:
        finding("missing_targets", "search_plan.json", "No coverage targets")
    if not (run_dir / "raw/source_reviews.jsonl").is_file():
        finding("missing_source_reviews", "raw/source_reviews.jsonl", "Source/window checks have not been saved")
    notes = run_dir / "raw/event_candidates.jsonl"
    if notes.is_file():
        required = snapshots["editorial_policy"]["event_candidate"]["required_fields"]
        for number, line in enumerate(notes.read_text(encoding="utf-8-sig").splitlines(), 1):
            if not line.strip():
                continue
            try:
                note = json.loads(line)
            except ValueError:
                continue  # Already reported as invalid_json above.
            if not isinstance(note, dict) or set(required) - note.keys():
                finding("event_note_fields", f"raw/event_candidates.jsonl:{number}", "Missing required event fields")

    validator = _validator(project_dir / "schemas/article.schema.json")

    def inspect(path, label, *, staged=False):
        try:
            value = read_json(path)
        except (ValueError, UnicodeError) as exc:
            finding("invalid_article", label, str(exc))
            return None
        errors = sorted(validator.iter_errors(value), key=lambda e: str(list(e.absolute_path)))
        for error in errors:
            field = "/".join(map(str, error.absolute_path))
            finding("article_schema", label + ("#/" + field if field else ""), error.message)
        if errors:
            return value
        text = value["text"]
        expected_hash = sha256(text.encode("utf-8")).hexdigest() if text is not None else None
        if value["text_sha256"] != expected_hash:
            finding("text_hash", label, "text_sha256 does not match original text")
        for key in ("url", "canonical_url"):
            try:
                normalize_url(value[key])
            except ValueError as exc:
                finding("article_url", label + "#/" + key, str(exc))
        attention = []
        published = value["published_at"]
        precision = value["date_precision"]
        if precision == "unknown":
            attention.append("unknown_date")
        elif precision == "timestamp":
            if published is None:
                finding("missing_timestamp", label, "timestamp precision requires published_at")
            else:
                stamp = datetime.fromisoformat(published.replace("Z", "+00:00"))
                if not start <= stamp < end:
                    attention.append("outside_window")
        else:
            # A date-only publication occupies a day, not an invented instant.
            day = publication_day(value)
            if day is None:
                attention.append("unknown_date")
            else:
                first = datetime.combine(day, time.min, MOSCOW)
                last = first + timedelta(days=1)
                if last <= start or first >= end:
                    attention.append("outside_window")
                elif first < start or last > end:
                    attention.append("ambiguous_day")
        if value["extraction_status"] != "full" or not text or not text.strip():
            attention.append("incomplete_text")
        if value.get("extraction_diagnostics", {}).get("quality_issues"):
            attention.append("extraction_diagnostics")
        if not value.get("tags"):
            attention.append("missing_tags")
        if any("\ufffd" in (value.get(key) or "") for key in ("title", "text")):
            attention.append("encoding_damage")
        for code in attention:
            finding(code, label, "Requires source-based assessment or exclusion", "attention")
        if staged:
            articles.append(dict(path=label, article_id=value["article_id"], attention=attention))
        return value

    staged_paths = sorted((run_dir / "articles").glob("*.json"))
    values = [inspect(path, path.relative_to(run_dir).as_posix(), staged=True) for path in staged_paths]
    ids = [v.get("article_id") for v in values if v is not None and isinstance(v.get("article_id"), str)]
    if len(ids) != len(set(ids)):
        finding("duplicate_ids", "articles", "Staged article IDs must be unique")
    for field in ("canonical_url", "text_sha256"):
        counts = Counter(v.get(field) for v in values if v and isinstance(v.get(field), str))
        for value, count in counts.items():
            if count > 1:
                finding("possible_duplicate", "articles", f"{count} articles share {field}: {value}", "attention")

    receipt_path = run_dir / "article_archive.json"
    if staged_paths and not receipt_path.exists():
        finding("missing_archive_receipt", "article_archive.json", "Archive articles before research QA")
    if staged_paths and not notes.is_file():
        finding("missing_event_notes", "raw/event_candidates.jsonl", "Save candidate events or explicit exclusions")
    if receipt_path.exists():
        receipt = read_json(receipt_path)
        root = Path(receipt["archive_root"]).resolve()
        if receipt["run_id"] != manifest["run_id"] or root != Path(manifest["article_storage"]["archive_dir"]).resolve():
            finding("archive_mismatch", "article_archive.json", "Receipt run/archive differs from manifest")
        items = receipt["items"]
        if Counter(item["article_id"] for item in items) != Counter(ids):
            finding("archive_coverage", "article_archive.json", "Receipt does not cover exactly the staged IDs")
        archived = {}
        for relative in sorted({item["archive_path"] for item in items}):
            path = (root / relative).resolve()
            label = "archive/" + relative
            if not path.is_relative_to(root) or root.is_relative_to(run_dir) or run_dir.is_relative_to(root):
                finding("unsafe_archive_path", label, "Archive path escapes root or overlaps run")
            elif not path.is_file():
                finding("missing_archived_article", label, "Receipt points to missing Article")
            else:
                capture(path, label)
                archived[relative] = inspect(path, label)
        by_id = {v["article_id"]: v for v in values if v and isinstance(v.get("article_id"), str)}
        for item in items:
            body = archived.get(item["archive_path"])
            staged = by_id.get(item["article_id"])
            if body and body.get("article_id") != item["stored_article_id"]:
                finding("archive_identity", "article_archive.json", "Stored Article ID differs from receipt")
            if body and staged and isinstance(staged.get("text"), str) and isinstance(body.get("text"), str):
                normalize = lambda text: " ".join(unicodedata.normalize("NFC", text).split())
                if normalize(staged["text"]) != normalize(body["text"]):
                    finding("stale_archive_receipt", "article_archive.json", "Staged text differs from archived body; rerun archive-articles")
                if body.get("article_id") == staged["article_id"] and staged["text"].strip():
                    keys = ("published_at", "date_precision", "extraction_status")
                    if (" ".join(str(body.get("title", "")).split()) != " ".join(str(staged.get("title", "")).split())
                            or any(body.get(key) != staged.get(key) for key in keys)
                            or publication_day(body) != publication_day(staged)):
                        finding("stale_archive_receipt", "article_archive.json", "Staged metadata differs from archived version; rerun archive-articles")

    return dict(schema_version="0.1", run_id=manifest["run_id"],
                window_start=manifest["window_start"], window_end_exclusive=manifest["window_end_exclusive"],
                input_sha256=json_sha256(inputs), inputs=inputs,
                automatic_status="blocked" if any(f["severity"] == "blocking" for f in findings) else "checks_passed",
                semantic_status="not_reviewed", articles=articles, coverage_targets=targets,
                findings=findings, note="Local checks never certify facts or research completeness.")


def prepare_research_qa(run_dir: Path, project_dir: Path = Path(".")) -> dict:
    report = audit_research(run_dir, project_dir)
    write_json(Path(run_dir) / "research_audit.json", report, replace=True)
    return report


def check_research_qa(run_dir: Path, project_dir: Path = Path("."), *, verified_subset: bool = False) -> dict:
    """Validate research, optionally returning eligible articles for a limited draft."""
    run_dir, project_dir = Path(run_dir), Path(project_dir)
    current = audit_research(run_dir, project_dir)
    saved = read_json(run_dir / "research_audit.json")
    review = read_json(run_dir / "research_review.json")
    error = next(_validator(project_dir / "schemas/research-review.schema.json").iter_errors(review), None)
    if error:
        raise ValueError("Invalid research_review.json: " + error.message)
    if saved != current or review["input_sha256"] != current["input_sha256"] or review["run_id"] != current["run_id"]:
        raise ValueError("Research QA is stale; rerun qa-research and source-based assessment")
    if current["automatic_status"] != "checks_passed":
        raise ValueError("Research QA has blocking local findings; see research_audit.json")
    if not verified_subset:
        if review["verdict"] != "passed" or any(f["severity"] == "blocking" for f in review["findings"]):
            raise ValueError("Research assessment is not passed or contains blocking findings")
        if set(review["checks"]) != set(CHECKS) or any(row["status"] != "completed" for row in review["checks"].values()):
            raise ValueError("Every research check must be completed")
    rows = review["articles"]
    by_path = {row["path"]: row for row in rows}
    if len(rows) != len(by_path) or set(by_path) != {a["path"] for a in current["articles"]}:
        raise ValueError("Assessment must cover every staged Article exactly once")
    for article in current["articles"]:
        row = by_path[article["path"]]
        if row["article_id"] != article["article_id"] or (not verified_subset and row["disposition"] == "needs_review"):
            raise ValueError("Unresolved or mismatched Article: " + article["path"])
        if row["disposition"] == "eligible" and INELIGIBLE.intersection(article["attention"]):
            raise ValueError("Ineligible date/text must be corrected or excluded: " + article["path"])
    coverage = review["coverage"]
    if not verified_subset and (len(coverage) != len({r["target"] for r in coverage})
            or {r["target"] for r in coverage} != set(current["coverage_targets"])
            or any(r["status"] not in {"checked", "no_findings"} for r in coverage)):
        raise ValueError("Coverage has missing, duplicate or unfinished targets")

    # References must identify actual saved observations, not just planned IDs.
    def observation(ref):
        relative, separator, number = ref.rpartition(":")
        if not separator or not number.isdigit():
            raise ValueError("Expected raw/file.jsonl:line evidence reference: " + ref)
        path = (run_dir / relative).resolve()
        raw = (run_dir / "raw").resolve()
        if not path.is_relative_to(raw) or path.suffix != ".jsonl":
            raise ValueError("Evidence must reference a raw JSONL observation: " + ref)
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        index = int(number) - 1
        if not 0 <= index < len(lines):
            raise ValueError("Missing observation: " + ref)
        row = json.loads(lines[index])
        if (not isinstance(row, dict) or not any(row.get(key) for key in ("source_url", "url", "query", "evidence_urls", "results"))
                or row.get("status") in {"planned", "error", "failed", "not_checked", "inflight", "budget_not_started"}):
            raise ValueError("Evidence is not a completed source/search observation: " + ref)

    evidence_rows = ([row for row in rows if row["disposition"] == "eligible"] if verified_subset
                     else [*rows, *coverage, *review["checks"].values()])
    references = {ref for row in evidence_rows for ref in row["evidence_refs"]}
    for ref in sorted(references):
        observation(ref)
    return dict(run_id=current["run_id"], input_sha256=current["input_sha256"],
                verdict="verified_subset" if verified_subset else "passed",
                eligible_article_paths=[row["path"] for row in rows if row["disposition"] == "eligible"])
