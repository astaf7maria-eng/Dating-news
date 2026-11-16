# От корпуса к Markdown-дайджесту

Контекст Lovito включается здесь, при работе с готовым корпусом. Редактор выбирает важные
события и пишет обобщения и выводы. На предшествующих этапах поиска, парсинга и сохранения
материалов допуск определяется тематикой, периодом и качеством источников.

Редакционный маршрут продолжает поиск, обработку страниц, архив и QA исследования.
Исходный пользовательский промпт сохранён в [dating-market-digest.md](../prompts/dating-market-digest.md).
Период берётся из manifest.json. Ноябрьская дата исходного запроса относится к первому выпуску.
Агент выполняет смысловую работу в сессии. CLI готовит входы, проверяет переходы и формирует файл.

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
$env:PYTHONIOENCODING = 'utf-8'
$run = 'data/runs/<run_id>'
python -m dating_digest prepare-editorial --run-dir $run
# Агент следует $run/editorial/instructions.md.
```

Команды выполняются из корня проекта в окружении с установленными зависимостями.
Обычный режим требует успешной research QA. Для явно запрошенного пользователем черновика
по проверенной части корпуса доступен отдельный режим с сохранением основания.
Для выпуска 1–15 ноября такое поручение сохранено в handoff-editorial.md.

```powershell
python -m dating_digest prepare-editorial --run-dir $run --verified-subset-reason 'Поручение пользователя подготовить черновик по eligible после остановки исследования'
```

Режим использует eligible из research_review.json, сверяя текущий отпечаток корпуса,
локальные ошибки, даты, полноту текстов и сохранённые наблюдения. Выпуск получает draft
и оговорку об ограниченном покрытии. Вердикт исследования сохраняется.

## Четыре шага

| Шаг | Промпт | Результат | Проверка |
|---|---|---|---|
| 1. Анализ новостей | analyze-news.md | events/*.json, editorial/analysis.json | check-editorial --step analysis |
| 2. Отбор приоритетных новостей | select-and-rank.md | ranking.json | check-editorial --step selection |
| 3. Написание саммари | write-digest.md | editorial/summaries.json | check-editorial --step summaries |
| 4. So what для Ловито | so-what.md | digest.json | check-editorial --step digest |

Каждый артефакт содержит run_id и input_sha256 из editorial/inputs.json.
prepare-editorial сохраняет Article в editorial/news.json и снимки рабочих промптов
в editorial/prompts/. Контекст Lovito берётся из context_bundle.json при отборе на шаге 2 и написании выводов на шаге 4. Редакционные правила фиксируются в editorial/policy.json.

Шаг 1 учитывает каждую статью ровно один раз в article_decisions с article_id, event_ids и reason.
analysis.event_ids перечисляет карточки событий. Event дополняется primary_article_id, mechanism
и possible_consequences. facts содержат русские утверждения и дословные evidence_quote.
Внешнее предупреждение допускает пустой company_ids. Перепечатки связываются с одним событием.

Шаг 2 оценивает каждое событие. ranking.items содержит event_id, criteria, total_score, decision и reason.
Критерии и веса совпадают со снимком pipeline. Оценки имеют rationale.
Сортировка идёт по total_score, evidence_quality, свежести primary Article и event_id.
decision принимает include или exclude. strategic_assessment раскрывает competitor_logic, strategic_signal, lovito_implication, evidence_limits, context_refs и has_strategic_value. Для включения требуется содержательный стратегический вывод. Выбираются максимум десять событий.

Шаг 3 сохраняет items выбранных событий в том же порядке с полями event_id, company,
original_title, original_url, published_at и summary из 2–3 предложений.

Шаг 4 копирует эти поля в digest.items и добавляет so_what.text из 1–2 предложений.
Служебные поля so_what раскрывают бизнес-логику, сигнал рынка, связь с контекстом и ограничения. hypothesis_or_action допускает null. Смысловую глубину проверяет редактор по so-what-examples.md, CLI сверяет наличие обоснования и ссылки на контекст.
context_refs сохраняет ID вопросов и утверждений bundle. Семь разделов остаются метаданными JSON.
Пользовательский формат Mattermost задаёт единый порядок приоритета, rank и display_order совпадают.

```powershell
# После записи результатов каждого шага агентом
python -m dating_digest check-editorial --run-dir $run --step analysis
python -m dating_digest check-editorial --run-dir $run --step selection
python -m dating_digest check-editorial --run-dir $run --step summaries
python -m dating_digest check-editorial --run-dir $run --step digest
```

Каждая проверка сверяет предыдущие шаги и сохраняет editorial/check-<step>.json.
Изменение корпуса или research_review требует обновлённой подготовки.
Повтор prepare-editorial обновляет входы, сохраняя рабочие файлы. Старые отпечатки
выявляются при следующей проверке.

## Редакционная проверка и рендеринг

После шага 4 агент проверяет факты, стадии, отбор и выводы по review-digest.md.
Он записывает review.json по схеме Review с digest_sha256 и editorial_sha256 из check-editorial.
В reviewer указывается фактический способ смысловой проверки. passed относится к точной ревизии
после устранения blocking. Ограниченное покрытие сохраняется как warning и оговорка в посте.

```powershell
python -m dating_digest render-digest --run-dir $run
```

Результат находится в rendered/mattermost.md. Квитанция editorial/render.json связывает файл,
редакционные входы и Review через SHA-256. Повторный рендер атомарно обновляет локальный файл.
Исправленный выпуск требует повторной проверки точной ревизии.

Подготовленный выпуск находится в `data/runs/20261116T000000+0300_9d3cbbc0/rendered/mattermost.md`.
Корпус и результаты запуска сохраняются локально в исключённой из Git папке data/.

## Изменение редакционных правил

Новые запуски используют снимок актуального config/editorial_policy.json.
Для текущего корпуса явно выбранная пользователем новая редакционная политика подключается так.

```powershell
python -m dating_digest prepare-editorial --run-dir $run --editorial-policy config/editorial_policy.json
```

Команда сохраняет отдельный снимок editorial/policy.json и его отпечаток во входах.
Исторические поисковые снимки остаются источником происхождения корпуса.
Для ноябрьского выпуска прежняя редакция сохранена в editorial/history/revision-1/,
новая редакция имеет revision=2. В editorial/research-rule-update.json зафиксирована проверка
изменённого редакционного правила common.md и совпадения остальных исследовательских входов.
Исходный research verdict и статусы статей сохранены при обновлении отпечатка этого правила.
