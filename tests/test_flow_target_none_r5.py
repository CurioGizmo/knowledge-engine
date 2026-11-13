"""R5: переключение opencode → antigravity не работает (дефект приёмки).

Воспроизведения (docs/analysis/2026-10-18-post-implementation-review.md, R5):
фасад step_flow нормализует target="antigravity" в None — вызов без target
сбрасывает сохранённый opencode. Контракт фикса: target=None по умолчанию на
всех публичных входах (фасад/engine/CLI/MCP); None = «сохранённый target;
для новой сессии — antigravity»; явный target не нормализуется.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from mcp_skills.flow_engine import step_flow


def _init_test_catalog(cat_dir: Path) -> Path:
    """Создаёт тестовый каталог с одним flow (2 этапа) и агентом."""
    skills_dir = cat_dir / "skills"
    flows_dir = cat_dir / "flows"
    agents_dir = cat_dir / "agents"
    skills_dir.mkdir(parents=True, exist_ok=True)
    flows_dir.mkdir(parents=True, exist_ok=True)
    agents_dir.mkdir(parents=True, exist_ok=True)

    (skills_dir / "test-skill").mkdir(parents=True, exist_ok=True)
    (skills_dir / "test-skill" / "SKILL.md").write_text(
        "---\nname: test-skill\ntitle: Test Skill\ndescription: A test skill\n---\n# Skill content\n",
        encoding="utf-8",
    )

    agent_dir = agents_dir / "test-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    agent_yaml = {
        "name": "test-agent",
        "role": "Test Specialist",
        "description": "Agent for testing",
        "skills": ["test-skill"],
        "instructions": "Follow all testing instructions.",
        "permissions": {"write": False, "mcp": True, "subagents": False},
    }
    (agent_dir / "AGENT.yaml").write_text(yaml.safe_dump(agent_yaml), encoding="utf-8")

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


def _start_opencode_session(tmp_path: Path) -> tuple[Path, Path, str]:
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
    return cat_dir, proj_dir, res["session_id"]


def test_r5_retry_without_target_keeps_saved_opencode(tmp_path: Path):
    """Воспроизведение R5-1: retry без target НЕ сбрасывает сохранённый opencode.

    Старое поведение: target по умолчанию "antigravity" → нормализуется в
    None... но retry-ветка старого кода при target="antigravity" НЕ
    нормализует (explicit_target = "antigravity") → сессия перезаписывалась
    на antigravity. Контракт: вызов без target сохраняет прежний.
    """
    cat_dir, proj_dir, session_id = _start_opencode_session(tmp_path)

    res = step_flow(
        action="retry",
        session_id=session_id,
        notes="Retry without target",
        project_root=proj_dir,
        skills_dir=cat_dir,
    )
    # Сохранённый target не сброшен
    assert res["next_call"]["arguments"]["target"] == "opencode", (
        "retry без target обязан сохранять сохранённый target сессии (R5)"
    )
    # Сессия в хранилище всё ещё opencode
    status_res = step_flow(
        action="status",
        session_id=session_id,
        project_root=proj_dir,
        skills_dir=cat_dir,
    )
    assert status_res["next_call"]["arguments"]["target"] == "opencode"


def test_r5_retry_with_antigravity_switches_target(tmp_path: Path):
    """R5-2: retry с явным antigravity переключает target сессии."""
    cat_dir, proj_dir, session_id = _start_opencode_session(tmp_path)

    res = step_flow(
        action="retry",
        session_id=session_id,
        notes="Switch to antigravity",
        target="antigravity",
        project_root=proj_dir,
        skills_dir=cat_dir,
    )
    assert res["next_call"]["arguments"]["target"] == "antigravity"

    status_res = step_flow(
        action="status",
        session_id=session_id,
        project_root=proj_dir,
        skills_dir=cat_dir,
    )
    assert status_res["next_call"]["arguments"]["target"] == "antigravity"


def test_r5_advance_without_target_does_not_reset_saved_target(tmp_path: Path):
    """Воспроизведение R5-3: advance без target не сбрасывает opencode (needs_work)."""
    cat_dir, proj_dir, session_id = _start_opencode_session(tmp_path)

    # advance с needs_work возвращает этап с next_call
    res = step_flow(
        action="advance",
        session_id=session_id,
        verdict="needs_work",
        project_root=proj_dir,
        skills_dir=cat_dir,
    )
    assert res["next_call"]["arguments"]["target"] == "opencode", (
        "advance без target обязан сохранять opencode (R5: сброс — дефект)"
    )


def test_r5_explicit_antigravity_not_normalized_to_none(tmp_path: Path):
    """R5-4: явный target='antigravity' в next_call остаётся 'antigravity', не None.

    Проверяется через retry: переключение на antigravity даёт next_call
    target='antigravity' (а не отсутствие ключа/None — антирегрессия шага 5).
    """
    cat_dir, proj_dir, session_id = _start_opencode_session(tmp_path)

    res = step_flow(
        action="retry",
        session_id=session_id,
        notes="Explicit antigravity",
        target="antigravity",
        project_root=proj_dir,
        skills_dir=cat_dir,
    )
    assert res["next_call"]["arguments"]["target"] == "antigravity"
    assert res["next_call"]["arguments"].get("target") is not None


def test_r5_status_target_argument_accepts_none_default(tmp_path: Path):
    """R5-5: фасад step_flow target по умолчанию None (не "antigravity")."""
    import inspect

    sig = inspect.signature(step_flow)
    target_param = sig.parameters["target"]
    assert target_param.default is None, (
        f"step_flow target default должен быть None, сейчас {target_param.default!r} (R5)"
    )


def test_r5_payload_build_error_before_target_update(tmp_path: Path):
    """R5-6: ошибка сборки payload не меняет target сессии (порядок операций).

    _handle_step_retry строит payload ДО update_session(target_changed):
    при ошибке сборки сессия не остаётся с частично применённой сменой target.
    Воспроизведение: сессия opencode, retry с target='cursor', но этап
    ссылается на несуществующий flow-файл → исключение → target остаётся opencode.
    """
    cat_dir, proj_dir, session_id = _start_opencode_session(tmp_path)

    # Ломаем flow-файл после старта сессии — сборка payload упадёт
    (cat_dir / "flows" / "test-target-flow.md").unlink()

    import pytest
    from mcp_skills.errors import FlowNotFoundError

    with pytest.raises(FlowNotFoundError):
        step_flow(
            action="retry",
            session_id=session_id,
            notes="Should fail",
            target="cursor",
            project_root=proj_dir,
            skills_dir=cat_dir,
        )

    # Восстанавливаем flow и проверяем: target сессии не изменился
    cat_dir2 = _init_test_catalog(cat_dir)
    status_res = step_flow(
        action="status",
        session_id=session_id,
        project_root=proj_dir,
        skills_dir=cat_dir2,
    )
    assert status_res["next_call"]["arguments"]["target"] == "opencode"
