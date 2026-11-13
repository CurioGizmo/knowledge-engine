"""Юнит-тесты общего SQLite-guard _raise_sqlite_unavailable (аудит: sqlite-guard ×16)."""

import sqlite3
from unittest.mock import patch

import pytest

from mcp_knowledge.db_guard import _raise_sqlite_unavailable
from mcp_knowledge.errors import StorageUnavailableError


def test_guard_raises_storage_unavailable_error_with_class_name():
    err = sqlite3.OperationalError("database is locked")
    with pytest.raises(StorageUnavailableError) as exc_info:
        _raise_sqlite_unavailable(
            err,
            warn_template="Сбой операции (%s)",
            error_message="Сбой операции",
        )
    assert "OperationalError" in str(exc_info.value)
    assert "Сбой операции" in str(exc_info.value)


def test_guard_chains_original_exception():
    err = sqlite3.OperationalError("no such table")
    with pytest.raises(StorageUnavailableError) as exc_info:
        _raise_sqlite_unavailable(err, warn_template="X (%s)", error_message="X")
    assert exc_info.value.__cause__ is err


def test_guard_warn_template_receives_class_name():
    err = sqlite3.OperationalError("boom")
    with patch("mcp_knowledge.db_guard.logger") as mock_logger:
        with pytest.raises(StorageUnavailableError):
            _raise_sqlite_unavailable(
                err,
                warn_template="Сбой в %s: (%s)",
                warn_params=("search",),
                error_message="X",
            )
        mock_logger.warning.assert_called_once_with("Сбой в %s: (%s)", "search", "OperationalError")


def test_guard_debug_log_without_context():
    err = sqlite3.OperationalError("lock")
    with patch("mcp_knowledge.db_guard.logger") as mock_logger:
        with pytest.raises(StorageUnavailableError):
            _raise_sqlite_unavailable(err, warn_template="X (%s)", error_message="X")
        mock_logger.debug.assert_called_once_with("Детали ошибки SQLite: %s", err)


def test_guard_debug_log_with_context():
    err = sqlite3.OperationalError("corrupt")
    with patch("mcp_knowledge.db_guard.logger") as mock_logger:
        with pytest.raises(StorageUnavailableError):
            _raise_sqlite_unavailable(
                err,
                warn_template="X (%s)",
                debug_context="get_cards_by_ids",
                error_message="X",
            )
        mock_logger.debug.assert_called_once_with(
            "Детали ошибки SQLite в %s: %s", "get_cards_by_ids", err
        )
