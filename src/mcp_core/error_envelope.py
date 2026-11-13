"""Общий error-envelope для MCP-инструментов (mcp_core, stdlib-only).

Строительные блоки единого формата ошибок MCP-ответов, вынесенные из
``mcp_knowledge/server.py`` и ``mcp_skills/error_envelope.py``:

- :func:`make_error_response` — сборка словаря envelope (5 ключей);
- :func:`handle_tool_error` — движок маппинга исключений на envelope по
  таблице правил :class:`ErrorRule`;
- :func:`tool_error_guard` — декоратор tool-обёртки (wraps + try/except).

Модуль намеренно низкоуровневый (только stdlib): доменные классы ошибок
и reason-литерлы передаются ПАКЕТАМИ как данные через
:class:`ErrorRule` — инверсия зависимости, разрывающая цикл импортов
``mcp_core ← mcp_knowledge/mcp_audit/mcp_skills``.

Семантика ветвей движка:

- первое isinstance-совпадение в ``rules`` выигрывает (subclass раньше
  базового — порядок правил значим);
- ``error_type=None`` → ``e.__class__.__name__``;
- ``reason=None`` → runtime-параметр ``validation_reason`` (fallback —
  ``INTERNAL_ERROR``);
- ``log_template=None`` → ветвь не логируется вовсе; для правил с
  шаблоном используется ``log_level`` (``"warning"`` или ``"exception"``);
- встроенная ветвь ``ValueError`` → reason ``value_error_reason``;
- неизвестное исключение → ``logger.exception`` + ``UNEXPECTED_ERROR`` +
  ``error_type=<имя класса>``.
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, NamedTuple, TypeVar, cast

from mcp.types import CallToolResult, TextContent

if TYPE_CHECKING:  # stdlib-only модуль: тип пула — только для аннотаций, без циклического импорта
    from mcp_core.service_pool import ProjectServicePool

__all__ = [
    "ErrorRule",
    "error_envelope_to_tool_result",
    "handle_tool_error",
    "make_error_response",
    "pool_error_guard",
    "tool_error_guard",
]


class ErrorRule(NamedTuple):
    """Правило маппинга доменного исключения на ветку error-envelope.

    Порядок правил в таблице значим: первое isinstance-совпадение
    выигрывает (subclass размещается раньше базового класса).
    """

    exc: type[Exception]
    error_type: str | None
    reason: str | None
    log_template: str | None
    log_level: str = "exception"


def make_error_response(
    message: str,
    error_type: str,
    reason: str = "INTERNAL_ERROR",
    status: str = "rejected",
) -> dict[str, Any]:
    """Строит единый error-envelope для MCP-ответов (ровно 5 ключей)."""
    return {
        "error": True,
        "status": status,
        "reason": reason,
        "message": message,
        "error_type": error_type,
    }


def handle_tool_error(
    e: Exception,
    context: str = "",
    *,
    rules: Sequence[ErrorRule],
    logger: logging.Logger,
    validation_reason: str | None = None,
    value_error_reason: str = "INVALID_ARGUMENTS",
) -> dict[str, Any]:
    """Маппит исключение на error-envelope по таблице ``rules``.

    Движок не знает доменных reason-литералов: ``reason=None`` в правиле
    означает «взять runtime-параметр ``validation_reason``» (ядро —
    нейтральный ``None`` → ``INTERNAL_ERROR``). Доменные ветви с
    ``log_template=None`` не логируются вовсе.
    """
    err_msg = str(e)

    for rule in rules:
        if not isinstance(e, rule.exc):
            continue

        error_type = rule.error_type if rule.error_type is not None else e.__class__.__name__
        reason = rule.reason if rule.reason is not None else validation_reason
        if reason is None:
            reason = "INTERNAL_ERROR"

        if rule.log_template is not None:
            _log_with_level(logger, rule.log_level, rule.log_template, context, e)

        return make_error_response(
            message=err_msg,
            error_type=error_type,
            reason=reason,
        )

    if isinstance(e, ValueError):
        return make_error_response(
            message=err_msg,
            error_type="ValueError",
            reason=value_error_reason,
        )

    logger.exception("Непредвиденная ошибка (%s): %s", context, e)
    return make_error_response(
        message=f"Непредвиденная ошибка: {e}",
        error_type=e.__class__.__name__,
        reason="UNEXPECTED_ERROR",
    )


def _log_with_level(
    logger: logging.Logger,
    level: str,
    template: str,
    context: str,
    e: Exception,
) -> None:
    """Логирует доменную ветвь по шаблону и уровню из правила."""
    if level == "warning":
        logger.warning(template, context, e)
    else:
        logger.exception(template, context, e)


def error_envelope_to_tool_result(envelope: dict[str, Any]) -> CallToolResult:
    """Преобразует доменный error-envelope в CallToolResult с is_error=True.

    Сохраняет структуру envelope в structured_content и сериализованный JSON в content[0].text,
    выставляя флаг ошибки на уровне протокола MCP (is_error=True).
    """
    return CallToolResult(
        is_error=True,
        structured_content=envelope,
        content=[TextContent(type="text", text=json.dumps(envelope, ensure_ascii=False))],
    )


_T = TypeVar("_T", bound=Callable[..., Any])


def tool_error_guard(
    handle: Callable[[Exception], dict[str, Any]],
    on_exit: Callable[[], None] | None = None,
) -> Callable[[_T], _T]:
    """Декоратор tool-обёртки: маппит исключения и доменные ошибки на CallToolResult(is_error=True).

    ``functools.wraps`` сохраняет сигнатуру, аннотации и docstring,
    поэтому JSON-схема инструмента, строимая SDK, не меняется.
    ``on_exit`` (опционально) вызывается в finally ПОСЛЕ тела и обработки
    исключений — точка освобождения арен пула сервисов на конец вызова тула.
    """

    def decorator(fn: _T) -> _T:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                res = fn(*args, **kwargs)
                if isinstance(res, CallToolResult):
                    return res
                if isinstance(res, dict) and res.get("error") is True:
                    return error_envelope_to_tool_result(res)
                return res
            except Exception as e:
                err_envelope = handle(e)
                return error_envelope_to_tool_result(err_envelope)
            finally:
                if on_exit is not None:
                    with contextlib.suppress(Exception):
                        on_exit()

        return cast("_T", wrapper)

    return decorator


def pool_error_guard(
    handle: Callable[[Exception], dict[str, Any]],
    pool: ProjectServicePool[Any] | None,
) -> Callable[[_T], _T]:
    """Guard + scope-аренда пула: ЕДИНЫЙ каркас (Ф2 плана core-dedup v4).

    Поглощает построчные копии ``_tool_error_guard(context, pool)`` из
    ``mcp_audit/server.py`` и ``mcp_skills/error_envelope.py``: при наличии
    пула открывает scope аренды на весь вызов (``pool.start_scope``) и
    освобождает его в finally обёртки — бандл защищён от эвикции на всё
    время вызова тула, включая обработку ошибок.

    ``functools.wraps`` (внутри :func:`tool_error_guard`) сохраняет
    сигнатуру, аннотации и docstring — JSON-схема инструмента не меняется.

    ``pool=None`` → guard без scope (статический/legacy-режим пакета).
    """

    def _guard() -> Callable[[_T], _T]:
        return tool_error_guard(handle)

    if pool is None:
        return _guard()

    def decorator_with_scope(fn: _T) -> _T:
        guard = tool_error_guard(handle, on_exit=pool.release_scope)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            pool.start_scope()
            return guard(fn)(*args, **kwargs)

        return cast("_T", wrapper)

    return decorator_with_scope
