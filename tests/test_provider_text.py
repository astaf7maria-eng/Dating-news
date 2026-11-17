"""Original provider text and resuming extraction from an existing HTML file."""

from hashlib import sha256
from pathlib import Path
import tempfile
import unittest

from dating_digest.ingestion import extract_provider_article, save_html_article
from dating_digest.io import read_json


class ProviderTextTests(unittest.TestCase):
    def metadata(self):
        return dict(url='https://example.com/news', source_id='newsroom', title='Новая функция',
                    language='ru', raw_path='raw/provider.json', company_ids=[],
                    published_at=None, date_precision='unknown')

    def test_continuations_preserve_original_and_unknown_dates(self):
        article = extract_provider_article(['Source\nL10: Новый запуск.\nL11: Только Япония.',
            'Source continuation\nL11: Только Япония.\nL12@P1: Стоимость — 500 иен.'],
            self.metadata(), first_line=10, last_line=12, tags=['features', 'monetization'])
        self.assertEqual(article['text'], 'Новый запуск.\nТолько Япония.\nСтоимость — 500 иен.')
        self.assertEqual(article['text_sha256'], sha256(article['text'].encode('utf-8')).hexdigest())
        self.assertEqual(article['extraction_status'], 'full')
        self.assertIsNone(article['published_at'])
        self.assertEqual(article['raw_path'], 'raw/provider.json')
        self.assertEqual(article['tags'], ['features', 'monetization'])

    def test_missing_lines_and_rewritten_views_cannot_be_full(self):
        for pages, issue in ((['Source\nL10: Начало.\nL12: Окончание.'], 'provider_missing_body_lines'),
                             (['Rewritten summary\nL10: Summary.\nL11: Text.\nL12: End.'],
                              'provider_rewritten_view_requires_original')):
            with self.subTest(issue=issue):
                article = extract_provider_article(pages, self.metadata(), first_line=10, last_line=12)
                self.assertEqual(article['extraction_status'], 'partial')
                self.assertIn(issue, article['extraction_diagnostics']['quality_issues'])

    def test_conflicting_continuations_and_missing_provenance_fail(self):
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            extract_provider_article(['L1: Original.', 'L1: Changed.'], self.metadata(), first_line=1, last_line=1)
        with self.assertRaisesRegex(ValueError, 'raw_path'):
            extract_provider_article(['L1: Source.'], self.metadata() | {'raw_path': ''}, first_line=1, last_line=1)

    def test_saved_html_resumes_missing_article_without_changing_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            html = root / 'raw/page.html'
            html.parent.mkdir()
            html.write_text('<html lang="ru"><title>Новая функция</title><article><p>'
                            'Компания объявила новую функцию для взаимного выбора. '
                            'Участники могут начать разговор после подтверждения интереса.'
                            '</p></article></html>', encoding='utf-8')
            original = html.read_bytes()
            output = root / 'articles/one.json'
            article = save_html_article(html, self.metadata(), output, tags=['features'])
            self.assertEqual(read_json(output), article)
            self.assertEqual(html.read_bytes(), original)
            self.assertEqual(article['raw_path'], str(html))
            with self.assertRaises(FileExistsError):
                save_html_article(html, self.metadata(), output)
