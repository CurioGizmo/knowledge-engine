"""Доменные исключения для пакета mcp_skills."""

from __future__ import annotations

from typing import Any


class SkillsError(Exception):
    """Базовое исключение для ошибок mcp_skills."""


class SkillNotFoundError(SkillsError):
    """Скилл не найден в локальном проекте или глобальном кэше."""


class CircularExtensionError(SkillsError):
    """Обнаружена циклическая зависимость в extends скилла."""


class InvalidSkillFormatError(SkillsError):
    """Некорректный формат манифеста или файла скилла."""


class MaxExtensionDepthError(SkillsError):
    """Превышена максимальная глубина наследования скиллов."""


class FlowError(SkillsError):
    """Базовое исключение для ошибок подсистемы Flow."""


class FlowNotFoundError(FlowError):
    """Сценарий флоу не найден."""


class SessionNotFoundError(FlowNotFoundError):
    """Активная сессия флоу не найдена."""


class InvalidFlowFormatError(FlowError):
    """Некорректный формат сценария флоу, метаданных или этапов."""


class SessionConflictError(FlowError):
    """Конфликт параллельного изменения сессии флоу или недопустимый статус."""


class GateValidationError(FlowError):
    """Ошибка валидации шлюза качества этапа флоу."""


class StageMismatchError(FlowError):
    """Рассинхронизация этапа: переданный expected_stage_id не совпадает с текущим этапом сессии."""

    def __init__(
        self,
        message: str,
        expected: str | None = None,
        actual: str | None = None,
        recovery: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.expected = expected
        self.actual = actual
        self.recovery = recovery


class AmbiguousSessionError(FlowError):
    """Обнаружено несколько активных сессий флоу при отсутствии явного session_id."""


class SkillAlreadyExistsError(SkillsError):
    """Скилл с таким именем уже существует при overwrite=False."""

    def __init__(
        self, message: str, skill_name: str, existing_card: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.skill_name = skill_name
        self.existing_card = existing_card or {}


class SpamGateRejectedError(SkillsError):
    """Контент скилла отклонён проверкой SpamGate (секреты, токены, лимит размера)."""

    def __init__(self, message: str, issues: list[str] | None = None) -> None:
        super().__init__(message)
        self.issues = issues or []


class AssemblyBudgetExceededError(SkillsError):
    """Превышение бюджета токенов обязательным содержимым при сборке."""

    def __init__(
        self,
        message: str,
        estimated_tokens: int = 0,
        budget: int = 0,
        remediation: str | None = None,
    ) -> None:
        super().__init__(message)
        self.estimated_tokens = estimated_tokens
        self.budget = budget
        self.overflow_tokens = max(0, estimated_tokens - budget)
        self.remediation = remediation or (
            "Уменьшите выбор секций touches, исключите необязательные слоты или "
            "используйте курсорное чтение ресурсов через load_skill(resource_id=..., cursor=...)."
        )


class UnsupportedReadonlyProfileError(SkillsError):
    """R2 (ревизия 4): строгий read-only профиль отклонён при экспорте в OpenCode.

    Wildcard-запрет tools: {"*": false} не гарантирует запрет неизвестных
    инструментов в неподтверждённой версии клиента. Решение владельца
    профиля: permissions.write=True или readonly_acknowledged=true после
    фактической проверки read-only прав в целевой версии OpenCode.
    """


class MissingRequiredBlockError(SkillsError):
    """Отсутствует блок для обязательной секции скилла."""

    def __init__(self, message: str, missing_slots: list[str] | None = None) -> None:
        super().__init__(message)
        self.missing_slots = missing_slots or []


__all__ = [
    "AmbiguousSessionError",
    "AssemblyBudgetExceededError",
    "CircularExtensionError",
    "FlowError",
    "FlowNotFoundError",
    "GateValidationError",
    "InvalidFlowFormatError",
    "InvalidSkillFormatError",
    "MaxExtensionDepthError",
    "MissingRequiredBlockError",
    "SessionConflictError",
    "SessionNotFoundError",
    "SkillAlreadyExistsError",
    "SkillNotFoundError",
    "SkillsError",
    "SpamGateRejectedError",
    "StageMismatchError",
    "UnsupportedReadonlyProfileError",
]
