"""Тесты нормализации stack и семантики слотов в load_skill (план v3 + автодетект).

Группы:
- A — нормализация стека (CSV/список/tuple/пустой/мусорный тип/дедуп);
- B — семантика слотов и стека (фикс fe8e/B-1: стек НЕ подменяет слоты; B3/B9-B13 автодетект);
- C — fail-fast валидация (traversal, кириллица, CamelCase, несуществующий скилл);
- D — MCP-уровень (строковый stack через call_tool, схемы create_skill);
- E — defense-in-depth компилятора (прямые вызовы).
"""

import json
from pathlib import Path

import pytest

from mcp_skills.server import create_server, load_skill
from mcp_skills.slot_compiler import SlotCompiler
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer

SKILL_MD = """---
name: writing-plans
title: Writing Plans
category: planning
available_slots: [ui, backend]
---
# Writing Plans

<!-- SLOT: ui -->

<!-- SLOT: backend -->

Follow checklist.
"""


def _make_project(tmp_path: Path) -> tuple[SkillsStorage, SkillsSynchronizer, Path]:
    """Фикстура: скилл со слотами и блоками default/react/pydantic."""
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)

    skill_dir = skills_dir / "writing-plans"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")

    blocks = skill_dir / "blocks"
    for slot in ("ui", "backend"):
        (blocks / slot).mkdir(parents=True)
        (blocks / slot / "default.md").write_text(
            f"### {slot} default block\nGeneric guidance.", encoding="utf-8"
        )
    (blocks / "ui" / "react.md").write_text(
        "### React UI Guidelines\nUse React 18 hooks.", encoding="utf-8"
    )
    (blocks / "ui" / "pydantic.md").write_text(
        "### Pydantic block\nAuto-detected trap.", encoding="utf-8"
    )

    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()
    return storage, sync, tmp_path


# ---------- A. Нормализация стека ----------


def test_a1_csv_string_equivalent_to_list(tmp_path: Path) -> None:
    """A1: CSV "python,fastapi" эквивалентен списку ["python","fastapi"]."""
    storage, sync, root = _make_project(tmp_path)
    res_csv = load_skill(
        "writing-plans",
        stack="python,fastapi",
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    res_list = load_skill(
        "writing-plans",
        stack=["python", "fastapi"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    assert res_csv["content"] == res_list["content"]


def test_a2_csv_with_spaces(tmp_path: Path) -> None:
    """A2: CSV с пробелами нормализуется так же."""
    storage, sync, root = _make_project(tmp_path)
    res = load_skill(
        "writing-plans",
        stack=" python , fastapi ",
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    res_clean = load_skill(
        "writing-plans",
        stack="python,fastapi",
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    assert res["content"] == res_clean["content"]


def test_a3_empty_stack_equivalent_to_none(tmp_path: Path) -> None:
    """A3: stack="" и stack=[] ведут себя как stack=None."""
    storage, sync, root = _make_project(tmp_path)
    res_none = load_skill(
        "writing-plans", stack=None, storage=storage, synchronizer=sync, project_root=root
    )
    res_empty_str = load_skill(
        "writing-plans", stack="", storage=storage, synchronizer=sync, project_root=root
    )
    res_empty_list = load_skill(
        "writing-plans", stack=[], storage=storage, synchronizer=sync, project_root=root
    )
    assert res_empty_str["content"] == res_none["content"]
    assert res_empty_list["content"] == res_none["content"]


def test_a4_scalar_bad_type_raises(tmp_path: Path) -> None:
    """A4: stack=123 → ValueError (fail-fast, не тихая деградация)."""
    storage, sync, root = _make_project(tmp_path)
    with pytest.raises(ValueError, match="Ожидался список строк"):
        load_skill(
            "writing-plans", stack=123, storage=storage, synchronizer=sync, project_root=root
        )  # type: ignore[arg-type]


def test_a5_dedup_and_non_str_items(tmp_path: Path) -> None:
    """A5: дедуп стека (юнит-уровень) и нестроковые элементы списка → ValueError."""
    from mcp_skills.server import _validate_stack_items

    # Дедуп наблюдаем только на юнит-уровне: через load_skill-контент
    # дубликат tech не меняет результат (инъекция идёт по слотам, не по tech).
    assert _validate_stack_items(["python", "python"]) == ["python"]
    assert _validate_stack_items(["python", "fastapi", "python"]) == ["python", "fastapi"]

    storage, sync, root = _make_project(tmp_path)
    with pytest.raises(ValueError, match="список строк"):
        load_skill(
            "writing-plans", stack=[123], storage=storage, synchronizer=sync, project_root=root
        )  # type: ignore[list-item]


def test_a6_tuple_stack_equivalent_to_list(tmp_path: Path) -> None:
    """A6: tuple ("python","fastapi") эквивалентен списку."""
    storage, sync, root = _make_project(tmp_path)
    res_tuple = load_skill(
        "writing-plans",
        stack=("python", "fastapi"),
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    res_list = load_skill(
        "writing-plans",
        stack=["python", "fastapi"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    assert res_tuple["content"] == res_list["content"]


# ---------- B. Семантика слотов/стека ----------


def test_b1_stack_selects_tech_not_slots(tmp_path: Path) -> None:
    """B1 (регрессия B-1/fe8e): stack=["react"] выбирает blocks/ui/react.md, а не подменяет слоты."""
    storage, sync, root = _make_project(tmp_path)
    res = load_skill(
        "writing-plans", stack=["react"], storage=storage, synchronizer=sync, project_root=root
    )
    assert "React UI Guidelines" in res["content"]
    # backend-слот не имеет react-блока — заполняется default, а не мусором
    assert "backend default block" in res["content"]
    # Слоты из стека не появляются как спецификация в конце
    assert "Спецификация реализации: react" not in res["content"]


def test_b2_no_stack_fills_defaults(tmp_path: Path) -> None:
    """B2: stack=None → default-блоки во всех слотах, сырых маркеров нет."""
    storage, sync, root = _make_project(tmp_path)
    res = load_skill("writing-plans", storage=storage, synchronizer=sync, project_root=root)
    assert "ui default block" in res["content"]
    assert "backend default block" in res["content"]
    assert "<!-- SLOT:" not in res["content"]


def test_b3_explicit_stack_not_overridden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """B3: явный стек не перекрывается автодетектом pyproject.toml."""
    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)
    storage, sync, root = _make_project(tmp_path)
    (root / "pyproject.toml").write_text(
        '[project]\ndependencies = ["pydantic>=2"]\n', encoding="utf-8"
    )
    # Явный стек: react, pydantic не участвует, stack_source == "explicit"
    res_react = load_skill(
        "writing-plans", stack=["react"], storage=storage, synchronizer=sync, project_root=root
    )
    assert "React UI Guidelines" in res_react["content"]
    assert "Auto-detected trap" not in res_react["content"]
    assert res_react["stack_applied"] == ["react"]
    assert res_react["stack_source"] == "explicit"


def test_b4_skill_without_slots_and_markers(tmp_path: Path) -> None:
    """B4: без available_slots и без маркеров — контент без инъекции и маркеров; с маркером — fallback-заполнение."""
    storage, sync, root = _make_project(tmp_path)
    skills_dir = root / "skills"

    plain_dir = skills_dir / "plain-skill"
    plain_dir.mkdir()
    (plain_dir / "SKILL.md").write_text(
        "---\nname: plain-skill\ntitle: Plain\ncategory: general\n---\n# Plain\nNo slots.",
        encoding="utf-8",
    )

    legacy_dir = skills_dir / "legacy-skill"
    legacy_dir.mkdir()
    (legacy_dir / "SKILL.md").write_text(
        "---\nname: legacy-skill\ntitle: Legacy\ncategory: general\n---\n# Legacy\n\n<!-- SLOT: ui -->\n",
        encoding="utf-8",
    )
    legacy_blocks = legacy_dir / "blocks" / "ui"
    legacy_blocks.mkdir(parents=True)
    (legacy_blocks / "default.md").write_text(
        "### ui default block\nGeneric guidance.", encoding="utf-8"
    )

    sync.sync()

    res_plain = load_skill("plain-skill", storage=storage, synchronizer=sync, project_root=root)
    assert "No slots." in res_plain["content"]
    assert "<!-- SLOT:" not in res_plain["content"]

    # m-3 fallback: маркер есть, слот не объявлен — заполняется, не зачищается молча
    res_legacy = load_skill("legacy-skill", storage=storage, synchronizer=sync, project_root=root)
    assert "ui default block" in res_legacy["content"]
    assert "<!-- SLOT:" not in res_legacy["content"]


def test_b4b_extends_parent_slots(tmp_path: Path) -> None:
    """B4b: слоты extends-предка не отфильтровываются при touches."""
    storage, sync, root = _make_project(tmp_path)
    skills_dir = root / "skills"

    parent_dir = skills_dir / "parent-skill"
    parent_dir.mkdir()
    (parent_dir / "SKILL.md").write_text(
        "---\nname: parent-skill\ntitle: Parent\ncategory: general\navailable_slots: [ui]\n---\n# Parent\n\n<!-- SLOT: ui -->\n",
        encoding="utf-8",
    )
    child_dir = skills_dir / "child-skill"
    child_dir.mkdir()
    (child_dir / "SKILL.md").write_text(
        "---\nname: child-skill\ntitle: Child\ncategory: general\nextends: parent-skill\n---\n# Child\n\n<!-- SLOT: ui -->\n",
        encoding="utf-8",
    )
    child_blocks = child_dir / "blocks" / "ui"
    child_blocks.mkdir(parents=True)
    (child_blocks / "default.md").write_text(
        "### ui default block\nGeneric guidance.", encoding="utf-8"
    )
    sync.sync()

    # Слот родителя не отфильтровывается и заполняется
    res = load_skill(
        "child-skill", touches=["ui"], storage=storage, synchronizer=sync, project_root=root
    )
    assert "ui default block" in res["content"]
    assert "<!-- SLOT:" not in res["content"]


def test_b5_unknown_touches_raises_error(tmp_path: Path) -> None:
    """B5 (Шаг 13): touches вне available_slots дают явную ошибку с доступными вариантами."""
    storage, sync, root = _make_project(tmp_path)
    with pytest.raises(ValueError, match="Неизвестные секции для скилла 'writing-plans'"):
        load_skill(
            "writing-plans",
            touches=["nonexistent-slot"],
            storage=storage,
            synchronizer=sync,
            project_root=root,
        )


def test_b6_empty_touches_clears_markers(tmp_path: Path) -> None:
    """B6: touches=[] — инъекции нет, но маркеры зачищены."""
    storage, sync, root = _make_project(tmp_path)
    res = load_skill(
        "writing-plans", touches=[], storage=storage, synchronizer=sync, project_root=root
    )
    assert "<!-- SLOT:" not in res["content"]
    assert "ui default block" not in res["content"]


def test_b7_slots_filled_in_response(tmp_path: Path) -> None:
    """B7: в ответе slots_filled — слоты, заполненные при сборке."""
    storage, sync, root = _make_project(tmp_path)
    res = load_skill("writing-plans", storage=storage, synchronizer=sync, project_root=root)
    assert res["slots_filled"] == ["ui", "backend"]


def test_b8_duplicate_touches_no_content_duplication(tmp_path: Path) -> None:
    """B8: дубликаты touches дедупятся — контент блока не аппендится дважды."""
    storage, sync, root = _make_project(tmp_path)
    res = load_skill(
        "writing-plans", touches=["ui", "ui"], storage=storage, synchronizer=sync, project_root=root
    )
    assert res["content"].count("ui default block") == 1
    assert "Спецификация реализации: ui" not in res["content"]
    assert res["slots_filled"] == ["ui"]


def test_b9_autodetect_when_stack_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """B9: при stack=None срабатывает автодетект из pyproject.toml."""
    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)
    storage, sync, root = _make_project(tmp_path)
    (root / "pyproject.toml").write_text(
        '[project]\ndependencies = ["pydantic>=2"]\n', encoding="utf-8"
    )
    res = load_skill("writing-plans", storage=storage, synchronizer=sync, project_root=root)
    assert set(res["stack_applied"]) >= {"python", "pydantic"}
    assert res["stack_source"] == "autodetect"
    assert "Auto-detected trap" in res["content"]


def test_b10_empty_stack_string_triggers_autodetect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B10: stack="" нормализуется в None и триггерит автодетект."""
    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)
    storage, sync, root = _make_project(tmp_path)
    (root / "pyproject.toml").write_text(
        '[project]\ndependencies = ["pydantic>=2"]\n', encoding="utf-8"
    )
    res = load_skill(
        "writing-plans", stack="", storage=storage, synchronizer=sync, project_root=root
    )
    assert set(res["stack_applied"]) >= {"python", "pydantic"}
    assert res["stack_source"] == "autodetect"
    assert "Auto-detected trap" in res["content"]


def test_b11_mode_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """B11: режим определяется из MCP_ACTIVE_SERVERS (непустое -> mcp, пустое -> offline)."""
    storage, sync, root = _make_project(tmp_path)
    # Непустое значение -> "mcp"
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "skills-engine,knowledge-engine")
    res_mcp = load_skill("writing-plans", storage=storage, synchronizer=sync, project_root=root)
    assert "mcp" in res_mcp["stack_applied"]
    assert res_mcp["stack_source"] == "autodetect"

    # Явно пустое значение -> "offline"
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "")
    res_offline = load_skill("writing-plans", storage=storage, synchronizer=sync, project_root=root)
    assert "offline" in res_offline["stack_applied"]
    assert res_offline["stack_source"] == "autodetect"


def test_b12_mode_from_agents_markers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """B12: папка .agents/audit сама по себе не включает mcp; режим mcp задаётся через env."""
    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)
    storage, sync, root = _make_project(tmp_path)
    (root / ".agents" / "audit").mkdir(parents=True)
    res = load_skill("writing-plans", storage=storage, synchronizer=sync, project_root=root)
    assert "mcp" not in res["stack_applied"]

    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "audit-engine")
    res_mcp = load_skill("writing-plans", storage=storage, synchronizer=sync, project_root=root)
    assert "mcp" in res_mcp["stack_applied"]
    assert res_mcp["stack_source"] == "autodetect"


def test_b13_no_markers_no_env_no_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """B13: при отсутствии манифестов, маркеров и MCP_ACTIVE_SERVERS стек пуст и stack_source is None."""
    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)
    storage, sync, root = _make_project(tmp_path)
    res = load_skill("writing-plans", storage=storage, synchronizer=sync, project_root=root)
    assert res["stack_applied"] == []
    assert res["stack_source"] is None
    assert "ui default block" in res["content"]
    assert "backend default block" in res["content"]


# ---------- C. Fail-fast валидация ----------


def test_c1_traversal_stack_raises(tmp_path: Path) -> None:
    """C1: path traversal в стеке → ValueError."""
    storage, sync, root = _make_project(tmp_path)
    with pytest.raises(ValueError, match="Недопустимое имя технологии"):
        load_skill(
            "writing-plans",
            stack="../../etc",
            storage=storage,
            synchronizer=sync,
            project_root=root,
        )


def test_c2_cyrillic_stack_raises(tmp_path: Path) -> None:
    """C2: кириллица в стеке → ValueError (is_safe_name — ASCII-only)."""
    storage, sync, root = _make_project(tmp_path)
    with pytest.raises(ValueError, match="Недопустимое имя технологии"):
        load_skill(
            "writing-plans", stack=["питон"], storage=storage, synchronizer=sync, project_root=root
        )


def test_c3_uppercase_stack_raises(tmp_path: Path) -> None:
    """C3: CamelCase в стеке → ValueError (канон — нижний регистр)."""
    storage, sync, root = _make_project(tmp_path)
    with pytest.raises(ValueError, match="Недопустимое имя технологии"):
        load_skill(
            "writing-plans", stack=["Python"], storage=storage, synchronizer=sync, project_root=root
        )


@pytest.mark.asyncio
async def test_c4_missing_skill_error_envelope(tmp_path: Path) -> None:
    """C4: несуществующий скилл → error envelope SKILL_NOT_FOUND (tmp-изоляция)."""
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / ".agents" / "skills"
    skills_dir.mkdir(parents=True)
    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()
    server = create_server(project_root=tmp_path, db_path=db_path, skills_dir=skills_dir)

    result = await server.call_tool("load_skill", {"name": "definitely-missing-skill"})
    data = result.structured_content or json.loads(result.content[0].text)
    assert data.get("error") is True
    assert data.get("reason") == "SKILL_NOT_FOUND"


@pytest.mark.asyncio
async def test_c5_invalid_stack_error_envelope(tmp_path: Path) -> None:
    """C5: невалидный стек через MCP → error envelope (ValueError → INVALID_ARGUMENTS)."""
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / ".agents" / "skills"
    skills_dir.mkdir(parents=True)
    skill_dir = skills_dir / "writing-plans"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()
    server = create_server(project_root=tmp_path, db_path=db_path, skills_dir=skills_dir)

    result = await server.call_tool("load_skill", {"name": "writing-plans", "stack": "../../etc"})
    data = result.structured_content or json.loads(result.content[0].text)
    assert data.get("error") is True
    assert data.get("error_type") == "ValueError"
    assert "Недопустимое имя технологии" in data.get("message", "")


# ---------- D. MCP-уровень ----------


@pytest.mark.asyncio
async def test_d1_string_stack_via_call_tool(tmp_path: Path) -> None:
    """D1: строковый stack="ui,backend" принимается схемой anyOf и собирается.

    Примечание: "ui"/"backend" здесь — проверка эквивалентности формы CSV/списка,
    не семантики tech (файлов ui.md/backend.md в фикстуре нет — блоки выбираются
    из default.md).
    """
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / ".agents" / "skills"
    skills_dir.mkdir(parents=True)

    skill_dir = skills_dir / "writing-plans"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    blocks = skill_dir / "blocks"
    for slot in ("ui", "backend"):
        (blocks / slot).mkdir(parents=True)
        (blocks / slot / "default.md").write_text(
            f"### {slot} default block\nGeneric guidance.", encoding="utf-8"
        )

    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()
    server = create_server(project_root=tmp_path, db_path=db_path, skills_dir=skills_dir)

    result = await server.call_tool("load_skill", {"name": "writing-plans", "stack": "ui,backend"})
    data = result.structured_content or json.loads(result.content[0].text)
    assert data.get("error") is not True
    # Детерминированный assert: выбраны default-блоки
    assert "ui default block" in data["content"]
    assert "backend default block" in data["content"]
    assert "<!-- SLOT:" not in data["content"]

    # Эквивалентность прямому вызову со списком
    direct = load_skill(
        "writing-plans",
        stack=["ui", "backend"],
        storage=storage,
        synchronizer=sync,
        project_root=tmp_path,
    )
    assert data["content"] == direct["content"]


@pytest.mark.asyncio
async def test_d2_create_skill_schema_accepts_string(tmp_path: Path) -> None:
    """D2: у create_skill входные схемы stack/available_slots допускают string (anyOf)."""
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / ".agents" / "skills"
    skills_dir.mkdir(parents=True)
    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()
    server = create_server(project_root=tmp_path, db_path=db_path, skills_dir=skills_dir)
    tools = {t.name: t for t in await server.list_tools()}
    schema = tools["create_skill"].input_schema

    for param in ("stack", "available_slots", "aliases"):
        prop = schema["properties"][param]
        types = [sub.get("type") for sub in prop.get("anyOf", [])]
        assert "string" in types, f"create_skill.{param} должен принимать string: {prop}"
        assert "array" in types, f"create_skill.{param} должен принимать array: {prop}"

    # D3: схема load_skill.stack — тоже anyOf (string/array)
    load_schema = tools["load_skill"].input_schema
    types = [sub.get("type") for sub in load_schema["properties"]["stack"].get("anyOf", [])]
    assert "string" in types, f"load_skill.stack должен принимать string: {load_schema}"
    assert "array" in types, f"load_skill.stack должен принимать array: {load_schema}"


# ---------- E. Defense-in-depth компилятора ----------


def test_e1_traversal_tech_no_leak(tmp_path: Path) -> None:
    """E1: traversal-tech в project_stack не читает файлы за пределами blocks (выбирается default)."""
    storage, sync, root = _make_project(tmp_path)
    compiler = SlotCompiler(storage)
    _, content = compiler.assemble_skill(
        name="writing-plans",
        touches=["ui"],
        project_stack=["../../etc/passwd"],
    )
    assert "ui default block" in content
    assert "root:" not in content


def test_e2_empty_touches_clears_markers_without_fill(tmp_path: Path) -> None:
    """E2 (Шаг 13): touches=[] маркеры зачищаются, контент слотов не подставляется."""
    storage, sync, root = _make_project(tmp_path)
    compiler = SlotCompiler(storage)
    _, content = compiler.assemble_skill(
        name="writing-plans",
        touches=[],
        project_stack=["react"],
    )
    assert "<!-- SLOT:" not in content
    assert "React UI Guidelines" not in content
    assert "ui default block" not in content


def test_a10_explicit_empty_stack_vs_none_autodetect(tmp_path: Path) -> None:
    """A10: stack=[] (явный пустой выбор) vs stack=None (автодетект)."""
    storage, sync, root = _make_project(tmp_path)
    # Создаём маркер манифеста Python-проекта для автодетекта
    (root / "pyproject.toml").write_text(
        "[project]\ndependencies = ['pydantic']\n", encoding="utf-8"
    )

    # 1. stack=None -> автодетект
    res_none = load_skill(
        "writing-plans",
        stack=None,
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    assert res_none["stack_source"] == "autodetect"
    assert "pydantic" in res_none["stack_applied"]

    # 2. stack=[] -> явный пустой выбор (технологии НЕ детектятся)
    res_empty = load_skill(
        "writing-plans",
        stack=[],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    assert res_empty["stack_source"] == "explicit"
    assert "pydantic" not in res_empty["stack_applied"]
    # Контент собирается с базовыми дефолт-блоками
    assert "ui default block" in res_empty["content"]
