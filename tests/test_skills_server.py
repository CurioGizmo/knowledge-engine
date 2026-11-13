"""Тесты для FastMCP сервера mcp_skills."""

import json
from pathlib import Path

import pytest

from mcp_skills.server import (
    create_server,
    install_agent_workflow,
    load_agent,
    load_skill,
    search_skills,
)
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer


def test_server_search_and_load_tools(tmp_path: Path) -> None:
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / ".agents" / "skills"
    skills_dir.mkdir(parents=True)

    skill_path = skills_dir / "writing-plans"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text(
        """---
name: writing-plans
title: Writing Plans
category: planning
available_slots: [backend]
---
# Writing Plans
<!-- SLOT: backend -->
""",
        encoding="utf-8",
    )

    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()

    # 1. Search
    res_search = search_skills("writing-plans", storage=storage, synchronizer=sync)
    assert res_search["count"] == 1
    assert res_search["results"][0]["name"] == "writing-plans"

    # 2. Load
    res_load = load_skill(
        "writing-plans",
        touches=["backend"],
        storage=storage,
        synchronizer=sync,
        project_root=tmp_path,
    )
    assert res_load["name"] == "writing-plans"
    assert "Writing Plans" in res_load["content"]


def test_server_agent_tools(tmp_path: Path) -> None:
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / ".agents" / "skills"
    skills_dir.mkdir(parents=True)

    skill_path = skills_dir / "review-agent"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text(
        """---
name: review-agent
title: Code Reviewer
category: quality-review
---
# Review Instructions
Always check for security and performance.
""",
        encoding="utf-8",
    )

    # R2 ревизия 4: read-only экспорт в OpenCode требует подтверждения владельца.
    agents_dir = tmp_path / ".agents" / "agents"
    agent_dir = agents_dir / "review-agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "AGENT.yaml").write_text(
        """name: review-agent
role: Code Reviewer
description: Strict auditor
permissions:
  write: false
  mcp: true
  subagents: false
readonly_acknowledged: true
skills: [review-agent]
""",
        encoding="utf-8",
    )

    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()

    # load_agent
    res_agent = load_agent(
        "review-agent",
        target="antigravity",
        project_root=tmp_path,
        storage=storage,
        synchronizer=sync,
    )
    assert res_agent["agent"] == "review-agent"
    assert "Always check for security and performance." in res_agent["content"]

    # install_agent_workflow
    res_install = install_agent_workflow(
        "review-agent",
        target="opencode",
        project_root=tmp_path,
        storage=storage,
        synchronizer=sync,
    )
    assert res_install["success"] is True
    assert (tmp_path / ".opencode" / "agents" / "review-agent.md").is_file()


@pytest.mark.asyncio
async def test_create_server_instance(tmp_path: Path) -> None:
    server = create_server(project_root=tmp_path)
    assert server.name == "skills-engine"
    tools = await server.list_tools()
    tool_names = {t.name for t in tools}
    assert tool_names == {"search_skills", "load_skill", "create_skill", "get_help", "step_flow"}


def test_handle_tool_error_envelope_mapping() -> None:
    """Прямой юнит-тест маппера _handle_tool_error: доменные ошибки → error-envelope."""
    from mcp_skills.errors import (
        CircularExtensionError,
        InvalidSkillFormatError,
        MaxExtensionDepthError,
        SkillNotFoundError,
    )
    from mcp_skills.server import _handle_tool_error

    res = _handle_tool_error(SkillNotFoundError("Скилл 'x' не найден"))
    assert res["error"] is True
    assert res["error_type"] == "SkillNotFoundError"
    assert res["reason"] == "SKILL_NOT_FOUND"
    assert "не найден" in res["message"]

    res = _handle_tool_error(CircularExtensionError("цикл"))
    assert res["error_type"] == "CircularExtensionError"
    assert res["reason"] == "CIRCULAR_EXTENSION"

    res = _handle_tool_error(MaxExtensionDepthError("глубина"))
    assert res["error_type"] == "MaxExtensionDepthError"

    res = _handle_tool_error(InvalidSkillFormatError("формат"))
    assert res["error_type"] == "InvalidSkillFormatError"

    res = _handle_tool_error(ValueError("пустое имя"))
    assert res["error_type"] == "ValueError"
    assert res["reason"] == "INVALID_ARGUMENTS"

    res = _handle_tool_error(RuntimeError("внезапно"))
    assert res["error_type"] == "RuntimeError"
    assert res["reason"] == "UNEXPECTED_ERROR"


def test_tool_error_guard_preserves_docstring() -> None:
    """functools.wraps сохраняет docstring обёрнутой tool-функции (контракт JSON-схемы)."""

    def fake_tool(name: str) -> dict[str, object]:
        """Docstring инструмента."""
        return {"name": name}

    from mcp_skills.server import _tool_error_guard

    guarded = _tool_error_guard("fake_tool")(fake_tool)
    assert guarded.__doc__ == "Docstring инструмента."
    assert guarded("x") == {"name": "x"}


@pytest.mark.asyncio
async def test_mcp_load_skill_error_envelope(tmp_path: Path) -> None:
    """
    Вход: Вызов MCP-инструмента load_skill с несуществующим именем скилла.
    Выход: Структурированный error-envelope (error=True, error_type=SkillNotFoundError) вместо краша.
    Проверка: Сервер возвращает единый формат ошибки и продолжает работать.
    """
    server = create_server(project_root=tmp_path)
    result = await server.call_tool("load_skill", {"name": "definitely-missing-skill"})

    if result.structured_content:
        data = result.structured_content
    else:
        data = json.loads(result.content[0].text)

    assert data.get("error") is True
    assert data.get("error_type") == "SkillNotFoundError"
    assert data.get("reason") == "SKILL_NOT_FOUND"
    assert result.is_error is True

    # Сервер жив и обрабатывает последующие вызовы
    overview_res = await server.call_tool("search_skills", {})
    assert not overview_res.is_error


def test_server_search_and_load_with_new_terminology(tmp_path: Path) -> None:
    """Проверка работы search_skills(category=...) и load_skill(stack=...)."""
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / ".agents" / "skills"
    skills_dir.mkdir(parents=True)

    skill_path = skills_dir / "writing-plans"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text(
        """---
name: writing-plans
title: Writing Plans
category: planning
available_slots: [backend]
---
# Writing Plans
<!-- SLOT: backend -->
""",
        encoding="utf-8",
    )

    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()

    # 1. Zero-query search returns overview and categories
    zero_res = search_skills("", storage=storage, synchronizer=sync)
    assert zero_res.get("mode") == "overview"
    assert "categories" in zero_res
    assert "overview" not in zero_res  # дубль верхнего уровня убран (экономия токенов)

    # 2. Search by category
    res_search = search_skills(
        "writing-plans", category="planning", storage=storage, synchronizer=sync
    )
    assert res_search["count"] == 1
    assert res_search["results"][0]["name"] == "writing-plans"

    # 3. Load with stack
    res_load = load_skill("writing-plans", stack=["backend"], storage=storage, synchronizer=sync)
    assert res_load["name"] == "writing-plans"
    assert "Writing Plans" in res_load["content"]
