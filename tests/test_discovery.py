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

    def test_required_and_golden_companies_keep_configured_languages(self):
        required = set(self.catalog["required_always_company_ids"]
                       + self.catalog["golden_set_company_ids"]
                       + self.catalog["golden_adjacent_company_ids"])
        self.assertTrue({"tinder", "badoo", "bumble", "hinge", "grindr"} <= required)
        self.assertTrue({cid for case in self.golden["cases"] for cid in case["company_ids"]} <= required)
        plan = self.plan()
        self.assertEqual(plan["strategy"], "fast")
        for cid in required:
            languages = {q["language"] for q in plan["queries"]
                         if q["lane"] == "company" and q["family"] == "event_lookup" and cid in q["company_ids"]}
            company = next(c for c in self.catalog["companies"] if c["id"] == cid)
            self.assertEqual(languages, set(company["search_languages"]), cid)

    def test_no_silent_disabling_of_required_brand(self):
        catalog = copy.deepcopy(self.catalog)
        next(c for c in catalog["companies"] if c["id"] == "badoo")["enabled"] = False
        with self.assertRaisesRegex(ValueError, "Обязательную"):
            self.plan(catalog)

    def test_unknown_official_domain_does_not_disable_liangpei(self):
        self.assertFalse(next(c for c in self.catalog["companies"] if c["id"] == "liangpei_ai")["official_domains"])
        queries = [q for q in self.plan()["queries"] if "liangpei_ai" in q["company_ids"]]
        self.assertTrue(any(q["language"] == "zh" and "良配" in q["query"] for q in queries))

    def test_unconfirmed_period_rejected_and_custom_period_preserved(self):
        plan = self.plan()
        self.assertEqual(plan["window_start"], self.manifest["window_start"])
        self.assertEqual(plan["window_end_exclusive"], self.manifest["window_end_exclusive"])
        self.manifest["window_confirmation"] = {"status": "pending"}
        with self.assertRaisesRegex(ValueError, "период"):
            self.plan()

    def test_tasks_cover_every_query_once_without_mixing_language_results(self):
        plan = self.plan()
        by_id = {q["query_id"]: q for q in plan["queries"]}
        self.assertEqual(len(by_id), len(plan["queries"]))
        self.assertEqual(len({task["task_id"] for task in plan["search_tasks"]}), len(plan["search_tasks"]))
        assigned = []
        for task in plan["search_tasks"]:
            for language, query_ids in task["language_variants"].items():
                for query_id in query_ids:
                    self.assertEqual(by_id[query_id]["query"], task["query"])
                    self.assertEqual(by_id[query_id]["language"], language)
                    assigned.append(query_id)
        self.assertCountEqual(assigned, by_id)
        self.assertEqual(len(assigned), len(set(assigned)))

    def test_tasks_do_not_reuse_identifiers_across_reporting_windows(self):
        original = self.plan()
        self.manifest["window_start"] = "2026-11-02T00:00:00+03:00"
        changed = self.plan()
        self.assertEqual([t["query"] for t in original["search_tasks"]],
                         [t["query"] for t in changed["search_tasks"]])
        self.assertTrue({t["task_id"] for t in original["search_tasks"]}.isdisjoint(
            t["task_id"] for t in changed["search_tasks"]))

    def test_official_media_and_investor_sources_precede_media_and_open_search(self):
        plan = self.plan()
        by_source = {source["id"]: source for source in self.sources["sources"]}
        for entries in (plan["source_indexes"], plan["queries"], plan["search_tasks"]):
            priorities = [entry["search_priority"] for entry in entries]
            self.assertEqual(priorities, sorted(priorities))
        self.assertEqual({q["search_priority"] for q in plan["queries"]}, {0, 1, 2, 3, 4})
        self.assertIn('gdi', plan['source_indexes'][0]['source_ids'])
        for query in plan["queries"]:
            if query["search_priority"] == 1:
                self.assertEqual(query["lane"], "official_source")
                self.assertIn(by_source[query["source_id"]]["kind"],
                              {"official_newsroom", "investor_relations"})
        tasks = plan["search_tasks"]
        by_query = {q["query_id"]: q for q in plan["queries"]}
        for task in tasks:
            related = [by_query[qid] for ids in task["language_variants"].values() for qid in ids]
            self.assertEqual(task["search_priority"], min(q["search_priority"] for q in related))

    def test_invalid_key_media_policy_is_rejected_before_run(self):
        root = Path(__file__).resolve().parents[1]
        configs = {name: read_json(root / "config" / f"{name}.json") for name in CONFIG_NAMES}
        for change in ("unknown", "disabled", "not_media", "no_context", "unknown_initial_source"):
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
                elif change == 'no_context':
                    del invalid['queries']['discovery']['context']['en']
                else:
                    policy['collection_first_source_ids'] = ['missing']
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
                    del rules['search_intents']['product_change']['terms']['ja']
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

    def test_snapshot_without_current_policy_requires_new_run(self):
        root = Path(__file__).resolve().parents[1]
        configs = {name: read_json(root / "config" / f"{name}.json") for name in CONFIG_NAMES}
        del configs["queries"]["research_policy"]
        with self.assertRaisesRegex(ValueError, "research_policy.*new run"):
            validate_configs(configs, root / "schemas")
        with self.assertRaisesRegex(ValueError, "research_policy.*new run"):
            build_plan(configs["companies"], configs["sources"], configs["queries"], self.manifest)

    def test_domain_exclusions_do_not_change_source_order(self):
        baseline = self.plan()
        rules = copy.deepcopy(self.rules)
        rules['retrieval_policy']['excluded_domains'] = []
        changed = build_plan(self.catalog, self.sources, rules, self.manifest)
        self.assertEqual(baseline['source_indexes'], changed['source_indexes'])
        self.assertEqual(baseline['execution_policy']['source_order'], changed['execution_policy']['source_order'])

    def test_initial_source_order_is_explicit_and_configurable(self):
        sources = copy.deepcopy(self.sources)
        sources['source_policy']['collection_first_source_ids'] = ['techcrunch', 'gdi']
        plan = build_plan(self.catalog, sources, self.rules, self.manifest)
        self.assertEqual([s['source_ids'][0] for s in plan['source_indexes'][:2]], ['techcrunch', 'gdi'])


if __name__ == "__main__":
    unittest.main()
