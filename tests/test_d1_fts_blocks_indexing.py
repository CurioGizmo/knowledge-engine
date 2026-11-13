"""Тесты D1 плана review-fixes: FTS-индексация блоков и имён вариантов.

D1 — текст blocks/<slot>/<variant>.md индексируется в FTS различимо (запрос
находит слово, существующее только в блоке), а имена файлов-вариантов блоков
(значения слотов: mcp/offline/fastapi из blocks/<slot>/<value>.md) попадают в
колонку stack. При этом get_skill возвращает чистый каркас: блоки в сборке
подставляет компилятор с диска — иначе load_skill выдал бы контент блока дважды
(FTS-body + инъекция компилятора).
"""

from __future__ import annotations

from pathlib import Path

from mcp_skills.server import load_skill
from mcp_skills.slot_compiler import SlotCompiler
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer

_SKILL_MD = """---
name: blocky-skill
title: Blocky Skill
description: Skeleton guidance
category: planning
available_slots: [integration, backend]
---
# Blocky Skill

Skeleton body with marker-skeleton-abc.

<!-- SLOT: integration -->

<!-- SLOT: backend -->
"""


def _make_project(tmp_path: Path) -> tuple[SkillsStorage, SkillsSynchronizer, Path]:
    """Фикстура: скилл с блоками; уникальные слова живут ТОЛЬКО в блоках."""
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "blocky-skill"
    (skill_dir / "blocks" / "integration").mkdir(parents=True)
    (skill_dir / "blocks" / "backend").mkdir(parents=True)

    (skill_dir / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    # Слово conftest-marker-xyz существует ТОЛЬКО в блоке, не в каркасе.
    (skill_dir / "blocks" / "integration" / "mcp.md").write_text(
        "Integration MCP guidance with conftest-marker-xyz inside.", encoding="utf-8"
    )
    (skill_dir / "blocks" / "integration" / "offline.md").write_text(
        "Integration offline file-mode guidance.", encoding="utf-8"
    )
    # Вариант fastapi: имя не встречается ни в каркасе, ни в текстах блоков —
    # найти его можно только через колонку stack.
    (skill_dir / "blocks" / "backend" / "fastapi.md").write_text(
        "Web framework async rules.", encoding="utf-8"
    )
    (skill_dir / "blocks" / "backend" / "default.md").write_text(
        "Backend default guidance.", encoding="utf-8"
    )

    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()
    return storage, sync, tmp_path


# ---------------------------------------------------------------------------
# D1: блоки различимо индексируются в content
# ---------------------------------------------------------------------------


def test_d1_block_only_word_is_searchable(tmp_path: Path) -> None:
    """Слово, существующее только в blocks/integration/mcp.md, находится поиском."""
    storage, _sync, _root = _make_project(tmp_path)

    res = storage.search_skills("conftest-marker-xyz")
    names = [c.name for c in res]
    assert "blocky-skill" in names, f"Скилл не найден по слову из блока: {names}"


# ---------------------------------------------------------------------------
# D1: имена вариантов блоков — в колонку stack
# ---------------------------------------------------------------------------


def test_d1_variant_names_in_stack_column(tmp_path: Path) -> None:
    """Имена файлов-вариантов блоков попадают в FTS-колонку stack (без 'default')."""
    storage, _sync, _root = _make_project(tmp_path)

    with storage.get_connection() as conn:
        row = conn.execute("SELECT stack FROM skills_index WHERE name = 'blocky-skill'").fetchone()
    assert row is not None
    stack_vals = set(row["stack"].split())
    assert {"mcp", "offline", "fastapi"} <= stack_vals
    # default — не значение стека, в колонку не попадает
    assert "default" not in stack_vals


def test_d1_variant_name_searchable_when_absent_elsewhere(tmp_path: Path) -> None:
    """Запрос по имени варианта находит скилл, даже если слова нет в каркасе и блоках."""
    storage, _sync, _root = _make_project(tmp_path)

    res = storage.search_skills("fastapi")
    names = [c.name for c in res]
    assert "blocky-skill" in names, f"Скилл не найден по имени варианта блока: {names}"


# ---------------------------------------------------------------------------
# D1: get_skill — чистый каркас, сборка без дублей
# ---------------------------------------------------------------------------


def test_d1_get_skill_returns_clean_skeleton(tmp_path: Path) -> None:
    """get_skill отдаёт чистый каркас: без текста блоков и без FTS-секции блоков."""
    storage, _sync, _root = _make_project(tmp_path)

    found = storage.get_skill("blocky-skill")
    assert found is not None
    _card, body = found
    assert "marker-skeleton-abc" in body
    assert "conftest-marker-xyz" not in body
    assert "Backend default guidance" not in body
    assert "mcp-skills:blocks" not in body


def test_d1_load_skill_no_block_duplication(tmp_path: Path) -> None:
    """load_skill не задваивает контент блока (каркас из FTS + инъекция компилятора)."""
    storage, sync, root = _make_project(tmp_path)

    res = load_skill(
        "blocky-skill",
        stack=["mcp"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    content = res["content"]
    # Блок mcp подставлен ровно один раз, offline-альтернатива не подмешана
    assert content.count("conftest-marker-xyz") == 1
    assert content.count("Integration offline file-mode guidance") == 0
    # default-блок backend (нет backend/mcp.md) — один раз
    assert content.count("Backend default guidance") == 1


def test_d1_compiler_single_injection(tmp_path: Path) -> None:
    """Прямая сборка компилятором: блок подставляется один раз (guard от дубля)."""
    storage, _sync, _root = _make_project(tmp_path)

    compiler = SlotCompiler(storage)
    _card, md = compiler.assemble_skill(
        "blocky-skill", touches=["integration"], project_stack=["mcp"]
    )
    assert md.count("conftest-marker-xyz") == 1
    assert md.count("Integration offline file-mode guidance") == 0
