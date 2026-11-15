"""Immutable dated JSON files, cross-run identities and source observations."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import unicodedata
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from dating_digest.domain.window import MOSCOW
from dating_digest.io import read_json as _read, write_json as _publish


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def normalize_url(value: str) -> str:
    """Drop tracking only; retain meaningful query values and path case."""
    if not isinstance(value, str):
        raise ValueError("Article URL must be a string")
    p = urlsplit(value.strip())
    if p.scheme.lower() not in {"http", "https"} or not p.hostname:
        raise ValueError(f"Invalid article URL: {value}")
    query = "&".join(part for part in p.query.split("&") if part and not (
        unquote_plus(part.partition("=")[0]).lower().startswith("utm_")
        or unquote_plus(part.partition("=")[0]).lower() in {"fbclid", "gclid", "msclkid", "trk"}
    ))
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", query, ""))


def publication_day(article: dict) -> date | None:
    """Use the source's raw calendar date; fall back to a verified Moscow date."""
    precision = article.get("date_precision")
    if precision == "unknown":
        return None
    for key in ("published_date_raw", "published_at"):
        raw = article.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        raw = raw.strip()
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if key == "published_date_raw":
                return value.date()
            if value.tzinfo is not None:
                return value.astimezone(MOSCOW).date()
            if precision == "day":
                return value.date()
        except ValueError:
            pass
        for pattern in ("%d.%m.%Y", "%d.%m.%y"):
            try:
                return datetime.strptime(raw, pattern).date()
            except ValueError:
                pass
    return None


def _prepare(article: dict) -> tuple[dict, str | None, str, list[str], date | None]:
    value = dict(article)
    article_id = value.get("article_id")
    if not isinstance(article_id, str) or not article_id.strip():
        raise ValueError("article_id must be a nonempty string")
    urls = sorted({normalize_url(value[key]) for key in ("url", "canonical_url") if value.get(key)})
    if not urls:
        raise ValueError("Article must have a URL")
    text = value.get("text")
    if text is not None and not isinstance(text, str):
        raise ValueError("Article text must be a string or null")
    exact = _digest(text) if text is not None else None
    if value.get("text_sha256") not in {None, exact}:
        raise ValueError("text_sha256 does not match the article text")
    value["text_sha256"] = exact
    normalized = " ".join(unicodedata.normalize("NFC", text or "").split())
    text_hash = _digest(normalized) if normalized else None
    day = publication_day(value)
    # Volatile collected_at, run-local paths and query IDs do not create versions.
    signature = _digest(_json({
        "title": " ".join(str(value.get("title", "")).split()),
        "text_hash": text_hash, "publication_day": day.isoformat() if day else None,
        "published_at": value.get("published_at"), "date_precision": value.get("date_precision"),
        "extraction_status": value.get("extraction_status"),
    }))
    return value, text_hash, signature, urls, day


class ArticleArchive:
    def __init__(self, root: Path | str = "article"):
        self.root = Path(root).resolve()

    @contextmanager
    def _connect(self):
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.root / ".archive.sqlite3", timeout=30)
        db.row_factory = sqlite3.Row
        statements = (
            "CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, article_id TEXT NOT NULL, text_hash TEXT, signature TEXT NOT NULL)",
            "CREATE INDEX IF NOT EXISTS record_text ON records(text_hash)",
            "CREATE TABLE IF NOT EXISTS identifiers (kind TEXT NOT NULL, value TEXT NOT NULL, record_id INTEGER NOT NULL REFERENCES records(id), PRIMARY KEY(kind,value,record_id))",
            "CREATE TABLE IF NOT EXISTS observations (record_id INTEGER NOT NULL REFERENCES records(id), run_id TEXT NOT NULL, signature TEXT NOT NULL, metadata_hash TEXT NOT NULL, metadata_json TEXT NOT NULL, first_seen_at TEXT NOT NULL, PRIMARY KEY(record_id,run_id,metadata_hash))",
            "CREATE INDEX IF NOT EXISTS observation_signature ON observations(record_id,signature)",
        )
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            for sql in statements:
                db.execute(sql)
            with db:
                yield db
        finally:
            db.close()

    def _record(self, db, path: Path, article: dict):
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("Archive path escapes the archive directory")
        value, text_hash, signature, urls, _ = _prepare(article)
        relative = path.relative_to(self.root).as_posix()
        db.execute("INSERT OR IGNORE INTO records(path,article_id,text_hash,signature) VALUES (?,?,?,?)",
                   (relative, value["article_id"], text_hash, signature))
        row = db.execute("SELECT * FROM records WHERE path=?", (relative,)).fetchone()
        for kind, identifier in [("article_id", value["article_id"]), *(("url", u) for u in urls)]:
            db.execute("INSERT OR IGNORE INTO identifiers VALUES (?,?,?)", (kind, identifier, row["id"]))
        return row

    def save(self, article: dict, *, run_id: str = "manual", source_path: str | None = None) -> dict:
        value, text_hash, signature, urls, day = _prepare(article)
        with self._connect() as db:
            # Recover immutable JSONs if the catalog was lost or first creation
            # was interrupted. Normal saves use indexed lookups, not a tree scan.
            if not db.execute("SELECT 1 FROM records LIMIT 1").fetchone():
                for path in sorted(self.root.rglob("*.json")):
                    if path.parent.name.startswith(".write-"):
                        continue
                    existing = _read(path)
                    if "article_id" in existing:
                        self._record(db, path, existing)

            matches = {}
            matched_by = set()
            for kind, identifier in [("article_id", value["article_id"]), *(("url", u) for u in urls)]:
                for row in db.execute("SELECT r.* FROM records r JOIN identifiers i ON i.record_id=r.id WHERE i.kind=? AND i.value=?", (kind, identifier)):
                    matches[row["id"]] = row
                    matched_by.add(kind)
            row = None
            for candidate in matches.values():
                observed = candidate["signature"] == signature or db.execute(
                    "SELECT 1 FROM observations WHERE record_id=? AND signature=? LIMIT 1", (candidate["id"], signature)
                ).fetchone()
                if observed or text_hash is None and candidate["text_hash"] is not None:
                    row = candidate
                    break
            if row is None and text_hash and (not matches or all(r["text_hash"] != text_hash for r in matches.values())):
                row = db.execute("SELECT * FROM records WHERE text_hash=? ORDER BY id LIMIT 1", (text_hash,)).fetchone()
                if row:
                    matched_by.add("text")

            action = "duplicate" if row else ("new_version" if matches else "created")
            previous_paths = sorted(r["path"] for r in matches.values())
            if row is None:
                directory = self.root / (f"{day:%Y}/{day:%m}/{day:%d.%m.%y}" if day else "_undated")
                name = value["article_id"]
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or name.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1,10)), *(f"LPT{i}" for i in range(1,10))}:
                    name = _digest(name)[:24]
                path = directory / f"{name}.json"
                if path.exists():
                    orphan = self._record(db, path, _read(path))
                    if orphan["signature"] == signature:
                        row = orphan
                        action = "duplicate"
                    else:
                        matches[orphan["id"]] = orphan
                        action = "new_version"
                        previous_paths = sorted(r["path"] for r in matches.values())
                if matches:
                    path = directory / f"{name}__{signature}.json"
                if row is None:
                    if not path.resolve().is_relative_to(self.root):
                        raise ValueError("Archive path escapes the archive directory")
                    try:
                        _publish(path, value)
                    except FileExistsError:
                        # A file can survive a failed SQLite transaction.
                        if _prepare(_read(path))[2] != signature:
                            raise ValueError(f"Archive filename conflict: {path}")
                    row = self._record(db, path, value)
            elif not (self.root / row["path"]).is_file():
                raise FileNotFoundError(f"Archive index points to a missing file: {row['path']}")

            if action == "duplicate" and text_hash and row["text_hash"] == text_hash:
                matched_by.add("text")
            for kind, identifier in [("article_id", value["article_id"]), *(("url", u) for u in urls)]:
                db.execute("INSERT OR IGNORE INTO identifiers VALUES (?,?,?)", (kind, identifier, row["id"]))
            metadata = {k: v for k, v in value.items() if k != "text"}
            metadata["source_path"] = source_path
            metadata_json = _json(metadata)
            db.execute("INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?)", (
                row["id"], run_id, signature, _digest(metadata_json), metadata_json, datetime.now(timezone.utc).isoformat()
            ))
            return {"article_id": value["article_id"], "stored_article_id": row["article_id"],
                    "url": value.get("url"), "canonical_url": value.get("canonical_url"),
                    "action": action, "archive_path": row["path"], "matched_by": sorted(matched_by),
                    "previous_versions": previous_paths if action == "new_version" else []}


def archive_run(run_dir: Path | str, archive_dir: Path | str | None = None) -> dict:
    run_dir = Path(run_dir).resolve()
    manifest = _read(run_dir / "manifest.json")
    if archive_dir is None:
        archive_dir = manifest.get("article_storage", {}).get("archive_dir", "article")
    archive = ArticleArchive(archive_dir)
    if archive.root.is_relative_to(run_dir) or run_dir.is_relative_to(archive.root):
        raise ValueError("Persistent article archive must not overlap the individual run directory")
    paths = sorted((run_dir / "articles").glob("*.json"))
    if not paths:
        raise ValueError(f"No staged Article JSON files in {run_dir / 'articles'}")
    # Validate the whole input before publishing the first new archive entry.
    prepared = [(p, _read(p)) for p in paths]
    article_ids = set()
    for _, article in prepared:
        _prepare(article)
        if article["article_id"] in article_ids:
            raise ValueError(f"Duplicate staged article_id: {article['article_id']}")
        article_ids.add(article["article_id"])
    index_path = run_dir / "article_index.json"
    index = None
    if index_path.exists():
        index = json.loads(index_path.read_text(encoding="utf-8-sig"))
        if not isinstance(index, list) or any(
            not isinstance(entry, dict)
            or "article_id" in entry and not isinstance(entry["article_id"], str)
            for entry in index
        ):
            raise ValueError("article_index.json must be a list of objects with string article IDs")
    items = []
    for path, article in prepared:
        result = archive.save(article, run_id=manifest["run_id"], source_path=str(path))
        result["run_relative_path"] = Path(os.path.relpath(archive.root / result["archive_path"], run_dir)).as_posix()
        items.append(result)
    report = {"schema_version": "0.1", "run_id": manifest["run_id"], "archive_root": str(archive.root),
              "layout": "YYYY/MM/DD.MM.YY", "articles_processed": len(items),
              "actions": dict(Counter(x["action"] for x in items)), "items": items}
    _publish(run_dir / "article_archive.json", report, replace=True)
    manifest["article_storage"] = {"archive_dir": str(archive.root), "layout": "YYYY/MM/DD.MM.YY",
                                   "mode": "append_only_deduplicated", "receipt": "article_archive.json"}
    _publish(run_dir / "manifest.json", manifest, replace=True)
    if index is not None:
        by_id = {x["article_id"]: x for x in items}
        for entry in index:
            if entry.get("article_id") in by_id:
                entry["archive_path"] = by_id[entry["article_id"]]["run_relative_path"]
        # The run index is a mutable pointer list, never the archive body.
        _publish(index_path, index, replace=True)
    return report
