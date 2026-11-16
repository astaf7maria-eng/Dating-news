"""Regression checks for title selection, substantive text and staged archiving."""

from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from dating_digest.ingestion import extract_article, extract_page
from dating_digest.io import read_json, write_json
from dating_digest.storage import ArticleArchive

URL = "https://example.com/news/new-feature"
FIRST = "The service launched verified profiles in November. Members can now choose when to complete the identity check."
LAST = "The experiment also removed a paid restriction. Existing subscribers keep access, and the rollout starts next month."


def page(body, head="", lang="en"):
    return f'<html lang="{lang}"><head><title>A new identity check</title>{head}</head><body>{body}</body></html>'


class PageTests(unittest.TestCase):
    def test_branding_h1_does_not_override_publication_metadata(self):
        result = extract_page(page('<aside><h1 class="site-name">Swipe &amp; Soul</h1></aside>'
                                   f'<article><p>{FIRST}</p></article>',
                                   '<meta property="og:title" content="Hinge vs Bumble vs Tinder">'
                                   '<meta property="og:site_name" content="Swipe &amp; Soul">'), URL)
        self.assertEqual(result["title"], "Hinge vs Bumble vs Tinder")
        self.assertEqual(result["extraction_diagnostics"]["title_source"], "og:title")

    def test_jsonld_matching_article_ignores_other_stories(self):
        data = [{"@type": "NewsArticle", "url": "https://example.com/other", "headline": "Wrong story"},
                {"@type": "NewsArticle", "url": URL, "headline": "Verified profiles launch"}]
        result = extract_page(page(f'<article><p>{FIRST}</p></article>',
                                   '<script type="application/ld+json">' + json.dumps(data) + '</script>'), URL)
        self.assertEqual(result["title"], "Verified profiles launch")

    def test_remove_widgets_but_keep_interview_quotes_and_conditions(self):
        result = extract_page(page(f'<article><div class="article-body"><p>{FIRST}</p>'
                                   f'<blockquote><p>{LAST}</p></blockquote>'
                                   '<div class="ad-banner"><p>Buy a house today.</p></div>'
                                   '<div class="comments"><p>User comment.</p></div></div>'
                                   '<section><h2>Latest in dating</h2><p>Other launch.</p></section>'
                                   '<section><h2>In this story</h2><a href="/tags">Brands</a></section></article>'), URL)
        self.assertEqual(result["text"], FIRST + "\n\n" + LAST)
        self.assertEqual(result["extraction_status"], "full")

    def test_desktop_mobile_duplicates_removed_globally_without_losing_unique_details(self):
        result = extract_page(page(f'<div class="ssr-variant hidden-mobile"><p>{FIRST}</p><p>{LAST}</p></div>'
                                   f'<div class="ssr-variant hidden-desktop"><p>{FIRST}</p><p>{LAST}</p>'
                                   '<p>Access is limited to Japan for this first test.</p></div>'), URL)
        self.assertEqual(result["text"].count(FIRST), 1)
        self.assertEqual(result["text"].count(LAST), 1)
        self.assertIn("limited to Japan", result["text"])

    def test_partial_is_content_based_even_inside_preferred_selector(self):
        result = extract_page(page('<div class="article-body"><p>Only the introduction…</p></div>'), URL)
        self.assertEqual(result["extraction_status"], "partial")
        self.assertIn("truncated_ending", result["extraction_diagnostics"]["quality_issues"])

    def test_sidebar_layout_name_does_not_delete_article_content(self):
        result = extract_page(page('<article><section class="block-text-sidebar">'
                                   '<div class="block-text-sidebar__grid--has-sidebar">'
                                   f'<div class="block-text-sidebar__content-column"><p>{FIRST}</p></div>'
                                   '<div class="block-text-sidebar__sidebar-column"><p>Related material.</p></div>'
                                   '</div></section></article>'), URL)
        self.assertEqual(result["text"], FIRST)

    def test_body_navigation_cannot_outweigh_actual_article(self):
        result = extract_page(page('<div>' + '<p>Many unrelated page items and links.</p>' * 40 + '</div>'
                                   f'<main><p>{FIRST}</p><p>{LAST}</p></main>'), URL)
        self.assertEqual(result["text"], FIRST + "\n\n" + LAST)

    def test_complete_short_article_without_specific_css_can_be_full(self):
        result = extract_page(page(f'<main><p>{FIRST}</p></main>'), URL)
        self.assertEqual(result["text"], FIRST)
        self.assertEqual(result["extraction_status"], "full")

    def test_campaign_conditions_lists_and_tables_survive_cleaning(self):
        result = extract_page(page(f'<article class="sponsored"><p>{FIRST}</p>'
                                   '<h2>Campaign conditions</h2><ul><li>New members only.</li>'
                                   '<li>One refund per account, before November 30.</li></ul>'
                                   '<table><tr><th>Refund</th><td>2450 yen</td></tr></table>'
                                   f'<p>{LAST}</p></article>'), URL)
        for detail in ("New members only.", "One refund per account, before November 30.", "Refund 2450 yen", LAST):
            self.assertIn(detail, result["text"])

    def test_unmarked_site_h1_does_not_override_html_publication_title(self):
        result = extract_page(page(f'<header><h1>Example Media</h1></header><p>{FIRST}</p>'), URL)
        self.assertEqual(result["title"], "A new identity check")

    def test_small_container_does_not_hide_later_product_events(self):
        result = extract_page(page(f'<article><div class="entry-content"><p>{FIRST}</p></div>'
                                   f'<div><p>{LAST * 8}</p></div></article>'), URL)
        self.assertIn(FIRST, result["text"])
        self.assertIn(LAST, result["text"])

    def test_omitted_substantive_tail_requires_review(self):
        main = " ".join(FIRST + f" Experiment {i}." for i in range(12))
        result = extract_page(page(f'<article><div class="entry-content"><p>{main}</p></div>'
                                   f'<p>{LAST}</p></article>'), URL)
        self.assertTrue(LAST in result["text"] or result["extraction_status"] == "partial")
        if LAST not in result["text"]:
            self.assertIn("additional_substantive_text_outside_container",
                          result["extraction_diagnostics"]["quality_issues"])

    def test_compact_variant_cannot_hide_a_short_unique_condition(self):
        main = " ".join(FIRST + f" Experiment {i}." for i in range(20))
        result = extract_page(page(f'<div><p>{main}</p></div>'
                                   f'<div><p>{main}</p><p>Japan only.</p></div>'), URL)
        self.assertIn("Japan only.", result["text"])
        self.assertEqual(result["text"].count(main), 1)

    def test_paywall_and_encoding_defects_remain_partial(self):
        gated = extract_page(page(f'<article><p>{FIRST}</p><div class="paywall">Subscribe to continue.</div></article>'), URL)
        self.assertEqual(gated["extraction_status"], "partial")
        broken = extract_page(page(f'<article><p>{FIRST} â\x80 â\x80 â\x80</p></article>'), URL)
        self.assertIn("encoding_artifacts", broken["extraction_diagnostics"]["quality_issues"])

    def test_original_language_tags_and_verified_metadata_are_preserved(self):
        text = "Сервис запустил новую проверку личности. Участники смогут выбрать момент проверки и сохранить доступ к действующим диалогам."
        metadata = dict(url=URL, source_id="press", published_at="2026-11-02T00:00:00+03:00",
                        date_precision="day", published_date_raw="2026-11-02", company_ids=["twinby"])
        result = extract_article(page(f'<article><p>{text}</p></article>', lang="ru"), metadata,
                                 tags=["features", "trust_safety", "features"])
        self.assertEqual(result["text"], text)
        self.assertEqual(result["language"], "ru")
        self.assertEqual(result["tags"], ["features", "trust_safety"])
        self.assertEqual(result["published_at"], metadata["published_at"])
        self.assertEqual(result["text_sha256"], sha256(text.encode("utf-8")).hexdigest())
        self.assertNotIn("page_kind", result)

    def test_unknown_dates_and_invalid_tags(self):
        metadata = dict(url=URL, source_id="press")
        result = extract_article(page(f'<article><p>{FIRST}</p></article>'), metadata)
        self.assertIsNone(result["published_at"])
        self.assertEqual(result["date_precision"], "unknown")
        with self.assertRaises(ValueError):
            extract_article(page(FIRST), metadata, tags=["interview"])

    def test_japanese_without_html_language_is_not_labelled_chinese(self):
        text = "マッチングアプリの新しいキャンペーンを開始しました。"
        result = extract_page(f'<title>新機能</title><main><p>{text * 4}</p></main>', URL)
        self.assertEqual(result["language"], "ja")
        self.assertIn(text, result["text"])

    def test_original_article_and_russian_event_roundtrip_in_utf8(self):
        title = "テストアプリの新しいキャンペーン"
        quote = "新規会員は条件を満たすと2450円のキャッシュバックを受けられます。"
        html = f'<html lang="ja"><title>{title}</title><article><h1>{title}</h1><p>{quote}</p></article></html>'
        article = extract_article(html, dict(url=URL, source_id="test_newsroom", company_ids=["test_app"]),
                                  tags=["marketing"])
        event = {"schema_version": "0.4", "event_id": "campaign", "company_ids": ["test_app"],
                 "headline": "Тестовое приложение запустило кампанию с возвратом денег",
                 "section_id": "marketing", "event_kind": "marketing_event", "launch_stage": "launched",
                 "markets": ["JP"], "article_ids": [article["article_id"]],
                 "facts": [{"claim": "Новые участники получают 2450 иен при выполнении условий.",
                            "article_id": article["article_id"], "evidence_quote": quote}],
                 "eligibility": {"status": "pending", "reasons": []}, "ranking": None, "context_assessment": None}
        with tempfile.TemporaryDirectory() as tmp:
            original_path, event_path = Path(tmp) / "article.json", Path(tmp) / "event.json"
            write_json(original_path, article)
            original_bytes = original_path.read_bytes()
            write_json(event_path, event)
            restored_article, restored_event = read_json(original_path), read_json(event_path)
            self.assertEqual(original_path.read_bytes(), original_bytes)
            self.assertEqual(restored_article["title"], title)
            self.assertEqual(restored_article["text"], quote)
            self.assertEqual(restored_article["language"], "ja")
            self.assertEqual(restored_event["headline"], event["headline"])
            self.assertEqual(restored_event["facts"][0]["evidence_quote"], quote)
            self.assertEqual(restored_event["facts"][0]["article_id"], restored_article["article_id"])
            self.assertEqual(restored_article["text_sha256"], sha256(quote.encode("utf-8")).hexdigest())
            for path in (original_path, event_path):
                self.assertIn(quote.encode("utf-8"), path.read_bytes())
                self.assertNotIn(b"\\u", path.read_bytes())

    def test_cli_to_existing_archive_route_utf8_and_immutable_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            html, metadata, output = (root / name for name in ("page.html", "meta.json", "article.json"))
            html.write_text(page(f'<article><p>{FIRST}</p><p>{LAST}</p></article>'), encoding="utf-8")
            metadata.write_text(json.dumps(dict(url=URL, source_id="press")), encoding="utf-8")
            command = [sys.executable, "-m", "dating_digest", "extract-page", "--html", str(html),
                       "--metadata", str(metadata), "--output", str(output), "--tags", "features", "trust_safety"]
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            article = json.loads(output.read_text(encoding="utf-8"))
            receipt = ArticleArchive(root / "archive").save(article)
            self.assertIn("_undated", receipt["archive_path"])
            self.assertEqual(article["tags"], ["features", "trust_safety"])
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
