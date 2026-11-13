"""Атомарный schema-guard для storage-хранилищ MCP-пакетов.

Контракт (план v5, schema-guard-refactoring-plan.md):
- ordered-сравнение колонок FTS-таблиц (позиционная семантика bm25(tbl, weights));
- транзакционный drift-rebuild: precondition — соединение БЕЗ активной транзакции,
  BEGIN IMMEDIATE с повторным drift-check под lock (двойной rebuild-блокер),
  ROLLBACK только при conn.in_transaction, немаскировка ошибки BEGIN;
- service_ddl выполняется БЕЗУСЛОВНО после rebuild-транзакции как отдельная
  модель отказа (сбой service_ddl не откатывает rebuild);
- rebuild_on_missing: отсутствие (name, kind) в sqlite_master = drift-ребилд.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

logger = logging.getLogger("mcp_core.schema_guard")

__all__ = [
    "FtsSpec",
    "RebuildDependency",
    "SchemaGuardContractError",
    "StorageSchemaSpec",
    "ensure_storage_schema",
]


class SchemaGuardContractError(RuntimeError):
    """Нарушение транзакционного контракта guard'а (например, активная транзакция на входе)."""


@dataclass(frozen=True)
class FtsSpec:
    """Ожидаемая структура FTS-таблицы: ORDERED-кортеж колонок.

    Порядок значим: веса bm25(tbl, weights) позиционны, перестановка колонок
    при неупорядоченном сравнении прошла бы guard, сломав ранжирование.
    """

    expected_columns: tuple[str, ...]


@dataclass(frozen=True)
class RebuildDependency:
    """Типизированная зависимость схемы: существование объекта (name, kind).

    kind — тип объекта в sqlite_master ("table" | "trigger" | "view").
    Объект правильного имени, но неправильного типа считается отсутствующим.
    """

    name: str
    kind: str


@dataclass(frozen=True)
class StorageSchemaSpec:
    """Декларативная спецификация схемы хранилища.

    - fts_checks: ordered-сравнение tuple(PRAGMA table_info) с expected_columns.
    - service_ddl: идемпотентные CREATE ... IF NOT EXISTS (после rebuild-транзакции).
    - rebuild: пакетный module-level callback: DROP-ы + FTS-DDL + триггеры.
    - rebuild_on_missing: отсутствие ЭТИХ объектов (name+kind в sqlite_master)
      → drift-ребилд (не тихий CREATE).
    """

    schema_version: int
    fts_checks: Mapping[str, FtsSpec]
    service_ddl: Sequence[str]
    rebuild: Callable[[sqlite3.Connection], None]
    rebuild_on_missing: frozenset[RebuildDependency] = field(default_factory=frozenset)


def _table_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    """ORDERED-кортеж имён колонок таблицы (пустой, если таблицы нет)."""
    rows = conn.execute(f"PRAGMA table_info({table});").fetchall()
    return tuple(row[1] for row in rows)


def _object_exists(conn: sqlite3.Connection, dep: RebuildDependency) -> bool:
    """True, если в sqlite_master есть объект с точным (name, kind)."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? AND type = ?;",
        (dep.name, dep.kind),
    ).fetchone()
    return row is not None


def _has_drift(conn: sqlite3.Connection, spec: StorageSchemaSpec) -> bool:
    """Полный drift-check: user_version, FTS-колонки (ordered), rebuild_on_missing."""
    version = conn.execute("PRAGMA user_version;").fetchone()[0]
    if version != spec.schema_version:
        return True

    for table, fts_spec in spec.fts_checks.items():
        actual = _table_columns(conn, table)
        if not actual or actual != tuple(fts_spec.expected_columns):
            return True

    return any(not _object_exists(conn, dep) for dep in spec.rebuild_on_missing)


def ensure_storage_schema(conn: sqlite3.Connection, spec: StorageSchemaSpec) -> None:
    """Гарантирует соответствие схемы conn спецификации spec.

    Последовательность:
    1. Быстрый drift-check без лока.
    2. При drift — транзакционный контракт:
       - precondition: conn.in_transaction → SchemaGuardContractError
         (иначе вложенный BEGIN недетерминирован);
       - BEGIN IMMEDIATE (isolation_level сохраняется и восстанавливается в finally);
       - ошибка самого BEGIN IMMEDIATE (lock timeout) прокидывается как есть,
         БЕЗ ROLLBACK-ветки (ROLLBACK «no transaction is active» замаскировал бы её);
       - повторный drift-check под lock (другой процесс мог пересобрать, пока
         мы ждали lock — защита от двойного rebuild);
       - drift остался → spec.rebuild(conn) → PRAGMA user_version → COMMIT;
         сбой внутри → ROLLBACK только если conn.in_transaction → re-raise;
         drift исчез под lock → COMMIT пустой транзакции.
    3. Безусловно ПОСЛЕ rebuild-транзакции: spec.service_ddl — самолечение
       service-таблиц.

    Модель отказа service_ddl ОТДЕЛЬНАЯ от rebuild: сбой service_ddl не откатывает
    rebuild (транзакция уже закоммичена) и оставляет состояние «FTS валиден,
    служебные таблицы отсутствуют» — детектируется следующим открытием (service_ddl
    идемпотентен). Ошибка service_ddl прокидывается вызывающему.
    """
    if not _has_drift(conn, spec):
        # Быстрый путь: схема здорова — только service-самолечение (не дропает FTS).
        _apply_service_ddl(conn, spec)
        return

    if conn.in_transaction:
        raise SchemaGuardContractError(
            "ensure_storage_schema требует соединение без активной транзакции "
            "(conn.in_transaction = True): вложенный BEGIN IMMEDIATE недетерминирован."
        )

    cursor = conn.cursor()
    old_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        cursor.execute("BEGIN IMMEDIATE;")
    except Exception:
        # Ошибка BEGIN (например, lock timeout) — прокинуть исходную ошибку,
        # ROLLBACK не выполняется: транзакции нет.
        conn.isolation_level = old_isolation
        raise

    try:
        # Повторный drift-check ПОД lock: конкурирующий процесс мог пересобрать,
        # пока мы ждали BEGIN IMMEDIATE.
        if _has_drift(conn, spec):
            spec.rebuild(conn)
            cursor.execute(f"PRAGMA user_version = {spec.schema_version};")
            cursor.execute("COMMIT;")
        else:
            cursor.execute("COMMIT;")
    except Exception:
        if conn.in_transaction:
            cursor.execute("ROLLBACK;")
        raise
    finally:
        conn.isolation_level = old_isolation

    # Отдельная модель отказа: сбой service_ddl не откатывает rebuild.
    _apply_service_ddl(conn, spec)


def _apply_service_ddl(conn: sqlite3.Connection, spec: StorageSchemaSpec) -> None:
    cursor = conn.cursor()
    for ddl in spec.service_ddl:
        cursor.execute(ddl)
