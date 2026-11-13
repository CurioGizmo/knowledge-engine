"""Тесты D6: единый ProjectContext и дополнение явного стека осью режима.

План review-fixes (docs/plans/2026-10-15-review-fixes-plan.md, D6) + решение
по вопросу №2 (verdict_7c): явный stack дополняется ТОЛЬКО осью режима
(mcp/offline, detect_mode); языки/фреймворки НЕ дополняются. Режим определяется
единым ProjectContext один раз для search_skills / load_skill / bundle_agent.

Группы:
- PC  — кэширование ProjectContext (detect_mode вызывается один раз);
- LS  — load_skill: явный стек + дополнение режима (сценарий writing-plans);
- SRL — согласованность search_skills / load_skill / bundle_agent;
- REG — регрессии автодетекта без явного стека.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mcp_skills import project_context as pc_module
from mcp_skills.models import AgentProfile
from mcp_skills.server import _resolve_search_stack, load_skill, search_skills
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer
from mcp_skills.bundler import AgentBundler
from mcp_skills.slot_compiler import SlotCompiler

# Скилл с дуальным слотом integration: default (зонд) / mcp / offline —
# как writing-plans из сценария D6 (verdict_7c, вопрос 2).
SKILL_MD = """---
name: writing-plans
title: Writing Plans
category: planning
available_slots: [integration]
---
# Writing Plans

<!-- SLOT: integration -->

Follow checklist.
"""


def _make_dual_skill_project(tmp_path: Path) -> tuple[SkillsStorage, SkillsSynchronizer, Path]:
    """Фикстура: скилл с блоками integration/{default,mcp,offline}.md."""
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)

    skill_dir = skills_dir / "writing-plans"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")

    blocks = skill_dir / "blocks" / "integration"
    blocks.mkdir(parents=True)
    (blocks / "default.md").write_text(
        "### Integration probe\nCheck MCP availability and choose.", encoding="utf-8"
    )
    (blocks / "mcp.md").write_text(
        "### Integration MCP\nCall tools via MCP protocol.", encoding="utf-8"
    )
    (blocks / "offline.md").write_text(
        "### Integration offline\nFile-based protocol, no tool calls.", encoding="utf-8"
    )

    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()
    return storage, sync, tmp_path


def _clear_pc_cache() -> None:
    pc_module._CONTEXT_CACHE.clear()


@pytest.fixture(autouse=True)
def _isolated_context_cache():
    """Каждый тест стартует с чистым кэшем ProjectContext."""
    _clear_pc_cache()
    yield
    _clear_pc_cache()


# ---------- PC. Кэширование ProjectContext ----------


def test_pc1_detect_mode_called_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """PC1: ProjectContext кэширует detect_mode — два обращения, один вызов."""
    calls = {"n": 0}
    real_detect_mode = pc_module.detect_mode

    def counting_detect_mode(root: Path | None = None) -> str | None:
        calls["n"] += 1
        return real_detect_mode(root)

    monkeypatch.setattr(pc_module, "detect_mode", counting_detect_mode)

    ctx1 = pc_module.get_project_context(tmp_path)
    ctx2 = pc_module.get_project_context(tmp_path)
    assert calls["n"] == 1
    assert ctx1 is ctx2
    assert ctx1.mode == real_detect_mode(tmp_path)


def test_pc2_cache_invalidated_by_env_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PC2: смена MCP_ACTIVE_SERVERS меняет режим (кэш не протухает между тестами)."""
    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)
    ctx_markers = pc_module.get_project_context(tmp_path)
    assert ctx_markers.mode is None  # без маркеров и env

    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "skills-engine")
    ctx_mcp = pc_module.get_project_context(tmp_path)
    assert ctx_mcp.mode == "mcp"

    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "")
    ctx_offline = pc_module.get_project_context(tmp_path)
    assert ctx_offline.mode == "offline"


def test_pc3_stack_with_sources(tmp_path: Path) -> None:
    """PC3: технологии возвращаются с источниками обнаружения."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "d6-demo"\ndependencies = ["fastapi>=0.100"]\n', encoding="utf-8"
    )
    ctx = pc_module.get_project_context(tmp_path)
    assert set(ctx.stack) >= {"python", "fastapi"}
    assert ctx.stack_sources["python"] == "pyproject.toml"
    assert ctx.stack_sources["fastapi"] == "pyproject.toml"


# ---------- LS. load_skill: явный стек + ось режима ----------


def test_ls1_explicit_stack_supplemented_with_mcp_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """LS1 (сценарий D6): stack=['fastapi'] в MCP-проекте → 'mcp' добавлен, подставлен integration/mcp.md."""
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "skills-engine,knowledge-engine")
    storage, sync, root = _make_dual_skill_project(tmp_path)

    res = load_skill(
        "writing-plans",
        stack=["fastapi"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    # Зонд default НЕ подставлен — выбран mcp-блок
    assert "Integration MCP" in res["content"]
    assert "Integration probe" not in res["content"]
    # Ось режима добавлена к явному стеку
    assert res["stack_applied"] == ["fastapi", "mcp"]
    assert res["stack_source"] == "explicit"
    assert res["stack_supplemented"] == ["mcp"]


def test_ls2_explicit_stack_offline_not_overwritten(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """LS2: явный 'offline' в стеке не перетирается (MCP-окружение его не заменяет на mcp)."""
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "skills-engine")
    storage, sync, root = _make_dual_skill_project(tmp_path)

    res = load_skill(
        "writing-plans",
        stack=["fastapi", "offline"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    assert "Integration offline" in res["content"]
    assert "mcp" not in res["stack_applied"]
    assert res["stack_applied"] == ["fastapi", "offline"]
    assert res["stack_supplemented"] == []


def test_ls3_explicit_stack_no_mode_detected_no_supplement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """LS3: режим неизвестен (нет env/маркеров) → стек не дополняется."""
    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)
    storage, sync, root = _make_dual_skill_project(tmp_path)

    res = load_skill(
        "writing-plans",
        stack=["fastapi"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    assert res["stack_applied"] == ["fastapi"]
    assert res["stack_supplemented"] == []
    # Зонд default — единственный доступный вариант без оси режима
    assert "Integration probe" in res["content"]


def test_ls4_mode_from_agents_markers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """LS4: без MCP_ACTIVE_SERVERS папка .agents — это данные (не mcp). При явном env — mcp."""
    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)
    storage, sync, root = _make_dual_skill_project(tmp_path)
    (root / ".agents" / "audit").mkdir(parents=True)

    res = load_skill(
        "writing-plans",
        stack=["fastapi"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    # Без MCP_ACTIVE_SERVERS режим mcp не навязывается
    assert res["stack_applied"] == ["fastapi"]

    # С явным MCP_ACTIVE_SERVERS режим mcp подключается
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "audit-engine")
    res_mcp = load_skill(
        "writing-plans",
        stack=["fastapi"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    assert res_mcp["stack_applied"] == ["fastapi", "mcp"]
    assert "Integration MCP" in res_mcp["content"]


def test_ls5_languages_not_supplemented(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """LS5: язык/фреймворк из автодетекта НЕ подмешиваются в явный стек."""
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "skills-engine")
    storage, sync, root = _make_dual_skill_project(tmp_path)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "d6"\ndependencies = ["pydantic>=2"]\n', encoding="utf-8"
    )

    res = load_skill(
        "writing-plans",
        stack=["fastapi"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    assert res["stack_applied"] == ["fastapi", "mcp"]
    assert "python" not in res["stack_applied"]
    assert "pydantic" not in res["stack_applied"]


# ---------- SRL. Единый режим в search / load_skill / bundler ----------


def test_srl1_same_mode_across_search_load_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SRL1: search_skills / load_skill / bundle_agent дают одинаковый режим для корня."""
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "skills-engine")
    storage, sync, root = _make_dual_skill_project(tmp_path)

    load_res = load_skill(
        "writing-plans",
        stack=["fastapi"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )

    bundler = AgentBundler(storage=storage, compiler=SlotCompiler(storage))
    bundle = bundler.bundle_agent(
        profile=AgentProfile(
            name="plans-agent",
            description="Plans guard",
            skills=["writing-plans"],
            instructions="Plan.",
        ),
        target="prompt",
        project_root=root,
    )

    # search_skills ПОСЛЕДНИМ: его внутренний sync с дефолтными dirs
    # (resolve_skills_repo_dir без skills_dir) может вычистить фикстурную
    # карточку из общего storage.
    search_resp = search_skills(
        query="writing",
        project_stack=["fastapi"],
        storage=storage,
        project_root=root,
        skills_dir=root / "skills",
    )

    def modes_in(items: list[str]) -> set[str]:
        return {v for v in items if v in ("mcp", "offline")}

    search_modes = modes_in(search_resp["stack_filter"]["project_stack"])
    load_modes = modes_in(load_res["stack_applied"])
    assert search_modes == {"mcp"}
    assert load_modes == {"mcp"}
    # Бандлер: mcp-блок интеграции в сборке = тот же режим для того же корня
    assert "Integration MCP" in bundle
    assert "Integration probe" not in bundle


# ---------- REG. Регрессия автодетекта без явного стека ----------


def test_reg1_autodetect_without_explicit_stack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """REG1: без явного стека автодетект прежний (манифесты + режим; python+fastapi+mcp)."""
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "skills-engine")
    storage, sync, root = _make_dual_skill_project(tmp_path)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "d6"\ndependencies = ["fastapi>=0.100"]\n', encoding="utf-8"
    )

    res = load_skill("writing-plans", storage=storage, synchronizer=sync, project_root=root)
    assert res["stack_source"] == "autodetect"
    assert set(res["stack_applied"]) >= {"python", "fastapi", "mcp"}
    assert "Integration MCP" in res["content"]


def test_reg2_search_auto_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """REG2: без маркеров MCP режим остаётся unknown, 'mcp' не подмешивается в search-стек."""
    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "d6"\ndependencies = ["fastapi>=0.100"]\n', encoding="utf-8"
    )
    resolved = _resolve_search_stack("auto", tmp_path)
    assert resolved is not None
    assert set(resolved) == {"python", "fastapi"}  # mode is unknown, no "mcp" assumption

    # А с активным MCP env -> "mcp" подтверждён и входит в стек
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "skills")
    _clear_pc_cache()
    resolved_mcp = _resolve_search_stack("auto", tmp_path)
    assert resolved_mcp is not None
    assert set(resolved_mcp) == {"python", "fastapi", "mcp"}


def test_reg3_no_manifests_no_env_filter_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """REG3: без манифестов/маркеров/env фильтр 'auto' выключен (resolved None)."""
    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)
    empty = tmp_path / "empty"
    empty.mkdir()
    resolved = _resolve_search_stack("auto", empty)
    assert resolved is None


def test_reg4_explicit_search_stack_supplemented_with_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """REG4: явный стек в search_skills тоже дополняется осью режима (единая семантика D6)."""
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "skills-engine")
    storage, sync, root = _make_dual_skill_project(tmp_path)

    resp = search_skills(
        query="writing",
        project_stack=["fastapi"],
        storage=storage,
        project_root=root,
        skills_dir=root / "skills",
    )
    assert resp["stack_filter"]["project_stack"] == ["fastapi", "mcp"]


def test_reg5_explicit_search_stack_offline_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """REG5: явный offline в search-стеке сохраняется, mcp не добавляется."""
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "skills-engine")
    storage, sync, root = _make_dual_skill_project(tmp_path)

    resp = search_skills(
        query="writing",
        project_stack=["fastapi", "offline"],
        storage=storage,
        project_root=root,
        skills_dir=root / "skills",
    )
    assert resp["stack_filter"]["project_stack"] == ["fastapi", "offline"]


def test_reg6_search_no_mode_no_supplement(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """REG6: режим неизвестен → явный search-стек не дополняется (фильтр не искажается)."""
    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)
    storage, sync, root = _make_dual_skill_project(tmp_path)

    resp = search_skills(
        query="writing",
        project_stack=["fastapi"],
        storage=storage,
        project_root=root,
        skills_dir=root / "skills",
    )
    assert resp["stack_filter"]["project_stack"] == ["fastapi"]


# Дополнительно: stack_supplemented отсутствует при автодетекте (поле только для явного стека)
def test_ls6_autodetect_has_no_supplemented_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "skills-engine")
    storage, sync, root = _make_dual_skill_project(tmp_path)

    res = load_skill("writing-plans", storage=storage, synchronizer=sync, project_root=root)
    assert res["stack_source"] == "autodetect"
    assert "stack_supplemented" not in res or res["stack_supplemented"] == []


def test_step_11_acceptance_unknown_mode_and_live_qa_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 1. Python-проект без маркеров MCP: mode остаётся unknown (None) во всех трёх входах
    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)
    storage, sync, root = _make_dual_skill_project(tmp_path)
    (root / "pyproject.toml").write_text(
        '[project]\nname="demo"\ndependencies=["fastapi"]\n', encoding="utf-8"
    )

    ctx = pc_module.get_project_context(root)
    assert ctx.mode is None
    assert "python" in ctx.stack
    assert "fastapi" in ctx.stack
    assert "mcp" not in ctx.stack
    assert "offline" not in ctx.stack

    # search_skills("auto") не добавляет "mcp"
    resolved_search = _resolve_search_stack("auto", root)
    assert resolved_search is not None
    assert "mcp" not in resolved_search
    assert "offline" not in resolved_search

    # load_skill без явного стека -> fallback на default-зонд (режим unknown)
    res_load = load_skill("writing-plans", storage=storage, synchronizer=sync, project_root=root)
    assert res_load["stack_applied"] == ["fastapi", "python"]
    assert "Integration probe" in res_load["content"]
    assert "Integration MCP" not in res_load["content"]

    # bundler -> fallback на default-зонд
    bundler = AgentBundler(storage=storage, compiler=SlotCompiler(storage))
    bundle = bundler.bundle_agent(
        profile=AgentProfile(
            name="p-agent",
            description="Agent",
            skills=["writing-plans"],
            instructions="Do plan",
        ),
        target="prompt",
        project_root=root,
    )
    assert "Integration probe" in bundle
    assert "Integration MCP" not in bundle

    # 2. Подтверждённый режим offline (MCP_ACTIVE_SERVERS="")
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "")
    _clear_pc_cache()
    ctx_off = pc_module.get_project_context(root)
    assert ctx_off.mode == "offline"

    res_load_off = load_skill(
        "writing-plans", storage=storage, synchronizer=sync, project_root=root
    )
    assert "offline" in res_load_off["stack_applied"]
    assert "Integration offline" in res_load_off["content"]

    # 3. live-qa-verification доступен в offline-режиме
    live_qa_dir = root / "skills" / "live-qa-verification"
    live_qa_dir.mkdir(parents=True)
    # Копируем или создаём карточку без activation: mcp
    from tests.conftest import get_companion_skills_dir

    _cat = get_companion_skills_dir()
    catalog_qa = (
        (_cat / "skills" / "quality-review" / "live-qa-verification" / "SKILL.md") if _cat else None
    )
    if catalog_qa and catalog_qa.is_file():
        (live_qa_dir / "SKILL.md").write_text(
            catalog_qa.read_text(encoding="utf-8"), encoding="utf-8"
        )
    else:
        (live_qa_dir / "SKILL.md").write_text(
            "---\nname: live-qa-verification\ntitle: Live QA\ncategory: quality-review\n---\n# QA\n",
            encoding="utf-8",
        )
    sync.sync()

    resp_qa = search_skills(
        query="live-qa-verification",
        project_stack=["offline"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    assert resp_qa["count"] >= 1
    found_qa = next(c for c in resp_qa["results"] if c["name"] == "live-qa-verification")
    assert found_qa["applicability"] != "incompatible"

    # 4. Различие stack=None (автодетект) и stack=[] (явный пустой стек)
    res_none = load_skill(
        "writing-plans", stack=None, storage=storage, synchronizer=sync, project_root=root
    )
    assert res_none["stack_source"] == "autodetect"
    assert "fastapi" in res_none["stack_applied"]

    res_empty = load_skill(
        "writing-plans", stack=[], storage=storage, synchronizer=sync, project_root=root
    )
    assert res_empty["stack_source"] == "explicit"
    assert "fastapi" not in res_empty["stack_applied"]
    assert "python" not in res_empty["stack_applied"]
