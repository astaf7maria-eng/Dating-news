"""User context stays fixed for so what and never changes search coverage."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from dating_digest.domain.context import (
    build_context_bundle, validate_context_bundle, validate_context_refs,
)
from dating_digest.discovery.plan import build_plan


ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.context = read(ROOT / "config/business_context.json")
        self.bundle = build_context_bundle(self.context)

    def test_plans_test_results_and_unknowns_never_become_live_capabilities(self):
        claims = {c["id"]: c for c in self.bundle["business_context"]["context_claims"]}
        self.assertEqual(claims["verification_test"]["status"], "observed_in_test")
        self.assertEqual(claims["verification_design"]["status"], "planned")
        self.assertEqual(claims["liveness_unknown"]["status"], "hypothesis")
        self.assertEqual(claims["release_conflicts"]["status"], "unresolved")
        self.assertEqual(self.bundle["business_context"]["known_product_capabilities"], [])
        self.assertTrue(self.bundle["warnings"])
        self.context["context_claims"][0]["statement"] = "changed after snapshot"
        self.assertNotEqual(self.bundle, build_context_bundle(self.context))
        validate_context_bundle(self.bundle)

    def test_broken_provenance_or_narrowing_policy_rejected(self):
        for mutate in (
            lambda c: c["decision_questions"][0]["claim_ids"].append("invented"),
            lambda c: c["context_claims"][0].update(status="launched"),
            lambda c: c["context_claims"][0].update(pages=[]),
            lambda c: c["context_policy"].update(no_topic_or_geography_quota=False),
            lambda c: c["decision_questions"].append(copy.deepcopy(c["decision_questions"][0])),
        ):
            context = copy.deepcopy(self.context)
            mutate(context)
            with self.assertRaises(ValueError):
                build_context_bundle(context)

    def test_market_watch_allowed_but_unknown_references_rejected(self):
        validate_context_refs(self.bundle, {
            "relevance_type": "market_watch", "question_ids": [], "claim_ids": [],
        })
        for refs in (
            {"relevance_type": "decision_support", "question_ids": [], "claim_ids": []},
            {"relevance_type": "decision_support", "question_ids": ["invented"], "claim_ids": []},
            {"relevance_type": "market_watch", "question_ids": [], "claim_ids": ["invented"]},
            {"relevance_type": "market_watch", "question_ids": "", "claim_ids": []},
            {"relevance_type": "market_watch", "question_ids": [], "claim_ids": ["positioning", "positioning"]},
        ):
            with self.assertRaises(ValueError):
                validate_context_refs(self.bundle, refs)

    def test_declared_context_requires_matching_bundle_hash_and_version(self):
        config = ROOT / "config"
        catalog, sources, rules = [read(config / f"{name}.json") for name in ("companies", "sources", "queries")]
        manifest = {
            "run_id": "context-check", "window_start": "2026-11-01T00:00:00+03:00",
            "window_end_exclusive": "2026-11-15T00:00:00+03:00",
            "window_confirmation": {"status": "confirmed"},
            "business_context": {"sha256": self.bundle["context_sha256"], "version": "wrong-version"},
        }
        with self.assertRaisesRegex(ValueError, "missing"):
            build_plan(catalog, sources, rules, manifest)
        with self.assertRaisesRegex(ValueError, "manifest"):
            build_plan(catalog, sources, rules, manifest, self.bundle)

    def test_context_is_saved_for_editorial_without_changing_search(self):
        config = ROOT / "config"
        catalog, sources, rules = [read(config / f"{name}.json") for name in ("companies", "sources", "queries")]
        manifest = {
            "run_id": "context-test", "window_start": "2026-11-01T00:00:00+03:00",
            "window_end_exclusive": "2026-11-15T00:00:00+03:00",
            "window_confirmation": {"status": "confirmed"},
        }
        baseline = build_plan(catalog, sources, rules, manifest)
        plan = build_plan(catalog, sources, rules, manifest, self.bundle)
        self.assertEqual(baseline["queries"], plan["queries"])
        self.assertEqual(baseline["search_tasks"], plan["search_tasks"])
        self.assertEqual(baseline["coverage"], plan["coverage"])
        self.assertEqual(plan["window_start"], manifest["window_start"])
        self.assertEqual(plan["window_end_exclusive"], manifest["window_end_exclusive"])
        self.assertFalse(any(q["lane"] == "context" or q["decision_id"] is not None for q in plan["queries"]))
        self.assertNotIn("business_context", plan["config_snapshot"])
        self.assertNotIn("context_warnings", plan)
        self.assertEqual(plan["context_sha256"], self.bundle["context_sha256"])
        self.assertEqual(plan["business_context_usage"], "editorial_only_no_revalidation")

    def test_unused_context_search_fields_do_not_change_requests(self):
        config = ROOT / "config"
        catalog, sources, rules = [read(config / f"{name}.json") for name in ("companies", "sources", "queries")]
        manifest = {
            "run_id": "fixed-context", "window_start": "2026-11-01T00:00:00+03:00",
            "window_end_exclusive": "2026-11-15T00:00:00+03:00",
            "window_confirmation": {"status": "confirmed"},
        }
        before = build_plan(catalog, sources, rules, manifest, self.bundle)
        context = copy.deepcopy(self.context)
        context["context_policy"].pop("search_mode")
        for question in context["decision_questions"]:
            question.pop("search_queries")
        after = build_plan(catalog, sources, rules, manifest, build_context_bundle(context))
        self.assertEqual(before["queries"], after["queries"])
        self.assertEqual(before["search_tasks"], after["search_tasks"])


class ContextCliTests(unittest.TestCase):
    def test_plan_search_rejects_removed_options_and_unfrozen_runs(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            manifest_path = run_dir / "manifest.json"
            command = [sys.executable, "-m", "dating_digest", "plan-search", "--run-dir", str(run_dir)]
            for version in ("0.3", "0.4"):
                manifest_path.write_text(json.dumps({"schema_version": version}), encoding="utf-8")
                before = manifest_path.read_bytes()
                result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("create a new run", result.stderr)
                self.assertFalse((run_dir / "search_plan.json").exists())
                self.assertEqual(manifest_path.read_bytes(), before)
            for options in (("--strategy", "exhaustive"), ("--strategy", "fast"), ("--config-dir", "config")):
                result = subprocess.run(command + list(options), cwd=ROOT, env=env,
                                        capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("unrecognized arguments", result.stderr)
                self.assertFalse((run_dir / "search_plan.json").exists())

    def test_run_freezes_context_and_rejects_tampering_without_pdf_dependency(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = base / "config"
            config.mkdir()
            # A clean checkout need not have the local source PDF.
            for path in (ROOT / "config").glob("*.json"):
                (config / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

            def run(*args):
                return subprocess.run([sys.executable, "-m", "dating_digest", *args],
                                      cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")

            company_path = config / "companies.json"
            valid = read(company_path)
            invalid = copy.deepcopy(valid)
            invalid["companies"][0]["source_ids"].append("missing")
            company_path.write_text(json.dumps(invalid), encoding="utf-8")
            rejected = run("init-run", "--start-date", "2026-11-01", "--end-date", "2026-11-14",
                           "--config-dir", str(config), "--data-dir", str(base / "data"))
            self.assertEqual(rejected.returncode, 2, rejected.stderr)
            self.assertIn("references", rejected.stderr)
            self.assertFalse((base / "data").exists())
            company_path.write_text(json.dumps(valid), encoding="utf-8")
            created = run("init-run", "--start-date", "2026-11-01", "--end-date", "2026-11-14",
                          "--config-dir", str(config), "--data-dir", str(base / "data"))
            self.assertEqual(created.returncode, 0, created.stderr)
            run_dir = next((base / "data/runs").iterdir())
            original = read(run_dir / "context_bundle.json")
            manifest = read(run_dir / "manifest.json")
            self.assertEqual(manifest["business_context"]["sha256"], original["context_sha256"])
            self.assertEqual(read(run_dir / "config_snapshot/business_context.json"), original["business_context"])

            changed = read(config / "business_context.json")
            changed["version"] = "future-version"
            (config / "business_context.json").write_text(json.dumps(changed), encoding="utf-8")
            # Every input must come from init-run, even if the live catalog vanishes.
            (config / "companies.json").unlink()
            (config / "sources.json").unlink()
            (config / "queries.json").unlink()
            planned = run("plan-search", "--run-dir", str(run_dir))
            self.assertEqual(planned.returncode, 0, planned.stderr)
            plan_path = run_dir / "search_plan.json"
            plan_bytes = plan_path.read_bytes()
            plan = read(plan_path)
            self.assertEqual(plan["catalog_origin"], "run_snapshot")
            self.assertEqual(plan["config_snapshot"]["companies"], read(ROOT / "config/companies.json"))
            self.assertEqual(plan["context_sha256"], original["context_sha256"])
            self.assertEqual(plan["config_versions"]["business_context"], original["context_version"])
            self.assertNotEqual(run("plan-search", "--run-dir", str(run_dir)).returncode, 0)
            self.assertEqual(plan_path.read_bytes(), plan_bytes)

            tampered = copy.deepcopy(original)
            tampered["business_context"]["context_claims"][0]["status"] = "hypothesis"
            (run_dir / "context_bundle.json").write_text(json.dumps(tampered), encoding="utf-8")
            rejected = run("plan-search", "--run-dir", str(run_dir))
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("snapshot", rejected.stderr)
            self.assertEqual(plan_path.read_bytes(), plan_bytes)

            snapshot = run_dir / "config_snapshot/companies.json"
            snapshot.write_text('{}', encoding="utf-8")
            rejected = run("plan-search", "--run-dir", str(run_dir))
            self.assertIn("Configuration snapshot", rejected.stderr)
            self.assertEqual(plan_path.read_bytes(), plan_bytes)


if __name__ == "__main__":
    unittest.main()
