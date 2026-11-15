# 4. Review and publishing

Будущие компоненты: validate.py (программные проверки), review.py (независимая смысловая проверка), renderers/, channels/telegram.py, channels/mattermost.py и delivery.py. Вход — точная ревизия digest и доказательства; выход — review, сообщения и квитанции доставки. Проверка предшествует отправке; delivery хранит состояние отдельных сообщений и предотвращает повторную доставку.
