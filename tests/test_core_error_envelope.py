"""Юнит-тесты общего error-envelope ``mcp_core/error_envelope`` (фаза 1 плана core-dedup)."""

import logging

import pytest
from mcp.types import CallToolResult

from mcp_core.error_envelope import (
    ErrorRule,
    handle_tool_error,
    make_error_response,
    tool_error_guard,
)


class _DomainError(Exception):
    """Модель доменного исключения (не знает о core)."""


class _SubDomainError(_DomainError):
    """Модель subclass-исключения для проверки порядка правил."""


_RULES = (
    ErrorRule(
        exc=_SubDomainError,
        error_type=None,
        reason="SUB_DOMAIN",
        log_template=None,
    ),
    ErrorRule(
        exc=_DomainError,
        error_type="DomainError",
        reason=None,
        log_template=None,
    ),
)


def test_make_error_response_shape_and_defaults() -> None:
    """Envelope содержит ровно 5 ключей; дефолты INTERNAL_ERROR/rejected."""
    res = make_error_response(message="msg", error_type="Err")
    assert res == {
        "error": True,
        "status": "rejected",
        "reason": "INTERNAL_ERROR",
        "message": "msg",
        "error_type": "Err",
    }
    assert set(res) == {"error", "status", "reason", "message", "error_type"}


def test_first_matching_rule_wins_subclass_before_base() -> None:
    """Первое isinstance-совпадение выигрывает: subclass в таблице раньше базового."""
    res = handle_tool_error(_SubDomainError("boom"), rules=_RULES, logger=logging.getLogger("t"))
    assert res["error_type"] == "_SubDomainError"
    assert res["reason"] == "SUB_DOMAIN"

    res_base = handle_tool_error(_DomainError("boom"), rules=_RULES, logger=logging.getLogger("t"))
    assert res_base["error_type"] == "DomainError"


def test_reason_none_falls_back_to_validation_reason_then_internal() -> None:
    """reason=None → runtime validation_reason; отсутствует → INTERNAL_ERROR."""
    log = logging.getLogger("t")
    res = handle_tool_error(
        _DomainError("x"), rules=_RULES, logger=log, validation_reason="ANTI_SPAM_REJECTED"
    )
    assert res["reason"] == "ANTI_SPAM_REJECTED"

    res_none = handle_tool_error(_DomainError("x"), rules=_RULES, logger=log)
    assert res_none["reason"] == "INTERNAL_ERROR"


def test_value_error_branch_uses_value_error_reason() -> None:
    """Встроенная ValueError-ветвь: error_type=ValueError, reason=value_error_reason."""
    res = handle_tool_error(ValueError("пусто"), rules=_RULES, logger=logging.getLogger("t"))
    assert res["error_type"] == "ValueError"
    assert res["reason"] == "INVALID_ARGUMENTS"

    res_custom = handle_tool_error(
        ValueError("пусто"),
        rules=_RULES,
        logger=logging.getLogger("t"),
        value_error_reason="VALIDATION_FAILED",
    )
    assert res_custom["reason"] == "VALIDATION_FAILED"


def test_fallback_unexpected_error_logs_exception(caplog: pytest.LogCaptureFixture) -> None:
    """Неизвестное исключение: caplog получает ровно одну exception-запись + UNEXPECTED_ERROR."""
    with caplog.at_level(logging.ERROR, logger="mcp_core.test"):
        res = handle_tool_error(
            RuntimeError("внезапно"),
            rules=_RULES,
            logger=logging.getLogger("mcp_core.test"),
        )
    assert res["error_type"] == "RuntimeError"
    assert res["reason"] == "UNEXPECTED_ERROR"
    assert res["message"] == "Непредвиденная ошибка: внезапно"
    exceptions = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(exceptions) == 1
    assert "Непредвиденная ошибка" in exceptions[0].getMessage()


def test_logging_semantics_silent_warning_exception(caplog: pytest.LogCaptureFixture) -> None:
    """log_template=None → ветвь молчит; с шаблоном и warning → ровно одна warning-запись."""
    log = logging.getLogger("mcp_core.test")

    # Доменная ветвь без log_template не логируется вовсе
    with caplog.at_level(logging.DEBUG, logger="mcp_core.test"):
        handle_tool_error(_DomainError("тихо"), rules=_RULES, logger=log)
    assert caplog.records == []

    # Правило с log_template и уровнем warning — ровно одна warning-запись
    warning_rule = (
        ErrorRule(
            exc=_DomainError,
            error_type="DomainError",
            reason="DOMAIN",
            log_template="Хранилище недоступно (%s): %s",
            log_level="warning",
        ),
    )
    with caplog.at_level(logging.WARNING, logger="mcp_core.test"):
        res = handle_tool_error(
            _DomainError("недоступно"), rules=warning_rule, logger=log, context="ctx"
        )
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Хранилище недоступно" in warnings[0].getMessage()
    assert res["reason"] == "DOMAIN"


def test_tool_error_guard_preserves_docstring_and_maps_errors() -> None:
    """tool_error_guard: functools.wraps сохраняет __doc__/__name__; исключение → envelope."""

    def fake_tool(name: str) -> dict[str, object]:
        """Docstring инструмента."""
        if name == "bad":
            raise _DomainError(name)
        return {"name": name}

    def handle(e: Exception) -> dict[str, object]:
        return make_error_response(message=str(e), error_type=type(e).__name__)

    guarded = tool_error_guard(handle)(fake_tool)
    assert guarded.__doc__ == "Docstring инструмента."
    assert guarded.__name__ == "fake_tool"
    assert guarded("x") == {"name": "x"}
    err = guarded("bad")
    assert isinstance(err, CallToolResult)
    assert err.is_error is True
    assert err.structured_content["error"] is True
    assert err.structured_content["error_type"] == "_DomainError"
