"""Единый error-envelope для MCP-инструментов `mcp_skills`.

Модуль-адаптер над общим движком ``mcp_core/error_envelope``: старые
имена и сигнатуры (``_handle_tool_error``/``_make_error_response``/
``_tool_error_guard``) сохранены для существующих вызовов и тестов
(инвариант §4.1 плана core-dedup). Доменные правила — таблица
``SKILLS_ERROR_RULES`` (данные, не логика).

Модуль намеренно низкоуровневый (импортирует только errors, logger и
низкоуровневые модули mcp_core — без composition-root), как ``db_guard`` в
``mcp_knowledge``: его импортирует server, поэтому здесь не должно быть
зависимостей от composition-root (иначе циклический импорт).
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from mcp_core.error_envelope import ErrorRule
from mcp_core.error_envelope import handle_tool_error as _core_handle_tool_error
from mcp_core.error_envelope import make_error_response as _core_make_error_response
from mcp_core.error_envelope import (
    pool_error_guard as _core_pool_error_guard,
)
from mcp_core.paths import (
    InvalidProjectPathError,
    RootNotAnchoredError,
    SessionProjectMismatchError,
)
from mcp_skills.errors import (
    AmbiguousSessionError,
    AssemblyBudgetExceededError,
    CircularExtensionError,
    FlowNotFoundError,
    GateValidationError,
    InvalidFlowFormatError,
    InvalidSkillFormatError,
    MaxExtensionDepthError,
    MissingRequiredBlockError,
    SessionConflictError,
    SkillAlreadyExistsError,
    SkillNotFoundError,
    SkillsError,
    SpamGateRejectedError,
    StageMismatchError,
)

if TYPE_CHECKING:
    from mcp_core.service_pool import ProjectServicePool
    from mcp_skills.server import SkillsServiceBundle

__all__ = ["_handle_tool_error", "_make_error_response", "_tool_error_guard"]

logger = logging.getLogger("mcp_skills.server")

# Доменные правила маппинга: subclass-исключения раньше базового SkillsError
# (первое isinstance-совпадение выигрывает).
SKILLS_ERROR_RULES: tuple[ErrorRule, ...] = (
    ErrorRule(PermissionError, "PermissionError", "PERMISSION_DENIED", None),
    ErrorRule(FileNotFoundError, "FileNotFoundError", "RESOURCE_NOT_FOUND", None),
    ErrorRule(
        exc=SkillNotFoundError,
        error_type="SkillNotFoundError",
        reason="SKILL_NOT_FOUND",
        log_template=None,
    ),
    ErrorRule(
        exc=CircularExtensionError,
        error_type="CircularExtensionError",
        reason="CIRCULAR_EXTENSION",
        log_template=None,
    ),
    ErrorRule(
        exc=MaxExtensionDepthError,
        error_type="MaxExtensionDepthError",
        reason="MAX_EXTENSION_DEPTH",
        log_template=None,
    ),
    ErrorRule(
        exc=InvalidSkillFormatError,
        error_type="InvalidSkillFormatError",
        reason="INVALID_SKILL_FORMAT",
        log_template=None,
    ),
    ErrorRule(
        exc=FlowNotFoundError,
        error_type="FlowNotFoundError",
        reason="FLOW_NOT_FOUND",
        log_template=None,
    ),
    ErrorRule(
        exc=InvalidFlowFormatError,
        error_type="InvalidFlowFormatError",
        reason="INVALID_FLOW_FORMAT",
        log_template=None,
    ),
    ErrorRule(
        exc=SessionConflictError,
        error_type="SessionConflictError",
        reason="SESSION_CONFLICT",
        log_template=None,
    ),
    ErrorRule(
        exc=GateValidationError,
        error_type="GateValidationError",
        reason="GATE_VALIDATION_ERROR",
        log_template=None,
    ),
    ErrorRule(
        exc=StageMismatchError,
        error_type="StageMismatchError",
        reason="STAGE_MISMATCH",
        log_template=None,
    ),
    ErrorRule(
        exc=AmbiguousSessionError,
        error_type="AmbiguousSessionError",
        reason="AMBIGUOUS_SESSION",
        log_template=None,
    ),
    ErrorRule(
        exc=RootNotAnchoredError,
        error_type="RootNotAnchoredError",
        reason="ROOT_NOT_ANCHORED",
        log_template="Отказ записи: корень проекта не закреплён (%s): %s",
        log_level="warning",
    ),
    ErrorRule(
        exc=InvalidProjectPathError,
        error_type="InvalidProjectPathError",
        reason="INVALID_PROJECT_PATH",
        log_template="Некорректный project_path (%s): %s",
        log_level="warning",
    ),
    ErrorRule(
        exc=SessionProjectMismatchError,
        error_type="SessionProjectMismatchError",
        reason="SESSION_PROJECT_MISMATCH",
        log_template="Несоответствие проекта сессии (%s): %s",
        log_level="warning",
    ),
    ErrorRule(
        exc=SkillAlreadyExistsError,
        error_type="SkillAlreadyExistsError",
        reason="SKILL_ALREADY_EXISTS",
        log_template=None,
    ),
    ErrorRule(
        exc=SpamGateRejectedError,
        error_type="SpamGateRejectedError",
        reason="SPAM_GATE_REJECTED",
        log_template=None,
    ),
    ErrorRule(
        exc=AssemblyBudgetExceededError,
        error_type="AssemblyBudgetExceededError",
        reason="ASSEMBLY_BUDGET_EXCEEDED",
        log_template=None,
    ),
    ErrorRule(
        exc=MissingRequiredBlockError,
        error_type="MissingRequiredBlockError",
        reason="MISSING_REQUIRED_BLOCK",
        log_template=None,
    ),
    ErrorRule(
        exc=SkillsError,
        error_type=None,
        reason="SKILLS_ERROR",
        log_template=None,
    ),
)


def _make_error_response(
    message: str,
    error_type: str,
    reason: str = "INTERNAL_ERROR",
    status: str = "rejected",
) -> dict[str, Any]:
    """Строит единый error-envelope для MCP-ответов (консистентно с mcp_knowledge)."""
    return _core_make_error_response(
        message=message,
        error_type=error_type,
        reason=reason,
        status=status,
    )


def _handle_tool_error(e: Exception, context: str = "") -> dict[str, Any]:
    """Маппер доменных исключений mcp_skills на единый error-envelope."""
    res = _core_handle_tool_error(e, context, rules=SKILLS_ERROR_RULES, logger=logger)
    if isinstance(e, AssemblyBudgetExceededError):
        res["estimated_tokens"] = e.estimated_tokens
        res["budget"] = e.budget
        res["overflow_tokens"] = e.overflow_tokens
        res["remediation"] = e.remediation
    elif isinstance(e, MissingRequiredBlockError):
        res["missing_slots"] = e.missing_slots
    elif isinstance(e, StageMismatchError):
        res["expected"] = e.expected
        res["actual"] = e.actual
        if e.recovery is not None:
            res["recovery"] = e.recovery
    return res


def _tool_error_guard(
    context: str,
    pool: ProjectServicePool[SkillsServiceBundle] | None = None,
) -> Callable[[Callable[..., dict[str, Any]]], Callable[..., dict[str, Any]]]:
    """Декоратор tool-обёртки: маппит исключения доменных функций на error-envelope.

    Домен пакета (``handle`` → :func:`_handle_tool_error`) поверх единого
    каркаса core :func:`mcp_core.error_envelope.pool_error_guard` (Ф2 плана
    core-dedup v4): при наличии пула core-каркас открывает scope аренды на
    весь вызов (``pool.start_scope``) и освобождает его в finally обёртки —
    бандл защищён от эвикции на всё время вызова тула, включая обработку
    ошибок.

    ``functools.wraps`` (внутри core-guard) сохраняет сигнатуру, аннотации
    и docstring, поэтому JSON-схема инструмента, строимая SDK, не меняется.
    """
    handle = functools.partial(_handle_tool_error, context=context)
    return cast(
        "Callable[[Callable[..., dict[str, Any]]], Callable[..., dict[str, Any]]]",
        _core_pool_error_guard(handle, pool),
    )
