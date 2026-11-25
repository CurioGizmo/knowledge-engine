"""Тесты серверного управления состоянием оркестрации в step_flow (aud-skills-2ab9)."""

from __future__ import annotations

from pathlib import Path

import pytest

import mcp_skills.flow_engine as flow_engine_module
from mcp_skills.errors import SessionConflictError
from mcp_skills.flow_engine import FlowEngine, step_flow
from mcp_skills.flow_storage import FlowStorage
from mcp_skills.orchestration_state import resolve_orchestration_path
from mcp_skills.server import create_server
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer
from tests.conftest import get_companion_skills_dir


@pytest.fixture
def catalog_root() -> Path:
    root = get_companion_skills_dir()
    if root is None:
        pytest.skip("companion mcp_skills catalog is not checked out")
    return root


@pytest.fixture
def engine(tmp_path: Path, catalog_root: Path, monkeypatch: pytest.MonkeyPatch) -> FlowEngine:
    monkeypatch.setattr(flow_engine_module, "get_current_branch", lambda _root: "main")
    skills = SkillsStorage(tmp_path / "skills.db")
    SkillsSynchronizer(skills, [catalog_root / "skills"]).sync()
    return FlowEngine(
        project_root=tmp_path,
        storage=FlowStorage(db_path=tmp_path / "flows.db"),
        skills_storage=skills,
        skills_dir=catalog_root,
    )


def test_orchestration_state_materialized_and_updated_across_stages(
    engine: FlowEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """start_flow и advance_flow автоматически создают и обновляют orchestration-state.md."""
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "audit-engine,skills-engine")

    started = engine.start_flow("execute-plan-flow", target="prompt")
    sid = started["session_id"]
    initial_orch = started["orchestration_state"]
    assert initial_orch["mode"] == "mcp"
    assert initial_orch["path"].startswith(".agents/plans/.orchestration/")
    placeholder_file = tmp_path / initial_orch["path"]
    assert placeholder_file.is_file()

    # Этап inspect_plan: передаём task_id/slug/plan_path/base_git_sha — slug обновляется, старый файл чистится
    after_inspect = engine.advance_flow(
        sid,
        expected_stage_id="inspect_plan",
        deliverable={
            "slug": "auth-refactor",
            "task_id": "AUTH-42",
            "plan_path": ".agents/plans/auth-refactor.md",
            "base_git_sha": "abc1234",
            "feature_branch": "feat/auth-refactor",
        },
        target="prompt",
    )
    orch = after_inspect["orchestration_state"]
    assert orch["slug"] == "auth-refactor"
    assert orch["path"] == ".agents/plans/.orchestration/auth-refactor/orchestration-state.md"
    assert not placeholder_file.exists()

    state_file = tmp_path / orch["path"]
    assert state_file.is_file()

    # Этап schedule_wave: передаём tasks и waves
    after_sched = engine.advance_flow(
        sid,
        expected_stage_id="schedule_wave",
        deliverable={
            "tasks": [
                {
                    "id": "T1",
                    "name": "Add token guard",
                    "files": ["src/auth.py"],
                    "status": "IN_PROGRESS",
                },
                {
                    "id": "T2",
                    "name": "Add tests",
                    "files": ["tests/test_auth.py"],
                    "status": "PENDING",
                },
            ],
            "waves": [{"wave": 1, "task_ids": ["T1", "T2"], "status": "IN_PROGRESS"}],
        },
        target="prompt",
    )
    assert after_sched["orchestration_state"]["tasks_total"] == 2
    assert after_sched["orchestration_state"]["tasks_done"] == 0
    assert after_sched["orchestration_state"]["waves_total"] == 1

    # Этап execute_wave + verify_wave: закрываем T1, оставляем T2
    engine.advance_flow(
        sid,
        expected_stage_id="execute_wave",
        verdict="approved",
        deliverable={
            "tasks": [{"id": "T1", "status": "DONE", "notes": "committed"}],
            "waves": [{"wave": 1, "status": "DONE"}],
            "merge_log": ["Merged T1 into feat/auth-refactor"],
        },
        target="prompt",
    )
    after_verify = engine.advance_flow(
        sid,
        expected_stage_id="verify_wave",
        verdict="approved",
        transition="next_wave_none",
        target="prompt",
    )
    assert after_verify["orchestration_state"]["tasks_done"] == 1
    assert after_verify["orchestration_state"]["tasks_total"] == 2

    md = state_file.read_text(encoding="utf-8")
    assert "Base SHA: abc1234" in md
    assert "Feature Branch: feat/auth-refactor" in md
    assert "50% (1/2 tasks)" in md
    assert "| T1 | Add token guard | src/auth.py | DONE | committed |" in md
    assert "| T2 | Add tests | tests/test_auth.py | PENDING |  |" in md
    assert "- Merged T1 into feat/auth-refactor" in md

    # status остаётся read-only и не пересоздаёт удалённый файл
    state_file.unlink()
    status_res = engine.get_flow_status(session_id=sid, project_root=tmp_path)
    assert status_res["orchestration_state"]["slug"] == "auth-refactor"
    assert not state_file.exists()


def test_orchestration_offline_mode_uses_docs_plans_path(
    engine: FlowEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """В offline-режиме (MCP_ACTIVE_SERVERS='') трекер пишется в docs/plans/.orchestration/."""
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "")

    started = engine.start_flow("execute-plan-flow", task_id="OFFLINE-1", target="prompt")
    orch = started["orchestration_state"]
    assert orch["mode"] == "offline"
    assert orch["path"] == "docs/plans/.orchestration/offline-1/orchestration-state.md"
    assert (tmp_path / orch["path"]).is_file()


def test_non_execution_flows_do_not_create_orchestration_files(
    engine: FlowEngine, tmp_path: Path
) -> None:
    """Обычные флоу (review-flow, feature-delivery) не создают .orchestration/ файлов."""
    started = engine.start_flow(
        "review-flow", flow_options={"target_kind": "code"}, target="prompt"
    )
    assert "orchestration_state" not in started
    assert not (tmp_path / ".agents" / "plans" / ".orchestration").exists()
    assert not (tmp_path / "docs" / "plans" / ".orchestration").exists()


def test_orchestration_slug_sanitized_against_path_traversal(tmp_path: Path) -> None:
    """Опасный slug не позволяет выйти за пределы корня проекта."""
    abs_path, rel_path, _ = resolve_orchestration_path(tmp_path, "../../etc/passwd")
    assert abs_path.resolve().is_relative_to(tmp_path.resolve())
    assert ".." not in rel_path


def test_orchestration_file_not_written_when_db_update_fails(
    engine: FlowEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Если update_session в SQLite падает, orchestration-state.md на диске не обновляется."""
    started = engine.start_flow("execute-plan-flow", task_id="ATOMIC-1", target="prompt")
    sid = started["session_id"]
    state_file = tmp_path / started["orchestration_state"]["path"]
    before = state_file.read_text(encoding="utf-8")

    storage = engine._get_storage(tmp_path)

    def _fail_update(*args: object, **kwargs: object) -> None:
        raise SessionConflictError("simulated conflict")

    monkeypatch.setattr(storage, "update_session", _fail_update)
    with pytest.raises(SessionConflictError):
        engine.advance_flow(
            sid,
            expected_stage_id="inspect_plan",
            deliverable={"base_git_sha": "deadbeef"},
            target="prompt",
        )
    assert state_file.read_text(encoding="utf-8") == before


def test_orchestration_abort_updates_final_status(engine: FlowEngine, tmp_path: Path) -> None:
    """abort_flow фиксирует Final Status = ABORTED в orchestration-state.md."""
    started = engine.start_flow("execute-plan-flow", task_id="ABORT-1", target="prompt")
    sid = started["session_id"]
    state_file = tmp_path / started["orchestration_state"]["path"]

    engine.abort_flow(sid, reason="user cancelled", project_root=tmp_path)
    md = state_file.read_text(encoding="utf-8")
    assert "\n## Final Status\nABORTED\n" in md


@pytest.mark.asyncio
async def test_mcp_step_flow_tool_manages_orchestration_state(
    tmp_path: Path, catalog_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP-инструмент step_flow поддерживает task_id и материализует трекер."""
    import json

    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "audit-engine,skills-engine")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    (tmp_path / ".agents" / "audit").mkdir(parents=True, exist_ok=True)
    server = create_server(project_root=tmp_path, skills_dir=catalog_root)

    start_res = await server.call_tool(
        "step_flow",
        {
            "action": "advance",
            "flow": "execute-plan-flow",
            "task_id": "MCP-ORCH-99",
            "project_path": str(tmp_path),
        },
    )
    res = start_res.structured_content or json.loads(start_res.content[0].text)
    assert res["status"] == "active"
    orch = res["orchestration_state"]
    assert orch["slug"] == "mcp-orch-99"
    assert (tmp_path / orch["path"]).is_file()

    # action='status' via MCP read-only
    st_res = await server.call_tool(
        "step_flow",
        {
            "action": "status",
            "session_id": res["session_id"],
            "project_path": str(tmp_path),
        },
    )
    st = st_res.structured_content or json.loads(st_res.content[0].text)
    assert st["orchestration_state"]["slug"] == "mcp-orch-99"


def test_orchestration_review_iterations_and_escalation_paths(
    engine: FlowEngine, tmp_path: Path
) -> None:
    """Многораундовое ревью, Circuit Breaker, escalation_resolve и защита от нечислового round."""
    started = engine.start_flow(
        "execute-plan-flow",
        task_id="REV-99",
        flow_options={"autonomy": "semi"},
        target="prompt",
    )
    sid = started["session_id"]
    state_file = tmp_path / started["orchestration_state"]["path"]

    engine.advance_flow(sid, expected_stage_id="inspect_plan", target="prompt")
    engine.advance_flow(
        sid,
        expected_stage_id="schedule_wave",
        deliverable={
            "tasks": [
                {"id": "T1", "name": "Pipe|Name\nLine2", "files": ["a.py"], "status": "PENDING"}
            ],
            "waves": [{"wave": 1, "task_ids": ["T1"], "status": "PENDING"}],
        },
        target="prompt",
    )
    # Частичный апдейт с None не затирает поля; экранирование '|' в таблице
    engine.advance_flow(
        sid,
        expected_stage_id="execute_wave",
        verdict="approved",
        deliverable={
            "tasks": [{"id": "T1", "name": None, "files": None, "status": "DONE"}],
            "waves": [{"wave": 1, "task_ids": None, "status": "DONE"}],
        },
        target="prompt",
    )
    engine.advance_flow(
        sid,
        expected_stage_id="verify_wave",
        verdict="approved",
        transition="all_done_ready",
        target="prompt",
    )
    # ready_for_review не должен трогать секцию Review
    engine.advance_flow(
        sid, expected_stage_id="ready_for_review", human_confirmed=True, target="prompt"
    )
    md_before_review = state_file.read_text(encoding="utf-8")
    assert "## Review\nStatus: PENDING\nIteration: 0\n" in md_before_review
    assert "Pipe\\|Name Line2" in md_before_review

    # Раунд 1: needs_work с нечисловым round (fallback на iteration 1, без ValueError)
    engine.advance_flow(
        sid,
        expected_stage_id="independent_review",
        verdict="needs_work",
        deliverable={
            "subagent_sessions": ["sub-1"],
            "review_id": "rev-backend-1",
            "round": "R1",
            "verdict": "needs_work",
            "summary": "Missing edge test",
        },
        target="prompt",
    )
    md_r1 = state_file.read_text(encoding="utf-8")
    assert "Status: NEEDS_FIX\nIteration: 1\n" in md_r1
    assert "Open Issues: Missing edge test" in md_r1

    # Раунд 2: approved БЕЗ поля round — должен сохранить previous_iteration=2, а не сбросить в 1
    engine.advance_flow(
        sid,
        expected_stage_id="independent_review",
        verdict="approved",
        deliverable={
            "subagent_sessions": ["sub-2"],
            "review_id": "rev-backend-2",
            "verdict": "approved",
            "summary": "All clean",
        },
        target="prompt",
    )
    md_r2 = state_file.read_text(encoding="utf-8")
    assert "Status: APPROVED\nIteration: 2\n" in md_r2
    assert "Open Issues: —" in md_r2


def test_orchestration_circuit_breaker_and_escalation_resolutions(
    engine: FlowEngine, tmp_path: Path
) -> None:
    """Circuit Breaker выставляет BLOCKED, а escalation_resolve retry/override/abort обновляют трекер."""
    started = engine.start_flow("execute-plan-flow", task_id="CB-1", target="prompt")
    sid = started["session_id"]
    state_file = tmp_path / started["orchestration_state"]["path"]

    # inspect_plan имеет loop_max=2 -> 2 раза needs_work триггерят Circuit Breaker
    engine.advance_flow(
        sid, expected_stage_id="inspect_plan", verdict="needs_work", notes="m1", target="prompt"
    )
    cb = engine.advance_flow(
        sid, expected_stage_id="inspect_plan", verdict="needs_work", notes="m2", target="prompt"
    )
    assert cb["status"] == "blocked_escalate"
    assert cb["orchestration_state"]["final_status"] == "BLOCKED"
    assert "\n## Final Status\nBLOCKED\n" in state_file.read_text(encoding="utf-8")

    # retry возвращает IN_PROGRESS
    retried = engine.resolve_flow_escalation(sid, resolution="retry", project_root=tmp_path)
    assert retried["orchestration_state"]["final_status"] == "IN_PROGRESS"

    # Снова доводим до Circuit Breaker и проверяем override_approved + abort
    engine.advance_flow(
        sid, expected_stage_id="inspect_plan", verdict="needs_work", notes="m3", target="prompt"
    )
    engine.advance_flow(
        sid, expected_stage_id="inspect_plan", verdict="needs_work", notes="m4", target="prompt"
    )
    overridden = engine.resolve_flow_escalation(
        sid, resolution="override_approved", project_root=tmp_path
    )
    assert overridden["stage"]["id"] == "schedule_wave"
    assert overridden["orchestration_state"]["final_status"] == "IN_PROGRESS"

    aborted = engine.abort_flow(sid, reason="done testing", project_root=tmp_path)
    assert aborted["orchestration_state"]["final_status"] == "ABORTED"
    assert "\n## Final Status\nABORTED\n" in state_file.read_text(encoding="utf-8")
