"""R5 (ревизия 4): status --target меняет только представление ответа.

Критерий: get_flow_status с явным target НЕ записывает target в сессию БД.
Явный target в status — фильтр представления (next_call формируется под
указанный рантайм), а не переключение сохранённого target сессии: смена
рантайма — операция start/retry/advance/resolve с target, не read-only status.

Дополнительно: status без target отражает сохранённый target сессии.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mcp_skills.flow_engine import FlowEngine
from mcp_skills.flow_storage import FlowStorage


def _init_test_catalog(cat_dir: Path) -> Path:
    skills_dir = cat_dir / "skills"
    flows_dir = cat_dir / "flows"
    agents_dir = cat_dir / "agents"
    skills_dir.mkdir(parents=True, exist_ok=True)
    flows_dir.mkdir(parents=True, exist_ok=True)
    agents_dir.mkdir(parents=True, exist_ok=True)

    (skills_dir / "test-skill").mkdir(parents=True, exist_ok=True)
    (skills_dir / "test-skill" / "SKILL.md").write_text(
        "---\nname: test-skill\ntitle: Test Skill\ndescription: A test skill\n---\n# Skill\n",
        encoding="utf-8",
    )

    agent_dir = agents_dir / "test-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "AGENT.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "test-agent",
                "role": "Test Specialist",
                "description": "Agent",
                "skills": ["test-skill"],
                "instructions": "Follow.",
                "permissions": {"write": False, "mcp": True, "subagents": False},
            }
        ),
        encoding="utf-8",
    )

    (flows_dir / "test-status-flow.md").write_text(
        "# Test Flow\nСценарий\n\n"
        "## 1. Шаг [id: step_one]\n"
        '<!-- meta: {"agent": "test-agent"} -->\n'
        "Инструкция.\n",
        encoding="utf-8",
    )
    return cat_dir


def _saved_target(proj_dir: Path, session_id: str) -> str | None:
    storage = FlowStorage(project_root=proj_dir)
    session = storage.get_session(session_id)
    assert session is not None
    ctx = session.context_data if isinstance(session.context_data, dict) else {}
    return ctx.get("target")


def test_status_with_target_does_not_persist_it(tmp_path: Path) -> None:
    """status --target antigravity не перезаписывает сохранённый opencode."""
    cat_dir = _init_test_catalog(tmp_path / "catalog")
    proj_dir = tmp_path / "project"
    proj_dir.mkdir()

    engine = FlowEngine(project_root=proj_dir, skills_dir=cat_dir)
    res = engine.start_flow("test-status-flow", target="opencode")
    session_id = res["session_id"]

    # Сессия сохранена с opencode
    assert _saved_target(proj_dir, session_id) == "opencode"

    # status с ДРУГИМ явным target: представление меняется...
    status_res = engine.get_flow_status(
        session_id=session_id, project_root=proj_dir, target="antigravity"
    )
    assert status_res["next_call"]["arguments"]["target"] == "antigravity"

    # ...но в БД target сессии остаётся opencode (read-only семантика status)
    assert _saved_target(proj_dir, session_id) == "opencode", (
        "get_flow_status с явным target не должен писать target в сессию (R5, ревизия 4)"
    )

    # status без target отражает сохранённый opencode
    plain = engine.get_flow_status(session_id=session_id, project_root=proj_dir)
    assert plain["next_call"]["arguments"]["target"] == "opencode"


def test_status_with_invalid_target_raises(tmp_path: Path) -> None:
    """NIT-9: невалидный target в status отклоняется, а не молча игнорируется.

    get_flow_status(target="bogus") → ValueError: фильтр представления не
    может «успешно» сформировать next_call под неизвестный рантайм.
    """
    cat_dir = _init_test_catalog(tmp_path / "catalog")
    proj_dir = tmp_path / "project"
    proj_dir.mkdir()

    engine = FlowEngine(project_root=proj_dir, skills_dir=cat_dir)
    res = engine.start_flow("test-status-flow", target="opencode")
    session_id = res["session_id"]

    with pytest.raises(ValueError, match="bogus"):
        engine.get_flow_status(session_id=session_id, project_root=proj_dir, target="bogus")
