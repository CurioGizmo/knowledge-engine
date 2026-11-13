"""Tests for Step 15: Unified search dependencies and cache invalidation (P2 / F5-F6).

Scenarios:
1. '40001' finds PostgreSQL resource and associated skill.
2. 'ContextVar' finds reference resource and can be loaded via load_skill.
3. Rename 'python.md' to 'groovy.md' with identical file size invalidates cache and overview reflects 'groovy'.
4. Same-size content modification updates dependency revision and index.
5. Deletion of shared block or reference updates file_sync and index without server restart.
6. Domain skill with local match ranks higher than general skills with shared match.
"""

from __future__ import annotations

import os
from pathlib import Path
import pytest

from mcp_core.paths import resolve_skills_repo_dir
from mcp_skills.models import SkillCard
from mcp_skills.server import load_skill, search_skills
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer


from tests.conftest import get_companion_skills_dir


@pytest.fixture(autouse=True)
def configure_catalog_env(monkeypatch: pytest.MonkeyPatch) -> Path:
    _cat = get_companion_skills_dir()
    repo = _cat if _cat is not None else (Path(__file__).resolve().parents[1].parent / "mcp_skills")
    if not (repo / "skills").is_dir():
        pytest.skip(f"mcp_skills catalog not found at {repo}")
    monkeypatch.setenv("MCP_SKILLS_PATH", str(repo))
    return repo


@pytest.fixture
def catalog_root() -> Path:
    repo = resolve_skills_repo_dir()
    return repo if repo.name != "skills" else repo.parent


@pytest.fixture
def synced_storage(tmp_path: Path, catalog_root: Path) -> tuple[SkillsStorage, SkillsSynchronizer]:
    db_path = tmp_path / "skills.db"
    store = SkillsStorage(db_path)
    skills_dir = catalog_root / "skills"
    sync = SkillsSynchronizer(store, [skills_dir])
    sync.sync()
    return store, sync


def test_search_postgres_error_40001(
    synced_storage: tuple[SkillsStorage, SkillsSynchronizer], catalog_root: Path
) -> None:
    """'40001' находит ресурс PostgreSQL и связанный скил."""
    store, sync = synced_storage
    res = search_skills(
        query="40001", storage=store, synchronizer=sync, skills_dir=catalog_root / "skills"
    )
    assert res["count"] > 0
    names = [r["name"] for r in res["results"]]
    assert "code-review" in names or "plan-review" in names

    # Проверяем, что в matched_resources указан ресурс PostgreSQL
    found_card = next(r for r in res["results"] if r["name"] in ("code-review", "plan-review"))
    assert "matched_resources" in found_card
    matched_ids = [m["resource_id"] for m in found_card["matched_resources"]]
    assert any("postgres" in rid for rid in matched_ids)


def test_search_contextvar_and_load(
    synced_storage: tuple[SkillsStorage, SkillsSynchronizer], catalog_root: Path, tmp_path: Path
) -> None:
    """'ContextVar' из справочника находится и загружается."""
    store, sync = synced_storage
    res = search_skills(
        query="ContextVar", storage=store, synchronizer=sync, skills_dir=catalog_root / "skills"
    )
    assert res["count"] > 0

    # Находим code-review и его ресурс api-architecture.md
    cr = next((r for r in res["results"] if r["name"] == "code-review"), None)
    assert cr is not None
    assert "matched_resources" in cr
    api_res = next(
        (m for m in cr["matched_resources"] if "api-architecture" in m["resource_id"]), None
    )
    assert api_res is not None

    # Загружаем ресурс через load_skill
    loaded = load_skill(
        name="code-review",
        resource_id=api_res["resource_id"],
        storage=store,
        project_root=tmp_path,
    )
    assert loaded["name"] == "code-review"
    assert "ContextVar" in loaded["content"]
    assert loaded["was_truncated"] is False


def test_rename_block_same_size_updates_overview(tmp_path: Path) -> None:
    """После переименования python.md в groovy.md с сохранением размера sync overview показывает groovy."""
    cat_dir = tmp_path / "catalog"
    skills_dir = cat_dir / "skills"
    skill_dir = skills_dir / "my-skill"
    blocks_dir = skill_dir / "blocks" / "backend"
    blocks_dir.mkdir(parents=True)

    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ntitle: My Skill\ncategory: backend\navailable_slots: [backend]\n---\n# My Skill\n<!-- SLOT: backend -->\n",
        encoding="utf-8",
    )
    py_block = blocks_dir / "python.md"
    py_block.write_text("1234567890", encoding="utf-8")  # 10 bytes

    db_path = tmp_path / "skills.db"
    store = SkillsStorage(db_path)
    sync = SkillsSynchronizer(store, [skills_dir])
    sync.sync()

    overview1 = search_skills(
        query="", storage=store, synchronizer=sync, skills_dir=skills_dir, project_root=tmp_path
    )
    assert "python" in overview1["stack_values"]
    assert "groovy" not in overview1["stack_values"]

    # Переименовываем python.md в groovy.md с сохранением РОВНО того же размера (10 bytes)
    groovy_block = blocks_dir / "groovy.md"
    py_block.rename(groovy_block)
    assert groovy_block.stat().st_size == 10

    # Запускаем синхронизацию
    sync.sync()

    # Overview обязан вернуть groovy и не возвращать python
    overview2 = search_skills(
        query="", storage=store, synchronizer=sync, skills_dir=skills_dir, project_root=tmp_path
    )
    assert "groovy" in overview2["stack_values"], f"groovy не найден в {overview2['stack_values']}"
    assert "python" not in overview2["stack_values"]


def test_content_change_same_size_detected(tmp_path: Path) -> None:
    """Смена содержимого файла того же размера распознаётся и обновляет FTS-индекс."""
    cat_dir = tmp_path / "catalog"
    skills_dir = cat_dir / "skills"
    skill_dir = skills_dir / "test-skill"
    blocks_dir = skill_dir / "blocks" / "core"
    blocks_dir.mkdir(parents=True)

    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ntitle: Test Skill\ncategory: general\navailable_slots: [core]\n---\n# Test\n<!-- SLOT: core -->\n",
        encoding="utf-8",
    )
    block_file = blocks_dir / "variant.md"
    block_file.write_text("cat_keyword", encoding="utf-8")  # 11 bytes

    db_path = tmp_path / "skills.db"
    store = SkillsStorage(db_path)
    sync = SkillsSynchronizer(store, [skills_dir])
    sync.sync()

    res1 = store.search_skills("cat_keyword")
    assert any(c.name == "test-skill" for c in res1)

    # Меняем содержимое на dog_keyword того же размера (11 bytes)
    # Искусственно сдвигаем mtime для файловых систем с низким разрешением
    block_file.write_text("dog_keyword", encoding="utf-8")
    new_mtime = block_file.stat().st_mtime + 5.0
    os.utime(block_file, (new_mtime, new_mtime))
    assert block_file.stat().st_size == 11

    sync.sync()

    res2 = store.search_skills("dog_keyword")
    assert any(c.name == "test-skill" for c in res2)
    res_old = store.search_skills("cat_keyword")
    assert not any(c.name == "test-skill" for c in res_old)


def test_deletion_of_shared_or_reference_updates_index(tmp_path: Path) -> None:
    """Удаление связанного ресурса или общего блока очищает индекс без перезапуска сервера."""
    cat_dir = tmp_path / "catalog"
    skills_dir = cat_dir / "skills"
    shared_blocks_dir = cat_dir / "blocks" / "stack-invariants"
    shared_blocks_dir.mkdir(parents=True)

    custom_block = shared_blocks_dir / "custom.md"
    custom_block.write_text("unique_shared_token_xyz", encoding="utf-8")

    skill_dir = skills_dir / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ntitle: My Skill\ncategory: general\navailable_slots: [stack-invariants]\n---\n# My Skill\n<!-- SLOT: stack-invariants -->\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "skills.db"
    store = SkillsStorage(db_path)
    sync = SkillsSynchronizer(store, [skills_dir])
    sync.sync()

    res1 = store.search_skills("unique_shared_token_xyz")
    assert any(c.name == "my-skill" for c in res1)

    # Удаляем общий блок
    custom_block.unlink()

    sync.sync()

    res2 = store.search_skills("unique_shared_token_xyz")
    assert not any(c.name == "my-skill" for c in res2)


def test_domain_skill_ranks_higher_than_general_skill(tmp_path: Path) -> None:
    """Предметный скилл с локальным блоком ранжируется выше общего скилла с унаследованным блоком."""
    cat_dir = tmp_path / "catalog"
    skills_dir = cat_dir / "skills"
    shared_blocks_dir = cat_dir / "blocks" / "storage-slot"
    shared_blocks_dir.mkdir(parents=True)

    # Общий блок содержит query-слово
    shared_file = shared_blocks_dir / "default.md"
    shared_file.write_text("Guidance mentioning database_indexing_perf rules.", encoding="utf-8")

    # Скилл 1: общий скилл в general, только наследует общий блок
    gen_skill = skills_dir / "general-helper"
    gen_skill.mkdir(parents=True)
    (gen_skill / "SKILL.md").write_text(
        "---\nname: general-helper\ntitle: General Helper\ncategory: general\navailable_slots: [storage-slot]\n---\n# Helper\n<!-- SLOT: storage-slot -->\n",
        encoding="utf-8",
    )

    # Скилл 2: предметный скилл в backend-db, имеет локальный блок с этим словом
    db_skill = skills_dir / "db-optimizer"
    (db_skill / "blocks" / "storage-slot").mkdir(parents=True)
    (db_skill / "SKILL.md").write_text(
        "---\nname: db-optimizer\ntitle: DB Optimizer\ncategory: backend-db\navailable_slots: [storage-slot]\n---\n# DB Opt\n<!-- SLOT: storage-slot -->\n",
        encoding="utf-8",
    )
    (db_skill / "blocks" / "storage-slot" / "default.md").write_text(
        "Local deep database_indexing_perf rules.", encoding="utf-8"
    )

    db_path = tmp_path / "skills.db"
    store = SkillsStorage(db_path)
    sync = SkillsSynchronizer(store, [skills_dir])
    sync.sync()

    res = store.search_skills("database_indexing_perf")
    names = [c.name for c in res]
    assert "db-optimizer" in names
    assert "general-helper" in names
    # Предметный db-optimizer обязан идти ПЕРЕД general-helper
    assert names.index("db-optimizer") < names.index("general-helper")
