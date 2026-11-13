"""R2 (ревизия 4): строгий read-only профиль в OpenCode отклоняется при экспорте.

Контекст (правка ревьюера к R2): wildcard-запрет ``tools: {"*": false}`` не
гарантирует запрет неизвестных инструментов — семантика приоритета правил в
OpenCode не подтверждена живой проверкой. Точечные запреты только для
profile.tools проблему не решают: неизвестные инструменты остаются
доступными. Поэтому строгий read-only профиль (write=False) считается
НЕПОДДЕРЖИВАЕМЫМ для экспорта в OpenCode: адаптер обязан ОТКЛОНИТЬ экспорт
с понятной ошибкой вместо небезопасного артефакта с предупреждением.

Условия отклонения (все одновременно):
- target=opencode, permissions.write=False
- в конфигурации нет явного «я понимаю риск» — флаг профиля
  ``readonly_acknowledged=True`` (документирован как подтверждение владельцем
  фактической проверки в целевой версии клиента; снимает отклонение).
"""

from __future__ import annotations

import logging

import pytest
import yaml

from mcp_skills.adapters.opencode import OpenCodeAdapter
from mcp_skills.errors import UnsupportedReadonlyProfileError
from mcp_skills.models import AgentPermissions, AgentProfile


def _readonly_profile(**kwargs: object) -> AgentProfile:
    base: dict[str, object] = {
        "name": "strict-reviewer",
        "role": "Reviewer",
        "description": "Read-only reviewer",
        "permissions": AgentPermissions(write=False, mcp=True, subagents=False),
    }
    base.update(kwargs)
    return AgentProfile(**base)  # type: ignore[arg-type]


def test_r2b_readonly_export_rejected() -> None:
    """write=False без acknowledgment → экспорт отклонён (ошибка, не warning)."""
    adapter = OpenCodeAdapter()
    with pytest.raises(
        UnsupportedReadonlyProfileError, match="read-only|readonly|не поддерживается"
    ):
        adapter.export_agent(_readonly_profile(), "# Skill")


def test_r2b_rejection_happens_before_any_warnings(caplog: pytest.LogCaptureFixture) -> None:
    """MINOR-3: отклонение — ДО каких-либо warning-логов и сборки артефакта.

    Профиль про/flash без model-маппинга генерирует warning о модели;
    subagents=True при write=False — ещё один warning. Оба НЕ должны
    выпускаться: профиль отклоняется до всякой генерации.
    """
    adapter = OpenCodeAdapter()
    profile = _readonly_profile(
        model="flash",  # без model_name → warning о маппинге модели
        tools=["read", "grep"],
        # subagents остаётся False по умолчанию (permission build не нужен)
    )
    with caplog.at_level(logging.WARNING, logger="mcp_skills.adapters.opencode"):
        with pytest.raises(UnsupportedReadonlyProfileError):
            adapter.export_agent(profile, "# Skill")
    assert not caplog.records, (
        "отклонение должно происходить до выпуска любых warning-логов "
        "(R2 MINOR-3): profile rejected before artifact assembly"
    )


def test_r2b_readonly_export_rejected_not_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Отклонение — исключение: артефакт вообще не формируется."""
    adapter = OpenCodeAdapter()
    with caplog.at_level(logging.WARNING, logger="mcp_skills.adapters.opencode"):
        with pytest.raises(UnsupportedReadonlyProfileError):
            adapter.export_agent(_readonly_profile(tools=["read", "grep"]), "# Skill")
    # warning о «остаточном риске» больше не выпускается как замена отказа
    assert "остаточный риск" not in caplog.text.lower()


def test_r2b_readonly_acknowledged_still_exports() -> None:
    """readonly_acknowledged=True → экспорт возможен (владелец подтвердил риск).

    Контракт deny-by-default (wildcard + точечные разрешения) сохраняется
    для подтверждённых профилей.
    """
    adapter = OpenCodeAdapter()
    profile = _readonly_profile(
        readonly_acknowledged=True,
        tools=["read", "search_knowledge"],
    )
    output = adapter.export_agent(profile, "# Skill")
    fm = yaml.safe_load(output.split("---\n")[1])
    assert fm["tools"]["*"] is False
    assert fm["tools"]["read"] is True
    assert fm["tools"]["search_knowledge"] is True


def test_r2b_writable_profile_export_unaffected() -> None:
    """write=True → прежнее поведение (отклонение не касается write-профилей)."""
    adapter = OpenCodeAdapter()
    profile = AgentProfile(
        name="coder",
        role="Engineer",
        description="Writer",
        permissions=AgentPermissions(write=True, mcp=True, subagents=True),
        tools=["read", "edit"],
    )
    output = adapter.export_agent(profile, "# Skill")
    fm = yaml.safe_load(output.split("---\n")[1])
    assert fm["tools"]["read"] is True
    assert fm["tools"]["edit"] is True
    assert "*" not in fm["tools"]
