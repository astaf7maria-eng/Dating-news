# Шаг 4. So what для Авито Ловито

Вход — проверенные шаги 1–3, strategic_assessment выбранных событий, context_bundle.json,
editorial/policy.json и so-what-examples.md из редакционного снимка.

Применяй требования шага 4 и ограничения из editorial/prompts/dating-market-digest.md.
Три вопроса для вывода раскрывают причину события, рыночный сигнал и последствия для Lovito.

Внутри рабочей карточки раскрой competitor_logic, market_signal и context_link.
Укажи ограничения в limitations и связанные события в related_event_ids по смыслу.
Гипотеза о рыночном сдвиге остаётся интерпретацией. Повторение паттерна в двух кейсах
позволяет сопоставление, степень распространённости требует отдельных данных.

Сохрани digest.json по digest.schema.json с run_id и input_sha256 редакционных входов.
Скопируй поля саммари и исходные title/URL из summaries. Видимые 1–2 предложения храни в so_what.text.
market_signal и context_link раскрывают основание вывода. hypothesis_or_action допускает null
и используется при содержательном основании. context_refs ссылается на вопросы и утверждения bundle.
Период, версии и отпечатки копируй из inputs, title получи через dating_digest.editorial.digest_title(inputs).
Семь разделов сохраняются в JSON. rank и display_order совпадают с приоритетом.
В verified_subset статус draft, coverage_notes сохраняет оговорку inputs.
При числе новостей ниже лимита shortfall_reason содержит внутреннее объяснение.

Выполни `python -m dating_digest check-editorial --run-dir <run_dir> --step digest`.
Затем выполни review-digest и render-digest. Пользователю верни готовый Markdown-пост.
