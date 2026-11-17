"""Saved-corpus QA: real contracts, stale evidence and the editorial gate."""

import copy
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from dating_digest.__main__ import CONFIG_NAMES
from dating_digest.discovery.plan import build_plan
from dating_digest.domain.context import json_sha256
from dating_digest.io import read_json, write_json
from dating_digest.research_qa import CHECKS, audit_research, check_research_qa, prepare_research_qa
from dating_digest.storage import archive_run

ROOT = Path(__file__).resolve().parents[1]


class ResearchQATests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run = self.root / "run"
        self.run.mkdir()
        configs = {name: read_json(ROOT / "config" / f"{name}.json") for name in CONFIG_NAMES}
        for name, value in configs.items():
            write_json(self.run / "config_snapshot" / f"{name}.json", value)
        self.manifest = dict(run_id="qa-fixture", window_start="2026-11-01T00:00:00+03:00",
                             window_end_exclusive="2026-11-16T00:00:00+03:00",
                             window_confirmation={"status": "confirmed"},
                             config_sha256={name: json_sha256(value) for name, value in configs.items()},
                             article_storage={"archive_dir": str(self.root / "archive")})
        write_json(self.run / "manifest.json", self.manifest)
        write_json(self.run / "search_plan.json", build_plan(configs["companies"], configs["sources"],
                                                            configs["queries"], self.manifest, None))
        self.article = dict(schema_version="0.1", article_id="one", source_id="test-newsroom",
                            url="https://example.com/one", canonical_url="https://example.com/one",
                            title="新しい機能", language="ja", published_at="2026-11-15T23:59:59+03:00",
                            date_precision="timestamp", event_date=None, collected_at="2026-11-16T12:00:00+03:00",
                            text="日本語の原文と制限。", extraction_status="full", company_ids=[], tags=["features"])
        self.save_article()
        raw = self.run / "raw"
        raw.mkdir()
        self.observation = dict(source_url=self.article["url"], checked_at="2026-11-16T12:00:00+03:00",
                                article_id="one", status="checked", evidence="日本語の原文と制限。")
        plan = read_json(self.run / "search_plan.json")
        # Explicit synthetic observations; real targets require source-based decisions.
        self.observation["targets"] = [f"company:{r['company_id']}:{lang}" for r in plan['coverage'] for lang in r['languages']]
        self.observation["targets"] += [f"source:{sid}:{direction}" for r in plan['source_indexes']
                                        for sid in r['source_ids'] for direction in r['direction_ids']]
        (raw / "source_reviews.jsonl").write_text(json.dumps(self.observation, ensure_ascii=False) + "\n", encoding="utf-8")
        (raw / "event_candidates.jsonl").write_text("", encoding="utf-8")
        self.ref = "raw/source_reviews.jsonl:1"
        archive_run(self.run)

    def save_article(self):
        text = self.article["text"]
        self.article["text_sha256"] = sha256(text.encode("utf-8")).hexdigest() if text is not None else None
        write_json(self.run / "articles/one.json", self.article, replace=True)

    def audit(self):
        return prepare_research_qa(self.run, ROOT)

    def review(self, audit=None):
        audit = audit or self.audit()
        review = dict(schema_version="0.1", run_id=audit["run_id"], input_sha256=audit["input_sha256"],
                      verdict="passed", summary="Fixture source assessment", findings=[],
                      checks={name: dict(status="completed", summary="Checked", evidence_refs=[self.ref]) for name in CHECKS},
                      articles=[dict(path=a["path"], article_id=a["article_id"], disposition="eligible",
                                     reason="Source confirms date and new fact", evidence_refs=[self.ref]) for a in audit["articles"]],
                      coverage=[dict(target=t, status="no_findings", basis="Fixture observation", evidence_refs=[self.ref])
                                for t in audit["coverage_targets"]])
        self.write_review(review)
        return review

    def write_review(self, review):
        write_json(self.run / "research_review.json", review, replace=True)

    def check(self):
        return check_research_qa(self.run, ROOT)

    def test_local_success_is_not_semantic_approval_and_does_not_modify_corpus(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*.json")}
        audit = self.audit()
        self.assertEqual(audit["automatic_status"], "checks_passed")
        self.assertEqual(audit["semantic_status"], "not_reviewed")
        self.assertTrue(audit["coverage_targets"])
        for p, value in before.items():
            self.assertEqual(p.read_bytes(), value)
        with self.assertRaises(FileNotFoundError):
            self.check()
        self.review(audit)
        self.assertEqual(self.check()["eligible_article_paths"], ["articles/one.json"])

    def test_schema_types_formats_hashes_and_status_are_checked(self):
        for changes in ({"company_ids": "one"}, {"published_at": "2026-11-15"},
                        {"collected_at": "2026-02-31T00:00:00Z"}, {"extraction_status": "ok"},
                        {"text_sha256": "0" * 64}, {"canonical_url": "ftp://example.com/one"}):
            with self.subTest(changes=changes):
                invalid = dict(self.article, **changes)
                write_json(self.run / "articles/one.json", invalid, replace=True)
                self.assertEqual(audit_research(self.run, ROOT)["automatic_status"], "blocked")
        self.save_article()
        invalid = dict(self.article)
        del invalid["title"]
        write_json(self.run / "articles/one.json", invalid, replace=True)
        self.assertEqual(self.audit()["automatic_status"], "blocked")

    def test_outside_unknown_partial_and_day_boundary_require_exclusion(self):
        original = copy.deepcopy(self.article)
        for changes in ({"published_at": "2026-11-16T00:00:00+03:00"},
                        {"published_at": None, "date_precision": "unknown"},
                        {"extraction_status": "partial"},
                        {"text": "Damaged \ufffd text"}):
            with self.subTest(changes=changes):
                self.article = dict(original, **changes)
                self.save_article()
                archive_run(self.run)
                review = self.review()
                with self.assertRaisesRegex(ValueError, "Ineligible"):
                    self.check()
                review["articles"][0]["disposition"] = "excluded"
                self.write_review(review)
                self.assertEqual(self.check()["eligible_article_paths"], [])
        self.article = dict(original, date_precision="day", published_at=None, published_date_raw="2026-11-01")
        self.save_article()
        self.manifest["window_start"] = "2026-11-01T12:00:00+03:00"
        # Isolate day-boundary calculation; mismatched plan also correctly blocks.
        manifest = read_json(self.run / "manifest.json")
        manifest.update(window_start=self.manifest["window_start"])
        write_json(self.run / "manifest.json", manifest, replace=True)
        self.assertIn("ambiguous_day", self.audit()["articles"][0]["attention"])

    def test_naive_timestamp_is_rejected(self):
        self.article["published_at"] = "2026-11-15T12:00:00"
        self.save_article()
        self.assertEqual(self.audit()["automatic_status"], "blocked")

    def test_any_article_or_raw_file_change_invalidates_review(self):
        self.review()
        raw = self.run / "raw/qa_search_log.jsonl"
        raw.write_text(json.dumps(dict(query="dating launch", status="completed")) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.check()
        self.review()
        self.article["tags"] = ["features", "ai"]
        self.save_article()
        with self.assertRaisesRegex(ValueError, "stale"):
            self.check()

    def test_changed_archived_body_and_deleted_input_invalidate_review(self):
        self.review()
        path = next((self.root / "archive").rglob("*.json"))
        original = path.read_bytes()
        path.write_bytes(original + b"\n")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.check()
        path.write_bytes(original)
        (self.run / "raw/event_candidates.jsonl").unlink()
        with self.assertRaisesRegex(ValueError, "stale"):
            self.check()

    def test_passed_cannot_override_blocking_missing_checks_or_coverage(self):
        original = self.review()
        mutations = [lambda r: r["findings"].append(dict(severity="blocking", path="articles/one.json", issue="Unconfirmed", fix="Verify")),
                     lambda r: r["checks"].pop("dates"), lambda r: r["checks"]["sources"].update(status="incomplete"),
                     lambda r: r["coverage"].pop(), lambda r: r["coverage"][0].update(status="error"),
                     lambda r: r["articles"].clear(), lambda r: r["articles"][0].update(disposition="needs_review"),
                     lambda r: r["articles"].append(copy.deepcopy(r["articles"][0]))]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                value = copy.deepcopy(original)
                mutate(value)
                self.write_review(value)
                with self.assertRaises(ValueError):
                    self.check()

    def test_evidence_must_reference_existing_completed_observations(self):
        original = self.review()
        for ref in ("query-id-only", "raw/source_reviews.jsonl:999", "raw/source_reviews.jsonl:0", "../outside.jsonl:1"):
            with self.subTest(ref=ref):
                review = copy.deepcopy(original)
                review["articles"][0]["evidence_refs"] = [ref]
                self.write_review(review)
                with self.assertRaises((ValueError, OSError)):
                    self.check()
        for observation in ({}, dict(query="planned", status="planned"), dict(query="failed", status="error")):
            (self.run / "raw/source_reviews.jsonl").write_text(json.dumps(observation) + "\n", encoding="utf-8")
            self.review()
            with self.assertRaisesRegex(ValueError, "completed source"):
                self.check()

    def test_receipt_must_cover_corpus_and_actual_saved_text(self):
        self.article["text"] += " 追加情報。"
        self.save_article()
        self.assertIn("stale_archive_receipt", {f["code"] for f in self.audit()["findings"]})
        archive_run(self.run)
        self.assertEqual(self.audit()["automatic_status"], "checks_passed")
        receipt = read_json(self.run / "article_archive.json")
        receipt["items"].clear()
        write_json(self.run / "article_archive.json", receipt, replace=True)
        self.assertIn("archive_coverage", {f["code"] for f in self.audit()["findings"]})

    def test_staged_metadata_correction_requires_archiving_the_new_version(self):
        self.article["published_at"] = "2026-11-14T12:00:00+03:00"
        self.save_article()
        self.assertIn("stale_archive_receipt", {f["code"] for f in self.audit()["findings"]})
        archive_run(self.run)
        self.assertEqual(self.audit()["automatic_status"], "checks_passed")

    def test_zero_articles_is_valid_only_with_completed_research_assessment(self):
        (self.run / "articles/one.json").unlink()
        (self.run / "article_archive.json").unlink()
        self.review()
        self.assertEqual(self.check()["eligible_article_paths"], [])
        review = read_json(self.run / "research_review.json")
        review["coverage"][0]["status"] = "not_checked"
        self.write_review(review)
        with self.assertRaisesRegex(ValueError, "Coverage"):
            self.check()

    def test_malformed_journal_and_incomplete_event_note_are_reported(self):
        (self.run / "raw/broken.jsonl").write_text('{"unfinished":', encoding="utf-8")
        (self.run / "raw/event_candidates.jsonl").write_text('{"actor": "Company"}\n', encoding="utf-8")
        codes = {f["code"] for f in self.audit()["findings"]}
        self.assertTrue({"invalid_json", "event_note_fields"} <= codes)

    def test_changed_snapshot_or_reduced_coverage_is_blocked(self):
        plan = read_json(self.run / "search_plan.json")
        plan["coverage"].pop()
        write_json(self.run / "search_plan.json", plan, replace=True)
        self.assertIn("coverage_plan_mismatch", {f["code"] for f in self.audit()["findings"]})
        policy = read_json(self.run / "config_snapshot/editorial_policy.json")
        policy["max_news_total"] = 9
        write_json(self.run / "config_snapshot/editorial_policy.json", policy, replace=True)
        self.assertIn("snapshot_mismatch", {f["code"] for f in self.audit()["findings"]})

    def test_cli_gate_fails_without_review_and_succeeds_with_current_assessment(self):
        result = subprocess.run([sys.executable, "-m", "dating_digest", "qa-research", "--run-dir", str(self.run)],
                                cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([sys.executable, "-m", "dating_digest", "check-research-qa", "--run-dir", str(self.run)],
                                cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertNotEqual(result.returncode, 0)
        self.review()
        result = subprocess.run([sys.executable, "-m", "dating_digest", "check-research-qa", "--run-dir", str(self.run)],
                                cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["verdict"], "passed")

    def test_subset_allows_coverage_and_excluded_article_blockers_but_not_eligible_or_unscoped(self):
        review = self.review()
        review['verdict'] = 'needs_revision'
        review['findings'] = [dict(severity='blocking', path='coverage', issue='Incomplete', fix='Finish research')]
        self.write_review(review)
        self.assertEqual(check_research_qa(self.run, ROOT, verified_subset=True)['eligible_article_paths'], ['articles/one.json'])
        review['findings'].append(dict(severity='blocking', path='articles/one.json#/facts', issue='Contradiction', fix='Verify'))
        self.write_review(review)
        with self.assertRaisesRegex(ValueError, 'blocking finding'):
            check_research_qa(self.run, ROOT, verified_subset=True)
        review['articles'][0]['disposition'] = 'needs_review'
        self.write_review(review)
        self.assertEqual(check_research_qa(self.run, ROOT, verified_subset=True)['eligible_article_paths'], [])
        review['findings'][1]['path'] = 'articles'
        self.write_review(review)
        with self.assertRaisesRegex(ValueError, 'Unscoped blocking'):
            check_research_qa(self.run, ROOT, verified_subset=True)

    def test_evidence_requires_terminal_status_and_explicit_article_and_target(self):
        for changes, message in (({'status': 'needs_review'}, 'completed source'),
                                 ({'status': None}, 'completed source'),
                                 ({'status': 'retrieved'}, 'completed source'),
                                 ({'article_id': 'another'}, 'assessed Article'),
                                 ({'targets': []}, 'coverage target'),
                                 ({'result': 'unverified'}, 'contradictory evidence'),
                                 ({'result_assignment': 'batch_union_not_per_query'}, 'completed source')):
            with self.subTest(changes=changes):
                (self.run / 'raw/source_reviews.jsonl').write_text(
                    json.dumps(self.observation | changes, ensure_ascii=False) + '\n', encoding='utf-8')
                self.review()
                with self.assertRaisesRegex(ValueError, message):
                    self.check()

    def test_plan_queries_tasks_policy_and_embedded_snapshots_are_verified(self):
        original = read_json(self.run / 'search_plan.json')
        for mutate in (lambda p: p['queries'].clear(), lambda p: p['search_tasks'].clear(),
                       lambda p: p['execution_policy'].update(budget={}),
                       lambda p: p['config_snapshot']['queries']['search_intents']['product_change']['terms'].update(en=['unrelated'])):
            plan = copy.deepcopy(original)
            mutate(plan)
            write_json(self.run / 'search_plan.json', plan, replace=True)
            self.assertIn('plan_content_mismatch', {f['code'] for f in self.audit()['findings']})

    def test_corrected_legacy_metadata_can_reach_qa_without_rewriting_archive(self):
        receipt = read_json(self.run / 'article_archive.json')
        path = self.root / 'archive' / receipt['items'][0]['archive_path']
        legacy = read_json(path)
        legacy['company_ids'] = 'invalid legacy data'
        write_json(path, legacy, replace=True)  # Simulate an archive created by the former permissive writer.
        before = path.read_bytes()
        result = archive_run(self.run)
        self.assertEqual(result['actions'], {'new_version': 1})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.audit()['automatic_status'], 'checks_passed')

    def test_tag_correction_requires_a_matching_archived_version(self):
        self.article['tags'] = ['ai']
        self.save_article()
        self.assertIn('stale_archive_receipt', {f['code'] for f in self.audit()['findings']})
        self.assertEqual(archive_run(self.run)['actions'], {'new_version': 1})
        self.assertEqual(self.audit()['automatic_status'], 'checks_passed')


if __name__ == "__main__":
    unittest.main()
