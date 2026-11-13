"""Репро-тесты миграции mcp_audit на schema-guard (план v5, Этап 2).

t1: легаси-БД (uv=4, без file_sync) → самолечение служебных таблиц guard'ом.
t2: инжект сбоя rebuild → полный ROLLBACK, консистентность БД.
t3: N×open идемпотентен (rebuild ровно один раз на первом открытии).
t4: отсутствие триггеров/artifacts_index (корректные колонки FTS) → drift-ребилд,
    не тихий CREATE (блокер №2: «здоровые колонки при мёртвых триггерах» — не здоровая схема).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mcp_audit.storage import (
    AUDIT_FTS_COLUMNS,
    AuditStorage,
    _rebuild_audit,
)
from mcp_core.fts import SCHEMA_VERSION
from mcp_core.schema_guard import StorageSchemaSpec

import mcp_audit.storage as audit_storage


def _cols(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row[1] for row in conn.execute(f"PRAGMA table_info({table});").fetchall())


def _object(conn: sqlite3.Connection, name: str) -> tuple[str, str] | None:
    return conn.execute("SELECT name, type FROM sqlite_master WHERE name = ?;", (name,)).fetchone()


def test_t1_legacy_uv4_without_file_sync_self_heals(tmp_path: Path) -> None:
    """Легаси-БД v4 без file_sync/artifacts: guard делает drift-ребилд (пустая БД не
    содержит «тихо создаваемых» артефактов — schema-heal = rebuild + service DDL)."""
    db = tmp_path / "audit.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE VIRTUAL TABLE audit_index USING fts5(id, title, symptom, root_cause, "
        "solution, category, status, priority, blocked_by, target_file, symbol, task_id, aliases);"
    )
    conn.execute("PRAGMA user_version = 4;")
    conn.commit()
    # индексируем карточку ДО миграции — drift-ребилд легаси индекса допустим
    # (контракт v5: БД v4 без артефактной схемы = drift, не здоровая схема)
    conn.execute("INSERT INTO audit_index(id, title) VALUES('card-1', 'legacy card');")
    conn.commit()
    conn.close()

    storage = AuditStorage(db)
    with storage.get_connection() as c:
        # служебные таблицы созданы (drift-ребилд + service DDL)
        assert _object(c, "file_sync") is not None
        assert _object(c, "artifacts_file_sync") is not None
        assert _object(c, "artifacts_index") is not None
        # FTS-структура валидна и полна
        assert _cols(c, "audit_index") == tuple(AUDIT_FTS_COLUMNS)
        assert _cols(c, "artifacts_fts") == ("title", "body")
        assert c.execute("PRAGMA user_version;").fetchone()[0] == SCHEMA_VERSION


def test_t1b_legacy_uv4_full_schema_keeps_content(tmp_path: Path) -> None:
    """Легаси-БД v4 с полной здоровой схемой (только без file_sync): самолечение
    service-таблиц БЕЗ дропа FTS — контент audit_index выживает."""
    db = tmp_path / "audit.db"
    storage = AuditStorage(db)
    with storage.get_connection() as c:
        c.execute("DROP TABLE file_sync;")
        # индексируем карточку после сноса служебной таблицы
        c.execute("INSERT INTO audit_index(id, title) VALUES('card-keep', 'survivor');")
        c.commit()
        # файлы — как будто ничего не синхронизировано
        c.execute("DELETE FROM artifacts_file_sync;")
        c.commit()

    storage2 = AuditStorage(db)
    with storage2.get_connection() as c:
        rows = [row["id"] for row in c.execute("SELECT id FROM audit_index;").fetchall()]
        assert "card-keep" in rows, "service-самолечение не должно дропать FTS-контент"
        assert _object(c, "file_sync") is not None
        assert _cols(c, "audit_index") == tuple(AUDIT_FTS_COLUMNS)
        assert c.execute("PRAGMA user_version;").fetchone()[0] == SCHEMA_VERSION


def test_t2_rebuild_failure_keeps_db_consistent(tmp_path: Path) -> None:
    """Инжект сбоя rebuild: guard откатывает транзакцию целиком — БД консистентна."""
    db = tmp_path / "audit.db"
    storage = AuditStorage(db)
    with storage.get_connection() as c:
        c.execute("PRAGMA user_version = 3;")  # провоцируем drift при следующем open
        c.commit()

    def _failing_rebuild(c: sqlite3.Connection) -> None:
        _rebuild_audit(c)
        raise RuntimeError("injected rebuild failure")

    original = audit_storage.AUDIT_STORAGE_SCHEMA_SPEC.rebuild
    object.__setattr__(
        audit_storage,
        "AUDIT_STORAGE_SCHEMA_SPEC",
        StorageSchemaSpec(
            schema_version=SCHEMA_VERSION,
            fts_checks=audit_storage.AUDIT_STORAGE_SCHEMA_SPEC.fts_checks,
            service_ddl=audit_storage.AUDIT_STORAGE_SCHEMA_SPEC.service_ddl,
            rebuild=_failing_rebuild,
            rebuild_on_missing=audit_storage.AUDIT_STORAGE_SCHEMA_SPEC.rebuild_on_missing,
        ),
    )
    try:
        import pytest

        with pytest.raises(RuntimeError, match="injected rebuild failure"):
            # новый open видит drift (uv=3) и падает в rebuild
            with sqlite3.connect(db) as raw:
                from mcp_core.schema_guard import ensure_storage_schema

                ensure_storage_schema(raw, audit_storage.AUDIT_STORAGE_SCHEMA_SPEC)
    finally:
        object.__setattr__(
            audit_storage,
            "AUDIT_STORAGE_SCHEMA_SPEC",
            StorageSchemaSpec(
                schema_version=SCHEMA_VERSION,
                fts_checks=audit_storage.AUDIT_STORAGE_SCHEMA_SPEC.fts_checks,
                service_ddl=audit_storage.AUDIT_STORAGE_SCHEMA_SPEC.service_ddl,
                rebuild=original,
                rebuild_on_missing=audit_storage.AUDIT_STORAGE_SCHEMA_SPEC.rebuild_on_missing,
            ),
        )

    # Консистентность: схема не «полуразобрана», user_version не поднят
    with sqlite3.connect(db) as c:
        ver = c.execute("PRAGMA user_version;").fetchone()[0]
        assert ver == 3, "сбой rebuild не должен поднимать user_version"
        # следующий open чинит схему штатно
    storage2 = AuditStorage(db)
    with storage2.get_connection() as c:
        assert c.execute("PRAGMA user_version;").fetchone()[0] == SCHEMA_VERSION
        assert _cols(c, "audit_index") == tuple(AUDIT_FTS_COLUMNS)


def test_t3_n_open_idempotent(tmp_path: Path) -> None:
    """5×AuditStorage(db): rebuild выполняется ровно один раз (на первом открытии)."""
    db = tmp_path / "audit.db"
    calls: list[int] = []

    original = audit_storage.AUDIT_STORAGE_SCHEMA_SPEC.rebuild

    def _counting(c: sqlite3.Connection) -> None:
        calls.append(1)
        original(c)

    object.__setattr__(
        audit_storage,
        "AUDIT_STORAGE_SCHEMA_SPEC",
        StorageSchemaSpec(
            schema_version=SCHEMA_VERSION,
            fts_checks=audit_storage.AUDIT_STORAGE_SCHEMA_SPEC.fts_checks,
            service_ddl=audit_storage.AUDIT_STORAGE_SCHEMA_SPEC.service_ddl,
            rebuild=_counting,
            rebuild_on_missing=audit_storage.AUDIT_STORAGE_SCHEMA_SPEC.rebuild_on_missing,
        ),
    )
    try:
        for _ in range(5):
            AuditStorage(db)
    finally:
        object.__setattr__(
            audit_storage,
            "AUDIT_STORAGE_SCHEMA_SPEC",
            StorageSchemaSpec(
                schema_version=SCHEMA_VERSION,
                fts_checks=audit_storage.AUDIT_STORAGE_SCHEMA_SPEC.fts_checks,
                service_ddl=audit_storage.AUDIT_STORAGE_SCHEMA_SPEC.service_ddl,
                rebuild=original,
                rebuild_on_missing=audit_storage.AUDIT_STORAGE_SCHEMA_SPEC.rebuild_on_missing,
            ),
        )
    assert calls == [1], f"ожидался ровно один rebuild, выполнено: {len(calls)}"


def test_t4_missing_triggers_with_valid_fts_columns_is_drift(tmp_path: Path) -> None:
    """artifacts_fts с корректными колонками при отсутствующих триггерах → drift-ребилд.

    Блокер №2: прежний алгоритм (drift только по FTS-таблицам) тихо создал бы
    artifacts_index без пересборки artifacts_fts — триггеры так и остались бы
    мёртвыми. Тихий CREATE здесь запрещён.
    """
    db = tmp_path / "audit.db"
    storage = AuditStorage(db)
    # сносим триггеры и artifacts_index, оставляя artifacts_fts с корректными колонками
    with storage.get_connection() as c:
        c.execute("DROP TRIGGER art_ai;")
        c.execute("DROP TRIGGER art_ad;")
        c.execute("DROP TABLE artifacts_index;")
        c.commit()

    storage2 = AuditStorage(db)
    with storage2.get_connection() as c:
        # триггеры восстановлены ребилдом, не тихим CREATE
        for trigger in ("art_ai", "art_ad", "art_au"):
            row = _object(c, trigger)
            assert row is not None and row[1] == "trigger", f"{trigger} должен быть пересоздан"
        assert _object(c, "artifacts_index") is not None
        # FTS-структура валидна
        assert _cols(c, "artifacts_fts") == ("title", "body")
        assert c.execute("PRAGMA user_version;").fetchone()[0] == SCHEMA_VERSION
