"""Тесты для мульти-рантайм адаптеров и бандлера субагентов (Спринт 4)."""

import json
import logging
from pathlib import Path

import pytest

from mcp_skills.adapters import (
    AntigravityAdapter,
    CursorAdapter,
    OpenCodeAdapter,
    PromptAdapter,
    SUPPORTED_TARGETS,
    get_adapter,
)
from mcp_skills.adapters.base import BaseAdapter
from mcp_skills.bundler import AgentBundler
from mcp_skills.models import AgentPermissions, AgentProfile, SkillCard
from mcp_skills.storage import SkillsStorage


def test_get_adapter() -> None:
    assert isinstance(get_adapter("antigravity"), AntigravityAdapter)
    assert isinstance(get_adapter("opencode"), OpenCodeAdapter)
    assert isinstance(get_adapter("cursor"), CursorAdapter)
    assert isinstance(get_adapter("prompt"), PromptAdapter)

    with pytest.raises(ValueError, match="Неизвестный целевой рантайм"):
        get_adapter("unsupported_runtime")


def test_supported_targets() -> None:
    """SUPPORTED_TARGETS — единственный источник истины списка рантаймов."""
    assert SUPPORTED_TARGETS == ("antigravity", "cursor", "opencode", "prompt")
    assert list(SUPPORTED_TARGETS) == sorted(SUPPORTED_TARGETS)
    for target in SUPPORTED_TARGETS:
        assert get_adapter(target) is not None


def test_antigravity_adapter() -> None:
    adapter = AntigravityAdapter()
    profile = AgentProfile(
        name="plan-reviewer",
        role="Principal Quality Auditor",
        description="Audits implementation plans",
        model="pro",
        skills=["writing-plans"],
        permissions=AgentPermissions(write=False, mcp=True),
    )
    skills_content = "# Skill: writing-plans\nAlways check UI states."

    output = adapter.export_agent(profile, skills_content)
    data = json.loads(output)
    assert data["name"] == "plan-reviewer"
    assert data["role"] == "Principal Quality Auditor"
    assert data["model"] == "pro"
    assert "Always check UI states." in data["system_prompt"]
    assert data["permissions"]["write"] is False


def test_opencode_adapter_read_only_and_permissions(caplog: pytest.LogCaptureFixture) -> None:
    import yaml

    adapter = OpenCodeAdapter()
    profile = AgentProfile(
        name="code-reviewer",
        role="Principal Quality Auditor",
        description="Audits codebase and enforces security",
        model="pro",
        model_name=None,
        skills=["code-review"],
        permissions=AgentPermissions(write=False, mcp=True, subagents=False),
        # R2 ревизия 4: экспорт read-only в OpenCode — только с подтверждением
        readonly_acknowledged=True,
    )
    skills_content = "# Code Review\nEnforce strict contracts."

    output = adapter.export_agent(profile, skills_content)
    assert output.startswith("---\n")
    parts = output.split("---\n")
    assert len(parts) >= 3
    fm = yaml.safe_load(parts[1])

    # 1. mode must be subagent
    assert fm["mode"] == "subagent"
    assert fm["description"] == "Audits codebase and enforces security"

    # 2. model: inherit/unmapped pro -> model field omitted, warning logged
    assert "model" not in fm
    assert "модель будет унаследована от хоста" in caplog.text.lower()

    # 3. permission (singular)
    assert "permission" in fm
    assert "permissions" not in fm
    assert fm["permission"]["edit"] == "deny"
    assert fm["permission"]["bash"] == "deny"
    assert fm["permission"]["task"] == "deny"

    # 4. tools: deny-by-default — wildcard-запрет присутствует даже при
    # пустом profile.tools (R2: отсутствие ключа ≠ запрет; эффективный
    # запрет неизвестных инструментов обеспечивается "*": False).
    assert "tools" in fm
    assert fm["tools"]["*"] is False

    # 5. prompt body
    assert "# System Prompt" in output
    assert "Enforce strict contracts." in output


def test_opencode_adapter_effective_deny_for_attack_set(tmp_path: Path) -> None:
    """R2: эффективный запрет для attack-set при write=False.

    Каждый инструмент attack-set (включая НЕ перечисленные в profile.tools
    имена вида weird_new_tool) должен быть покрыт либо явным `False`, либо
    wildcard `"*": False`. Имя из проверенного allowlist остаётся `True`,
    порядок в YAML: `"*": False` идёт первым, точечные правила — после.
    """
    import yaml

    adapter = OpenCodeAdapter()
    attack_tools = [
        "edit",
        "apply_patch",
        "propose_hypothesis",
        "resolve_hypotheses",
        "record_knowledge",
        "record_debt",
        "step_plan",
        "step_flow",
        "task",
        "mcp_skills_create_skill",
        "save_agent_override",
        "remote_delete_file",
    ]
    profile = AgentProfile(
        name="strict-reviewer",
        role="Reviewer",
        description="Read-only reviewer",
        permissions=AgentPermissions(write=False, mcp=False, subagents=False),
        tools=[*attack_tools, "search_knowledge"],
        # Контракт deny-by-default ниже проверяется на подтверждённом профиле;
        # БЕЗ ack экспорт отклоняется (см. test_opencode_readonly_reject_r2b).
        readonly_acknowledged=True,
    )

    output = adapter.export_agent(profile, "# Read-only skill")
    fm = yaml.safe_load(output.split("---\n")[1])

    # Тотальные запреты edit/bash/task в permission не переопределяются
    assert fm["permission"]["edit"] == "deny"
    assert fm["permission"]["bash"] == "deny"
    assert fm["permission"]["task"] == "deny"

    tools = fm["tools"]
    # Wildcard-запрет — первое правило; покрывает всё, что не разрешено явно
    assert tools["*"] is False

    # Каждый инструмент attack-set: либо явный False, либо покрыт "*": False
    for t in attack_tools:
        effective = tools.get(t, tools["*"])
        assert effective is False, f"tool {t!r} must be effectively denied when write=False"

    # Инструмент вне attack-set и вне allowlist — неизвестное имя, покрывается
    # wildcard-запретом (отсутствие ключа не означает разрешение)
    assert tools.get("weird_new_tool", tools["*"]) is False

    # Проверенный read-only инструмент остаётся разрешённым
    assert tools["search_knowledge"] is True


def test_opencode_proven_safe_tools_invariant() -> None:
    """R2: инвариант allowlist — _PROVEN_SAFE_TOOLS ∩ write-классы = ∅.

    Имена из allowlist обязаны оставаться read-only: префиксы
    get_/check_/verify_ и «безопасно звучащие» имена не дают права попасть
    в список; пересечение с классами записи должно быть пустым.
    """
    from mcp_skills.adapters.opencode import _PROVEN_SAFE_TOOLS

    write_classes = {
        # Прямые инструменты записи в код/файлы
        "edit",
        "apply_patch",
        "write_file",
        "write",
        "bash",
        "task",
        # MCP-инструменты записи knowledge/audit/планов
        "record_knowledge",
        "update_knowledge_status",
        "record_debt",
        "update_debt_status",
        "record_plan",
        "update_plan",
        "step_plan",
        "step_flow",
        "record_review",
        "propose_hypothesis",
        "resolve_hypotheses",
        "create_skill",
        # Namespaced MCP-инструменты записи
        "mcp_skills_create_skill",
        "mcp_audit_record_plan",
        "mcp_knowledge-engine_record_knowledge",
        # «Безопасно звучащие» имена, не являющиеся read-only
        "check_and_delete",
        "get_and_write",
        "verify_then_apply",
        "remote_delete_file",
        "save_agent_override",
    }

    intersection = _PROVEN_SAFE_TOOLS & write_classes
    assert not intersection, (
        f"Allowlist пересекается с write-классами: {sorted(intersection)}. "
        "Расширение allowlist требует живой проверки в OpenCode (R2)."
    )


def test_opencode_adapter_profile_tools_cannot_restore_write(tmp_path: Path) -> None:
    """Итоговые запреты имеют приоритет над profile.tools (шаг 4, план строка 58).

    Явное включение пишущих tools (edit, apply_patch, propose_hypothesis,
    resolve_hypotheses и др.) через profile.tools НЕ может восстановить запись
    при write=False; subagents=False → task: deny не обходится через tools.
    Проверяем фактические namespaced имена MCP-инструментов.
    """
    import yaml

    adapter = OpenCodeAdapter()
    profile = AgentProfile(
        name="strict-reviewer",
        role="Reviewer",
        description="Read-only reviewer",
        permissions=AgentPermissions(write=False, mcp=False, subagents=False),
        tools=[
            "edit",
            "bash",
            "apply_patch",
            "propose_hypothesis",
            "resolve_hypotheses",
            "record_knowledge",
            "record_debt",
            "step_plan",
            "step_flow",
            "task",
            "mcp_skills_create_skill",
            "search_knowledge",
        ],
        # R2 ревизия 4: подтверждённый read-only профиль (см. r2b-тесты на
        # отклонение без ack)
        readonly_acknowledged=True,
    )

    output = adapter.export_agent(profile, "# Read-only skill")
    fm = yaml.safe_load(output.split("---\n")[1])

    # Тотальные запреты edit/bash/task в permission не переопределяются
    assert fm["permission"]["edit"] == "deny"
    assert fm["permission"]["bash"] == "deny"
    assert fm["permission"]["task"] == "deny"

    # Пишущие tools из profile.tools не восстанавливают запись
    tools = fm["tools"]
    for t in ("edit", "apply_patch", "propose_hypothesis", "resolve_hypotheses", "task"):
        assert tools.get(t) is not True, f"tool {t!r} must not be enabled when write=False"
    # Namespaced MCP-инструменты записи тоже запрещены
    for t in (
        "record_knowledge",
        "record_debt",
        "step_plan",
        "step_flow",
        "mcp_skills_create_skill",
    ):
        assert tools.get(t) is not True, f"tool {t!r} must not be enabled when write=False"

    # Читающий tool, не входящий в запреты, остаётся разрешённым
    assert tools.get("search_knowledge") is True


def test_opencode_adapter_subagents_false_task_deny(tmp_path: Path) -> None:
    """subagents=False → task: deny даже при write=True и tools с 'task'.

    Это отдельное ограничение, не следствие write=False (шаг 4, план строка 56).
    """
    import yaml

    adapter = OpenCodeAdapter()
    profile = AgentProfile(
        name="lone-coder",
        role="Coder",
        description="Writable agent without subagents",
        permissions=AgentPermissions(write=True, mcp=True, subagents=False),
        tools=["task", "edit"],
    )

    output = adapter.export_agent(profile, "# Skill")
    fm = yaml.safe_load(output.split("---\n")[1])

    assert fm["permission"]["edit"] == "allow"
    assert fm["permission"]["bash"] == "allow"
    # task остаётся deny — запрет субагентов не обходится через profile.tools
    assert fm["permission"]["task"] == "deny"
    assert fm["tools"].get("task") is not True


def test_opencode_adapter_readonly_subagents_true_task_deny_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R2 п.5: write=False && subagents=True → task: deny + warning.

    Делегирование не разрешается для read-only агентов: субагент не
    наследует ограничения tools родителя и мог бы выполнять запись от его
    имени. Контракт закреплён явным deny в permission + диагностическим
    warning в лог (профиль сконфигурирован противоречиво).
    """
    import yaml

    adapter = OpenCodeAdapter()
    profile = AgentProfile(
        name="readonly-delegator",
        role="Reviewer",
        description="Read-only reviewer that wants subagents",
        permissions=AgentPermissions(write=False, mcp=True, subagents=True),
        tools=["read", "grep"],
        readonly_acknowledged=True,
    )

    with caplog.at_level(logging.WARNING, logger="mcp_skills.adapters.opencode"):
        output = adapter.export_agent(profile, "# Read-only skill")
    fm = yaml.safe_load(output.split("---\n")[1])

    assert fm["permission"]["edit"] == "deny"
    assert fm["permission"]["bash"] == "deny"
    # Контракт R2: противоречивая конфигурация → task всё равно deny
    assert fm["permission"]["task"] == "deny"
    assert "делегирование запрещено" in caplog.text.lower()


def test_opencode_adapter_writable_and_explicit_model() -> None:
    import yaml

    adapter = OpenCodeAdapter()
    profile = AgentProfile(
        name="coder",
        role="Senior Software Engineer",
        description="Writes production code",
        model="pro",
        model_name="anthropic/claude-3-7-sonnet",
        skills=["python-patterns"],
        permissions=AgentPermissions(write=True, mcp=True, subagents=True),
    )
    skills_content = "# Python Patterns\nUse type annotations."

    output = adapter.export_agent(profile, skills_content)
    parts = output.split("---\n")
    fm = yaml.safe_load(parts[1])

    assert fm["mode"] == "subagent"
    assert fm["model"] == "anthropic/claude-3-7-sonnet"
    assert fm["permission"]["edit"] == "allow"
    assert fm["permission"]["bash"] == "allow"
    assert fm["permission"]["task"] == "allow"


def test_opencode_install_workflow_path(tmp_path: Path) -> None:
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    bundler = AgentBundler(storage=storage)

    profile = AgentProfile(
        name="reviewer-agent",
        role="Reviewer",
        description="Strict auditor",
        permissions=AgentPermissions(write=False, mcp=True, subagents=False),
        # R2 ревизия 4: read-only экспорт в OpenCode требует подтверждения
        readonly_acknowledged=True,
    )
    (tmp_path / ".agents" / "agents" / "reviewer-agent").mkdir(parents=True)
    (tmp_path / ".agents" / "agents" / "reviewer-agent" / "AGENT.yaml").write_text(
        profile.model_dump_json(), encoding="utf-8"
    )

    res = bundler.install_workflow(
        name="reviewer-agent",
        target="opencode",
        project_root=tmp_path,
    )
    assert res["success"] is True
    installed_path = Path(res["path"])
    assert installed_path.exists()
    # Must install to .opencode/agents/ (plural), not .opencode/agent/
    assert installed_path == tmp_path / ".opencode" / "agents" / "reviewer-agent.md"


def test_cursor_adapter() -> None:
    adapter = CursorAdapter()
    profile = AgentProfile(
        name="frontend-dev",
        role="Frontend Specialist",
        description="React and Next.js guidelines",
        skills=["react-best-practices"],
    )
    skills_content = "# React Best Practices\nUse hooks cleanly."

    output = adapter.export_agent(profile, skills_content)
    assert "description: React and Next.js guidelines" in output
    assert "Use hooks cleanly." in output


def test_prompt_adapter() -> None:
    adapter = PromptAdapter()
    profile = AgentProfile(
        name="assistant",
        role="Helpful Assistant",
        description="Assists with coding tasks",
    )
    skills_content = "Be concise and direct."

    output = adapter.export_agent(profile, skills_content)
    assert "Role: Helpful Assistant" in output
    assert "Be concise and direct." in output


def test_agent_bundler(tmp_path: Path) -> None:
    # Инициализируем хранилище со скиллом
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skill = SkillCard(
        name="test-skill",
        title="Test Skill",
        category="planning",
        description="A test skill",
    )
    storage.index_skill(skill, content="# Test Skill Content\nFollow rules.")

    bundler = AgentBundler(storage=storage)

    # Профиль задан программно
    profile = AgentProfile(
        name="test-agent",
        role="Tester",
        description="Test agent role",
        skills=["test-skill"],
    )

    result_json = bundler.bundle_agent(profile=profile, target="antigravity", project_root=tmp_path)
    parsed = json.loads(result_json)
    assert parsed["name"] == "test-agent"
    assert "Follow rules." in parsed["system_prompt"]
