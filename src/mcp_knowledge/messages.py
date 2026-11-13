"""Централизованные сообщения, подсказки и шаблоны ответов MCP Knowledge Engine.

Изолирует пользовательские и протокольные тексты от логики use case и хэндлеров.
"""

from __future__ import annotations

__all__ = [
    "DRAFT_QUARANTINE_HINT_TEMPLATE",
    "DUPLICATE_CONFIRMATION_MESSAGE_TEMPLATE",
    "DUPLICATE_REJECTION_MESSAGE_TEMPLATE",
    "PROMOTE_SUCCESS_HINT_TEMPLATE",
    "REPORT_CONFIRMATION_MESSAGE_TEMPLATE",
    "SUSPECT_SUPERSEDED_HINT_TEMPLATE",
    "USER_CONFIRMED_CONFLICT_MESSAGE_TEMPLATE",
    "format_draft_hint",
    "format_duplicate_confirmation",
    "format_duplicate_rejection",
    "format_promote_hint",
    "format_report_confirmation",
    "format_suspect_hint",
    "format_user_confirmed_conflict",
]

# Подсказка для карточки, сохранённой в статусе draft (карантин агентов)
DRAFT_QUARANTINE_HINT_TEMPLATE = (
    "The card '{card_id}' is saved in draft status (quarantine). It is isolated from "
    "the general search until reviewed. Show this card to the user and ask for their "
    "approval to promote it. Never promote the card on your own and never mark it as "
    "human-approved without actual user consent. If the user approves, call "
    "update_knowledge_status(id='{card_id}', action='promote', note='...') with "
    "source='user_confirmed'."
)

# Запрос подтверждения при обнаружении дубликата существующей карточки
DUPLICATE_CONFIRMATION_MESSAGE_TEMPLATE = (
    "Обнаружена похожая существующая карточка '{dup_id}'{snippet_part}. "
    "Пожалуйста, выберите действие:\n"
    "1. Обновление решения: укажите supersedes='{dup_id}'.\n"
    "2. Дополнение нюансов: вызовите update_knowledge_status(id='{dup_id}', action='append_note', note='...').\n"
    "3. Создание независимой карточки: повторите вызов с force=True и confirmed_force=True."
)

# Ошибка при попытке записи дубликата без force=True
DUPLICATE_REJECTION_MESSAGE_TEMPLATE = (
    "Обнаружен дубликат карточки с похожим симптомом: '{dup_id}'. "
    "Если это обновление существующего решения, укажите supersedes='{dup_id}'. "
    "Используйте force=True для создания, если случай принципиально отличается."
)

# Запрос подтверждения при обнаружении контента, похожего на отчёт
REPORT_CONFIRMATION_MESSAGE_TEMPLATE = (
    "Обнаружен текст, похожий на отчёт о работе ({report_reason}). "
    "Карточки базы знаний предназначены для долговременных знаний и архитектурных решений. "
    "Для подтверждения принудительной записи повторите вызов с force=True и confirmed_force=True."
)

# Защитный барьер: конфликт замещения подтверждённой человеком карточки
USER_CONFIRMED_CONFLICT_MESSAGE_TEMPLATE = (
    "Карточка '{supersedes}' подтверждена пользователем (user_confirmed) "
    "и не может быть автоматически вытеснена (auto_inferred). "
    "Требуется явное подтверждение пользователя."
)

# Подсказка при отметке карточки как suspect, если она замещала старую версию
SUSPECT_SUPERSEDED_HINT_TEMPLATE = (
    "Карточка отмечена как suspect. Ранее она вытеснила '{supersedes}'. "
    "Если новое решение не сработало, старую версию можно изучить через "
    "get_knowledge(ids=['{supersedes}']) или search_knowledge(..., include_archived=True)."
)

# Подсказка при успешном промоушене черновика в активную базу
PROMOTE_SUCCESS_HINT_TEMPLATE = (
    "Карточка '{card_id}' успешно переведена из draft в active "
    "и теперь доступна в боевом поиске search_knowledge."
)


def format_draft_hint(card_id: str) -> str:
    """Форматирует подсказку для карточки, сохранённой в карантине черновиков."""
    return DRAFT_QUARANTINE_HINT_TEMPLATE.format(card_id=card_id)


def format_duplicate_confirmation(dup_id: str, snippet: str = "") -> str:
    """Форматирует интерактивный запрос подтверждения дубликата."""
    snippet_part = f' с симптомом: "{snippet}"' if snippet else ""
    return DUPLICATE_CONFIRMATION_MESSAGE_TEMPLATE.format(dup_id=dup_id, snippet_part=snippet_part)


def format_duplicate_rejection(dup_id: str) -> str:
    """Форматирует сообщение об ошибке при обнаружении дубликата."""
    return DUPLICATE_REJECTION_MESSAGE_TEMPLATE.format(dup_id=dup_id)


def format_report_confirmation(report_reason: str) -> str:
    """Форматирует запрос подтверждения записи контента, похожего на отчёт."""
    return REPORT_CONFIRMATION_MESSAGE_TEMPLATE.format(report_reason=report_reason)


def format_user_confirmed_conflict(supersedes: str) -> str:
    """Форматирует ошибку конфликта вытеснения проверенной человеком карточки."""
    return USER_CONFIRMED_CONFLICT_MESSAGE_TEMPLATE.format(supersedes=supersedes)


def format_suspect_hint(supersedes: str) -> str:
    """Форматирует подсказку о возможности отката к вытесненной версии."""
    return SUSPECT_SUPERSEDED_HINT_TEMPLATE.format(supersedes=supersedes)


def format_promote_hint(card_id: str) -> str:
    """Форматирует подсказку после промоушена черновика в статус active."""
    return PROMOTE_SUCCESS_HINT_TEMPLATE.format(card_id=card_id)
