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
        return build_plan(catalog or self.catalog, self.sources, self.rules, self.manifest, strategy="exhaustive")

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

    def test_tasks_cover_every_query_once_without_mixing_language_results(self):
        plan = self.plan()
        by_id = {q["query_id"]: q for q in plan["queries"]}
        assigned = []
        for task in plan["search_tasks"]:
            for language, query_ids in task["language_variants"].items():
                for query_id in query_ids:
                    self.assertEqual(by_id[query_id]["query"], task["query"])
                    self.assertEqual(by_id[query_id]["language"], language)
                    assigned.append(query_id)
        self.assertCountEqual(assigned, by_id)
        self.assertEqual(len(assigned), len(set(assigned)))
        tinder = next(q for q in plan["queries"] if q["lane"] == "company"
                      and q["family"] == "baseline" and "tinder" in q["company_ids"])
        task = next(t for t in plan["search_tasks"] if t["query"] == tinder["query"])
        self.assertEqual(set(task["language_variants"]), {"ru", "en"})
        self.assertTrue(set(task["language_variants"]["ru"]).isdisjoint(task["language_variants"]["en"]))

    def test_tasks_do_not_reuse_identifiers_across_reporting_windows(self):
        original = self.plan()
        self.manifest["window_start"] = "2026-11-02T00:00:00+03:00"
        changed = self.plan()
        self.assertEqual([t["query"] for t in original["search_tasks"]],
                         [t["query"] for t in changed["search_tasks"]])
        self.assertTrue({t["task_id"] for t in original["search_tasks"]}.isdisjoint(
            t["task_id"] for t in changed["search_tasks"]))

    def test_catalog_references_resolve(self):
        root = Path(__file__).resolve().parents[1]
        validate_configs({name: read_json(root / "config" / f"{name}.json") for name in CONFIG_NAMES}, root / "schemas")
        self.assertEqual(len(self.golden["cases"]), 10)

    def test_official_media_and_investor_sources_precede_media_and_open_search(self):
        plan = self.plan()
        by_source = {source["id"]: source for source in self.sources["sources"]}
        priorities = [q["search_priority"] for q in plan["queries"]]
        self.assertEqual(priorities, sorted(priorities))
        self.assertEqual(set(priorities), {1, 2, 3, 4})
        for query in plan["queries"]:
            if query["search_priority"] == 1:
                self.assertEqual(query["lane"], "official_source")
                self.assertIn(by_source[query["source_id"]]["kind"],
                              {"official_newsroom", "investor_relations"})
        tasks = plan["search_tasks"]
        self.assertEqual([t["search_priority"] for t in tasks], sorted(t["search_priority"] for t in tasks))
        by_query = {q["query_id"]: q for q in plan["queries"]}
        for task in tasks:
            related = [by_query[qid] for ids in task["language_variants"].values() for qid in ids]
            self.assertEqual(task["search_priority"], min(q["search_priority"] for q in related))

    def test_key_media_get_keyword_search_beyond_startup_discovery(self):
        queries = [q for q in self.plan()["queries"] if q["lane"] == "media_review"]
        policy = self.sources["source_policy"]
        self.assertEqual({q["source_id"] for q in queries}, set(policy["key_media_ids"]))
        self.assertEqual([q["source_id"] for q in queries], policy["key_media_ids"])
        techcrunch = next(q for q in queries if q["source_id"] == "techcrunch")
        self.assertIn("site:techcrunch.com", techcrunch["query"])
        for query in queries:
            language = query["language"]
            for word in policy["media_keywords"][language] + self.rules["discovery"]["context"][language]:
                self.assertIn('"' + word + '"', query["query"])
        rules = copy.deepcopy(self.rules)
        rules["discovery"]["enabled"] = False
        plan = build_plan(self.catalog, self.sources, rules, self.manifest, strategy="exhaustive")
        self.assertEqual(queries, [q for q in plan["queries"] if q["lane"] == "media_review"])

    def test_invalid_key_media_policy_is_rejected_before_run(self):
        root = Path(__file__).resolve().parents[1]
        configs = {name: read_json(root / "config" / f"{name}.json") for name in CONFIG_NAMES}
        for change in ("unknown", "disabled", "not_media", "no_keywords"):
            with self.subTest(change=change):
                invalid = copy.deepcopy(configs)
                policy = invalid["sources"]["source_policy"]
                source = next(s for s in invalid["sources"]["sources"] if s["id"] == "techcrunch")
                if change == "unknown":
                    policy["key_media_ids"].append("unknown")
                elif change == "disabled":
                    source["enabled"] = False
                elif change == "not_media":
                    policy["key_media_ids"].append("tinder_newsroom")
                else:
                    policy["media_keywords"]["en"] = []
                with self.assertRaises(ValueError):
                    validate_configs(invalid, root / "schemas")

    def test_event_policy_rejects_missing_language_queries_and_conflicting_filters(self):
        root = Path(__file__).resolve().parents[1]
        configs = {name: read_json(root / "config" / f"{name}.json") for name in CONFIG_NAMES}
        for change in ('direction_terms', 'company_terms', 'native_query', 'primary_language',
                       'filter_pattern', 'editorial_filter', 'hint_company', 'campaign_company'):
            with self.subTest(change=change):
                invalid = copy.deepcopy(configs)
                rules = invalid['queries']
                if change == 'direction_terms':
                    del rules['research_policy']['directions']['marketing']['terms']['ja']
                elif change == 'company_terms':
                    del rules['company_query_policy']['event_terms']['ja']
                elif change == 'native_query':
                    del rules['discovery']['native_language_queries'][0]['direction_queries']['marketing']
                elif change == 'primary_language':
                    invalid['companies']['companies'][0]['primary_search_language'] = 'missing'
                elif change == 'filter_pattern':
                    rules['retrieval_policy']['candidate_filter']['title_rules'][0]['pattern'] = '['
                elif change == 'editorial_filter':
                    invalid['editorial_policy']['material_filter']['excluded_types'].remove('service_comparison')
                elif change == 'campaign_company':
                    invalid['golden_set']['marketing_reference_cases'][0]['company_ids'] = ['unknown_brand']
                else:
                    rules['company_event_hints']['unknown_brand'] = {'en': ['launches']}
                with self.assertRaises(ValueError):
                    validate_configs(invalid, root / 'schemas')

    def test_old_query_snapshots_remain_available_only_for_explicit_exhaustive_comparison(self):
        root = Path(__file__).resolve().parents[1]
        legacy = {name: read_json(root / 'config' / f'{name}.json') for name in CONFIG_NAMES}
        del legacy['queries']['research_policy']
        del legacy['queries']['retrieval_policy']['candidate_filter']
        del legacy['editorial_policy']['material_filter']
        del legacy['editorial_policy']['research_focus']
        with self.assertRaisesRegex(ValueError, 'research_policy'):
            validate_configs(legacy, root / 'schemas')
        validate_configs(legacy, root / 'schemas', allow_legacy_queries=True)
        comparison = build_plan(legacy['companies'], legacy['sources'], legacy['queries'],
                                self.manifest, strategy='exhaustive')
        self.assertEqual(comparison['status'], 'planned_not_executed')
        with self.assertRaisesRegex(ValueError, 'research_policy'):
            build_plan(legacy['companies'], legacy['sources'], legacy['queries'], self.manifest)

    def test_same_query_for_two_models_preserves_both_coverage_links(self):
        rules = copy.deepcopy(self.rules)
        pattern = copy.deepcopy(rules["discovery"]["business_model_patterns"][0])
        original_id = pattern["id"]
        pattern["id"] = "same_words_different_model"
        rules["discovery"]["business_model_patterns"].append(pattern)
        plan = build_plan(self.catalog, self.sources, rules, self.manifest, strategy="exhaustive")
        original = [q for q in plan["queries"] if q["model_id"] == original_id]
        duplicate = [q for q in plan["queries"] if q["model_id"] == pattern["id"]]
        self.assertEqual([q["query"] for q in original], [q["query"] for q in duplicate])
        self.assertTrue({q["query_id"] for q in original}.isdisjoint(q["query_id"] for q in duplicate))
        for query in original:
            task = next(t for t in plan["search_tasks"] if t["query"] == query["query"])
            covered = task["language_variants"][query["language"]]
            self.assertIn(query["query_id"], covered)
            matching = next(q for q in duplicate if q["language"] == query["language"])
            self.assertIn(matching["query_id"], covered)
        baseline = self.plan()
        self.assertEqual(plan["task_count"], baseline["task_count"])
        self.assertEqual(plan["language_request_count"], baseline["language_request_count"])


if __name__ == "__main__":
    unittest.main()
