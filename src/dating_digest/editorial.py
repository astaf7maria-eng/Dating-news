"""Sequential, session-authored editorial workflow and local Mattermost rendering."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re

from jsonschema import Draft202012Validator, FormatChecker

from .domain.context import json_sha256, validate_context_bundle, validate_context_refs
from .domain.window import MOSCOW, validate_window
from .io import read_json, write_json, write_text
from .research_qa import check_research_qa
from .storage.archive import publication_day

ROOT = Path(__file__).resolve().parents[2]
STEPS = ("analysis", "selection", "summaries", "digest")
PROMPTS = ("common", "dating-market-digest", "analyze-news", "select-and-rank",
           "write-digest", "so-what", "so-what-examples", "review-digest")
LIMITED_COVERAGE = "Подборка подготовлена по проверенной части корпуса с ограниченным покрытием рынка."


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _validate(value: dict, schema: str, project_dir: Path) -> None:
    validator = Draft202012Validator(read_json(project_dir / "schemas" / f"{schema}.schema.json"),
                                    format_checker=FormatChecker())
    error = next(validator.iter_errors(value), None)
    if error:
        raise ValueError(f"{schema}: {'/'.join(map(str, error.absolute_path))}: {error.message}")


def _hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def prepare_editorial(run_dir: Path, project_dir: Path = ROOT, *, verified_subset_reason: str | None = None,
                      editorial_policy_path: Path | None = None) -> dict:
    run_dir, project_dir = Path(run_dir).resolve(), Path(project_dir).resolve()
    subset = verified_subset_reason is not None
    _require(not subset or bool(verified_subset_reason.strip()), "Verified-subset drafting requires a reason")
    review_sha256 = _hash(run_dir / "research_review.json")
    gate = check_research_qa(run_dir, project_dir, verified_subset=subset)
    bundle = read_json(run_dir / "context_bundle.json")
    validate_context_bundle(bundle)
    manifest = read_json(run_dir / "manifest.json")
    _require(manifest["business_context"]["sha256"] == bundle["context_sha256"], "Context differs from manifest")
    articles = [read_json(run_dir / path) for path in gate["eligible_article_paths"]]
    editorial = run_dir / "editorial"
    policy_source = Path(editorial_policy_path) if editorial_policy_path else run_dir / "config_snapshot/editorial_policy.json"
    policy = read_json(policy_source)
    _require(type(policy["max_news_total"]) is int and 1 <= policy["max_news_total"] <= 10,
             "Editorial policy limit must be between 1 and 10")
    frozen_policy = read_json(run_dir / "config_snapshot/editorial_policy.json")
    _require({s["id"] for s in policy["sections"]} == {s["id"] for s in frozen_policy["sections"]},
             "Editorial policy sections differ from the run")
    write_json(editorial / "policy.json", policy, replace=True)
    prompt_hashes = {}
    for name in PROMPTS:
        value = (project_dir / "prompts" / f"{name}.md").read_text(encoding="utf-8-sig")
        path = editorial / "prompts" / f"{name}.md"
        write_text(path, value, replace=True)
        prompt_hashes[name] = _hash(path)
    news = dict(run_id=gate["run_id"], articles=articles)
    write_json(editorial / "news.json", news, replace=True)
    inputs = dict(schema_version="0.2", run_id=gate["run_id"],
                  research_input_sha256=gate["input_sha256"], research_review_sha256=review_sha256,
                  mode="verified_subset" if subset else "full",
                  subset_reason=verified_subset_reason,
                  coverage_notes=[LIMITED_COVERAGE] if subset else [],
                  eligible_article_paths=gate["eligible_article_paths"], news_sha256=_hash(editorial / "news.json"),
                  prompt_hashes=prompt_hashes, window_start=manifest["window_start"],
                  window_end_exclusive=manifest["window_end_exclusive"],
                  context_version=bundle["context_version"], context_sha256=bundle["context_sha256"],
                  editorial_policy_sha256=_hash(editorial / "policy.json"),
                  editorial_policy_origin=str(policy_source),
                  output_format="mattermost_priority_order",
                  format_origin="prompts/dating-market-digest.md")
    inputs["input_sha256"] = json_sha256(inputs)
    _require(_hash(run_dir / "research_review.json") == review_sha256, "Research review changed during preparation")
    write_text(editorial / "instructions.md", _instructions(), replace=True)
    write_json(editorial / "inputs.json", inputs, replace=True)
    return dict(run_id=inputs["run_id"], mode=inputs["mode"], article_count=len(articles),
                instructions=str(editorial / "instructions.md"), input_sha256=inputs["input_sha256"])


def _instructions() -> str:
    return """# От корпуса к дайджесту

Работай из корня проекта. `--run-dir` указывает папку этого запуска.
Агент выполняет весь маршрут в текущей сессии: после успешной проверки сразу
переходи к следующему шагу, не запрашивая подтверждение пользователя.
Пользователь проверяет готовый Markdown после рецензии и рендеринга.
Прочитай editorial/inputs.json и снимок editorial/prompts/dating-market-digest.md.
Сохраняй input_sha256 из inputs.json в каждом артефакте шага.
Редакционные шаги используют только сохранённый корпус. Новые факты или источники
требуют возврата к QA исследования: сохранить Article и архив, повторить QA и prepare-editorial.
Поиск и загрузку страниц выполняй на этапе исследования и его QA.

1. Анализ. Используй editorial/news.json, editorial/prompts/common.md и analyze-news.md.
   Сохрани events/<event_id>.json и editorial/analysis.json, затем выполни check-editorial --step analysis.
2. Отбор. Используй проверенные события, context_bundle.json, editorial/policy.json,
   config_snapshot/pipeline.json, sources.json и editorial/prompts/select-and-rank.md. Сохрани ranking.json
   с бизнес-логикой, стратегическим сигналом и последствиями для Lovito по каждому кандидату,
   затем выполни check-editorial --step selection.
3. Написание выпуска одним проходом. Используй выбранные события, их strategic_assessment,
   context_bundle.json и editorial/prompts/write-digest.md; so-what.md и so-what-examples.md — критерии качества.
   Напиши саммари и So what вместе, переиспользуя уже проверенное стратегическое обоснование.
   Загружай только выбранные Article для нужных цитат и метаданных, весь корпус повторно не передавай.
   Сохрани editorial/summaries.json и digest.json, затем выполни check-editorial --step digest:
   эта команда также проверяет саммари и все предыдущие артефакты.

После трёх проходов выполни отдельную редакционную проверку по editorial/prompts/review-digest.md,
а не только check-editorial. Сохрани review.json с фактическим reviewer для точных
digest_sha256 и editorial_sha256 проверяемой ревизии. Собственная рецензия агента
помечается session_agent_self_review и не является независимой проверкой другой моделью.
При failed исправь затронутые артефакты, повтори их проверки и смысловую рецензию;
при изменении digest.json увеличь revision. Не выставляй passed по одному успеху CLI.
Допустимы две попытки исправления шага и не более двух циклов исправлений после
первой смысловой рецензии. Возврат из рецензии к research QA также входит в эти циклы.
При исчерпании лимита сохрани замечания, укажи этап и причину остановки; готовый
выпуск не объявляй. Не повторяй весь корпус и поиск автоматически.
После passed выполни render-digest и прочитай rendered/mattermost.md.
Передай его содержимое и путь пользователю для финальной проверки, сохранив оговорки.
Автоматическая отправка в канал не выполняется. На этом запрос полного дайджеста завершён.
Полные команды и контракты артефактов находятся в docs/editorial-workflow.md.
"""


def _load(run_dir: Path, project_dir: Path) -> tuple[dict, dict, dict, dict]:
    inputs = read_json(run_dir / "editorial/inputs.json")
    expected = {key: value for key, value in inputs.items() if key != "input_sha256"}
    _require(json_sha256(expected) == inputs["input_sha256"], "Editorial input fingerprint differs")
    _require(_hash(run_dir / "research_review.json") == inputs["research_review_sha256"],
             "Editorial research inputs changed; rerun prepare-editorial")
    current = check_research_qa(run_dir, project_dir, verified_subset=inputs["mode"] == "verified_subset")
    _require(current["input_sha256"] == inputs["research_input_sha256"]
             and current["eligible_article_paths"] == inputs["eligible_article_paths"],
             "Editorial research inputs changed; rerun prepare-editorial")
    _require(_hash(run_dir / "editorial/news.json") == inputs["news_sha256"], "Editorial news bundle changed")
    for name, fingerprint in inputs["prompt_hashes"].items():
        _require(_hash(run_dir / "editorial/prompts" / f"{name}.md") == fingerprint, "Editorial prompt snapshot changed")
    news = read_json(run_dir / "editorial/news.json")
    _require(news["articles"] == [read_json(run_dir / path) for path in current["eligible_article_paths"]],
             "Editorial corpus differs from QA-approved Articles; rerun prepare-editorial")
    articles = {a["article_id"]: a for a in news["articles"]}
    _require(len(articles) == len(news["articles"]) and news["run_id"] == inputs["run_id"], "Invalid editorial corpus")
    policy_path = run_dir / "config_snapshot/editorial_policy.json"
    if "editorial_policy_sha256" in inputs:
        policy_path = run_dir / "editorial/policy.json"
        _require(_hash(policy_path) == inputs["editorial_policy_sha256"], "Editorial policy snapshot changed")
    policy = read_json(policy_path)
    return inputs, articles, policy, read_json(run_dir / "context_bundle.json")


def _artifact(run_dir: Path, name: str, inputs: dict, fingerprints: dict) -> dict:
    fingerprints[name] = _hash(run_dir / name)
    value = read_json(run_dir / name)
    _require(value.get("run_id") == inputs["run_id"] and value.get("input_sha256") == inputs["input_sha256"],
             "Stale or mismatched editorial artifact: " + name)
    return value


def _sentences(text: str) -> int:
    text = re.sub(r"\bт\.\s*(?:е|д|п)\.", "", text)
    text = re.sub(r"\b(?:млн|млрд|тыс|руб|г|др)\.", "", text)
    return len([part for part in re.split(r"(?<=[.!?])[»”\"’')\]]*\s+", text.strip()) if part])


def publication_timestamp(article: dict) -> str:
    """Preserve timestamps and normalize a known publication day for Digest."""
    if article["published_at"] is not None:
        return article["published_at"]
    day = publication_day(article)
    _require(day is not None, "Article has no known publication day")
    return datetime.combine(day, time.min, MOSCOW).isoformat()


def check_editorial(run_dir: Path, step: str = "digest", project_dir: Path = ROOT) -> dict:
    """Validate each preceding artifact before permitting a later stage."""
    _require(step in STEPS, "Unknown editorial step")
    run_dir, project_dir = Path(run_dir).resolve(), Path(project_dir).resolve()
    fingerprints = {"editorial/inputs.json": _hash(run_dir / "editorial/inputs.json")}
    inputs, articles, policy, bundle = _load(run_dir, project_dir)
    analysis = _artifact(run_dir, "editorial/analysis.json", inputs, fingerprints)
    ids = analysis["event_ids"]
    _require(len(ids) == len(set(ids)) and all(re.fullmatch(r"[a-zA-Z0-9_-]+", x) for x in ids), "Invalid event IDs")
    events = {}
    for identity in ids:
        event = _artifact(run_dir, f"events/{identity}.json", inputs, fingerprints)
        _validate(event, "event", project_dir)
        _require(all(key in event for key in ("primary_article_id", "mechanism", "possible_consequences")),
                 "Event needs primary_article_id, mechanism and possible_consequences")
        _require(event["event_id"] == identity and set(event["article_ids"]) <= articles.keys(), "Invalid event evidence")
        _require(event["primary_article_id"] in event["article_ids"], "Primary Article must support the event")
        _require(event["context_assessment"] is None and event["ranking"] is None, "Analyze events before applying context or ranking")
        _require(bool(event["facts"]) and bool(event["mechanism"].strip()), "Event needs facts and a mechanism")
        for fact in event["facts"]:
            _require(bool(fact["evidence_quote"].strip()) and fact["article_id"] in event["article_ids"]
                     and fact["evidence_quote"] in articles[fact["article_id"]]["text"], "Evidence quote differs from Article")
        events[identity] = event
    decisions = analysis["article_decisions"]
    _require(len(decisions) == len(articles) and {d["article_id"] for d in decisions} == articles.keys(),
             "Analysis must account for every eligible Article exactly once")
    for decision in decisions:
        linked = {eid for eid, event in events.items() if decision["article_id"] in event["article_ids"]}
        _require(set(decision["event_ids"]) == linked and bool(decision["reason"].strip()), "Incomplete Article/event mapping")
    result = dict(run_id=inputs["run_id"], step=step, event_count=len(events), mode=inputs["mode"])
    if step != "analysis":
        ranking = _artifact(run_dir, "ranking.json", inputs, fingerprints)
        rows = ranking["items"]
        _require(len(rows) == len(events) and {r["event_id"] for r in rows} == events.keys(), "Rank every event exactly once")
        weights = read_json(run_dir / "config_snapshot/pipeline.json")["ranking"]["weights"]
        for row in rows:
            _validate(dict(events[row["event_id"]], ranking={"criteria": row["criteria"], "total_score": row["total_score"]}), "event", project_dir)
            criteria = {c["name"]: c["score"] for c in row["criteria"]}
            _require(len(criteria) == len(row["criteria"]) and criteria.keys() == weights.keys(), "Ranking criteria differ from weights")
            score = sum(criteria[name] / 5 * weight * 100 for name, weight in weights.items())
            _require(abs(score - row["total_score"]) < 1e-7, "Incorrect weighted event score")
            _require(row["decision"] in {"include", "exclude"} and bool(row["reason"].strip()), "Missing selection decision")
            if policy.get("strategic_selection", {}).get("required"):
                assessment = row.get("strategic_assessment", {})
                _require(isinstance(assessment, dict), "Invalid strategic assessment")
                _require(type(assessment.get("has_strategic_value")) is bool, "Assess strategic value for every event")
                _require(row["decision"] != "include" or assessment["has_strategic_value"],
                         "Selected event has no substantive strategic implication")
                if assessment["has_strategic_value"]:
                    for field in policy["strategic_selection"]["reasoning_fields"]:
                        _require(isinstance(assessment.get(field), str) and bool(assessment[field].strip()),
                                 "Missing strategic reasoning: " + field)
                    validate_context_refs(bundle, assessment["context_refs"])
                    _require(set(assessment.get("related_event_ids", [])) <= events.keys(), "Unknown related event")
            if row["decision"] == "include":
                _require(events[row["event_id"]]["eligibility"]["status"] == "included", "Selected event is ineligible or pending")
        def order(row):
            evidence = next(c["score"] for c in row["criteria"] if c["name"] == "evidence_quality")
            article = articles[events[row["event_id"]]["primary_article_id"]]
            return (-row["total_score"], -evidence, -datetime.fromisoformat(publication_timestamp(article)).timestamp(), row["event_id"])
        _require(rows == sorted(rows, key=order), "Ranking must follow descending score, evidence, freshness and ID")
        selected = [row["event_id"] for row in rows if row["decision"] == "include"]
        _require(len(selected) <= min(10, policy["max_news_total"]), "Too many selected news items")
        result["selected_count"] = len(selected)
    if step in {"summaries", "digest"}:
        summaries = _artifact(run_dir, "editorial/summaries.json", inputs, fingerprints)["items"]
        _require([item["event_id"] for item in summaries] == selected, "Summaries differ from selected priority order")
        for item in summaries:
            _require(all(isinstance(item.get(key), str) and bool(item[key].strip())
                         for key in ("company", "summary")), "Summary needs nonblank company and text")
            article = articles[events[item["event_id"]]["primary_article_id"]]
            _require(all(item[key] == article[source] for key, source in
                         (("original_title", "title"), ("original_url", "url")))
                     and item["published_at"] == publication_timestamp(article),
                     "Preserve original Article title, URL and date")
            _require(2 <= _sentences(item["summary"]) <= 3, "Summary needs 2–3 sentences")
    if step == "digest":
        digest = _artifact(run_dir, "digest.json", inputs, fingerprints)
        _validate(digest, "digest", project_dir)
        _require(digest["status"] in {"draft", "review_failed", "review_passed"}, "Local rendering requires an unpublished digest")
        _require(all(digest[key] == inputs[key] for key in ("window_start", "window_end_exclusive", "context_version", "context_sha256")), "Digest window/context differs")
        _require(digest["title"] == digest_title(inputs), "Digest title differs from the Moscow reporting window")
        _require([item["event_id"] for item in digest["items"]] == selected, "Digest differs from selected priority order")
        _require(set(inputs["coverage_notes"]) <= set(digest["coverage_notes"]), "Preserve coverage limitations")
        if inputs["mode"] == "verified_subset":
            _require(digest["status"] == "draft", "Verified-subset workflow produces a draft")
        _require(len(selected) == min(10, policy["max_news_total"])
                 or bool((digest["shortfall_reason"] or "").strip()), "Explain an editorial shortfall internally")
        sections = {s["section_id"]: s for s in digest["sections"]}
        _require(len(sections) == len(digest["sections"]) == 7, "Digest requires seven unique internal sections")
        for number, (item, summary) in enumerate(zip(digest["items"], summaries), 1):
            _require(item["rank"] == item["display_order"] == number, "Mattermost uses descending priority order")
            _require(all(item[key] == summary[key] for key in ("company", "original_title", "original_url", "published_at", "summary")), "Digest changed approved summary fields")
            event = events[item["event_id"]]
            _require(item["section_id"] == event["section_id"], "Digest section differs from event")
            allowed_urls = {articles[aid]["url"] for aid in event["article_ids"]}
            _require(set(item["sources"]) <= allowed_urls and item["original_url"] in item["sources"], "Digest source is unsupported")
            stamp = datetime.fromisoformat(item["published_at"])
            _require(datetime.fromisoformat(inputs["window_start"]) <= stamp < datetime.fromisoformat(inputs["window_end_exclusive"]), "Publication outside window")
            validate_context_refs(bundle, item["so_what"]["context_refs"])
            _require(isinstance(item["so_what"].get("text"), str) and bool(item["so_what"]["text"].strip()),
                     "Visible so what needs text")
            _require(1 <= _sentences(item["so_what"]["text"]) <= 2, "Visible so what needs 1–2 sentences")
            if policy.get("strategic_selection", {}).get("required"):
                _require(isinstance(item["so_what"].get("competitor_logic"), str)
                         and bool(item["so_what"]["competitor_logic"].strip()), "So what needs competitor or market logic")
                _require(set(item["so_what"].get("related_event_ids", [])) <= events.keys(), "Unknown related event")
        for section_id, section in sections.items():
            has_news = any(item["section_id"] == section_id for item in digest["items"])
            expected = "has_news" if has_news else ("coverage_incomplete" if inputs["mode"] == "verified_subset" else "no_significant_news")
            _require(section["status"] == expected, "Section status differs from items/coverage")
        result["digest_sha256"] = fingerprints["digest.json"]
    _require(all(_hash(run_dir / name) == fingerprint for name, fingerprint in fingerprints.items()),
             "Editorial artifact changed during validation; retry the check")
    result["editorial_sha256"] = json_sha256(fingerprints)
    write_json(run_dir / "editorial" / f"check-{step}.json", result, replace=True)
    return result


def digest_title(inputs: dict) -> str:
    start, end = validate_window(datetime.fromisoformat(inputs["window_start"]),
                                 datetime.fromisoformat(inputs["window_end_exclusive"]))
    if start.time() == end.time() == time.min:
        last_day = end - timedelta(days=1)
        return f"💘 DatingTech | Дайджест за {start:%d.%m.%Y}–{last_day:%d.%m.%Y}"
    return f"💘 DatingTech | Дайджест с {start:%d.%m.%Y %H:%M:%S} до {end:%d.%m.%Y %H:%M:%S} МСК"


def render_digest(run_dir: Path, project_dir: Path = ROOT) -> dict:
    run_dir, project_dir = Path(run_dir).resolve(), Path(project_dir).resolve()
    checked = check_editorial(run_dir, "digest", project_dir)
    digest_bytes = (run_dir / "digest.json").read_bytes()
    review_bytes = (run_dir / "review.json").read_bytes()
    digest = json.loads(digest_bytes.decode("utf-8-sig"))
    review = json.loads(review_bytes.decode("utf-8-sig"))
    _require(sha256(digest_bytes).hexdigest() == checked["digest_sha256"], "Digest changed during rendering")
    _validate(review, "review", project_dir)
    _require(review["digest_id"] == digest["digest_id"] and review["revision"] == digest["revision"]
             and review.get("digest_sha256") == checked["digest_sha256"]
             and review.get("editorial_sha256") == checked["editorial_sha256"], "Review belongs to another editorial revision")
    _require(review["result"] == "passed" and not any(i["severity"] == "blocking" for i in review["issues"]), "Digest review has blocking findings")
    blocks = [f"## {digest['title']}\n\n*Ключевые продуктовые и рыночные события периода*"]
    for item in digest["items"]:
        blocks.append(f"### {item['display_order']}. {item['company']} — {item['original_title']}\n\n"
                      f"🔗 {item['original_url']}\n\n"
                      f"**Что произошло:** {item['summary']}\n\n"
                      f"**💡 So what для Ловито:** {item['so_what']['text']}")
    if digest["coverage_notes"]:
        blocks.append("*" + " ".join(digest["coverage_notes"]) + "*")
    output = run_dir / "rendered/mattermost.md"
    write_text(output, "\n\n---\n\n".join(blocks) + "\n", replace=True)
    receipt = dict(checked, output="rendered/mattermost.md", output_sha256=_hash(output),
                   review_sha256=sha256(review_bytes).hexdigest(), publication_status="local_file")
    write_json(run_dir / "editorial/render.json", receipt, replace=True)
    return receipt
