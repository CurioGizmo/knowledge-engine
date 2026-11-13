"""Доменные исключения для MCP Knowledge Engine."""

from mcp_knowledge.models import StorageUnavailableError


class CardNotFoundError(Exception):
    """Исключение, если карточка с указанным ID не найдена на диске."""

    pass


class UserConfirmedConflictError(Exception):
    """Исключение при попытке вытеснить карточку user_confirmed запросом auto_inferred."""

    pass


class ForbiddenUserConfirmedActionError(Exception):
    """Исключение при попытке оспорить или изменить карточку user_confirmed от имени auto_inferred."""

    pass


class DuplicateCardError(Exception):
    """Исключение при обнаружении дубликата карточки."""

    def __init__(self, existing_id: str):
        super().__init__(f"Обнаружен дубликат карточки: {existing_id}")
        self.existing_id = existing_id


class UncertainDuplicateError(ValueError):
    """Исключение при неопределённости дубликата из-за нехватки данных о причине."""

    def __init__(self, candidate_id: str):
        super().__init__(
            f"Недостаточно данных о причине для сравнения с кандидатом '{candidate_id}' "
            "(исход: uncertain). Требуется ручной разбор."
        )
        self.candidate_id = candidate_id


class ReportLikeContentError(Exception):
    """Исключение при попытке записать отчёт о задаче/сессии вместо долговременного знания."""

    pass


__all__ = [
    "CardNotFoundError",
    "DuplicateCardError",
    "ForbiddenUserConfirmedActionError",
    "ReportLikeContentError",
    "StorageUnavailableError",
    "UncertainDuplicateError",
    "UserConfirmedConflictError",
]
