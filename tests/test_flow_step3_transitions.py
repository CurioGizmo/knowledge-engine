"""Тесты для Шага 3: однозначные переходы flow, required_inputs, next_call и StageMismatchError recovery."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from mcp_skills.error_envelope import _handle_tool_error
from mcp_skills.errors import StageMismatchError
from mcp_skills.flow_engine import FlowEngine, step_flow
from mcp_skills.flow_storage import FlowStorage
from mcp_skills.models import FlowSession, FlowStatus
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer


@pytest.fixture
def test_env(tmp_path: Path):
    """Окружение с базой данных флоу и минимальными флоу."""
    db_file = tmp_path / "flows.db"
    store = FlowStorage(db_path=db_file)
    skills_db = tmp_path / "skills.db"
    skills_store = SkillsStorage(skills_db)

    # Создаём flows директорию в проекте для изоляции
    flows_dir = tmp_path / ".agents" / "flows"
    flows_dir.mkdir(parents=True)

    # 1. Flow с 3 этапами: auto, agent (require verdict), gate (target_file + deliverable_must_exist)
    custom_flow = flows_dir / "test-workflow.md"
    custom_flow.write_text(
        """---
intent_group: "test"
when_to_use:
  - "тестовый сценарий"
---

# Тестовый сценарий

## Этап 1: Подготовка [id: stage_prep]
<!-- meta: {"agent": null, "validator": null, "loop_max": 2} -->
Инструкция подготовки.

## Этап 2: Проверка агентом [id: stage_check]
<!-- meta: {"agent": "tester-agent", "validator": null, "loop_max": 3} -->
Инструкция проверки.

## Этап 3: Финальный артефакт [id: stage_artifact]
<!-- meta: {"agent": null, "validator": null, "target_file": "report.md", "deliverable_must_exist": true, "require_human_confirm": true, "loop_max": 2} -->
Инструкция артефакта.
""",
        encoding="utf-8",
    )

    return {
        "root": tmp_path,
        "flow_store": store,
        "skills_store": skills_store,
        "skills_dir": tmp_path / ".agents" / "skills",
    }


def test_start_flow_returns_required_inputs_and_next_call(test_env: dict) -> None:
    root = test_env["root"]
    store = test_env["flow_store"]

    res = step_flow(
        action="advance",
        flow="test-workflow",
        project_root=root,
        storage=store,
    )

    assert res["status"] == "active"
    assert res["stage"]["id"] == "stage_prep"
    assert "required_inputs" in res
    assert "next_call" in res
    # stage_prep: agent=null, require_verdict=false, require_human_confirm=false, deliverable=false
    assert res["required_inputs"] == []
    assert res["next_call"] == {
        "tool": "step_flow",
        "arguments": {
            "action": "advance",
            "project_path": str(root),
            "session_id": res["session_id"],
            "expected_stage_id": "stage_prep",
            "target": "antigravity",
        },
    }


def test_advance_to_agent_stage_requires_verdict(test_env: dict) -> None:
    root = test_env["root"]
    store = test_env["flow_store"]

    start_res = step_flow(
        action="advance",
        flow="test-workflow",
        project_root=root,
        storage=store,
    )
    sid = start_res["session_id"]

    # Продвигаем stage_prep -> stage_check
    res2 = step_flow(
        action="advance",
        session_id=sid,
        expected_stage_id="stage_prep",
        project_root=root,
        storage=store,
    )

    assert res2["status"] == "active"
    assert res2["stage"]["id"] == "stage_check"
    assert "verdict" in res2["required_inputs"]
    assert res2["next_call"] == {
        "tool": "step_flow",
        "arguments": {
            "action": "advance",
            "project_path": str(root),
            "session_id": sid,
            "expected_stage_id": "stage_check",
            "target": "antigravity",
        },
    }
    # Проверяем, что verdict НЕ подставлен автоматически в arguments!
    assert "verdict" not in res2["next_call"]["arguments"]


def test_gate_blocked_returns_required_inputs_and_next_call(test_env: dict) -> None:
    root = test_env["root"]
    store = test_env["flow_store"]

    start_res = step_flow(
        action="advance",
        flow="test-workflow",
        project_root=root,
        storage=store,
    )
    sid = start_res["session_id"]
    # prep -> check
    step_flow(
        action="advance",
        session_id=sid,
        expected_stage_id="stage_prep",
        project_root=root,
        storage=store,
    )
    # check -> artifact (with verdict="approved")
    res_art = step_flow(
        action="advance",
        session_id=sid,
        expected_stage_id="stage_check",
        verdict="approved",
        project_root=root,
        storage=store,
    )
    assert res_art["stage"]["id"] == "stage_artifact"
    assert "deliverable" in res_art["required_inputs"]
    assert "human_confirmed" in res_art["required_inputs"]

    # Теперь пытаемся advance без deliverable и human_confirmed -> gate_blocked
    gate_res = step_flow(
        action="advance",
        session_id=sid,
        expected_stage_id="stage_artifact",
        project_root=root,
        storage=store,
    )
    assert gate_res["status"] == "gate_blocked"
    assert "required_inputs" in gate_res
    assert "deliverable" in gate_res["required_inputs"]
    assert "human_confirmed" in gate_res["required_inputs"]
    assert gate_res["next_call"] == {
        "tool": "step_flow",
        "arguments": {
            "action": "advance",
            "project_path": str(root),
            "session_id": sid,
            "expected_stage_id": "stage_artifact",
            "target": "antigravity",
        },
    }


def test_status_completed_and_aborted_states(test_env: dict) -> None:
    root = test_env["root"]
    store = test_env["flow_store"]

    # 1. no_active_session
    status_empty = step_flow(action="status", project_root=root, storage=store)
    assert status_empty["status"] == "no_active_session"
    assert status_empty["required_inputs"] == []
    assert status_empty["next_call"] is None

    # 2. active session
    start_res = step_flow(
        action="advance",
        flow="test-workflow",
        project_root=root,
        storage=store,
    )
    sid = start_res["session_id"]

    status_active = step_flow(action="status", session_id=sid, project_root=root, storage=store)
    assert status_active["required_inputs"] == []
    assert status_active["next_call"] == {
        "tool": "step_flow",
        "arguments": {
            "action": "advance",
            "project_path": str(root),
            "session_id": sid,
            "expected_stage_id": "stage_prep",
            "target": "antigravity",
        },
    }

    # 3. aborted
    abort_res = step_flow(
        action="abort", session_id=sid, notes="test abort", project_root=root, storage=store
    )
    assert abort_res["status"] == "aborted"
    assert abort_res["required_inputs"] == []
    assert abort_res["next_call"] is None

    status_aborted = step_flow(action="status", session_id=sid, project_root=root, storage=store)
    assert status_aborted["required_inputs"] == []
    assert status_aborted["next_call"] is None


def test_blocked_escalate_inputs_and_next_call(test_env: dict) -> None:
    root = test_env["root"]
    store = test_env["flow_store"]

    start_res = step_flow(
        action="advance",
        flow="test-workflow",
        project_root=root,
        storage=store,
    )
    sid = start_res["session_id"]
    # prep -> check
    step_flow(
        action="advance",
        session_id=sid,
        expected_stage_id="stage_prep",
        project_root=root,
        storage=store,
    )

    # loop_max = 3: шлём needs_work пока не сработает Circuit Breaker
    r1 = step_flow(
        action="advance",
        session_id=sid,
        expected_stage_id="stage_check",
        verdict="needs_work",
        project_root=root,
        storage=store,
    )
    assert r1["status"] == "active"
    assert r1["required_inputs"] == ["verdict"]
    assert r1["next_call"]["arguments"]["action"] == "advance"

    r2 = step_flow(
        action="advance",
        session_id=sid,
        expected_stage_id="stage_check",
        verdict="needs_work",
        project_root=root,
        storage=store,
    )
    assert r2["status"] == "active"

    # 3-я итерация превышает loop_max (1 + 1 + 1 + 1 > 3)
    r3 = step_flow(
        action="advance",
        session_id=sid,
        expected_stage_id="stage_check",
        verdict="needs_work",
        project_root=root,
        storage=store,
    )
    assert r3["status"] == "blocked_escalate"
    assert r3["required_inputs"] == ["resolution"]
    assert r3["next_call"] == {
        "tool": "step_flow",
        "arguments": {
            "action": "escalation_resolve",
            "project_path": str(root),
            "session_id": sid,
            "target": "antigravity",
        },
    }

    # Проверяем status сессии в blocked_escalate
    st = step_flow(
        action="status",
        session_id=sid,
        project_root=root,
        storage=store,
    )
    assert st["required_inputs"] == ["resolution"]
    assert st["next_call"] == {
        "tool": "step_flow",
        "arguments": {
            "action": "escalation_resolve",
            "project_path": str(root),
            "session_id": sid,
            "target": "antigravity",
        },
    }


def test_stage_mismatch_error_and_recovery(test_env: dict) -> None:
    root = test_env["root"]
    store = test_env["flow_store"]

    start_res = step_flow(
        action="advance",
        flow="test-workflow",
        project_root=root,
        storage=store,
    )
    sid = start_res["session_id"]

    # Переходим prep -> check
    step_flow(
        action="advance",
        session_id=sid,
        expected_stage_id="stage_prep",
        project_root=root,
        storage=store,
    )

    # Теперь сессия на этапе 'stage_check'.
    # Повторно вызываем advance со старым expected_stage_id="stage_prep":
    with pytest.raises(StageMismatchError) as exc_info:
        step_flow(
            action="advance",
            session_id=sid,
            expected_stage_id="stage_prep",
            project_root=root,
            storage=store,
        )

    err = exc_info.value
    assert err.expected == "stage_prep"
    assert err.actual == "stage_check"
    assert err.recovery is not None
    assert err.recovery["session_id"] == sid
    assert err.recovery["current_stage"]["id"] == "stage_check"
    assert err.recovery["required_inputs"] == ["verdict"]
    assert err.recovery["next_call"] == {
        "tool": "step_flow",
        "arguments": {
            "action": "advance",
            "project_path": str(root),
            "session_id": sid,
            "expected_stage_id": "stage_check",
            "target": "antigravity",
        },
    }

    # Проверяем маппинг в error_envelope
    envelope = _handle_tool_error(err, context="step_flow")
    assert envelope["reason"] == "STAGE_MISMATCH"
    assert envelope["expected"] == "stage_prep"
    assert envelope["actual"] == "stage_check"
    assert envelope["recovery"] == err.recovery

    # Проверяем, что сессия не изменила статус и этап
    saved_session = store.get_session(sid)
    assert saved_session.current_stage_id == "stage_check"
    assert saved_session.status == FlowStatus.ACTIVE


def test_live_qa_flow_lifecycle(tmp_path: Path) -> None:
    """Проверяет прохождение live-qa-flow из каталога mcp_skills корректными вызовами."""
    from tests.conftest import get_companion_skills_dir

    _cat = get_companion_skills_dir()
    catalog_dir = _cat if _cat is not None else Path("/home/anton/projects/mcp_skills")
    if not (catalog_dir / "flows" / "live-qa-flow.md").exists():
        pytest.skip(f"Каталог {catalog_dir}/flows/live-qa-flow.md не найден")

    db_file = tmp_path / "flows.db"
    store = FlowStorage(db_path=db_file)

    # Старт live-qa-flow
    res1 = step_flow(
        action="advance",
        flow="live-qa-flow",
        project_root=tmp_path,
        storage=store,
        skills_dir=catalog_dir,
    )
    assert res1["status"] == "active"
    assert res1["stage"]["id"] == "inspect_runbook"
    sid = res1["session_id"]
    assert res1["required_inputs"] == []
    assert res1["next_call"]["arguments"]["expected_stage_id"] == "inspect_runbook"

    # Шаг 1: inspect_runbook (auto) -> execute_verification
    res2 = step_flow(
        action="advance",
        session_id=sid,
        expected_stage_id="inspect_runbook",
        project_root=tmp_path,
        storage=store,
        skills_dir=catalog_dir,
    )
    assert res2["status"] == "active"
    assert res2["stage"]["id"] == "execute_verification"
    assert "verdict" in res2["required_inputs"]

    # Пропуск verdict на этапе с agent должен вызывать ошибку:
    with pytest.raises(ValueError, match="требует вердикт"):
        step_flow(
            action="advance",
            session_id=sid,
            expected_stage_id="execute_verification",
            project_root=tmp_path,
            storage=store,
            skills_dir=catalog_dir,
        )

    # Передача вердикта approved -> переход на report_and_verdict
    res3 = step_flow(
        action="advance",
        session_id=sid,
        expected_stage_id="execute_verification",
        verdict="approved",
        project_root=tmp_path,
        storage=store,
        skills_dir=catalog_dir,
    )
    assert res3["status"] == "active"
    assert res3["stage"]["id"] == "report_and_verdict"

    valid_report = f"""# QA Report
сессия: {sid}

| ID | Действие | Ожидание | Факт | Доказательство | Результат |
|---|---|---|---|---|---|
| TC-1 | Run test | Pass | Pass | test_run.log | PASSED |

Вердикт: QA_PASSED
"""
    report_file = tmp_path / "qa-report.md"
    report_file.write_text(valid_report, encoding="utf-8")

    # Шаг 3: report_and_verdict (с гейтом validate_qa_report) -> completed
    res4 = step_flow(
        action="advance",
        session_id=sid,
        expected_stage_id="report_and_verdict",
        deliverable="qa-report.md",
        project_root=tmp_path,
        storage=store,
        skills_dir=catalog_dir,
    )
    assert res4["status"] == "completed"
    assert res4["required_inputs"] == []
    assert res4["next_call"] is None
