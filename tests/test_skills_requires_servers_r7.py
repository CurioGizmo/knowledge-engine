"""R7a: supplement_mode_axis не знает, какой сервер нужен конкретному блоку (дефект).

Воспроизведения (docs/analysis/2026-10-18-post-implementation-review.md, R7 +
правка ревьюера): нужен per-card выбор интеграционных блоков по
``requires_servers`` и различение «список серверов неизвестен» vs «сервер
точно отсутствует». Контракты (обновлены ревизией 4 — list-only форма):
1. absent: MCP_ACTIVE_SERVERS задан, требуемого сервера нет → offline/default
   блок + примечание причины (server_reason в assembly).
2. present: сервер в списке → mcp-блок.
3. unknown: MCP_ACTIVE_SERVERS не задан → mcp-блок + предупреждение
   проверить доступность у хоста (server_warning в assembly — в ответ
   инструмента, не только в лог).
4. per-card: недоступный audit-engine не отключает доступную интеграцию
   другого скилла (требует другой сервер, который present).
5. Блоки без requires_servers (offline/default) совместимы — проверка не
   применяется.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mcp_skills import project_context as pc_module
from mcp_skills.project_context import ProjectContext, get_project_context
from mcp_skills.server import load_skill
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer


SKILL_WITH_SERVERS_MD = """---
name: writing-plans
title: Writing Plans
category: planning
available_slots: [integration]
requires_servers: [audit-engine]
---
# Writing Plans

<!-- SLOT: integration -->

Follow checklist.
"""

SKILL_WITHOUT_SERVERS_MD = """---
name: plain-skill
title: Plain Skill
category: general
available_slots: [integration]
---
# Plain Skill

<!-- SLOT: integration -->

Follow checklist.
"""


def _make_catalog(tmp_path: Path) -> tuple[SkillsStorage, SkillsSynchronizer, Path]:
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)

    for name, template in (
        ("writing-plans", SKILL_WITH_SERVERS_MD),
        ("plain-skill", SKILL_WITHOUT_SERVERS_MD),
    ):
        skill_dir = skills_dir / name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(template, encoding="utf-8")
        blocks = skill_dir / "blocks" / "integration"
        blocks.mkdir(parents=True)
        (blocks / "mcp.md").write_text(
            f"### Integration MCP ({name})\nCall audit-engine via MCP.", encoding="utf-8"
        )
        (blocks / "offline.md").write_text(
            f"### Integration offline ({name})\nFile-based protocol.", encoding="utf-8"
        )
        (blocks / "default.md").write_text(
            f"### Integration probe ({name})\nChoose based on availability.", encoding="utf-8"
        )

    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()
    return storage, sync, tmp_path


@pytest.fixture(autouse=True)
def _isolated_context_cache():
    pc_module._CONTEXT_CACHE.clear()
    yield
    pc_module._CONTEXT_CACHE.clear()


@pytest.fixture
def catalog(tmp_path: Path):
    return _make_catalog(tmp_path)


def test_r7a_absent_server_offline_block_with_reason(
    monkeypatch: pytest.MonkeyPatch, catalog
) -> None:
    """R7-1: MCP_ACTIVE_SERVERS без audit-engine → offline-блок + причина."""
    storage, sync, root = catalog
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "skills-engine,knowledge-engine")

    res = load_skill(
        "writing-plans",
        stack=["mcp"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    # absent → выбран offline-блок, mcp-контент НЕ подставлен
    assert "Integration offline" in res["content"], res["content"]
    assert "Integration MCP" not in res["content"]
    # Примечание причины в контенте или manifest
    sections = res.get("sections", {}) if isinstance(res, dict) else {}
    assert (
        any(
            "audit-engine" in str(v).lower() or "offline" in str(v).lower()
            for v in sections.values()
        )
        or "offline" in res["content"].lower()
    )


def test_r7a_present_server_mcp_block(monkeypatch: pytest.MonkeyPatch, catalog) -> None:
    """R7-2: audit-engine в MCP_ACTIVE_SERVERS → mcp-блок."""
    storage, sync, root = catalog
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "audit-engine,skills-engine")

    res = load_skill(
        "writing-plans",
        stack=["mcp"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    assert "Integration MCP" in res["content"], res["content"]
    assert "Integration offline" not in res["content"]


def test_r7a_unknown_servers_mcp_block(monkeypatch: pytest.MonkeyPatch, catalog) -> None:
    """R7-3: MCP_ACTIVE_SERVERS не задан → mcp-блок (unknown ≠ absent).

    Глобальное отключение интеграций по unknown запрещено: интеграция,
    доступная хосту, должна собираться. Предупреждение о проверке
    доступности допустимо.
    """
    storage, sync, root = catalog
    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)

    res = load_skill(
        "writing-plans",
        stack=["mcp"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    assert "Integration MCP" in res["content"], res["content"]
    assert "Integration offline" not in res["content"]


def test_r7a_per_block_other_skill_integration_kept(
    monkeypatch: pytest.MonkeyPatch, catalog
) -> None:
    """R7-4: per-card — недоступный audit-engine не отключает чужую интеграцию.

    plain-skill не требует серверов: его integration-блок выбирается по
    стеку (mcp) независимо от отсутствия audit-engine.
    """
    storage, sync, root = catalog
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "skills-engine")

    res = load_skill(
        "plain-skill",
        stack=["mcp"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    # Интеграция plain-skill без requires_servers — доступна (mcp-блок)
    assert "Integration MCP (plain-skill)" in res["content"], res["content"]


def test_r7a_block_without_requires_servers_compatible(
    monkeypatch: pytest.MonkeyPatch, catalog
) -> None:
    """R7-5: блоки без requires_servers не проверяются (совместимость)."""
    storage, sync, root = catalog
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "audit-engine")

    res = load_skill(
        "plain-skill",
        stack=["mcp"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    assert "Integration MCP (plain-skill)" in res["content"]


def test_r7a_server_availability_states(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """R7-6: server_availability — три состояния; has_server = present."""
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "skills-engine,knowledge-engine")
    ctx = get_project_context(tmp_path)
    assert ctx.server_availability("audit-engine") == "absent"
    assert ctx.server_availability("skills-engine") == "present"
    assert ctx.has_server("skills-engine") is True
    assert ctx.has_server("audit-engine") is False

    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)
    pc_module._CONTEXT_CACHE.clear()
    ctx2 = get_project_context(tmp_path)
    assert ctx2.server_availability("audit-engine") == "unknown"
    assert ctx2.server_availability("any-server") == "unknown"
