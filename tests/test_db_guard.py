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


def test_register_and_cleanup_temp_dbs(tmp_path):
    """Проверка регистрации и удаления временных SQLite БД и wal/shm файлов."""
    from mcp_core.db import _TEMP_DBS, cleanup_temp_dbs, register_temp_db

    db_file = tmp_path / "test_unanchored_temp.db"
    wal_file = tmp_path / "test_unanchored_temp.db-wal"
    shm_file = tmp_path / "test_unanchored_temp.db-shm"

    db_file.write_text("db", encoding="utf-8")
    wal_file.write_text("wal", encoding="utf-8")
    shm_file.write_text("shm", encoding="utf-8")

    registered = register_temp_db(db_file)
    assert registered == db_file.resolve()
    assert registered in _TEMP_DBS

    cleanup_temp_dbs()

    assert not db_file.exists()
    assert not wal_file.exists()
    assert not shm_file.exists()
    assert len(_TEMP_DBS) == 0


def test_unanchored_knowledge_and_audit_register_temp_dbs(tmp_path):
    """Проверка, что неанкоренные серверы регистрируют временные базы в mcp_core.db."""
    from mcp_audit.server import create_server as create_audit_server
    from mcp_core.db import _TEMP_DBS, cleanup_temp_dbs
    from mcp_knowledge.server import create_server as create_knowledge_server

    unanchored_dir = tmp_path / "bare_dir"
    unanchored_dir.mkdir()

    # Инициализация без project markers
    k_server = create_knowledge_server(project_root=unanchored_dir)
    a_server = create_audit_server(project_root=unanchored_dir)

    assert len(_TEMP_DBS) >= 2
    # Проверяем наличие временных файлов на диске
    for p in _TEMP_DBS:
        assert "unanchored" in p.name

    cleanup_temp_dbs()
    assert len(_TEMP_DBS) == 0
