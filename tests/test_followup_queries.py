"""Search regressions found in the stored November corpus; no live requests."""

import copy
import json
from pathlib import Path
import unittest

from dating_digest.__main__ import CONFIG_NAMES, validate_configs
from dating_digest.discovery.plan import build_plan
from dating_digest.discovery.queries import build_followup_query


class FollowupQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.configs = {name: json.loads((cls.root / 'config' / f'{name}.json').read_text(encoding='utf-8'))
                       for name in CONFIG_NAMES}
        cls.manifest = {
            'run_id': 'followup-test', 'window_start': '2026-11-01T00:00:00+03:00',
            'window_end_exclusive': '2026-11-15T00:00:00+03:00',
            'window_confirmation': {'status': 'confirmed'},
        }

    def plan(self, configs=None):
        configs = configs or self.configs
        return build_plan(configs['companies'], configs['sources'], configs['queries'], self.manifest)

    def test_tapple_and_cmb_use_neutral_news_indexes_with_scoped_fallbacks(self):
        plan = self.plan()
        for source_id, url, scope in (
            ('tapple_site', 'https://www.tapple.co.jp/news/', 'site:www.tapple.co.jp/news/'),
            ('cmb_site', 'https://www.coffeemeetsbagel.com/press', 'site:www.coffeemeetsbagel.com/press'),
        ):
            entry = next(index for index in plan['source_indexes'] if source_id in index['source_ids'])
            self.assertEqual(entry['url'], url)
            self.assertEqual(entry['index_kind'], 'publication_feed')
            self.assertEqual(entry['review']['status'], 'planned')
            self.assertIsNone(entry['review']['feed_available'])
            self.assertIsNone(entry['review']['checked_through'])
            self.assertIn('end_of_feed', entry['review'])
            fallback = next(query for query in plan['queries'] if query['source_id'] == source_id)
            self.assertIn(scope, fallback['query'])
            self.assertEqual(fallback['phase'], 'if_gap')
        self.assertEqual(plan['execution_policy']['source_review_log'], 'raw/source_reviews.jsonl')
        native_campaign = build_followup_query(plan, language='ja', intent='marketing', company_id='tapple')
        self.assertIn('タップル キャンペーン', native_campaign['query'])

    def test_external_company_lookup_applies_to_every_market_only_conditionally(self):
        plan = self.plan()
        queries = [query for query in plan['queries'] if query['lane'] == 'external_company']
        expected = {company['id'] for company in self.configs['companies']['companies'] if company['enabled']}
        self.assertEqual({query['company_ids'][0] for query in queries}, expected)
        self.assertEqual(len(queries), len(expected))
        for query in queries:
            self.assertEqual(query['phase'], 'if_gap')
            self.assertTrue(query['trigger'])
            self.assertEqual(set(query['intent_options']), {'interview', 'experiment_results', 'disclosure'})
        for cid, language, official_domain in (
            ('twinby', 'ru', 'twinby.ru'), ('hinge', 'en', 'hinge.co'),
            ('tapple', 'ja', 'tapple.co.jp'),
        ):
            query = next(query for query in queries if query['company_ids'] == [cid])
            self.assertEqual(query['language'], language)
            self.assertIn(official_domain, query['exclude_domains'])
            self.assertIn(f'-site:{official_domain}', query['query'])
            self.assertNotIn('-site:', query['query_without_domain_exclusions'])

    def test_negative_changes_and_next_synonyms_are_selected_from_the_observed_intent(self):
        plan = self.plan()
        changes = {
            'restrictions': 'restrictions', 'removal': 'discontinued',
            'redesign': 'redesign', 'pricing': 'pricing change', 'migration': 'platform migration',
        }
        for intent, term in changes.items():
            query = build_followup_query(plan, language='en', intent=intent, company_id='pure')
            self.assertIn(term, query['query'])
            self.assertNotIn('introduces', query['query'])
            self.assertEqual(query['window_start'], self.manifest['window_start'])
        first = build_followup_query(plan, language='en', intent='removal', company_id='pure')
        next_term = build_followup_query(plan, language='en', intent='removal', company_id='pure', term_index=1)
        self.assertNotEqual(first['query'], next_term['query'])
        self.assertIn('removes', next_term['query'])
        for language in ('ru', 'en', 'zh', 'ja'):
            result = build_followup_query(plan, language=language, intent='experiment_results')
            self.assertTrue(result['query'])
            self.assertEqual(result['language'], language)

    def test_new_company_can_be_researched_before_catalog_promotion(self):
        plan = self.plan()
        before = copy.deepcopy(plan['config_snapshot']['companies'])
        query = build_followup_query(plan, language='en', intent='interview', company_name='GENZA')
        self.assertIn('GENZA dating app interview', query['query'])
        self.assertEqual(query['company_ids'], [])
        self.assertEqual(query['company_name'], 'GENZA')
        self.assertEqual(plan['config_snapshot']['companies'], before)

    def test_four_open_external_queries_do_not_create_topic_quotas(self):
        plan = self.plan()
        first = [query for query in plan['queries'] if query['lane'] == 'external_discovery']
        self.assertEqual({(query['language'], query['query_intent']) for query in first},
                         {('ru', 'interview'), ('en', 'interview'),
                          ('ru', 'experiment_results'), ('en', 'experiment_results')})
        self.assertTrue(all(query['phase'] == 'first_pass' and not query['company_ids'] for query in first))
        self.assertFalse(plan['coverage_targets']['models_required_each_run'])

    def test_existing_v1_snapshot_keeps_working_without_external_policy(self):
        configs = copy.deepcopy(self.configs)
        del configs['queries']['external_search']
        del configs['queries']['search_intents']
        plan = self.plan(configs)
        validate_configs(configs, self.root / 'schemas')
        self.assertEqual(plan['schema_version'], '1.0')
        self.assertFalse(any(query['lane'].startswith('external_') for query in plan['queries']))
        self.assertEqual({entry['company_id'] for entry in plan['coverage']},
                         {company['id'] for company in configs['companies']['companies'] if company['enabled']})

    def test_bad_intents_languages_and_term_indexes_fail_before_search(self):
        plan = self.plan()
        for kwargs in (
            {'intent': 'unknown', 'language': 'en'},
            {'intent': 'interview', 'language': 'missing'},
            {'intent': 'interview', 'language': 'en', 'term_index': -1},
            {'intent': 'interview', 'language': 'en', 'term_index': 100},
            {'intent': 'interview', 'language': 'en', 'company_id': 'unknown'},
            {'intent': 'interview', 'language': 'en', 'company_name': ''},
            {'intent': 'interview', 'language': 'en', 'company_id': 'tinder', 'company_name': 'Other'},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                build_followup_query(plan, **kwargs)
        for change in ('missing_terms', 'official_flag', 'unknown_intent'):
            configs = copy.deepcopy(self.configs)
            if change == 'missing_terms':
                del configs['queries']['search_intents']['interview']['terms']['ja']
            elif change == 'official_flag':
                configs['queries']['search_intents']['interview']['outside_official'] = False
            else:
                configs['queries']['external_search']['intents'].append('unknown')
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_configs(configs, self.root / 'schemas')
