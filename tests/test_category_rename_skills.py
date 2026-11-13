"""Тесты миграции SQL-слоя mcp_skills: domain -> category (Задача 2 мастер-плана)."""

from pathlib import Path

from mcp_skills.models import SkillCard
from mcp_skills.storage import (
    REQUIRED_SKILLS_FTS_COLUMNS,
    SKILLS_BM25_WEIGHTS,
    SkillsStorage,
)
from mcp_skills.synchronizer import SkillsSynchronizer

LEGACY_FTS_SKILLS_CREATE_SQL = """
    CREATE VIRTUAL TABLE skills_index USING fts5(
        name,
        domain UNINDEXED,
        title,
        description,
        stack,
        content,
        metadata UNINDEXED,
        tokenize = 'unicode61 remove_diacritics 2'
    );
"""

FILE_SYNC_SKILLS_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS file_sync (
        path TEXT PRIMARY KEY,
        mtime REAL NOT NULL,
        size INTEGER NOT NULL,
        skill_name TEXT NOT NULL
    );
"""

SKILL_MD = """---
name: writing-plans
title: Writing Plans
description: Plan guidelines
category: planning
available_slots: [backend]
---
# Writing Plans Content
<!-- SLOT: backend -->
"""


def _make_card(name: str = "writing-plans", category: str = "planning") -> SkillCard:
    return SkillCard(
        name=name,
        title=f"Title of {name}",
        description="Standardized feature planning guidelines",
        category=category,
        stack=["react", "fastapi"],
        available_slots=["ui", "backend"],
    )


def test_skills_schema_has_category_unindexed(tmp_path: Path) -> None:
    storage = SkillsStorage(tmp_path / "skills_fts.db")

    with storage.get_connection() as conn:
        rows = conn.execute("PRAGMA table_info(skills_index);").fetchall()
        cols = {row[1] for row in rows}

    assert "category" in cols
    assert "domain" not in cols
    assert "category" in REQUIRED_SKILLS_FTS_COLUMNS
    assert "domain" not in REQUIRED_SKILLS_FTS_COLUMNS


def test_skills_bm25_weight_position() -> None:
    # Колонки схемы: name, category, title, description, stack, content, metadata.
    # Вес category (0.0, UNINDEXED) должен стоять на позиции 1 (0-based).
    assert SKILLS_BM25_WEIGHTS[1] == 0.0
    assert len(SKILLS_BM25_WEIGHTS) == 7


def test_skills_search_category_and_legacy_domain_kwargs(tmp_path: Path) -> None:
    storage = SkillsStorage(tmp_path / "skills_fts.db")
    storage.index_skill(_make_card("writing-plans", "planning"), content="plans content")
    storage.index_skill(_make_card("audio-ml-pipelines", "audio-ml"), content="audio content")

    # Новый kwarg: category
    res_cat = storage.search_skills("", category="planning")
    assert [c.name for c in res_cat] == ["writing-plans"]

    # domain kwarg более не поддерживается (обратная совместимость искоренена)
    import pytest

    with pytest.raises(TypeError):
        storage.search_skills("", domain="planning")  # type: ignore[call-arg]

    # Фильтр по другой категории не находит лишнего
    res_other = storage.search_skills("", category="audio-ml")
    assert [c.name for c in res_other] == ["audio-ml-pipelines"]

    # Комбинация с полнотекстовым запросом
    res_q = storage.search_skills("plans", category="planning")
    assert [c.name for c in res_q] == ["writing-plans"]


def test_skills_cache_drift_check(tmp_path: Path) -> None:
    db_path = tmp_path / "skills_fts.db"

    # 1. Создаём базу со старой схемой (domain) и записями в file_sync
    legacy = SkillsStorage.__new__(SkillsStorage)
    legacy.db_path = db_path
    legacy._init_db()  # создаёт актуальную схему
    # Подменяем на legacy-схему вручную
    import sqlite3

    with legacy.get_connection() as conn:
        conn.execute("DROP TABLE skills_index;")
        conn.execute(LEGACY_FTS_SKILLS_CREATE_SQL)
        conn.execute(
            "INSERT INTO skills_index(name, domain, title, description, stack, content, metadata)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "writing-plans",
                "planning",
                "Writing Plans",
                "desc",
                "stack",
                "content",
                "{}",
            ),
        )
        conn.commit()
        conn.execute(FILE_SYNC_SKILLS_CREATE_SQL)
        conn.execute(
            "INSERT INTO file_sync(path, mtime, size, skill_name) VALUES (?, ?, ?, ?)",
            ("/fake/path/SKILL.md", 1.0, 100, "writing-plans"),
        )
        conn.commit()

    # 2. Создаём директорию скиллов для переиндексации
    skills_dir = tmp_path / ".agents" / "skills"
    s1_dir = skills_dir / "writing-plans"
    s1_dir.mkdir(parents=True)
    (s1_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")

    # 3. Инициализация хранилища поверх legacy-базы: drift-check должен
    #    пересоздать skills_index с category и очистить file_sync
    storage = SkillsStorage(db_path)
    with storage.get_connection() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(skills_index);").fetchall()}
        sync_rows = conn.execute("SELECT COUNT(*) FROM file_sync").fetchone()[0]
        index_rows = conn.execute("SELECT COUNT(*) FROM skills_index").fetchone()[0]
    assert "category" in cols
    assert "domain" not in cols
    assert sync_rows == 0
    assert index_rows == 0

    # 4. После sync() skills_index НЕ остаётся пустым: скилл переиндексирован
    sync = SkillsSynchronizer(storage=storage, skills_dirs=[skills_dir])
    result = sync.sync()
    assert result.indexed_count == 1

    with storage.get_connection() as conn:
        index_rows = conn.execute("SELECT COUNT(*) FROM skills_index").fetchone()[0]
        sync_rows = conn.execute("SELECT COUNT(*) FROM file_sync").fetchone()[0]
    assert index_rows == 1
    assert sync_rows >= 1

    found = storage.search_skills("writing-plans")
    assert len(found) == 1
    assert found[0].name == "writing-plans"
