"""Scheduling boundaries and retrieval identity; no network in local tests."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from dating_digest.discovery.plan import build_plan
from dating_digest.discovery.session import Budget, WorkLedger, cache_is_fresh, checkpoint, request_key


class FastSearchTests(unittest.TestCase):
    def test_fast_pass_keeps_targets_and_avoids_article_seeds(self):
        root = Path(__file__).resolve().parents[1]
        c, s, q = [json.loads((root/'config'/f'{name}.json').read_text(encoding='utf-8'))
                   for name in ('companies', 'sources', 'queries')]
        manifest = dict(run_id='fast-test', window_start='2026-11-01T00:00:00+03:00',
                        window_end_exclusive='2026-11-15T00:00:00+03:00',
                        window_confirmation={'status': 'confirmed'})
        plan = build_plan(c, s, q, manifest)
        self.assertEqual(plan, build_plan(c, s, q, manifest))
        self.assertLessEqual(plan['initial_query_count'], 40)
        self.assertEqual({x['company_id'] for x in plan['coverage']},
                         {x['id'] for x in c['companies'] if x['enabled']})
        self.assertEqual(set(plan['coverage_targets']['topics']), set(q['category_signals']))
        self.assertEqual(set(plan['coverage_targets']['models']),
                         {x['id'] for x in q['discovery']['business_model_patterns']})
        self.assertEqual(set(plan['coverage_targets']['native_languages']), {'zh', 'ja'})
        for company in plan['coverage']:
            actual = {x['language'] for x in plan['queries'] if x['query_id'] in company['query_ids']
                      and x['family'] == 'baseline'}
            self.assertEqual(actual, set(company['languages']))
        self.assertTrue(all(' OR ' not in x['query'] for x in plan['queries']))
        self.assertTrue(all(len(x['query']) < 120 for x in plan['queries']))
        self.assertFalse(any('november-5-2026' in x['url'] or 'everyday-life' in x['url']
                             for x in plan['source_indexes']))
        vk_en = next(x for x in plan['queries'] if x['company_ids'] == ['vk_dating']
                     and x['family'] == 'baseline' and x['language'] == 'en')
        self.assertIn('VK Dating', vk_en['query'])
        world = [x for x in plan['queries'] if x['source_id'] == 'world_blog']
        self.assertTrue(all('dating' in x['query'] for x in world))
        assigned = [qid for t in plan['search_tasks'] for ids in t['language_variants'].values() for qid in ids]
        self.assertCountEqual(assigned, [x['query_id'] for x in plan['queries']])
        self.assertTrue(all(x['status'] == 'planned' for x in plan['coverage']))
        blog = next(source for source in s['sources'] if source['id'] == 'bumble_buzz')
        s['sources'].append(blog | {'id': 'shared_newsroom', 'kind': 'official_newsroom'})
        merged = build_plan(c, s, q, manifest)
        index = next(item for item in merged['source_indexes'] if 'bumble_buzz' in item['source_ids'])
        self.assertIn('shared_newsroom', index['source_ids'])
        self.assertEqual(index['search_priority'], 1)

    def test_actual_parameters_change_cache_identity_but_labels_are_not_parameters(self):
        args = dict(provider='p', query='Tinder news', window_start='2026-11-01',
                    window_end_exclusive='2026-11-15')
        base = request_key(**args)
        for update in ({'provider':'q'}, {'query':'Тиндер новости'}, {'filters':{'language':'ru'}},
                       {'filters':{'region':'RU'}}, {'page':2}, {'window_start':'2026-11-02'},
                       {'parameters':{'recency':7}}):
            self.assertNotEqual(base, request_key(**(args | update)))
        ledger = WorkLedger()
        self.assertTrue(ledger.reserve_request(base, ['ru']))
        self.assertFalse(ledger.reserve_request(base, ['en']))
        self.assertEqual(ledger.requests[base]['query_ids'], ['en','ru'])
        self.assertEqual(ledger.requests[base]['status'], 'inflight')

    def test_concurrent_url_reservation_aliases_failure_and_significant_parameters(self):
        ledger = WorkLedger()
        with ThreadPoolExecutor(max_workers=4) as pool:
            added = list(pool.map(lambda n: ledger.enqueue('https://a.test/x?utm_source=a#b', {'n':n}), range(20)))
        self.assertEqual(sum(added), 1)
        self.assertEqual(ledger.prevented_urls, 19)
        self.assertTrue(ledger.claim('https://a.test/x'))
        self.assertFalse(ledger.claim('https://a.test/x'))
        self.assertTrue(ledger.enqueue('https://a.test/canonical', {}))
        ledger.finish_url('https://a.test/x', aliases=['https://a.test/canonical'])
        self.assertFalse(ledger.claim('https://a.test/canonical'))
        self.assertTrue(ledger.enqueue('https://a.test/x?id=2', {}))
        ledger.finish_url('https://a.test/x?id=2', status='failed')
        self.assertFalse(ledger.enqueue('https://a.test/x?id=2', {}))

    def test_deadline_reserves_save_time_and_cache_requires_fresh_aware_timestamp(self):
        tick = [10.0]
        budget = Budget(clock=lambda: tick[0])
        tick[0] += 815
        self.assertTrue(budget.can_start(25))
        tick[0] += 1
        self.assertFalse(budget.can_start(25))
        self.assertEqual(budget.remaining(network=False), 84)
        now = datetime(2026,11,15,tzinfo=timezone.utc)
        self.assertTrue(cache_is_fresh('2026-11-14T12:00:00+00:00', now=now))
        for value in ('2026-11-12T00:00:00+00:00', '2026-11-16T00:00:00+00:00', '2026-11-15T00:00:00'):
            self.assertFalse(cache_is_fresh(value, now=now))
        ledger = WorkLedger()
        ledger.remember_analysis('same text', {'topic':'business'})
        self.assertEqual(ledger.cached_analysis('same text'), (True, {'topic':'business'}))
        self.assertEqual(ledger.cached_analysis('updated fact'), (False, None))

    def test_transitive_aliases_keep_all_observations_and_success_after_late_failure(self):
        ledger = WorkLedger()
        urls = [f'https://a.test/{name}' for name in ('first', 'second', 'canonical', 'redirect')]
        for url in urls:
            ledger.enqueue(url, {'url': url})
        ledger.finish_url(urls[0], aliases=[urls[2]], text_path='first.txt')
        ledger.finish_url(urls[1], aliases=[urls[2], urls[3]], text_path='second.txt')
        ledger.finish_url(urls[2], status='failed', error='late timeout')
        ledger.enqueue(urls[0], {'url': 'later observation'})
        for url in urls:
            self.assertFalse(ledger.claim(url))
            self.assertEqual(ledger.urls[url]['status'], 'fetched')
            self.assertEqual(ledger.urls[url]['text_path'], 'second.txt')
            self.assertNotIn('error', ledger.urls[url])
            self.assertCountEqual([o['url'] for o in ledger.urls[url]['observations']],
                                  urls + ['later observation'])

    def test_failed_alias_preserves_existing_canonical_success(self):
        ledger = WorkLedger()
        canonical, alias = 'https://a.test/story', 'https://a.test/redirect'
        for url in (canonical, alias):
            ledger.enqueue(url, {'url': url})
            ledger.claim(url)
        ledger.finish_url(canonical, text_path='saved.txt')
        ledger.finish_url(alias, status='failed', aliases=[canonical], error='timeout')
        for url in (canonical, alias):
            self.assertEqual(ledger.urls[url]['status'], 'fetched')
            self.assertEqual(ledger.urls[url]['text_path'], 'saved.txt')
            self.assertNotIn('error', ledger.urls[url])

    def test_invalid_alias_leaves_url_work_inflight_for_retry(self):
        ledger = WorkLedger()
        url = 'https://a.test/story'
        ledger.enqueue(url, {})
        ledger.claim(url)
        with self.assertRaises(ValueError):
            ledger.finish_url(url, aliases=['https://a.test/alias', 'not-a-url'])
        self.assertEqual(ledger.urls[url]['status'], 'inflight')
        self.assertEqual(set(ledger.urls), {url})

    def test_checkpoint_serialization_failure_leaves_existing_journal_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'journal.jsonl'
            checkpoint(path, [{'saved': 'данные'}])
            before = path.read_bytes()
            with self.assertRaises(TypeError):
                checkpoint(path, [{'partial': True}, {'invalid': object()}])
            self.assertEqual(path.read_bytes(), before)

    def test_concurrent_checkpoints_preserve_complete_batches_and_unicode(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'journal.jsonl'

            def save_batch(number):
                checkpoint(path, [{'batch': number, 'item': item, 'text': 'новости' * 3000}
                                  for item in range(2)])

            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(save_batch, range(16)))
            rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            self.assertCountEqual([(row['batch'], row['item']) for row in rows],
                                  [(batch, item) for batch in range(16) for item in range(2)])
            for start in range(0, len(rows), 2):
                self.assertEqual(rows[start]['batch'], rows[start + 1]['batch'])
            self.assertTrue(all(row['text'] == 'новости' * 3000 for row in rows))
