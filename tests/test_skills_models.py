"""Тесты для Pydantic DTO моделей и исключений mcp_skills."""

import pytest
from pydantic import ValidationError

from mcp_skills.errors import (
    CircularExtensionError,
    InvalidSkillFormatError,
    SkillNotFoundError,
    SkillsError,
)
from mcp_skills.models import (
    AgentPermissions,
    AgentProfile,
    SkillCard,
)


def test_skill_card_defaults() -> None:
    card = SkillCard(
        name="writing-plans",
        title="Writing Plans",
        category="planning",
    )
    assert card.name == "writing-plans"
    assert card.category == "planning"
    assert card.kind == "skill"
    assert card.merge_strategy == "append"
    assert card.scope == "global"
    assert card.available_slots == []


def test_agent_profile_with_permissions() -> None:
    profile = AgentProfile(
        name="plan-reviewer",
        role="Plan Quality Gate",
        description="Audits plans against invariants",
        skills=["writing-plans"],
    )
    assert profile.name == "plan-reviewer"
    assert profile.model == "pro"
    assert profile.permissions.write is False
    assert profile.permissions.mcp is True


def test_custom_permissions() -> None:
    perms = AgentPermissions(write=True, mcp=False, subagents=True)
    profile = AgentProfile(
        name="coder",
        role="Code Implementer",
        description="Writes code",
        permissions=perms,
        instructions="Focus on clean code.",
    )
    assert profile.permissions.write is True
    assert profile.permissions.mcp is False
    assert profile.permissions.subagents is True
    assert profile.instructions == "Focus on clean code."


def test_agent_override_model() -> None:
    from mcp_skills.models import AgentOverride

    override = AgentOverride(
        base_agent="code-reviewer",
        project="demo-repo",
        target="opencode",
        model="flash",
        model_name="anthropic/claude-3-7-sonnet",
        tools=["read_file", "grep_search"],
        permissions=AgentPermissions(write=True, mcp=True, subagents=True),
        extra_skills=["security-review"],
        instructions_suffix="Extra rule: check OWASP.",
    )
    assert override.base_agent == "code-reviewer"
    assert override.project == "demo-repo"
    assert override.target == "opencode"
    assert override.model == "flash"
    assert override.model_name == "anthropic/claude-3-7-sonnet"
    assert override.tools == ["read_file", "grep_search"]
    assert override.permissions.subagents is True
    assert override.extra_skills == ["security-review"]
    assert override.instructions_suffix == "Extra rule: check OWASP."


def test_domain_exceptions_hierarchy() -> None:
    from mcp_skills.errors import MaxExtensionDepthError

    assert issubclass(SkillNotFoundError, SkillsError)
    assert issubclass(CircularExtensionError, SkillsError)
    assert issubclass(MaxExtensionDepthError, SkillsError)
    assert issubclass(InvalidSkillFormatError, SkillsError)


def test_skill_card_aliases_coercion() -> None:
    # Scalar string
    c1 = SkillCard(name="s1", title="S1", aliases="ускорить базу")
    assert c1.aliases == ["ускорить базу"]

    # Comma-separated string
    c2 = SkillCard(name="s2", title="S2", aliases="ускорить базу, тюнинг sql")
    assert c2.aliases == ["ускорить базу", "тюнинг sql"]

    # List with duplicates and spaces
    c3 = SkillCard(name="s3", title="S3", aliases=["  foo  ", "bar", "foo", ""])
    assert c3.aliases == ["foo", "bar"]

    # None
    c4 = SkillCard(name="s4", title="S4", aliases=None)
    assert c4.aliases == []


def test_flow_definition_aliases_coercion() -> None:
    from mcp_skills.models import FlowDefinition, FlowStage

    stage = FlowStage(id="s1", title="Stage 1", instruction="Do work")

    # Scalar string
    f1 = FlowDefinition(
        name="f1",
        title="Flow 1",
        description="Desc",
        stages={"s1": stage},
        stage_order=["s1"],
        aliases="выполни план",
    )
    assert f1.aliases == ["выполни план"]

    # Comma-separated string
    f2 = FlowDefinition(
        name="f2",
        title="Flow 2",
        description="Desc",
        stages={"s1": stage},
        stage_order=["s1"],
        aliases="выполни план, реализация фичи",
    )
    assert f2.aliases == ["выполни план", "реализация фичи"]

    # List with duplicates and spaces
    f3 = FlowDefinition(
        name="f3",
        title="Flow 3",
        description="Desc",
        stages={"s1": stage},
        stage_order=["s1"],
        aliases=["  plan  ", "feature", "plan", ""],
    )
    assert f3.aliases == ["plan", "feature"]

    # Default empty
    f4 = FlowDefinition(
        name="f4",
        title="Flow 4",
        description="Desc",
        stages={"s1": stage},
        stage_order=["s1"],
    )
    assert f4.aliases == []
