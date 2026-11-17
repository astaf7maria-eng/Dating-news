"""Publication titles, clean original-language text and content quality checks."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urljoin, urlsplit

from dating_digest.storage.archive import normalize_url
from dating_digest.io import write_json

TAGS = ("features", "monetization", "trust_safety", "ai", "new_models", "business", "marketing")
BODY_SELECTORS = (
    '[itemprop="articleBody"]', ".article-body", ".article__body", ".article--content",
    ".article-content", ".entry-content", ".post-content", ".cm-entry-summary",
    ".baseEditorStyles", ".ModuleNewsDetail .ModuleBody", ".news-detail-content",
    ".newsDetail", ".news-detail", ".press-release-content", ".ui-prose",
)
BLOCKS = {"p", "div", "section", "article", "main", "body", "li", "blockquote", "pre",
          "h1", "h2", "h3", "h4", "h5", "h6", "tr", "ul", "ol", "table", "br"}
NOISE = re.compile(
    r"^(?:(?:comments?|related|recommendations?|recommended|advertisement|advertising|"
    r"ads?|social-share|share-buttons|sharedaddy|sidebar|cookie|newsletter-signup|author-bio)"
    r"(?:[-_].*)?|ui-sticky-aside|ui-card|internalAdBox|rootYieldFooter|"
    r"wp-block-tc23-author-card(?:__.*)?|.*__sidebar-column)$", re.I)
TAIL_HEADING = re.compile(
    r"^(?:related (?:articles|posts|stories)|recommended (?:articles|stories)|"
    r"latest (?:in|from)\b.*|in this story|читайте также|похожие (?:статьи|материалы)|"
    r"рекомендуем также|相关推荐|相关阅读)$", re.I)
SENTENCE = re.compile(r"[.!?。！？](?:[\s\"'»”’)]|$)")


def _space(value: str) -> str:
    return " ".join(value.replace("\u200b", "").split())


def _key(value: str) -> str:
    return _space(unicodedata.normalize("NFC", value)).casefold()


def _article_data(soup, url: str) -> list[dict]:
    found = []

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            kinds = value.get("@type", [])
            if isinstance(kinds, str):
                kinds = [kinds]
            if any(kind in {"Article", "NewsArticle", "BlogPosting", "ReportageNewsArticle"} for kind in kinds):
                identity = value.get("url") or value.get("mainEntityOfPage")
                if isinstance(identity, dict):
                    identity = identity.get("@id")
                if not identity or (isinstance(identity, str) and normalize_url(urljoin(url, identity)) == url):
                    found.append(value)
            if "@graph" in value:
                visit(value["@graph"])

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            visit(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError):
            continue
    return found


def _title(soup, data: list[dict], url: str) -> tuple[str, str]:
    site = soup.find("meta", property="og:site_name")
    site = _space(site.get("content", "")) if site else ""
    domain = (urlsplit(url).hostname or "").removeprefix("www.").split(".")[0]
    candidates = []

    def add(value, source, score):
        if not isinstance(value, str):
            return
        value = _space(value)
        parts = re.split(r"\s+[|–—-]\s+", value)
        if len(parts) > 1:
            suffix = re.sub(r"\W", "", parts[-1]).casefold()
            if (site and _key(parts[-1]) == _key(site)) or (len(domain) > 3 and suffix == domain.casefold()):
                value = " — ".join(parts[:-1])
        if not value or (site and _key(value) == _key(site)):
            return
        if re.fullmatch(r"home|news|latest news|blog|press|новости|главная", value, re.I):
            return
        if value.endswith(("…", "...")):
            score -= 35
        candidates.append((score, value, source))

    for item in data:
        add(item.get("headline"), "json_ld_headline", 105)
    for attr, name, score in (("property", "og:title", 100), ("name", "twitter:title", 98)):
        for meta in soup.find_all("meta", attrs={attr: name}):
            add(meta.get("content"), name, score)
    for heading in soup.find_all("h1"):
        identity = " ".join(heading.get("class", [])) + " " + heading.get("id", "")
        if re.search(r"site-name|site-title|logo|branding", identity, re.I) or heading.find_parent(["nav", "aside"]):
            continue
        in_article = bool(heading.find_parent(["article", "main"]))
        add(heading.get_text(" ", strip=True), "article_h1" if in_article else "h1", 92 if in_article else 80)
    if soup.title:
        add(soup.title.get_text(" ", strip=True), "html_title", 85)
    if not candidates:
        return "", "missing"
    # Agreement between independent metadata/headings strengthens a candidate.
    counts = {key: len({source for _, text, source in candidates if _key(text) == key})
              for key in {_key(text) for _, text, _ in candidates}}
    _, value, source = max(candidates, key=lambda c: c[0] + min(counts[_key(c[1])] - 1, 3) * 3)
    return value, source


def _clean(soup) -> None:
    for node in list(soup.find_all(["script", "style", "noscript", "template", "nav", "footer", "aside", "form", "svg", "button"])):
        node.decompose()
    for node in list(soup.find_all(True)):
        if node.attrs is None:
            continue
        identity = " ".join(node.get("class", [])) + " " + node.get("id", "")
        role = node.get("role", "")
        if any(NOISE.fullmatch(token) for token in identity.split()) or role in {"navigation", "complementary", "dialog"}:
            node.decompose()
    for node in list(soup.find_all("header")):
        if not node.find_parent(["article", "main"]):
            node.decompose()
    for heading in list(soup.find_all(["h2", "h3", "h4"])):
        if TAIL_HEADING.fullmatch(_space(heading.get_text(" ", strip=True))):
            # A separate related-content section, never the enclosing article.
            parent = heading.parent
            if parent.name == "section" and not any(parent.select(selector) for selector in BODY_SELECTORS):
                parent.decompose()


def _blocks(root) -> list[str]:
    result = []

    def walk(node):
        buffer = []

        def flush():
            text = _space(" ".join(buffer))
            if text:
                result.append(text)
            buffer.clear()

        for child in node.children:
            if child.name is None:
                # Exclude HTML comments, whose class also derives from str.
                if type(child).__name__ == "NavigableString":
                    buffer.append(str(child))
            elif child.name in BLOCKS or child.find(list(BLOCKS)):
                flush()
                walk(child)
            else:
                buffer.append(child.get_text(" ", strip=True))
        flush()

    walk(root)
    return result


def _unique(blocks: list[str], title: str) -> tuple[list[str], int]:
    seen, result, removed = set(), [], 0
    for block in blocks:
        key = _key(block)
        if key == _key(title):
            continue
        if key in seen and (len(key) >= 30 or (result and _key(result[-1]) == key)):
            removed += len(block)
            continue
        seen.add(key)
        result.append(block)
    return result, removed


def extract_page(html: str, url: str) -> dict:
    """Extract saved HTML, without network, page-type labels or semantic tagging.

    full means no detected content defect, not independently verified completeness.
    Diagnostics remain explicit so the session can check a suspicious extraction.
    """
    from bs4 import BeautifulSoup

    url = normalize_url(url)
    soup = BeautifulSoup(html, "html.parser")
    canonical = soup.find("link", rel="canonical")
    try:
        canonical_url = normalize_url(urljoin(url, canonical.get("href", ""))) if canonical else url
    except ValueError:
        canonical_url = url
    data = _article_data(soup, canonical_url)
    title, title_source = _title(soup, data, canonical_url)
    language = soup.html.get("lang", "").split("-")[0].lower() if soup.html else ""
    blocked = bool(soup.select('[class*="paywall"], [id*="paywall"], [data-testid="paywall"]'))
    _clean(soup)
    candidates = []
    seen = set()

    def candidate(node, method, preferred=False):
        if id(node) in seen:
            return
        seen.add(id(node))
        raw_blocks = _blocks(node)
        blocks, removed = _unique(raw_blocks, title)
        text = "\n\n".join(blocks)
        if not text:
            return
        link_chars = sum(len(a.get_text(" ", strip=True)) for a in node.find_all("a"))
        raw_chars = max(sum(map(len, raw_blocks)), 1)
        link_ratio = min(link_chars / raw_chars, 1)
        score = len(text) * (1 - link_ratio) ** 2
        candidates.append(dict(text=text, blocks=blocks, removed=removed, method=method,
                               in_content=node.name in {"article", "main"} or bool(node.find_parent(["article", "main"])),
                               preferred=preferred, score=score, raw_chars=raw_chars, link_ratio=link_ratio))

    for selector in BODY_SELECTORS:
        for node in soup.select(selector):
            candidate(node, selector, True)
    for selector in ("article", "main", "body"):
        for node in soup.select(selector):
            candidate(node, selector)
    # Sites such as Framer use unnamed divs instead of article/main. Consider
    # paragraph ancestors too, preferring a compact body with equivalent content.
    for paragraph in soup.find_all(["p", "blockquote"]):
        for ancestor in list(paragraph.parents)[:4]:
            if ancestor.name in {"div", "section"}:
                candidate(ancestor, "paragraph_ancestor")
    for item in data:
        body = item.get("articleBody")
        if isinstance(body, str) and body.strip():
            node = BeautifulSoup(body, "html.parser")
            candidate(node, "json_ld_articleBody", True)
    unselected = []
    if not candidates:
        chosen = dict(text="", blocks=[], removed=0, method="none", raw_chars=0, link_ratio=0)
    else:
        scoped = [c for c in candidates if c["in_content"] and len(c["text"]) >= 80]
        pool = [c for c in candidates if c["in_content"] or c["method"] == "json_ld_articleBody"] if scoped else candidates
        best = max(pool, key=lambda c: c["score"])
        preferred = [c for c in pool if c["preferred"] and c["score"] >= best["score"] * .75]
        if preferred:
            chosen = max(preferred, key=lambda c: c["score"])
            selected_text = _key(chosen["text"])
            unselected = [block for block in best["blocks"]
                          if len(block) >= 80 and SENTENCE.search(block) and _key(block) not in selected_text]
        else:
            best_blocks = {_key(block) for block in best["blocks"]}
            equivalent = [c for c in pool if c["score"] >= best["score"] * .97
                          and best_blocks <= {_key(block) for block in c["blocks"]}]
            chosen = min(equivalent, key=lambda c: c["raw_chars"])
    text = chosen["text"]
    issues = []
    if len(text) < 80:
        issues.append("very_short_text")
    if text and not SENTENCE.search(text):
        issues.append("no_sentence_boundary")
    if re.search(r"(?:read (?:the )?full (?:article|story)|continue reading|"
                 r"subscribe to (?:continue|read)|войдите,? чтобы (?:читать|продолжить)|"
                 r"подпишитесь,? чтобы (?:читать|продолжить))", text, re.I):
        issues.append("access_or_excerpt_notice")
    elif re.search(r"(?:read more|читать далее|продолжение статьи)\s*$", text, re.I):
        issues.append("access_or_excerpt_notice")
    if blocked and chosen["method"] != "json_ld_articleBody":
        issues.append("access_gate_in_html")
    if text.endswith(("...", "…")):
        issues.append("truncated_ending")
    if chosen["link_ratio"] > .35:
        issues.append("high_link_density")
    if unselected:
        issues.append("additional_substantive_text_outside_container")
    if len(re.findall(r"\ufffd|â\x80|â€|Ã[\x80-\xbf]|Ð[\x80-\xbf]", text)) >= 3:
        issues.append("encoding_artifacts")
    if not title:
        issues.append("missing_publication_title")
    if not language:
        language = ("ja" if re.search("[\u3040-\u30ff]", text) else
                    "ru" if re.search("[А-Яа-яЁё]", text) else
                    "zh" if re.search("[\u4e00-\u9fff]", text) else "en")
    return {"title": title, "canonical_url": canonical_url, "language": language,
            "text": text or None, "text_sha256": sha256(text.encode("utf-8")).hexdigest() if text else None,
            "extraction_status": "unavailable" if not text else "partial" if issues else "full",
            "extraction_method": "shared_html_v1:" + chosen["method"],
            "extraction_error": "; ".join(issues) or None,
            "extraction_diagnostics": {"title_source": title_source, "quality_issues": issues,
                                       "duplicate_chars_removed": chosen["removed"],
                                       "unselected_paragraph_count": len(unselected),
                                       "paragraph_count": len(chosen["blocks"])}}


def extract_article(html: str, metadata: dict, *, tags: list[str] | None = None) -> dict:
    """Build an Article from verified metadata plus common HTML extraction.

    Dates, source/company IDs and thematic tags are supplied by the session.
    Missing publication dates remain unknown; this function never guesses them.
    """
    return _build_article(extract_page(html, metadata["url"]), metadata, tags)


def _build_article(extracted: dict, metadata: dict, tags: list[str] | None) -> dict:
    value = dict(metadata)
    value["url"] = normalize_url(value["url"])
    if not isinstance(value.get("source_id"), str) or not value["source_id"].strip():
        raise ValueError("source_id must be a nonempty string")
    selected_tags = tags if tags is not None else value.get("tags", [])
    if not isinstance(selected_tags, list) or any(tag not in TAGS for tag in selected_tags):
        raise ValueError("tags must contain only thematic group IDs: " + ", ".join(TAGS))
    value.update(extracted)
    if not value["title"]:
        raise ValueError("Publication title not found; inspect HTML and supply a corrected extraction")
    value.setdefault("schema_version", "0.1")
    value.setdefault("article_id", sha256(value["canonical_url"].encode("utf-8")).hexdigest()[:24])
    value.setdefault("published_at", None)
    value.setdefault("date_precision", "unknown")
    value.setdefault("event_date", None)
    value.setdefault("collected_at", datetime.now(timezone.utc).isoformat())
    value.setdefault("company_ids", [])
    value["tags"] = list(dict.fromkeys(selected_tags))
    return value


def save_html_article(html_path: Path, metadata: dict, output: Path, *, tags: list[str] | None = None) -> dict:
    """Extract an already saved page, including recovery after an interrupted fetch."""
    article = extract_article(html_path.read_text(encoding="utf-8-sig"), metadata, tags=tags)
    article["raw_path"] = str(html_path)
    write_json(output, article)
    return article


def extract_provider_article(pages: list[str], metadata: dict, *, first_line: int, last_line: int,
                             tags: list[str] | None = None, quality_issues: list[str] | None = None) -> dict:
    """Import an explicitly reviewed range of original numbered provider text.

    Each entry is one source page or continuation, not a union of search results.
    The caller supplies verified title, language, dates and raw_path/provenance.
    Missing lines and rewritten views remain partial; conflicting continuations fail.
    """
    if (type(first_line) is not int or type(last_line) is not int
            or not 0 <= first_line <= last_line or not pages):
        raise ValueError("Provide source pages and an ordered nonnegative line range")
    if not all(isinstance(metadata.get(key), str) and metadata[key].strip()
               for key in ("title", "language", "raw_path")):
        raise ValueError("Provider text needs verified title, language and raw_path")
    lines, issues = {}, list(quality_issues or [])
    for page in pages:
        header = page.splitlines()[0] if page.splitlines() else ""
        if "rewritten summary" in header.casefold():
            issues.append("provider_rewritten_view_requires_original")
        for match in re.finditer(r"^L(\d+)(?:@P[\d-]+)?: ?(.*)$", page, re.M):
            number, value = int(match[1]), match[2]
            if number in lines and lines[number] != value:
                raise ValueError(f"Conflicting source text at line {number}")
            lines[number] = value
    missing = [number for number in range(first_line, last_line + 1) if number not in lines]
    if missing:
        issues.append("provider_missing_body_lines")
    parts = [lines[number] for number in range(first_line, last_line + 1) if number in lines
             and not re.fullmatch(r"【\d+†Image[^】]*】", lines[number])]
    text = re.sub(r"【\d+†([^】†]+)(?:†[^】]+)?】", r"\1", "\n".join(parts)).strip()
    issues = list(dict.fromkeys(issues))
    extracted = dict(title=metadata["title"], language=metadata["language"],
                     canonical_url=normalize_url(metadata.get("canonical_url", metadata["url"])),
                     text=text or None, text_sha256=sha256(text.encode("utf-8")).hexdigest() if text else None,
                     extraction_status="unavailable" if not text else "partial" if issues else "full",
                     extraction_method="provider_numbered_original_v1", extraction_error="; ".join(issues) or None,
                     extraction_diagnostics=dict(quality_issues=issues, paragraph_count=len(parts)))
    article = _build_article(extracted, metadata, tags)
    article["provenance"] = dict(metadata.get("provenance", {}), body_line_start=first_line,
                                 body_line_end=last_line, missing_lines=missing,
                                 provider_pages_sha256=sha256(json.dumps(pages, ensure_ascii=False).encode("utf-8")).hexdigest(),
                                 limitations="Original provider text; completeness of the chosen range requires source-based review.")
    return article
