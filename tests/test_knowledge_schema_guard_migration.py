"""Репро-тесты миграции mcp_knowledge на schema-guard (план v5, Этап 3).

k1: легаси-БД v4 без file_sync → самолечение service-таблиц БЕЗ дропа FTS-контента
    (t1b-стиль: knowledge_index выживает, sync пересоберёт только изменения).
k2: легаси-БД с неполной схемой knowledge_index (REQUIRED=8 при арности 20 —
    прежняя дыра) → ordered-drift-ребилд: недостающие колонки = drift.
k3: инжект сбоя rebuild → полный ROLLBACK rebuild-транзакции, консистентность.
k4: N×open идемпотентен (rebuild ровно один раз на первом открытии).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mcp_core.fts import SCHEMA_VERSION
from mcp_core.schema_guard import StorageSchemaSpec, ensure_storage_schema
from mcp_knowledge.storage import (
    KNOWLEDGE_FTS_COLUMNS,
    KNOWLEDGE_FTS_UNINDEXED,
    KnowledgeStorage,
    _rebuild_knowledge,
)

import mcp_knowledge.storage as knowledge_storage


def _cols(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row[1] for row in conn.execute(f"PRAGMA table_info({table});").fetchall())


def _object(conn: sqlite3.Connection, name: str) -> tuple[str, str] | None:
    return conn.execute("SELECT name, type FROM sqlite_master WHERE name = ?;", (name,)).fetchone()


def _create_fts5(conn: sqlite3.Connection, columns: list[str]) -> None:
    """Создаёт knowledge_index с заданным списком колонок (как легаси-схема)."""
    unindexed = [c for c in columns if c in KNOWLEDGE_FTS_UNINDEXED]
    col_sql = ", ".join(f"{c} UNINDEXED" if c in unindexed else c for c in columns)
    conn.execute(
        f"CREATE VIRTUAL TABLE knowledge_index USING fts5({col_sql}, "
        "tokenize = 'unicode61 remove_diacritics 2');"
    )


def test_k1_legacy_v4_without_file_sync_self_heals(tmp_path: Path) -> None:
    """Легаси-БД v4 (без conflict-колонки file_sync) — K12/5бис-миграция:
    guard пересоздаёт эфемерный кэш (drift-rebuild по user_version 4 != 5),
    данные восстанавливаются при следующем sync из knowledge_dir.
    v5-схема: file_sync создаётся сразу с conflict-колонкой."""
    db = tmp_path / "knowledge.db"
    conn = sqlite3.connect(db)
    _create_fts5(conn, KNOWLEDGE_FTS_COLUMNS)
    conn.execute("PRAGMA user_version = 4;")
    conn.execute("INSERT INTO knowledge_index(id, title) VALUES('card-1', 'legacy card');")
    conn.commit()
    conn.close()

    storage = KnowledgeStorage(db, knowledge_dir=tmp_path / "knowledge")
    with storage.get_connection() as c:
        # v4→v5 drift-rebuild: эфемерный кэш пересоздан, конфликт-колонка есть
        assert _object(c, "file_sync") is not None
        assert _object(c, "tool_calls") is not None
        cols = [r[1] for r in c.execute("PRAGMA table_info(file_sync)").fetchall()]
        assert "conflict" in cols, cols
        assert _cols(c, "knowledge_index") == tuple(KNOWLEDGE_FTS_COLUMNS)
        assert c.execute("PRAGMA user_version;").fetchone()[0] == SCHEMA_VERSION
        # FTS-контент эфемерен: rebuild очищает (данные на диске не тронуты),
        # восстановление — задача sync()
        rows = [row[0] for row in c.execute("SELECT id FROM knowledge_index;").fetchall()]
        assert rows == [], "v4-кэш обязан пересоздаваться при миграции на v5"


def test_k2_incomplete_columns_is_ordered_drift(tmp_path: Path) -> None:
    """Неполная схема knowledge_index (8 из 20 колонок — прежний REQUIRED-контракт
    пропустил бы это) → guard делает drift-ребилд до полной ordered-схемы."""
    db = tmp_path / "knowledge.db"
    conn = sqlite3.connect(db)
    # легаси-схема: только REQUIRED_FTS_COLUMNS (8 колонок), user_version=4
    _create_fts5(
        conn,
        [
            "generated_by",
            "stale_after",
            "verified_count",
            "confidence",
            "task_id",
            "refutation_reason",
            "created_at",
            "category",
        ],
    )
    conn.execute("PRAGMA user_version = 4;")
    conn.commit()
    conn.close()

    storage = KnowledgeStorage(db, knowledge_dir=tmp_path / "knowledge")
    with storage.get_connection() as c:
        # drift-ребилд восстановил ПОЛНЫЙ ordered-набор из 20 колонок
        assert _cols(c, "knowledge_index") == tuple(KNOWLEDGE_FTS_COLUMNS)
        assert _object(c, "file_sync") is not None
        assert c.execute("PRAGMA user_version;").fetchone()[0] == SCHEMA_VERSION


def test_k3_rebuild_failure_keeps_db_consistent(tmp_path: Path) -> None:
    """Инжект сбоя rebuild: guard откатывает транзакцию целиком — БД консистентна."""
    db = tmp_path / "knowledge.db"
    storage = KnowledgeStorage(db, knowledge_dir=tmp_path / "knowledge")
    with storage.get_connection() as c:
        c.execute("PRAGMA user_version = 3;")  # провоцируем drift при следующем open
        c.commit()

    def _failing_rebuild(c: sqlite3.Connection) -> None:
        _rebuild_knowledge(c)
        raise RuntimeError("injected rebuild failure")

    original = knowledge_storage.KNOWLEDGE_STORAGE_SCHEMA_SPEC.rebuild
    object.__setattr__(
        knowledge_storage,
        "KNOWLEDGE_STORAGE_SCHEMA_SPEC",
        StorageSchemaSpec(
            schema_version=SCHEMA_VERSION,
            fts_checks=knowledge_storage.KNOWLEDGE_STORAGE_SCHEMA_SPEC.fts_checks,
            service_ddl=knowledge_storage.KNOWLEDGE_STORAGE_SCHEMA_SPEC.service_ddl,
            rebuild=_failing_rebuild,
            rebuild_on_missing=knowledge_storage.KNOWLEDGE_STORAGE_SCHEMA_SPEC.rebuild_on_missing,
        ),
    )
    try:
        import pytest

        with pytest.raises(RuntimeError, match="injected rebuild failure"):
            # новый open видит drift (uv=3) и падает в rebuild
            with sqlite3.connect(db) as raw:
                ensure_storage_schema(raw, knowledge_storage.KNOWLEDGE_STORAGE_SCHEMA_SPEC)
    finally:
        object.__setattr__(
            knowledge_storage,
            "KNOWLEDGE_STORAGE_SCHEMA_SPEC",
            StorageSchemaSpec(
                schema_version=SCHEMA_VERSION,
                fts_checks=knowledge_storage.KNOWLEDGE_STORAGE_SCHEMA_SPEC.fts_checks,
                service_ddl=knowledge_storage.KNOWLEDGE_STORAGE_SCHEMA_SPEC.service_ddl,
                rebuild=original,
                rebuild_on_missing=knowledge_storage.KNOWLEDGE_STORAGE_SCHEMA_SPEC.rebuild_on_missing,
            ),
        )

    # Консистентность: схема не «полуразобрана», user_version не поднят
    with sqlite3.connect(db) as c:
        ver = c.execute("PRAGMA user_version;").fetchone()[0]
        assert ver == 3, "сбой rebuild не должен поднимать user_version"
    # следующий open чинит схему штатно
    storage2 = KnowledgeStorage(db, knowledge_dir=tmp_path / "knowledge")
    with storage2.get_connection() as c:
        assert c.execute("PRAGMA user_version;").fetchone()[0] == SCHEMA_VERSION
        assert _cols(c, "knowledge_index") == tuple(KNOWLEDGE_FTS_COLUMNS)


def test_k4_n_open_idempotent(tmp_path: Path) -> None:
    """5×KnowledgeStorage(db): rebuild выполняется ровно один раз (на первом открытии)."""
    db = tmp_path / "knowledge.db"
    calls: list[int] = []

    original = knowledge_storage.KNOWLEDGE_STORAGE_SCHEMA_SPEC.rebuild

    def _counting(c: sqlite3.Connection) -> None:
        calls.append(1)
        original(c)

    object.__setattr__(
        knowledge_storage,
        "KNOWLEDGE_STORAGE_SCHEMA_SPEC",
        StorageSchemaSpec(
            schema_version=SCHEMA_VERSION,
            fts_checks=knowledge_storage.KNOWLEDGE_STORAGE_SCHEMA_SPEC.fts_checks,
            service_ddl=knowledge_storage.KNOWLEDGE_STORAGE_SCHEMA_SPEC.service_ddl,
            rebuild=_counting,
            rebuild_on_missing=knowledge_storage.KNOWLEDGE_STORAGE_SCHEMA_SPEC.rebuild_on_missing,
        ),
    )
    try:
        for _ in range(5):
            KnowledgeStorage(db, knowledge_dir=tmp_path / "knowledge")
    finally:
        object.__setattr__(
            knowledge_storage,
            "KNOWLEDGE_STORAGE_SCHEMA_SPEC",
            StorageSchemaSpec(
                schema_version=SCHEMA_VERSION,
                fts_checks=knowledge_storage.KNOWLEDGE_STORAGE_SCHEMA_SPEC.fts_checks,
                service_ddl=knowledge_storage.KNOWLEDGE_STORAGE_SCHEMA_SPEC.service_ddl,
                rebuild=original,
                rebuild_on_missing=knowledge_storage.KNOWLEDGE_STORAGE_SCHEMA_SPEC.rebuild_on_missing,
            ),
        )
    assert calls == [1], f"ожидался ровно один rebuild, выполнено: {len(calls)}"
