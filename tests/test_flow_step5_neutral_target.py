"""Тесты Шага 5: нейтральный agent_spec и поддержка target в step_flow."""

from __future__ import annotations

import json
from pathlib import Path
import pytest
import yaml

from mcp_skills.flow_engine import step_flow
from mcp_skills.flow_storage import FlowStorage


def _init_test_catalog(cat_dir: Path) -> Path:
    """Создаёт тестовый каталог скиллов и флоу."""
    skills_dir = cat_dir / "skills"
    flows_dir = cat_dir / "flows"
    agents_dir = cat_dir / "agents"
    skills_dir.mkdir(parents=True, exist_ok=True)
    flows_dir.mkdir(parents=True, exist_ok=True)
    agents_dir.mkdir(parents=True, exist_ok=True)

    # Создаём скилл
    (skills_dir / "test-skill").mkdir(parents=True, exist_ok=True)
    (skills_dir / "test-skill" / "SKILL.md").write_text(
        "---\nname: test-skill\ntitle: Test Skill\ndescription: A test skill\n---\n# Skill content\n",
        encoding="utf-8",
    )

    # Создаём агента
    agent_dir = agents_dir / "test-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    agent_yaml = {
        "name": "test-agent",
        "role": "Test Specialist",
        "description": "Agent for testing",
        "skills": ["test-skill"],
        "instructions": "Follow all testing instructions.",
        "permissions": {
            "write": False,
            "mcp": True,
            "subagents": False,
        },
        # R2 ревизия 4: read-only экспорт в OpenCode — с подтверждением
        "readonly_acknowledged": True,
    }
    (agent_dir / "AGENT.yaml").write_text(yaml.safe_dump(agent_yaml), encoding="utf-8")

    # Создаём flow
    flow_content = """# Test Flow
Сценарий для тестирования

## 1. Шаг тестирования [id: step_one]
<!-- meta: {"agent": "test-agent", "require_verdict": true} -->
Инструкция к первому шагу.

## 2. Финальный шаг [id: step_two]
Инструкция ко второму шагу.
"""
    (flows_dir / "test-target-flow.md").write_text(flow_content, encoding="utf-8")
    return cat_dir


def test_step_flow_default_target_is_antigravity(tmp_path: Path):
    """По умолчанию target='antigravity': возвращается subagent Antigravity и agent_spec."""
    cat_dir = _init_test_catalog(tmp_path / "catalog")
    proj_dir = tmp_path / "project"
    proj_dir.mkdir()

    res = step_flow(
        action="advance",
        flow="test-target-flow",
        project_root=proj_dir,
        skills_dir=cat_dir,
    )

    assert "subagent" in res
    assert res["subagent"]["name"] == "test-agent"
    # Antigravity bundle is JSON string
    bundle_data = json.loads(res["subagent"]["bundle"])
    assert bundle_data["name"] == "test-agent"
    assert bundle_data["role"] == "Test Specialist"

    # Нейтральный agent_spec
    assert "agent_spec" in res
    spec = res["agent_spec"]
    assert spec["name"] == "test-agent"
    assert spec["role"] == "Test Specialist"
    assert spec["description"] == "Agent for testing"
    assert spec["instructions"] == "Follow all testing instructions."
    assert spec["permissions"] == {"write": False, "mcp": True, "subagents": False}
    assert spec["required_capabilities"] == ["mcp"]

    # next_call всегда содержит эффективный target, включая antigravity (шаг 5)
    assert res["next_call"]["arguments"]["target"] == "antigravity"


def test_step_flow_with_opencode_target(tmp_path: Path):
    """При target='opencode' subagent формируется через OpenCodeAdapter и next_call содержит target."""
    cat_dir = _init_test_catalog(tmp_path / "catalog")
    proj_dir = tmp_path / "project"
    proj_dir.mkdir()

    res = step_flow(
        action="advance",
        flow="test-target-flow",
        target="opencode",
        project_root=proj_dir,
        skills_dir=cat_dir,
    )

    assert "subagent" in res
    bundle_text = res["subagent"]["bundle"]
    # OpenCode profile starts with YAML frontmatter containing mode: subagent
    assert "mode: subagent" in bundle_text
    assert "description: Agent for testing" in bundle_text

    # agent_spec также присутствует
    assert "agent_spec" in res
    assert res["agent_spec"]["name"] == "test-agent"

    # next_call сохраняет target='opencode'
    assert res["next_call"]["tool"] == "step_flow"
    assert res["next_call"]["arguments"]["target"] == "opencode"


def test_step_flow_with_prompt_target(tmp_path: Path):
    """При target='prompt' subagent формируется в чистом текстовом формате Persona Mask."""
    cat_dir = _init_test_catalog(tmp_path / "catalog")
    proj_dir = tmp_path / "project"
    proj_dir.mkdir()

    res = step_flow(
        action="advance",
        flow="test-target-flow",
        target="prompt",
        project_root=proj_dir,
        skills_dir=cat_dir,
    )

    assert "subagent" in res
    bundle_text = res["subagent"]["bundle"]
    assert bundle_text.startswith("# Persona: test-agent")
    assert "Role: Test Specialist" in bundle_text

    assert "agent_spec" in res
    assert res["next_call"]["arguments"]["target"] == "prompt"


def test_step_flow_target_persistence_in_status_and_advance(tmp_path: Path):
    """Сессия помнит заданный target при последующих вызовах status и advance."""
    cat_dir = _init_test_catalog(tmp_path / "catalog")
    proj_dir = tmp_path / "project"
    proj_dir.mkdir()

    res1 = step_flow(
        action="advance",
        flow="test-target-flow",
        target="opencode",
        project_root=proj_dir,
        skills_dir=cat_dir,
    )
    session_id = res1["session_id"]
    assert res1["next_call"]["arguments"]["target"] == "opencode"

    # Проверяем status без явного target -> должен вернуть target='opencode' в next_call
    status_res = step_flow(
        action="status",
        session_id=session_id,
        project_root=proj_dir,
        skills_dir=cat_dir,
    )
    assert status_res["next_call"]["arguments"]["target"] == "opencode"


def test_step_flow_escalate_next_call_contains_target(tmp_path: Path):
    """next_call эскалации всегда содержит эффективный target, включая antigravity.

    target=None (совместимость) означает сохранённый target сессии —
    в next_call должен попасть эффективный, а не None (шаг 5, план строка 67).
    """
    cat_dir = _init_test_catalog(tmp_path / "catalog")
    proj_dir = tmp_path / "project"
    proj_dir.mkdir()

    # Старт с opencode — сохранённый target сессии
    res = step_flow(
        action="advance",
        flow="test-target-flow",
        target="opencode",
        project_root=proj_dir,
        skills_dir=cat_dir,
    )
    session_id = res["session_id"]

    # Этап step_one требует verdict (meta agent + require_verdict) —
    # нужно 3 needs_work для превышения loop_max=3
    for _ in range(3):
        blocked = step_flow(
            action="advance",
            session_id=session_id,
            verdict="needs_work",
            project_root=proj_dir,
            skills_dir=cat_dir,
        )
    assert blocked["status"] == "blocked_escalate"

    # next_call эскалации содержит эффективный target='opencode' (не None, не отсутствующий)
    assert blocked["next_call"]["arguments"]["action"] == "escalation_resolve"
    assert blocked["next_call"]["arguments"]["target"] == "opencode"


def test_step_flow_invalid_target_rejected(tmp_path: Path):
    """Неподдерживаемый target отклоняется с понятной ошибкой."""
    cat_dir = _init_test_catalog(tmp_path / "catalog")
    proj_dir = tmp_path / "project"
    proj_dir.mkdir()

    with pytest.raises(ValueError, match="Неизвестный целевой рантайм"):
        step_flow(
            action="advance",
            flow="test-target-flow",
            target="unsupported_runtime",
            project_root=proj_dir,
            skills_dir=cat_dir,
        )


def test_step_flow_target_specific_override(tmp_path: Path):
    """Специфичный для target оверрайд применяется при соответствующем target."""
    cat_dir = _init_test_catalog(tmp_path / "catalog")
    proj_dir = tmp_path / "project"
    proj_dir.mkdir()

    # Создаём override в проекте специально для opencode
    agents_dir = proj_dir / ".agents" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    override_yaml = {
        "base_agent": "test-agent",
        "target": "opencode",
        "permissions": {
            "write": True,
            "mcp": True,
            "subagents": True,
        },
    }
    (agents_dir / "test-agent_opencode.override.yaml").write_text(
        yaml.safe_dump(override_yaml), encoding="utf-8"
    )

    # При target="opencode" override должен примениться (write=True, subagents=True)
    res_oc = step_flow(
        action="advance",
        flow="test-target-flow",
        target="opencode",
        project_root=proj_dir,
        skills_dir=cat_dir,
    )
    assert res_oc["agent_spec"]["permissions"]["write"] is True
    assert res_oc["agent_spec"]["permissions"]["subagents"] is True
    assert "write" in res_oc["agent_spec"]["required_capabilities"]
    assert "subagents" in res_oc["agent_spec"]["required_capabilities"]

    # А для antigravity override не применяется (write=False)
    proj2 = tmp_path / "project2"
    proj2.mkdir()
    agents2 = proj2 / ".agents" / "agents"
    agents2.mkdir(parents=True, exist_ok=True)
    (agents2 / "test-agent_opencode.override.yaml").write_text(
        yaml.safe_dump(override_yaml), encoding="utf-8"
    )
    res_ag = step_flow(
        action="advance",
        flow="test-target-flow",
        target="antigravity",
        project_root=proj2,
        skills_dir=cat_dir,
    )
    assert res_ag["agent_spec"]["permissions"]["write"] is False
    assert res_ag["agent_spec"]["permissions"]["subagents"] is False
