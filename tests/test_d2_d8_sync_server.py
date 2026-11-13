"""Тесты D2 и D8 плана review-fixes (synchronizer / server).

D2 — локальные скиллы проекта (.agents/skills) + детерминизм и видимость коллизий:
- get_synchronizer добавляет .agents/skills ПОСЛЕ skills_base (last-wins: локальный
  одноимённый скилл полностью заменяет репозиторный);
- обход каталогов детерминирован (sorted rglob);
- коллизии имён видны в SkillsSyncResult.collisions (раньше — только logger.warning).

D8 — MCP-обёртка step_flow принимает human_confirmed и прокидывает в фасад:
этап с require_human_confirm без флага блокируется (gate_blocked), с
human_confirmed=true проходит.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_skills.server import create_server, get_synchronizer
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer

_SKILL_TMPL = """---
name: {name}
title: {title}
category: planning
---
# {title}

{body}
"""


def _write_skill(base: Path, name: str, title: str, body: str) -> None:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        _SKILL_TMPL.format(name=name, title=title, body=body), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# D2: last-wins + коллизии наружу
# ---------------------------------------------------------------------------


def test_sync_collision_last_wins_and_reported(tmp_path: Path) -> None:
    """Одноимённый скилл из второго каталога побеждает (last-wins), коллизия — в result."""
    repo_skills = tmp_path / "repo" / "skills"
    local_skills = tmp_path / "proj" / ".agents" / "skills"
    repo_skills.mkdir(parents=True)
    local_skills.mkdir(parents=True)

    _write_skill(repo_skills, "demo-skill", "Repo Demo", "REPO-BODY")
    _write_skill(local_skills, "demo-skill", "Local Demo", "LOCAL-BODY")
    _write_skill(repo_skills, "other-skill", "Other", "OTHER-BODY")

    storage = SkillsStorage(tmp_path / "skills.db")
    sync = SkillsSynchronizer(storage, [repo_skills, local_skills])
    result = sync.sync()

    # Коллизия видна потребителю (не только в логах)
    assert len(result.collisions) == 1
    collision = result.collisions[0]
    assert collision["skill"] == "demo-skill"
    assert str(repo_skills) in collision["previous"]
    assert str(local_skills) in collision["current"]

    # last-wins: проиндексирован локальный вариант
    found = storage.get_skill("demo-skill")
    assert found is not None
    card, body = found
    assert card.title == "Local Demo"
    assert "LOCAL-BODY" in body
    # несвязанный скилл на месте
    assert storage.get_skill("other-skill") is not None


def test_sync_deterministic_order_within_dir(tmp_path: Path) -> None:
    """sorted(rglob): повторяющиеся запуски дают идентичный результат синхронизации."""
    base = tmp_path / "skills"
    base.mkdir()
    for name in ["z-skill", "a-skill", "m-skill"]:
        _write_skill(base, name, name.title(), f"BODY-{name}")

    storage = SkillsStorage(tmp_path / "skills.db")
    sync = SkillsSynchronizer(storage, [base])
    r1 = sync.sync()
    r2 = sync.sync()  # второй прогон — ничего не dirty
    assert r1.indexed_count == 3
    assert r2.indexed_count == 0
    assert r1.collisions == [] and r2.collisions == []


def test_get_synchronizer_includes_local_agents_skills(tmp_path: Path) -> None:
    """get_synchronizer добавляет .agents/skills ПОСЛЕ skills_base репозитория."""
    proj = tmp_path / "proj"
    (proj / ".agents" / "skills").mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / "skills").mkdir(parents=True)

    sync = get_synchronizer(project_root=proj, skills_dir=repo)
    dirs = [str(d) for d in sync.skills_dirs]
    assert len(dirs) == 2
    assert dirs[0] == str(repo / "skills")  # репозиторий первым
    assert dirs[1] == str(proj / ".agents" / "skills")  # локальный — вторым (last-wins)


def test_get_synchronizer_no_local_dir_without_project_root(tmp_path: Path) -> None:
    """Без project_root список каталогов прежний (регресс-защита)."""
    repo = tmp_path / "repo"
    (repo / "skills").mkdir(parents=True)
    sync = get_synchronizer(skills_dir=repo)
    assert [str(d) for d in sync.skills_dirs] == [str(repo / "skills")]


# ---------------------------------------------------------------------------
# D8: human_confirmed в MCP-обёртке step_flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_step_flow_human_confirmed(tmp_path: Path) -> None:
    """MCP step_flow: require_human_confirm блокирует без флага и проходит с human_confirmed=true."""
    server = create_server(project_root=tmp_path)

    # Старт сессии feature-delivery: первый этап spec_analysis (require_human_confirm)
    start = await server.call_tool("step_flow", {"flow": "feature-delivery"})
    start_data = start.structured_content or json.loads(start.content[0].text)
    assert start_data.get("session_id")

    # advance БЕЗ флага — gate_blocked (notes больше не прокси, D4)
    blocked = await server.call_tool("step_flow", {"action": "advance", "notes": "согласовано"})
    blocked_data = blocked.structured_content or json.loads(blocked.content[0].text)
    assert blocked_data.get("status") == "gate_blocked"
    assert any("human_confirmed" in b for b in blocked_data.get("blockers", []))

    # advance с human_confirmed=true — этап проходит
    ok = await server.call_tool("step_flow", {"action": "advance", "human_confirmed": True})
    ok_data = ok.structured_content or json.loads(ok.content[0].text)
    assert ok_data.get("stage", {}).get("id") == "plan_draft"


# ---------------------------------------------------------------------------
# D3: стейл-индекс — внешние правки видны без перезапуска сервера
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_external_edit_visible_without_restart(tmp_path: Path) -> None:
    """Внешняя правка скилла видна через search/load_skill без пересоздания сервера (D3).

    Дефект (verdict_5): create_server синкает индекс один раз при старте,
    а MCP-обёртки передают инжектированный synchronizer и пропускают sync —
    изменённый на диске скилл оставался стейловым до перезапуска сервера.
    """
    skills_dir = tmp_path / ".agents" / "skills"
    skills_dir.mkdir(parents=True)
    skill_md = skills_dir / "stale-demo" / "SKILL.md"
    skill_md.parent.mkdir()
    skill_md.write_text(
        _SKILL_TMPL.format(name="stale-demo", title="Stale Demo", body="ORIGINALE-TOKEN-AAA"),
        encoding="utf-8",
    )

    server = create_server(project_root=tmp_path)

    loaded_before = await server.call_tool("load_skill", {"name": "stale-demo"})
    before_data = loaded_before.structured_content or json.loads(loaded_before.content[0].text)
    assert "ORIGINALE-TOKEN-AAA" in before_data["content"]

    # Внешняя правка файла на диске (сервер НЕ перезапускаем)
    skill_md.write_text(
        _SKILL_TMPL.format(name="stale-demo", title="Stale Demo", body="AKTUALNO-TOKEN-BBB"),
        encoding="utf-8",
    )

    loaded_after = await server.call_tool("load_skill", {"name": "stale-demo"})
    after_data = loaded_after.structured_content or json.loads(loaded_after.content[0].text)
    assert "AKTUALNO-TOKEN-BBB" in after_data["content"], (
        "load_skill вернул стейловый каркас: внешняя правка не видна без перезапуска"
    )
    assert "ORIGINALE-TOKEN-AAA" not in after_data["content"]

    # search_skills также видит изменённый скилл (title/description) по новому контенту
    searched = await server.call_tool(
        "search_skills", {"query": "stale-demo", "category": "planning"}
    )
    search_data = searched.structured_content or json.loads(searched.content[0].text)
    assert search_data.get("count", 0) >= 1


@pytest.mark.asyncio
async def test_mcp_external_new_skill_visible_without_restart(tmp_path: Path) -> None:
    """Новый скилл, добавленный на диск после старта сервера, находится без перезапуска (D3)."""
    skills_dir = tmp_path / ".agents" / "skills"
    skills_dir.mkdir(parents=True)
    server = create_server(project_root=tmp_path)

    # Скилла ещё нет. E-план расширил fuzzy-канал на содержимое тел скиллов:
    # обычные английские слова в телах (напр. 'fresh' в brainstorming) легитимно
    # матчатся fuzzy. Контракт D3 — сам скилла fresh-added в выдаче быть не должно.
    res1 = await server.call_tool("search_skills", {"query": "fresh-added", "category": "planning"})
    data1 = res1.structured_content or json.loads(res1.content[0].text)
    assert all(c.get("name") != "fresh-added" for c in data1.get("results", [])), (
        "skill 'fresh-added' found before it was created on disk"
    )

    # Добавляем скилл на диск, сервер не трогаем
    _write_skill(skills_dir, "fresh-added", "Fresh Added", "FRESH-BODY-MARKER")

    res2 = await server.call_tool("load_skill", {"name": "fresh-added"})
    data2 = res2.structured_content or json.loads(res2.content[0].text)
    assert data2.get("name") == "fresh-added"
    assert "FRESH-BODY-MARKER" in data2["content"]
