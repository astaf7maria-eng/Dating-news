"""Small session primitives, not a network provider or autonomous collector.

Callers own scheduling, extraction and ArticleArchive persistence. Never use a
coverage label or task_id as a provider cache key. State can be checkpointed in
the run's existing raw directory; no persistent article store is introduced.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from threading import Lock
from time import monotonic

from dating_digest.storage.archive import normalize_url


_checkpoint_lock = Lock()


def request_key(*, provider: str, query: str, window_start: str,
                window_end_exclusive: str, page=1, filters=None, parameters=None) -> str:
    """Hash actual submitted parameters, not intended language/coverage labels."""
    actual = dict(provider=provider, query=query, window_start=window_start,
                  window_end_exclusive=window_end_exclusive, page=page,
                  filters=filters or {}, parameters=parameters or {})
    return sha256(json.dumps(actual, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")).hexdigest()


class Budget:
    def __init__(self, total_seconds=900, save_reserve_seconds=60, *, clock=monotonic):
        if not 0 <= save_reserve_seconds < total_seconds <= 900:
            raise ValueError("Budget must reserve saving time within 900 seconds")
        self.clock, self.started = clock, clock()
        self.total, self.reserve = total_seconds, save_reserve_seconds

    @property
    def elapsed(self):
        return self.clock() - self.started

    def remaining(self, *, network=True):
        return max(0, self.total - self.elapsed - (self.reserve if network else 0))

    def can_start(self, expected_seconds=25):
        return 0 < expected_seconds <= self.remaining()


class WorkLedger:
    """Reserve one job, including its bounded transient retry, before submission.

    Keep the reservation in-flight while retrying; finish_* records the final
    outcome. Terminal errors are not reopened by reserve/enqueue. The caller
    owns retry limits and persistence; the ledger never starts network work.
    """
    def __init__(self):
        self.lock = Lock()
        self.requests, self.urls, self.analysis = {}, {}, {}
        self.prevented_requests = self.prevented_urls = self.analysis_hits = 0

    def reserve_request(self, key, query_ids):
        with self.lock:
            if key in self.requests:
                row = self.requests[key]
                row["query_ids"] = sorted(set(row["query_ids"]) | set(query_ids))
                self.prevented_requests += 1
                return False
            self.requests[key] = {"status": "inflight", "query_ids": sorted(set(query_ids))}
            return True

    def finish_request(self, key, *, status, result_path=None):
        if status not in {"completed", "empty", "error"}:
            raise ValueError("Unknown request outcome")
        with self.lock:
            self.requests[key].update(status=status, result_path=result_path)

    def enqueue(self, url, observation):
        key = normalize_url(url)
        with self.lock:
            if key in self.urls:
                self.urls[key]["observations"].append(observation)
                self.prevented_urls += 1
                return False
            self.urls[key] = {"status": "queued", "observations": [observation]}
            return True

    def claim(self, url):
        key = normalize_url(url)
        with self.lock:
            if self.urls[key]["status"] != "queued":
                return False
            self.urls[key]["status"] = "inflight"
            return True

    def finish_url(self, url, *, status="fetched", aliases=(), **metadata):
        if status not in {"fetched", "failed"}:
            raise ValueError("Unknown URL outcome")
        key = normalize_url(url)
        aliases = list(dict.fromkeys(normalize_url(alias) for alias in aliases))
        with self.lock:
            row = self.urls[key]
            for alias in aliases:
                other = self.urls.get(alias)
                if other is not None and other is not row:
                    # A late failed worker must not replace an already fetched
                    # canonical result. Every earlier alias follows the merge.
                    if other["status"] == "fetched" and row["status"] != "fetched":
                        row, other = other, row
                    row["observations"].extend(other["observations"])
                    for known, shared in self.urls.items():
                        if shared is other:
                            self.urls[known] = row
                self.urls[alias] = row
            if status == "fetched" or row["status"] != "fetched":
                row.update(status=status, **metadata)

    def remember_analysis(self, text: str, result):
        digest = sha256(text.encode("utf-8")).hexdigest()
        with self.lock:
            self.analysis.setdefault(digest, result)
        return digest

    def cached_analysis(self, text: str):
        digest = sha256(text.encode("utf-8")).hexdigest()
        with self.lock:
            found = digest in self.analysis
            if found:
                self.analysis_hits += 1
            return found, self.analysis.get(digest)


def cache_is_fresh(collected_at: str, *, now: datetime | None = None, max_age_seconds=86400):
    collected = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
    if collected.tzinfo is None:
        return False
    age = ((now or datetime.now(timezone.utc)) - collected).total_seconds()
    return 0 <= age <= max_age_seconds


def checkpoint(path: Path, records: list[dict]):
    """Append a pre-serialized batch, serializing writers within this process."""
    payload = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _checkpoint_lock:
        with path.open("a", encoding="utf-8", newline="\n") as output:
            output.write(payload)
