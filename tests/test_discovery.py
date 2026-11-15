"""Coverage contracts for required brands and open-ended discovery."""

import copy
import json
from pathlib import Path
import unittest

from dating_digest.discovery.plan import build_plan
from dating_digest.__main__ import CONFIG_NAMES, read_json, validate_configs


class DiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config = Path(__file__).resolve().parents[1] / "config"
        cls.catalog, cls.sources, cls.rules, cls.golden = [
            json.loads((config / name).read_text(encoding="utf-8"))
            for name in ("companies.json", "sources.json", "queries.json", "golden_set.json")
        ]

    def setUp(self):
        self.manifest = {
            "run_id": "test", "window_start": "2026-11-01T00:00:00+03:00",
            "window_end_exclusive": "2026-11-15T00:00:00+03:00",
            "window_confirmation": {"status": "confirmed"},
        }

    def plan(self, catalog=None):
        return build_plan(catalog or self.catalog, self.sources, self.rules, self.manifest)

    def test_required_five_and_all_golden_companies_get_ru_en_baselines(self):
        required = set(self.catalog["required_always_company_ids"]
                       + self.catalog["golden_set_company_ids"]
                       + self.catalog["golden_adjacent_company_ids"])
        self.assertTrue({"tinder", "badoo", "bumble", "hinge", "grindr"} <= required)
        self.assertTrue({cid for case in self.golden["cases"] for cid in case["company_ids"]} <= required)
        plan = self.plan()
        for cid in required:
            languages = {q["language"] for q in plan["queries"]
                         if q["lane"] == "company" and q["family"] == "baseline" and cid in q["company_ids"]}
            self.assertTrue({"ru", "en"} <= languages, cid)

    def test_no_silent_disabling_of_required_brand(self):
        catalog = copy.deepcopy(self.catalog)
        next(c for c in catalog["companies"] if c["id"] == "badoo")["enabled"] = False
        with self.assertRaisesRegex(ValueError, "Обязательную"):
            self.plan(catalog)

    def test_unknown_official_domain_does_not_disable_liangpei(self):
        self.assertFalse(next(c for c in self.catalog["companies"] if c["id"] == "liangpei_ai")["official_domains"])
        queries = [q for q in self.plan()["queries"] if "liangpei_ai" in q["company_ids"]]
        self.assertTrue(any(q["language"] == "zh" and "良配" in q["query"] for q in queries))

    def test_unbranded_search_includes_unknown_models_and_risk_bulletins(self):
        plan = self.plan()
        discovered = [q for q in plan["queries"] if q["lane"] == "discovery"]
        self.assertTrue(all(not q["company_ids"] for q in discovered))
        self.assertTrue({"pay_per_date", "outcome_guarantee", "open_ended"}
                        <= {q["model_id"] for q in discovered})
        self.assertTrue(any(q["lane"] == "risk_monitoring" for q in plan["queries"]))

    def test_unconfirmed_period_rejected_and_custom_period_preserved(self):
        plan = self.plan()
        self.assertEqual(plan["window_start"], self.manifest["window_start"])
        self.assertEqual(plan["window_end_exclusive"], self.manifest["window_end_exclusive"])
        self.manifest["window_confirmation"] = {"status": "pending"}
        with self.assertRaisesRegex(ValueError, "период"):
            self.plan()

    def test_queries_deterministic_unique_and_not_reported_as_executed(self):
        plan = self.plan()
        self.assertEqual(plan, self.plan())
        self.assertEqual(plan["query_count"], len({q["query_id"] for q in plan["queries"]}))
        self.assertEqual(plan["status"], "planned_not_executed")
        self.assertTrue(all(c["articles_saved"] is None for c in plan["coverage"]))
        self.assertTrue(all(q["status"] == "planned" for q in plan["queries"]))
        self.assertEqual(plan["config_snapshot"]["queries"], self.rules)

    def test_catalog_references_resolve(self):
        root = Path(__file__).resolve().parents[1]
        validate_configs({name: read_json(root / "config" / f"{name}.json") for name in CONFIG_NAMES}, root / "schemas")
        self.assertEqual(len(self.golden["cases"]), 10)

    def test_same_query_for_two_models_preserves_both_coverage_links(self):
        rules = copy.deepcopy(self.rules)
        pattern = copy.deepcopy(rules["discovery"]["business_model_patterns"][0])
        original_id = pattern["id"]
        pattern["id"] = "same_words_different_model"
        rules["discovery"]["business_model_patterns"].append(pattern)
        plan = build_plan(self.catalog, self.sources, rules, self.manifest)
        original = [q for q in plan["queries"] if q["model_id"] == original_id]
        duplicate = [q for q in plan["queries"] if q["model_id"] == pattern["id"]]
        self.assertEqual([q["query"] for q in original], [q["query"] for q in duplicate])
        self.assertTrue({q["query_id"] for q in original}.isdisjoint(q["query_id"] for q in duplicate))


if __name__ == "__main__":
    unittest.main()
