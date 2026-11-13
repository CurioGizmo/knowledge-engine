"""Локальная adoption-телеметрия вызовов инструментов (таблица tool_calls в SQLite).

Отделена от read-модели ``KnowledgeStorage``:
- ``TelemetryMixin`` добавляет методы записи/чистки/агрегации к классу хранилища
  (публичный API v0.2.0: ``record_tool_call`` / ``prune_tool_calls`` / ``get_tool_call_stats``);
- ``read_tool_call_stats`` — read-only доступ для CLI (``python -m mcp_knowledge.stats``),
  безопасный для «живой» базы сервера.
"""

import json
import sqlite3
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from mcp_knowledge.db_guard import _raise_sqlite_unavailable
from mcp_knowledge.errors import StorageUnavailableError
from mcp_knowledge.logger import logger

__all__ = ["TelemetryMixin", "read_tool_call_stats"]

_TOOL_CALLS_RETENTION = 10000
_TOOL_CALLS_PRUNE_EVERY = 500

TOOL_CALLS_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS tool_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tool_name TEXT NOT NULL,
        args TEXT,
        called_at TEXT NOT NULL,
        latency_ms REAL,
        error TEXT
    );
    """

_TOOL_CALL_STATS_SQL = """
    SELECT
        tool_name,
        COUNT(*) as total_calls,
        SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as error_calls,
        ROUND(AVG(latency_ms), 2) as avg_latency_ms,
        ROUND(MAX(latency_ms), 2) as max_latency_ms
    FROM tool_calls
    GROUP BY tool_name
    ORDER BY total_calls DESC;
    """


class _ConnectionProvider(Protocol):
    """Структурный контракт хоста миксина: подключение к SQLite с PRAGMA."""

    def get_connection(self) -> AbstractContextManager[sqlite3.Connection]: ...


def _rows_to_tool_call_stats(rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [
        {
            "tool_name": str(row[0]),
            "total_calls": int(row[1]),
            "error_calls": int(row[2]),
            "avg_latency_ms": float(row[3]) if row[3] is not None else 0.0,
            "max_latency_ms": float(row[4]) if row[4] is not None else 0.0,
        }
        for row in rows
    ]


def read_tool_call_stats(db_path: Path) -> list[dict[str, Any]]:
    """Read-only чтение статистики телеметрии — без мутации БД и файловой системы.

    Открывает соединение в режиме ``mode=ro`` и не создаёт каталоги/таблицы,
    поэтому безопасно для «живой» базы сервера (в отличие от ``KnowledgeStorage``,
    конструктор которого делает mkdir/WAL/CREATE TABLE).
    """
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as e:
        logger.warning("Сбой открытия телеметрии read-only (%s)", type(e).__name__)
        logger.debug("Детали ошибки SQLite в read_tool_call_stats: %s", e)
        raise StorageUnavailableError(
            f"Не удалось открыть статистику телеметрии ({type(e).__name__})"
        ) from e
    try:
        cursor = conn.cursor()
        cursor.execute(_TOOL_CALL_STATS_SQL)
        rows = cursor.fetchall()
    except sqlite3.OperationalError as e:
        _raise_sqlite_unavailable(
            e,
            warn_template="Сбой read-only чтения статистики телеметрии (%s)",
            debug_context="read_tool_call_stats",
            error_message="Не удалось прочитать статистику телеметрии",
        )
    finally:
        conn.close()
    return _rows_to_tool_call_stats(rows)


_PRUNE_TOOL_CALLS_SQL = """
                    DELETE FROM tool_calls
                    WHERE id NOT IN (
                        SELECT id FROM tool_calls ORDER BY id DESC LIMIT ?
                    );
                    """


def _prune_tool_calls(cursor: sqlite3.Cursor, max_records: int) -> None:
    """Выполняет pruning телеметрии, оставляя последние max_records записей."""
    cursor.execute(_PRUNE_TOOL_CALLS_SQL, (max_records,))


class TelemetryMixin:
    """Методы adoption-телеметрии для классов с ``get_connection()`` (см. _ConnectionProvider)."""

    def record_tool_call(
        self: _ConnectionProvider,
        tool_name: str,
        args: dict[str, Any] | None = None,
        latency_ms: float = 0.0,
        error: str | None = None,
    ) -> None:
        """Фиксирует вызов инструмента для метрики принятия (Adoption Telemetry)."""
        try:
            now_iso = datetime.now(UTC).isoformat()
            args_json = json.dumps(args, ensure_ascii=False) if args else None
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO tool_calls (tool_name, args, called_at, latency_ms, error)
                    VALUES (?, ?, ?, ?, ?);
                    """,
                    (tool_name, args_json, now_iso, round(latency_ms, 2), error),
                )
                # Оппортунистический pruning: раз в N вставок (без отдельного SELECT count)
                last_id = cursor.lastrowid
                if last_id and last_id % _TOOL_CALLS_PRUNE_EVERY == 0:
                    _prune_tool_calls(cursor, _TOOL_CALLS_RETENTION)
                conn.commit()
        except Exception as e:
            logger.warning("Не удалось записать adoption telemetry (%s)", type(e).__name__)
            logger.debug("Детали ошибки записи телеметрии: %s", e)

    def prune_tool_calls(
        self: _ConnectionProvider, max_records: int = _TOOL_CALLS_RETENTION
    ) -> int:
        """Очищает устаревшие записи телеметрии, оставляя последние max_records.

        Возвращает количество удаленных строк.
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                _prune_tool_calls(cursor, max_records)
                deleted = cursor.rowcount
                conn.commit()
                return deleted
        except sqlite3.OperationalError as e:
            logger.warning("Сбой pruning телеметрии (%s)", type(e).__name__)
            logger.debug("Детали ошибки SQLite в prune_tool_calls: %s", e)
            return 0

    def get_tool_call_stats(self: _ConnectionProvider) -> list[dict[str, Any]]:
        """Возвращает агрегированную статистику использования инструментов (Adoption Telemetry)."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(_TOOL_CALL_STATS_SQL)
                rows = cursor.fetchall()
                return _rows_to_tool_call_stats(rows)
        except sqlite3.OperationalError as e:
            _raise_sqlite_unavailable(
                e,
                warn_template="Сбой чтения статистики телеметрии (%s)",
                debug_context="get_tool_call_stats",
                error_message="Не удалось прочитать статистику телеметрии",
            )
