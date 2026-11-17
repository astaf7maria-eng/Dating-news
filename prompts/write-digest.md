# Написание саммари и So what одним проходом

Вход — проверенные analysis/ranking, выбранные Event и их strategic_assessment, editorial/inputs.json, editorial/policy.json и context_bundle.json. Используй требования dating-market-digest.md и критерии so-what.md/so-what-examples.md из редакционного снимка.

Для каждого выбранного события сразу напиши саммари и So what. Факты бери из Event и связанных Article; исходник открывай для нужного подтверждения и метаданных, весь news.json повторно не загружай. Заявления атрибутируй, сохрани даты реализации и раскрытия, рынки, условия и стадии. Различай annualized run rate и выручку периода.

В editorial/summaries.json сохрани run_id, input_sha256 и items в порядке выбранных ranking.items: event_id, company, original_title, original_url, published_at, summary. Саммари — 2–3 предложения. Title/URL копируй из primary Article; published_at получи через dating_digest.editorial.publication_timestamp(article).

В digest.json по digest.schema.json скопируй поля саммари без изменений и добавь So what. Переиспользуй reasoning из ranking.strategic_assessment: competitor_logic → competitor_logic, strategic_signal → market_signal, lovito_implication → context_link, evidence_limits → существенные limitations; context_refs/related_event_ids сохраняют ссылки. Новые выводы должны следовать тем же доказательствам. so_what.text — видимые 1–2 предложения; hypothesis_or_action необязателен.

Период, версии, mode и input_sha256 — из inputs; title получи через dating_digest.editorial.digest_title(inputs). Сохрани семь внутренних sections, непрерывные rank/display_order, sources из связанных Article. В verified_subset статус draft и обязательная оговорка покрытия. При недоборе shortfall_reason объясняет причину внутри JSON.

После сохранения обоих файлов выполни check-editorial --step digest: команда проверяет также summaries и предыдущие результаты. Затем отдельная проверка по review-digest.md и render-digest. Команда --step summaries остаётся доступной для адресного исправления саммари.
