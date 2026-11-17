"""Editorial regressions at the research, evidence and rendered-review boundaries."""

import copy
from datetime import datetime, timezone
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_research_qa
from dating_digest.domain.context import build_context_bundle, json_sha256
from dating_digest.discovery.plan import build_plan
from dating_digest import editorial
from dating_digest.editorial import check_editorial, digest_title, prepare_editorial, publication_timestamp, render_digest
from dating_digest.io import read_json, write_json
from dating_digest.storage import archive_run


class EditorialTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_research_qa.ResearchQATests('test_local_success_is_not_semantic_approval_and_does_not_modify_corpus')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.run = self.fixture.run
        self.project = test_research_qa.ROOT
        bundle = build_context_bundle(read_json(self.run / 'config_snapshot/business_context.json'))
        write_json(self.run / 'context_bundle.json', bundle)
        manifest = read_json(self.run / 'manifest.json')
        manifest['business_context'] = dict(sha256=bundle['context_sha256'], version=bundle['context_version'])
        write_json(self.run / 'manifest.json', manifest, replace=True)
        configs = {name: read_json(self.run / 'config_snapshot' / f'{name}.json')
                   for name in ('companies', 'sources', 'queries')}
        write_json(self.run / 'search_plan.json', build_plan(configs['companies'], configs['sources'],
                   configs['queries'], manifest, bundle), replace=True)
        self.fixture.review()

    def prepare(self, *, subset=False):
        if subset:
            review = read_json(self.run / 'research_review.json')
            review['verdict'] = 'needs_revision'
            review['coverage'][0]['status'] = 'not_checked'
            write_json(self.run / 'research_review.json', review, replace=True)
        prepare_editorial(self.run, self.project, verified_subset_reason='User requested an eligible-only draft' if subset else None)
        self.inputs = read_json(self.run / 'editorial/inputs.json')
        self.base = dict(run_id=self.inputs['run_id'], input_sha256=self.inputs['input_sha256'])

    def artifacts(self, *, subset=False):
        self.prepare(subset=subset)
        event = dict(self.base, schema_version='0.4', event_id='feature', company_ids=[],
                     headline='Компания раскрыла механику', section_id='features',
                     event_kind='product_change', launch_stage='launched', markets=['Япония'],
                     article_ids=['one'], primary_article_id='one', mechanism='Двусторонний выбор',
                     possible_consequences=[], facts=[dict(claim='Факт из исходника', article_id='one',
                                                          evidence_quote=self.fixture.article['text'])],
                     eligibility=dict(status='included', reasons=['Подтверждено текстом']),
                     ranking=None, context_assessment=None)
        write_json(self.run / 'events/feature.json', event)
        write_json(self.run / 'editorial/analysis.json', dict(self.base, event_ids=['feature'], article_decisions=[
            dict(article_id='one', event_ids=['feature'], reason='Разобрана полностью')]))
        weights = read_json(self.run / 'config_snapshot/pipeline.json')['ranking']['weights']
        write_json(self.run / 'ranking.json', dict(self.base, items=[dict(event_id='feature',
            criteria=[dict(name=name, score=5, rationale='Подтверждено источником') for name in weights],
            total_score=100, decision='include', reason='Конкретная новая механика',
            strategic_assessment=dict(has_strategic_value=True,
                competitor_logic='Компания снижает барьер начала общения.',
                strategic_signal='Ценность сервиса зависит от взаимного участия.',
                lovito_implication='Для серьёзных знакомств Lovito содержательный разговор становится частью ценности.',
                evidence_limits='Механика раскрыта, данные о результатах отсутствуют.',
                context_refs=dict(relevance_type='decision_support', question_ids=['match_quality'],
                                  claim_ids=['conversation_quality'])))]))
        summary = dict(event_id='feature', company='Example', original_title=self.fixture.article['title'],
                       original_url=self.fixture.article['url'], published_at=publication_timestamp(self.fixture.article),
                       summary='Компания раскрыла новую механику. Пользователи выбирают друг друга.')
        write_json(self.run / 'editorial/summaries.json', dict(self.base, items=[summary]))
        sections = read_json(self.run / 'config_snapshot/editorial_policy.json')['sections']
        self.digest = dict(self.base, schema_version='0.5', digest_id='fixture-digest', revision=1,
            context_version=self.inputs['context_version'], context_sha256=self.inputs['context_sha256'],
            window_start=self.inputs['window_start'], window_end_exclusive=self.inputs['window_end_exclusive'],
            timezone='Europe/Moscow', title=digest_title(self.inputs), status='draft', shortfall_reason='Одна значимая новость',
            coverage_notes=self.inputs['coverage_notes'],
            sections=[dict(section_id=s['id'], status='has_news' if s['id'] == 'features'
                           else 'coverage_incomplete' if subset else 'no_significant_news', note=None) for s in sections],
            items=[dict(summary, rank=1, display_order=1, section_id='features', headline=event['headline'],
                sources=[summary['original_url']], editorial_override_reason=None,
                so_what=dict(text='Компания снижает барьер начала общения, связывая ценность с взаимным участием. Для Lovito это принцип создания ценности через содержательный разговор.',
                    competitor_logic='Компания снижает барьер начала общения.', market_signal='Двусторонний выбор',
                    context_link='Качество общения', hypothesis_or_action=None, context_refs=dict(relevance_type='decision_support',
                        question_ids=['match_quality'], claim_ids=['conversation_quality'])))])
        write_json(self.run / 'digest.json', self.digest)

    def approve(self):
        result = check_editorial(self.run, 'digest', self.project)
        write_json(self.run / 'review.json', dict(schema_version='0.1', digest_id=self.digest['digest_id'], revision=1,
            result='passed', checked_at=datetime.now(timezone.utc).isoformat(), issues=[],
            digest_sha256=result['digest_sha256'], editorial_sha256=result['editorial_sha256']), replace=True)

    def test_full_gate_remains_strict_and_explicit_subset_keeps_coverage(self):
        self.artifacts(subset=True)
        with self.assertRaisesRegex(ValueError, 'not passed'):
            prepare_editorial(self.run, self.project)
        self.assertEqual(self.inputs['mode'], 'verified_subset')
        self.assertTrue(self.inputs['coverage_notes'])
        self.assertEqual(len(read_json(self.run / 'editorial/news.json')['articles']), 1)
        self.approve()
        render_digest(self.run, self.project)
        self.assertIn(self.inputs['coverage_notes'][0],
                      (self.run / 'rendered/mattermost.md').read_text(encoding='utf-8'))

    def test_date_only_article_survives_the_normal_editorial_route(self):
        self.fixture.article.update(published_at=None, date_precision='day', published_date_raw='2026-11-15')
        self.fixture.save_article()
        archive_run(self.run)
        self.fixture.review()
        self.artifacts()
        self.approve()
        render_digest(self.run, self.project)
        self.assertEqual(self.digest['items'][0]['published_at'], '2026-11-15T00:00:00+03:00')

    def test_partial_day_title_preserves_exact_moscow_window(self):
        title = digest_title(dict(window_start='2026-11-01T08:30:00Z', window_end_exclusive='2026-11-08T08:30:00Z'))
        self.assertIn('01.11.2026 11:30:00', title)
        self.assertIn('08.11.2026 11:30:00 МСК', title)

    def test_render_preserves_original_unicode_title_url_and_is_repeatable(self):
        self.artifacts()
        self.approve()
        first = render_digest(self.run, self.project)
        second = render_digest(self.run, self.project)
        self.assertEqual(first, second)
        markdown = (self.run / 'rendered/mattermost.md').read_text(encoding='utf-8')
        self.assertIn(self.fixture.article['title'], markdown)
        self.assertIn(self.fixture.article['url'], markdown)
        self.assertIn('**Что произошло:**', markdown)
        self.assertNotIn('total_score', markdown)

    def test_changed_corpus_or_fabricated_quote_cannot_reach_later_steps(self):
        self.artifacts()
        event_path = self.run / 'events/feature.json'
        event = read_json(event_path)
        event['facts'][0]['evidence_quote'] = 'Invented quotation'
        write_json(event_path, event, replace=True)
        with self.assertRaisesRegex(ValueError, 'quote'):
            check_editorial(self.run, 'summaries', self.project)
        self.fixture.article['text'] += ' Changed.'
        self.fixture.save_article()
        with self.assertRaisesRegex(ValueError, 'stale|inputs changed'):
            check_editorial(self.run, 'analysis', self.project)

    def test_digest_edit_and_upstream_edit_invalidate_review(self):
        self.artifacts()
        self.approve()
        self.digest['items'][0]['so_what']['text'] = 'Проверить другую гипотезу.'
        write_json(self.run / 'digest.json', self.digest, replace=True)
        with self.assertRaisesRegex(ValueError, 'another editorial revision'):
            render_digest(self.run, self.project)
        self.approve()
        event = read_json(self.run / 'events/feature.json')
        event['mechanism'] += ' Новое описание.'
        write_json(self.run / 'events/feature.json', event, replace=True)
        with self.assertRaisesRegex(ValueError, 'another editorial revision'):
            render_digest(self.run, self.project)

    def test_source_mutation_and_wrong_score_are_rejected(self):
        self.artifacts()
        original = copy.deepcopy(self.digest)
        self.digest['items'][0]['original_title'] = 'Переведённый заголовок'
        write_json(self.run / 'digest.json', self.digest, replace=True)
        with self.assertRaisesRegex(ValueError, 'approved summary'):
            check_editorial(self.run, 'digest', self.project)
        write_json(self.run / 'digest.json', original, replace=True)
        ranking = read_json(self.run / 'ranking.json')
        ranking['items'][0]['total_score'] = 90
        write_json(self.run / 'ranking.json', ranking, replace=True)
        with self.assertRaisesRegex(ValueError, 'weighted'):
            check_editorial(self.run, 'selection', self.project)

    def test_selected_event_requires_substantive_strategic_reasoning(self):
        self.artifacts()
        ranking = read_json(self.run / 'ranking.json')
        ranking['items'][0]['strategic_assessment']['has_strategic_value'] = False
        write_json(self.run / 'ranking.json', ranking, replace=True)
        with self.assertRaisesRegex(ValueError, 'no substantive strategic implication'):
            check_editorial(self.run, 'selection', self.project)
        ranking['items'][0]['strategic_assessment']['has_strategic_value'] = True
        ranking['items'][0]['strategic_assessment']['lovito_implication'] = ' '
        write_json(self.run / 'ranking.json', ranking, replace=True)
        with self.assertRaisesRegex(ValueError, 'lovito_implication'):
            check_editorial(self.run, 'selection', self.project)

    def test_ready_corpus_and_fact_analysis_do_not_require_lovito_conclusions(self):
        self.artifacts()
        ranking = read_json(self.run / 'ranking.json')
        del ranking['items'][0]['strategic_assessment']
        write_json(self.run / 'ranking.json', ranking, replace=True)
        # The corpus is complete and factual analysis precedes digest selection.
        result = check_editorial(self.run, 'analysis', self.project)
        self.assertEqual(result['event_count'], 1)
        self.assertEqual(len(read_json(self.run / 'editorial/news.json')['articles']), 1)
        with self.assertRaisesRegex(ValueError, 'strategic value'):
            check_editorial(self.run, 'selection', self.project)

    def test_strategic_so_what_accepts_no_experiment_and_policy_is_revision_bound(self):
        self.artifacts()
        self.assertIsNone(self.digest['items'][0]['so_what']['hypothesis_or_action'])
        self.approve()
        render_digest(self.run, self.project)
        policy = read_json(self.run / 'editorial/policy.json')
        policy['so_what'] = 'Changed policy'
        write_json(self.run / 'editorial/policy.json', policy, replace=True)
        with self.assertRaisesRegex(ValueError, 'policy snapshot changed'):
            check_editorial(self.run, 'digest', self.project)

    def test_subset_still_requires_completed_evidence_and_nonblank_reason(self):
        with self.assertRaisesRegex(ValueError, 'requires a reason'):
            prepare_editorial(self.run, self.project, verified_subset_reason=' ')
        self.fixture.observation['status'] = 'planned'
        (self.run / 'raw/source_reviews.jsonl').write_text(
            json.dumps(self.fixture.observation) + '\n', encoding='utf-8')
        self.fixture.review()
        with self.assertRaisesRegex(ValueError, 'completed source/search observation'):
            self.prepare(subset=True)

    def test_summary_sentence_count_handles_quotes_and_abbreviations(self):
        self.artifacts()
        path = self.run / 'editorial/summaries.json'
        summaries = read_json(path)
        summaries['items'][0]['summary'] = 'Компания пояснила «Это новая механика». Запуск прошёл в 2026 г., т. е. недавно.'
        write_json(path, summaries, replace=True)
        check_editorial(self.run, 'summaries', self.project)
        summaries['items'][0]['summary'] = None
        write_json(path, summaries, replace=True)
        with self.assertRaisesRegex(ValueError, 'Summary needs'):
            check_editorial(self.run, 'summaries', self.project)

    def test_concurrent_artifact_edit_cannot_receive_an_approved_fingerprint(self):
        self.artifacts()
        validate = editorial._validate

        def mutate_after_validation(value, schema, project):
            validate(value, schema, project)
            if schema == 'event':
                event = dict(value, mechanism='Concurrent edit')
                write_json(self.run / 'events/feature.json', event, replace=True)

        with patch.object(editorial, '_validate', side_effect=mutate_after_validation):
            with self.assertRaisesRegex(ValueError, 'changed during validation'):
                check_editorial(self.run, 'analysis', self.project)

    def test_editorial_commands_work_through_the_cli(self):
        self.artifacts()
        self.approve()
        for command, arguments in (('prepare-editorial', []),
                                   ('check-editorial', ['--step', 'digest']), ('render-digest', [])):
            result = subprocess.run([sys.executable, '-m', 'dating_digest', command,
                                     '--run-dir', str(self.run), *arguments],
                                    cwd=self.project, capture_output=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['run_id'], self.inputs['run_id'])

    def test_rehashed_news_cannot_replace_qa_approved_article(self):
        self.artifacts()
        path = self.run / 'editorial/news.json'
        news = read_json(path)
        news['articles'][0]['text'] += ' Unsupported new fact.'
        write_json(path, news, replace=True)
        self.inputs['news_sha256'] = editorial._hash(path)
        del self.inputs['input_sha256']
        self.inputs['input_sha256'] = json_sha256(self.inputs)
        write_json(self.run / 'editorial/inputs.json', self.inputs, replace=True)
        for name in ('editorial/analysis.json', 'events/feature.json'):
            artifact = read_json(self.run / name)
            artifact['input_sha256'] = self.inputs['input_sha256']
            write_json(self.run / name, artifact, replace=True)
        with self.assertRaisesRegex(ValueError, 'QA-approved Articles'):
            check_editorial(self.run, 'analysis', self.project)

    def test_new_search_observation_requires_research_qa_before_rendering(self):
        self.artifacts()
        self.approve()
        (self.run / 'raw/qa_search_log.jsonl').write_text(
            json.dumps(dict(query='A new research query', status='completed', results=[])) + '\n',
            encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'stale|inputs changed'):
            render_digest(self.run, self.project)
