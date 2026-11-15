"""Ask for a period before creating artifacts or starting collection."""

from collections.abc import Callable
from datetime import date, datetime

from dating_digest.domain.window import date_window, rolling_window


def choose_period(
    as_of: datetime,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
) -> tuple[datetime, datetime, str]:
    start, end = rolling_window(as_of)
    proposal = (
        f"Предлагаю период: {start:%d.%m.%Y %H:%M:%S} — "
        f"{end:%d.%m.%Y %H:%M:%S} МСК (конец не включается). "
        "Подходит? [да/нет]: "
    )
    while True:
        answer = read(proposal).strip().casefold()
        if answer in {"да", "д", "yes", "y"}:
            return start, end, "accepted_proposal"
        if answer in {"нет", "н", "no", "n"}:
            break
        write("Ответьте «да» или «нет». Без ответа период не выбирается.")

    while True:
        try:
            first = date.fromisoformat(read("Дата начала (ГГГГ-ММ-ДД): ").strip())
            last = date.fromisoformat(read("Дата окончания включительно (ГГГГ-ММ-ДД): ").strip())
            start, end = date_window(first, last)
        except ValueError as exc:
            write(f"Некорректные даты: {exc}. Введите период заново.")
            continue
        write(f"Выбран период: {first:%d.%m.%Y} — {last:%d.%m.%Y} включительно, МСК.")
        return start, end, "entered_custom_dates"
