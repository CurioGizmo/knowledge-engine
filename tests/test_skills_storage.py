"""Тесты для SQLite FTS5 хранилища и синхронизатора mcp_skills."""

from pathlib import Path

from mcp_skills.models import SkillCard
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer


def test_storage_crud_and_fts_search(tmp_path: Path) -> None:
    db_path = tmp_path / "skills_fts.db"
    storage = SkillsStorage(db_path)

    card1 = SkillCard(
        name="writing-plans",
        title="Writing Plans",
        description="Standardized feature planning guidelines",
        category="planning",
        stack=["react", "fastapi"],
        available_slots=["ui", "backend"],
    )
    card2 = SkillCard(
        name="audio-ml-processing",
        title="Audio ML Processing",
        description="Whisper and PyTorch audio pipelines",
        category="audio-ml",
        stack=["python", "pytorch"],
        available_slots=["model"],
    )

    storage.index_skill(card1, content="# Writing Plans\nAlways write plans before coding.")
    storage.index_skill(card2, content="# Audio ML\nHandle audio streams.")

    # 1. Search by exact name
    res = storage.search_skills("writing-plans")
    assert len(res) == 1
    assert res[0].name == "writing-plans"

    # 2. Search by keyword in title/description
    res = storage.search_skills("Whisper PyTorch")
    assert len(res) == 1
    assert res[0].name == "audio-ml-processing"

    # 3. Search with category filter
    res = storage.search_skills("", category="planning")
    assert len(res) == 1
    assert res[0].name == "writing-plans"

    # 4. Get by name
    card_ret, body_ret = storage.get_skill("writing-plans")
    assert card_ret is not None
    assert card_ret.name == "writing-plans"
    assert "Always write plans before coding" in body_ret

    # 5. Delete
    storage.delete_skill("writing-plans")
    assert storage.get_skill("writing-plans") is None


def test_synchronizer_discovers_skills(tmp_path: Path) -> None:
    db_path = tmp_path / "skills_fts.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / ".agents" / "skills"
    skills_dir.mkdir(parents=True)

    # Создаем скилл 1
    s1_dir = skills_dir / "writing-plans"
    s1_dir.mkdir()
    (s1_dir / "SKILL.md").write_text(
        """---
name: writing-plans
title: Writing Plans
description: Plan guidelines
category: planning
available_slots: [backend]
---
# Writing Plans Content
<!-- SLOT: backend -->
""",
        encoding="utf-8",
    )

    sync = SkillsSynchronizer(storage=storage, skills_dirs=[skills_dir])
    res_sync = sync.sync()
    assert res_sync.indexed_count == 1
    assert res_sync.removed_count == 0

    # Проверяем, что скилл появился в БД
    found = storage.search_skills("writing-plans")
    assert len(found) == 1

    # Вторичный sync без изменений ничего не должен повторно индексировать
    res_sync2 = sync.sync()
    assert res_sync2.indexed_count == 0

    # Удаляем файл и проверяем, что синхронизатор удалит запись из базы
    (s1_dir / "SKILL.md").unlink()
    res_sync3 = sync.sync()
    assert res_sync3.removed_count == 1
    assert len(storage.search_skills("writing-plans")) == 0
