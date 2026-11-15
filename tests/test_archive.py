"""Cross-run persistence, immutable versions and source-preserving deduplication."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from dating_digest.storage import ArticleArchive, archive_run

ROOT = Path(__file__).resolve().parents[1]


def article(**changes):
    value = {"schema_version": "0.1", "article_id": "first", "source_id": "newsroom",
             "url": "https://example.com/news/1", "canonical_url": "https://example.com/news/1",
             "title": "Новая функция", "language": "ru", "published_at": "2026-11-02T12:00:00+03:00",
             "published_date_raw": "2026-11-02T12:00:00+03:00", "date_precision": "timestamp",
             "event_date": None, "collected_at": "2026-11-15T12:00:00+03:00",
             "text": "Текст новости.", "text_sha256": None, "extraction_status": "full", "company_ids": []}
    value.update(changes)
    return value


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = ArticleArchive(self.root / "article")

    def files(self):
        return sorted(self.archive.root.rglob("*.json"))

    def test_dated_layout_and_source_date_preserved(self):
        a = article(published_at=None, published_date_raw="02.11.2026", date_precision="day")
        r = self.archive.save(a)
        self.assertEqual(r["archive_path"], "2026/11/02.11.26/first.json")
        self.assertEqual(json.loads(self.files()[0].read_text(encoding="utf-8"))["published_date_raw"], "02.11.2026")

    def test_date_uses_moscow_not_machine_timezone(self):
        r = self.archive.save(article(published_at="2026-11-01T23:30:00Z", published_date_raw=None))
        self.assertIn("/02.11.26/", r["archive_path"])

    def test_raw_date_is_not_shifted_to_another_day_by_timezone(self):
        r = self.archive.save(article(published_at="2026-11-05T00:06:39+03:00", published_date_raw="2026-11-04T21:06:39Z"))
        self.assertIn("/04.11.26/", r["archive_path"])

    def test_unknown_date_does_not_become_body_or_collection_date(self):
        r = self.archive.save(article(published_at=None, published_date_raw="2026-11-02", date_precision="unknown"))
        self.assertEqual(r["archive_path"], "_undated/first.json")

    def test_next_run_skips_unchanged_article_without_overwriting(self):
        r = self.archive.save(article(), run_id="run1")
        path = self.archive.root / r["archive_path"]
        before = path.read_bytes()
        newer = article(collected_at="2026-11-16T13:00:00+03:00", query_ids=["other"])
        duplicate = ArticleArchive(self.archive.root).save(newer, run_id="run2")
        self.assertEqual(duplicate["action"], "duplicate")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.files()), 1)

    def test_url_alias_and_tracking_do_not_create_another_article(self):
        self.archive.save(article())
        a = article(article_id="changed-id", url="https://EXAMPLE.com/news/1?utm_source=mail#top",
                    canonical_url="https://example.com/news/1?utm_source=mail")
        duplicate = self.archive.save(a)
        self.assertEqual(duplicate["action"], "duplicate")
        self.assertIn("url", duplicate["matched_by"])
        self.assertEqual(len(self.files()), 1)

    def test_same_text_different_source_keeps_both_source_observations(self):
        self.archive.save(article(), run_id="one")
        reprint = article(article_id="reprint", source_id="other", url="https://other.test/story",
                          canonical_url="https://other.test/story", text="Текст   новости.\n")
        result = self.archive.save(reprint, run_id="one")
        self.assertEqual(result["action"], "duplicate")
        self.assertEqual(len(self.files()), 1)
        with closing(sqlite3.connect(self.archive.root / ".archive.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM observations").fetchone()[0], 2)
        self.assertEqual(self.archive.save(reprint, run_id="two")["action"], "duplicate")

    def test_changed_text_appends_version_and_old_version_remains(self):
        first = self.archive.save(article())
        path = self.archive.root / first["archive_path"]
        before = path.read_bytes()
        changed = article(text="Новое существенное содержание.")
        second = self.archive.save(changed)
        self.assertEqual(second["action"], "new_version")
        self.assertEqual(path.read_bytes(), before)
        self.assertNotEqual(first["archive_path"], second["archive_path"])
        self.assertEqual(self.archive.save(changed)["action"], "duplicate")
        self.assertEqual(self.archive.save(article())["archive_path"], first["archive_path"])
        self.assertEqual(len(self.files()), 2)

    def test_verified_date_creates_dated_revision_preserving_undated_original(self):
        old = self.archive.save(article(published_at=None, published_date_raw=None, date_precision="unknown"))
        new = self.archive.save(article())
        self.assertEqual(new["action"], "new_version")
        self.assertTrue((self.archive.root / old["archive_path"]).exists())
        self.assertIn("/02.11.26/", new["archive_path"])

    def test_empty_texts_of_unrelated_urls_are_not_global_duplicates(self):
        self.archive.save(article(text=None, extraction_status="unavailable"))
        r = self.archive.save(article(article_id="other", url="https://example.com/2",
                                      canonical_url="https://example.com/2", text=None, extraction_status="unavailable"))
        self.assertEqual(r["action"], "created")
        self.assertEqual(len(self.files()), 2)

    def test_unavailable_retry_does_not_erase_saved_full_text(self):
        first = self.archive.save(article())
        r = self.archive.save(article(text=None, extraction_status="failed"))
        self.assertEqual(r["action"], "duplicate")
        self.assertEqual(r["archive_path"], first["archive_path"])
        self.assertEqual(len(self.files()), 1)

    def test_meaningful_query_parameters_are_distinct(self):
        self.archive.save(article(url="https://example.com/?id=1", canonical_url="https://example.com/?id=1"))
        r = self.archive.save(article(article_id="second", url="https://example.com/?id=2",
                                      canonical_url="https://example.com/?id=2", text="Другая новость"))
        self.assertEqual(r["action"], "created")

    def test_bad_text_hash_is_rejected_before_any_write(self):
        with self.assertRaises(ValueError):
            self.archive.save(article(text_sha256="0" * 64))
        self.assertFalse(self.archive.root.exists())

    def test_concurrent_saves_publish_only_one_json(self):
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda n: self.archive.save(article(), run_id=str(n)), range(5)))
        self.assertEqual(sum(x["action"] == "created" for x in results), 1)
        self.assertEqual(len(self.files()), 1)

    def test_failed_index_transaction_preserves_file_and_retry_recovers_it(self):
        self.archive.save(article(article_id="other", url="https://example.com/other",
                                  canonical_url="https://example.com/other", text="Already indexed"))
        with patch.object(self.archive, "_record", side_effect=OSError("index failure")):
            with self.assertRaises(OSError):
                self.archive.save(article())
        before = {p: p.read_bytes() for p in self.files()}
        self.assertEqual(self.archive.save(article())["action"], "duplicate")
        self.assertEqual({p: p.read_bytes() for p in self.files()}, before)

    def test_missing_index_can_be_rebuilt_without_duplicate_json(self):
        self.archive.save(article())
        (self.archive.root / ".archive.sqlite3").unlink()
        self.assertEqual(self.archive.save(article())["action"], "duplicate")
        self.assertEqual(len(self.files()), 1)

    def test_cli_two_runs_share_archive_and_link_to_existing_article(self):
        for name in ("run1", "run2"):
            run = self.root / "runs" / name
            (run / "articles").mkdir(parents=True)
            (run / "manifest.json").write_text(json.dumps({"run_id":name}),encoding="utf-8")
            (run / "articles/first.json").write_text(json.dumps(article(),ensure_ascii=False),encoding="utf-8")
            result = subprocess.run([sys.executable,"-m","dating_digest","archive-articles","--run-dir",str(run),"--archive-dir",str(self.archive.root)],cwd=ROOT,env={**os.environ,"PYTHONPATH":str(ROOT/"src"),"PYTHONIOENCODING":"utf-8"},capture_output=True,text=True,encoding="utf-8")
            self.assertEqual(result.returncode,0,result.stderr)
            report = json.loads((run/"article_archive.json").read_text(encoding="utf-8"))
            expected = "created" if name == "run1" else "duplicate"
            self.assertEqual(report["actions"],{expected:1})
            self.assertTrue((run/report["items"][0]["run_relative_path"]).resolve().is_file())
        self.assertEqual(len(self.files()),1)
        with self.assertRaises(ValueError):
            archive_run(self.root/"runs/run2",self.root/"runs/run2/archive")


if __name__ == "__main__":
    unittest.main()
