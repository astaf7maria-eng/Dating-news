# Шаг 1. Анализ новостей

Это начало формирования дайджеста из уже собранного корпуса. Сначала разбери факты
всех входных статей, затем редакционный шаг 2 применит контекст Lovito к выбору важных событий.

Работай после prepare-editorial, используя editorial/inputs.json, editorial/news.json,
снимок common.md, editorial/policy.json и config_snapshot/sources.json.
Период бери из inputs.json. Ноябрьский период исходного dating-market-digest.md относится к первому выпуску.
Режим verified_subset формирует черновик по eligible с сохранённой оговоркой о покрытии.

Прочитай все статьи news.json целиком. Для каждой выдели компанию или автора внешнего предупреждения,
ключевое событие, факты, механику, рынки, аудиторию и стадию. Объедини перепечатки одного события.
Существенные самостоятельные изменения внутри отчёта или интервью допустимы как отдельные события.
Различай дату публикации, исторического эксперимента, анонса и будущего запуска.

Сохрани events/<event_id>.json по event.schema.json с дополнительными run_id и input_sha256.
Заполни primary_article_id, mechanism и possible_consequences. Последствия формулируй как гипотезы,
подтверждённые утверждения пиши в facts с точными evidence_quote из исходных Article.
Для предупреждений без компании допустим пустой company_ids. context_assessment и ranking оставь null.
eligibility содержит included для допустимого кандидата, excluded или needs_review с причиной.

Сохрани editorial/analysis.json с run_id, input_sha256, event_ids и article_decisions.
Каждая статья news.json получает ровно одну запись article_decisions с article_id, event_ids и reason.
Для исключённого материала event_ids может быть пустым. Причина объясняет анализ или исключение.
Используй общий dating_digest.io для UTF-8 JSON.

Выполни `python -m dating_digest check-editorial --run-dir <run_dir> --step analysis`.
Исправь ошибки перед переходом к шагу 2.
