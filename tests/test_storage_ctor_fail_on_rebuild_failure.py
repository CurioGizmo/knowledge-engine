"""Заявленное изменение отказа (план v5, Этап 5, задача 3).

Контракт: сбой rebuild валит конструктор Storage на старте — исключение
НЕ маскируется (никакого «живой-но-слепой» режима: прежнее поведение
mcp_knowledge — маскировка StorageUnavailableError в search при поломанной
схеме). Инжект исключения в SPEC.rebuild → конструктор поднимает то же
исключение; БД остаётся консистентной (полный ROLLBACK rebuild-транзакции,
user_version не поднят, схема не «полуразобрана») — следующий старт чинит
её штатно.

Покрыты все три пакета: AuditStorage, KnowledgeStorage, SkillsStorage.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mcp_core.fts import SCHEMA_VERSION
from mcp_core.schema_guard import StorageSchemaSpec

import mcp_audit.storage as audit_storage
import mcp_knowledge.storage as knowledge_storage
import mcp_skills.storage as skills_storage


def _cols(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row[1] for row in conn.execute(f"PRAGMA table_info({table});").fetchall())


def _patch_rebuild(module, spec_name: str, new_rebuild) -> StorageSchemaSpec:
    """Подменяет rebuild-коллбек модульного SPEC (frozen dataclass — через новый SPEC)."""
    spec = getattr(module, spec_name)
    return StorageSchemaSpec(
        schema_version=spec.schema_version,
        fts_checks=spec.fts_checks,
        service_ddl=spec.service_ddl,
        rebuild=new_rebuild,
        rebuild_on_missing=spec.rebuild_on_missing,
    )


class _InjectedRebuildFailure(RuntimeError):
    """Маркерный тип: проверяем, что конструктор поднял именно исходное исключение."""


def _make_failing_ctor_case(module, spec_name: str, fts_columns: tuple[str, ...], fts_table: str):
    """Фабрика параметризации: (module, SPEC-имя, колонки, FTS-таблица)."""

    def run(db: Path, ctor) -> None:
        original = getattr(module, spec_name).rebuild

        def _failing_rebuild(c: sqlite3.Connection) -> None:
            original(c)
            raise _InjectedRebuildFailure("injected rebuild failure")

        spec = _patch_rebuild(module, spec_name, _failing_rebuild)
        object.__setattr__(module, spec_name, spec)
        try:
            # fresh DB = drift → rebuild вызывается в первом же _init_db конструктора
            with pytest.raises(_InjectedRebuildFailure, match="injected rebuild failure"):
                ctor()
        finally:
            object.__setattr__(module, spec_name, _patch_rebuild(module, spec_name, original))

        # БД консистентна: rebuild-транзакция полностью откачена
        with sqlite3.connect(db) as c:
            assert c.execute("PRAGMA user_version;").fetchone()[0] == 0, (
                "сбой rebuild не должен поднимать user_version"
            )
            # схема не «полуразобрана»: FTS-таблица не осталась наполовину созданной
            tables = {row[0] for row in c.execute("SELECT name FROM sqlite_master;").fetchall()}
            assert fts_table not in tables, (
                f"{fts_table} не должна существовать после ROLLBACK сбойного rebuild"
            )

        # следующий старт (без инжекта) чинит схему штатно — retry-семантика отказа
        ctor()
        with sqlite3.connect(db) as c:
            assert _cols(c, fts_table) == fts_columns
            assert c.execute("PRAGMA user_version;").fetchone()[0] == SCHEMA_VERSION

    return run


def test_audit_ctor_fails_on_rebuild_failure(tmp_path: Path) -> None:
    from mcp_audit.storage import AUDIT_FTS_COLUMNS, AuditStorage

    db = tmp_path / "audit.db"
    case = _make_failing_ctor_case(
        audit_storage, "AUDIT_STORAGE_SCHEMA_SPEC", tuple(AUDIT_FTS_COLUMNS), "audit_index"
    )
    case(db, lambda: AuditStorage(db))


def test_knowledge_ctor_fails_on_rebuild_failure(tmp_path: Path) -> None:
    from mcp_knowledge.storage import KNOWLEDGE_FTS_COLUMNS, KnowledgeStorage

    db = tmp_path / "knowledge.db"
    k_dir = tmp_path / "knowledge"
    case = _make_failing_ctor_case(
        knowledge_storage,
        "KNOWLEDGE_STORAGE_SCHEMA_SPEC",
        tuple(KNOWLEDGE_FTS_COLUMNS),
        "knowledge_index",
    )
    case(db, lambda: KnowledgeStorage(db, knowledge_dir=k_dir))


def test_skills_ctor_fails_on_rebuild_failure(tmp_path: Path) -> None:
    from mcp_skills.storage import SKILLS_FTS_COLUMNS, SkillsStorage

    db = tmp_path / "skills.db"
    case = _make_failing_ctor_case(
        skills_storage, "SKILLS_STORAGE_SCHEMA_SPEC", tuple(SKILLS_FTS_COLUMNS), "skills_index"
    )
    case(db, lambda: SkillsStorage(db))
