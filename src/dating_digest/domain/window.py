"""User-selected editorial windows, independent of retrieval overlap."""

from datetime import date, datetime, time, timedelta, timezone

# Explicit Moscow UTC offset avoids dependence on host timezone or Windows tzdata.
MOSCOW = timezone(timedelta(hours=3), name="Europe/Moscow")


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware datetime required")


def rolling_window(as_of: datetime) -> tuple[datetime, datetime]:
    """Propose the last seven days; this is not a confirmed selection."""
    _aware(as_of)
    end = as_of.astimezone(MOSCOW)
    return end - timedelta(days=7), end


def validate_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    _aware(start)
    _aware(end)
    if start >= end:
        raise ValueError("Начало периода должно быть раньше конца")
    return start.astimezone(MOSCOW), end.astimezone(MOSCOW)


def date_window(start: date, end_inclusive: date) -> tuple[datetime, datetime]:
    """User-entered dates include both days, normalized to [start, end)."""
    if start > end_inclusive:
        raise ValueError("Дата начала должна быть не позже даты окончания")
    try:
        end_exclusive = end_inclusive + timedelta(days=1)
    except OverflowError as exc:
        raise ValueError("Дата окончания выходит за поддерживаемый диапазон") from exc
    return validate_window(
        datetime.combine(start, time.min, MOSCOW),
        datetime.combine(end_exclusive, time.min, MOSCOW),
    )


def in_window(published_at: datetime, start: datetime, end: datetime) -> bool:
    """Timestamp check only; novelty and evidence are separate editorial gates."""
    _aware(published_at)
    start, end = validate_window(start, end)
    return start <= published_at < end
