"""Репро-тесты миграции mcp_skills на schema-guard (план v5, Этап 4).

s1: легаси-БД v4 без file_sync → service-самолечение БЕЗ дропа FTS-контента.
s2: инжект сбоя rebuild → полный ROLLBACK rebuild-транзакции, консистентность.
s3: N×open идемпотентен (rebuild ровно один раз на первом открытии).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mcp_core.fts import SCHEMA_VERSION
from mcp_core.schema_guard import StorageSchemaSpec, ensure_storage_schema
from mcp_skills.storage import SKILLS_FTS_COLUMNS, SkillsStorage, _rebuild_skills

import mcp_skills.storage as skills_storage


def _cols(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row[1] for row in conn.execute(f"PRAGMA table_info({table});").fetchall())


def _object(conn: sqlite3.Connection, name: str) -> tuple[str, str] | None:
    return conn.execute("SELECT name, type FROM sqlite_master WHERE name = ?;", (name,)).fetchone()


def test_s1_legacy_v4_without_file_sync_self_heals(tmp_path: Path) -> None:
    """Легаси-БД v4 с полной здоровой FTS-схемой, но без file_sync:
    guard чинит file_sync самолечением, НЕ дропая FTS-контент."""
    db = tmp_path / "skills.db"
    storage = SkillsStorage(db)
    with storage.get_connection() as c:
        c.execute("DROP TABLE file_sync;")
        c.execute("INSERT INTO skills_index(name, title) VALUES('skill-1', 'legacy skill');")
        c.commit()

    storage2 = SkillsStorage(db)
    with storage2.get_connection() as c:
        # service-самолечение: file_sync создан без drift-ребилда
        assert _object(c, "file_sync") is not None
        # FTS-контент не потерян — самолечение не дропает индекс
        rows = [row[0] for row in c.execute("SELECT name FROM skills_index;").fetchall()]
        assert "skill-1" in rows, "service-самолечение не должно дропать FTS-контент"
        assert _cols(c, "skills_index") == tuple(SKILLS_FTS_COLUMNS)
        assert c.execute("PRAGMA user_version;").fetchone()[0] == SCHEMA_VERSION


def test_s2_rebuild_failure_keeps_db_consistent(tmp_path: Path) -> None:
    """Инжект сбоя rebuild: guard откатывает транзакцию целиком — БД консистентна."""
    db = tmp_path / "skills.db"
    storage = SkillsStorage(db)
    with storage.get_connection() as c:
        c.execute("PRAGMA user_version = 3;")  # провоцируем drift при следующем open
        c.commit()

    def _failing_rebuild(c: sqlite3.Connection) -> None:
        _rebuild_skills(c)
        raise RuntimeError("injected rebuild failure")

    original = skills_storage.SKILLS_STORAGE_SCHEMA_SPEC.rebuild
    object.__setattr__(
        skills_storage,
        "SKILLS_STORAGE_SCHEMA_SPEC",
        StorageSchemaSpec(
            schema_version=SCHEMA_VERSION,
            fts_checks=skills_storage.SKILLS_STORAGE_SCHEMA_SPEC.fts_checks,
            service_ddl=skills_storage.SKILLS_STORAGE_SCHEMA_SPEC.service_ddl,
            rebuild=_failing_rebuild,
            rebuild_on_missing=skills_storage.SKILLS_STORAGE_SCHEMA_SPEC.rebuild_on_missing,
        ),
    )
    try:
        import pytest

        with pytest.raises(RuntimeError, match="injected rebuild failure"):
            # новый open видит drift (uv=3) и падает в rebuild
            with sqlite3.connect(db) as raw:
                ensure_storage_schema(raw, skills_storage.SKILLS_STORAGE_SCHEMA_SPEC)
    finally:
        object.__setattr__(
            skills_storage,
            "SKILLS_STORAGE_SCHEMA_SPEC",
            StorageSchemaSpec(
                schema_version=SCHEMA_VERSION,
                fts_checks=skills_storage.SKILLS_STORAGE_SCHEMA_SPEC.fts_checks,
                service_ddl=skills_storage.SKILLS_STORAGE_SCHEMA_SPEC.service_ddl,
                rebuild=original,
                rebuild_on_missing=skills_storage.SKILLS_STORAGE_SCHEMA_SPEC.rebuild_on_missing,
            ),
        )

    # Консистентность: схема не «полуразобрана», user_version не поднят
    with sqlite3.connect(db) as c:
        ver = c.execute("PRAGMA user_version;").fetchone()[0]
        assert ver == 3, "сбой rebuild не должен поднимать user_version"
    # следующий open чинит схему штатно
    storage2 = SkillsStorage(db)
    with storage2.get_connection() as c:
        assert c.execute("PRAGMA user_version;").fetchone()[0] == SCHEMA_VERSION
        assert _cols(c, "skills_index") == tuple(SKILLS_FTS_COLUMNS)


def test_s3_n_open_idempotent(tmp_path: Path) -> None:
    """5×SkillsStorage(db): rebuild выполняется ровно один раз (на первом открытии)."""
    db = tmp_path / "skills.db"
    calls: list[int] = []

    original = skills_storage.SKILLS_STORAGE_SCHEMA_SPEC.rebuild

    def _counting(c: sqlite3.Connection) -> None:
        calls.append(1)
        original(c)

    object.__setattr__(
        skills_storage,
        "SKILLS_STORAGE_SCHEMA_SPEC",
        StorageSchemaSpec(
            schema_version=SCHEMA_VERSION,
            fts_checks=skills_storage.SKILLS_STORAGE_SCHEMA_SPEC.fts_checks,
            service_ddl=skills_storage.SKILLS_STORAGE_SCHEMA_SPEC.service_ddl,
            rebuild=_counting,
            rebuild_on_missing=skills_storage.SKILLS_STORAGE_SCHEMA_SPEC.rebuild_on_missing,
        ),
    )
    try:
        for _ in range(5):
            SkillsStorage(db)
    finally:
        object.__setattr__(
            skills_storage,
            "SKILLS_STORAGE_SCHEMA_SPEC",
            StorageSchemaSpec(
                schema_version=SCHEMA_VERSION,
                fts_checks=skills_storage.SKILLS_STORAGE_SCHEMA_SPEC.fts_checks,
                service_ddl=skills_storage.SKILLS_STORAGE_SCHEMA_SPEC.service_ddl,
                rebuild=original,
                rebuild_on_missing=skills_storage.SKILLS_STORAGE_SCHEMA_SPEC.rebuild_on_missing,
            ),
        )
    assert calls == [1], f"ожидался ровно один rebuild, выполнено: {len(calls)}"
