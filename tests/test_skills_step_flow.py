"""Тесты для универсального раннера сценариев step_flow и режима Overview в search_skills."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from mcp_skills.errors import (
    AmbiguousSessionError,
    FlowNotFoundError,
    SessionConflictError,
    StageMismatchError,
)
from mcp_skills.flow_engine import FlowEngine, step_flow
from mcp_skills.flow_storage import FlowStorage
from mcp_skills.models import FlowSession, FlowStatus
from mcp_skills.server import create_server, search_skills
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer


@pytest.fixture
def project_setup(tmp_path: Path):
    """Фикстура с инициализированным проектом, хранилищем и скиллами."""
    db_file = tmp_path / "flows.db"
    store = FlowStorage(db_path=db_file)
    skills_db = tmp_path / "skills.db"
    skills_store = SkillsStorage(skills_db)

    skills_dir = tmp_path / ".agents" / "skills"
    skills_dir.mkdir(parents=True)
    skill_path = skills_dir / "sample-skill"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text(
        """---
name: sample-skill
title: Sample Skill
category: planning
---
# Sample Skill
Description here.
""",
        encoding="utf-8",
    )
    sync = SkillsSynchronizer(skills_store, [skills_dir])
    sync.sync()

    return {
        "root": tmp_path,
        "flow_store": store,
        "skills_store": skills_store,
        "sync": sync,
    }


def test_step_flow_start_and_status(project_setup: dict) -> None:
    """Проверяет старт флоу и получение статуса через step_flow."""
    root = project_setup["root"]
    store = project_setup["flow_store"]

    # 1. Статус когда нет сессий
    res_no = step_flow(action="status", project_root=root, storage=store)
    assert res_no["status"] == "no_active_session"

    # 2. Старт флоу
    res_start = step_flow(flow="feature-delivery", project_root=root, storage=store)
    assert res_start["status"] == "active"
    assert "session_id" in res_start
    session_id = res_start["session_id"]
    assert res_start["stage"]["id"] == "spec_analysis"

    # 3. Статус активной сессии (без явного session_id)
    res_status = step_flow(action="status", project_root=root, storage=store)
    assert res_status["session"]["status"] == "active"
    assert res_status["session"]["id"] == session_id
    assert res_status["current_stage"]["id"] == "spec_analysis"

    # 4. Статус по явному session_id
    res_status_id = step_flow(
        action="status", session_id=session_id, project_root=root, storage=store
    )
    assert res_status_id["session"]["id"] == session_id


def test_step_flow_double_advancement_protection(project_setup: dict) -> None:
    """Проверяет защиту от Double Advancement через expected_stage_id."""
    root = project_setup["root"]
    store = project_setup["flow_store"]

    res_start = step_flow(flow="feature-delivery", project_root=root, storage=store)
    session_id = res_start["session_id"]
    current_stage = res_start["stage"]["id"]
    assert current_stage == "spec_analysis"

    # Несовпадающий expected_stage_id -> ошибка StageMismatchError
    with pytest.raises(StageMismatchError) as exc_info:
        step_flow(
            action="advance",
            session_id=session_id,
            expected_stage_id="wrong_stage_id",
            project_root=root,
            storage=store,
        )
    assert exc_info.value.expected == "wrong_stage_id"
    assert exc_info.value.actual == "spec_analysis"

    # Совпадающий expected_stage_id -> успешный переход
    res_adv = step_flow(
        action="advance",
        session_id=session_id,
        expected_stage_id="spec_analysis",
        human_confirmed=True,
        project_root=root,
        storage=store,
    )
    assert res_adv["stage"]["id"] == "plan_draft"


def test_step_flow_retry_and_abort(project_setup: dict) -> None:
    """Проверяет откат (retry) и прерывание (abort) флоу."""
    root = project_setup["root"]
    store = project_setup["flow_store"]

    res_start = step_flow(flow="feature-delivery", project_root=root, storage=store)
    session_id = res_start["session_id"]

    # Retry текущего этапа
    res_retry = step_flow(action="retry", session_id=session_id, project_root=root, storage=store)
    assert res_retry["status"] == "active"
    assert res_retry["stage"]["id"] == "spec_analysis"

    # Abort сессии
    res_abort = step_flow(
        action="abort",
        session_id=session_id,
        notes="Cancelled by user",
        project_root=root,
        storage=store,
    )
    assert res_abort["status"] == "aborted"
    assert res_abort["session_id"] == session_id

    # После abort активных сессий нет
    res_status = step_flow(action="status", project_root=root, storage=store)
    assert res_status["status"] == "no_active_session"


def test_step_flow_escalation_resolve(project_setup: dict) -> None:
    """Проверяет снятие блокировки Circuit Breaker через action=escalation_resolve."""
    root = project_setup["root"]
    store = project_setup["flow_store"]

    # Создаём сессию в статусе BLOCKED_ESCALATE
    session = FlowSession(
        id="flow-escalated-1",
        project_root=str(root),
        git_branch=None,
        flow_name="feature-delivery",
        current_stage_id="plan_review_loop",
        iteration_count=5,
        status=FlowStatus.BLOCKED_ESCALATE,
        version=1,
        created_at=1000.0,
        updated_at=1000.0,
        context_data={"blockers": ["Max loops reached"]},
    )
    store.save_session(session)

    # Разрешаем эскалацию с resolution="override_approved"
    res = step_flow(
        action="escalation_resolve",
        session_id="flow-escalated-1",
        resolution="override_approved",
        notes="Operator confirmed bypass",
        project_root=root,
        storage=store,
    )
    assert res["status"] in ("active", "completed")
    updated_sess = store.get_session("flow-escalated-1")
    assert updated_sess is not None
    assert updated_sess.status != FlowStatus.BLOCKED_ESCALATE


def test_escalation_resolve_override_approved_last_stage(project_setup: dict) -> None:
    """Проверяет эскалацию override_approved на последнем этапе с записью события completed."""
    root = project_setup["root"]
    store = project_setup["flow_store"]

    session = FlowSession(
        id="flow-escalated-last",
        project_root=str(root),
        git_branch=None,
        flow_name="feature-delivery",
        current_stage_id="qa_verification",  # Последний этап feature-delivery
        iteration_count=4,
        status=FlowStatus.BLOCKED_ESCALATE,
        version=1,
        created_at=1000.0,
        updated_at=1000.0,
        context_data={"blockers": ["Review blocked"]},
    )
    store.save_session(session)

    res = step_flow(
        action="escalation_resolve",
        session_id="flow-escalated-last",
        resolution="override_approved",
        notes="Final sign-off by lead",
        project_root=root,
        storage=store,
    )
    assert res["status"] == "completed"
    sess = store.get_session("flow-escalated-last")
    assert sess is not None
    assert sess.status == FlowStatus.COMPLETED

    history = store.get_history("flow-escalated-last")
    events = [h["event"] for h in history]
    assert "escalation_override_approved" in events
    assert "completed" in events


def test_step_flow_ambiguous_session(project_setup: dict) -> None:
    """Проверяет отказ при нескольких активных сессиях без указания session_id."""
    root = project_setup["root"]
    store = project_setup["flow_store"]

    s1 = FlowSession(
        id="flow-ambig-1",
        project_root=str(root),
        git_branch=None,
        flow_name="feature-delivery",
        current_stage_id="spec_analysis",
        iteration_count=1,
        status=FlowStatus.ACTIVE,
        version=1,
        created_at=1000.0,
        updated_at=1000.0,
    )
    s2 = FlowSession(
        id="flow-ambig-2",
        project_root=str(root),
        git_branch=None,
        flow_name="feature-delivery",
        current_stage_id="plan_draft",
        iteration_count=1,
        status=FlowStatus.ACTIVE,
        version=1,
        created_at=1000.0,
        updated_at=1000.0,
    )
    store.save_session(s1)
    store.save_session(s2)

    # Вызов без session_id должен выбросить AmbiguousSessionError
    with pytest.raises(AmbiguousSessionError):
        step_flow(action="advance", project_root=root, storage=store)

    # Вызов с явным session_id должен работать штатно
    res_explicit = step_flow(
        action="status", session_id="flow-ambig-1", project_root=root, storage=store
    )
    assert res_explicit["session"]["id"] == "flow-ambig-1"


def test_step_flow_ambiguous_session_across_branches(project_setup: dict) -> None:
    """Проверяет отказ при нескольких активных сессиях на РАЗНЫХ ветках (D5).

    Смена ветки включает fallback-поиск по project_root; несколько
    незавершённых сессий в разных ветках — та же неоднозначность:
    AmbiguousSessionError, зомби-сессии не создаются.
    """
    from mcp_skills.flow_engine import get_current_branch

    root = project_setup["root"]
    store = project_setup["flow_store"]

    s_feat = FlowSession(
        id="flow-ambig-feat",
        project_root=str(root),
        git_branch="feature-x",
        flow_name="feature-delivery",
        current_stage_id="spec_analysis",
        iteration_count=1,
        status=FlowStatus.ACTIVE,
        version=1,
        created_at=1000.0,
        updated_at=1000.0,
    )
    s_hotfix = FlowSession(
        id="flow-ambig-hotfix",
        project_root=str(root),
        git_branch="hotfix-y",
        flow_name="feature-delivery",
        current_stage_id="plan_draft",
        iteration_count=1,
        status=FlowStatus.ACTIVE,
        version=1,
        created_at=1000.0,
        updated_at=1000.0,
    )
    store.save_session(s_feat)
    store.save_session(s_hotfix)

    # Текущая ветка (None в tmp_path без git) не совпадает ни с одной — fallback находит обе
    assert get_current_branch(root) is None
    with pytest.raises(AmbiguousSessionError):
        step_flow(action="advance", project_root=root, storage=store)

    # Новых сессий не создано, прежние на месте
    assert len(store.find_active_sessions(str(root), git_branch=None)) == 2
    assert len(store.find_active_sessions(str(root), git_branch="feature-x")) == 1
    assert len(store.find_active_sessions(str(root), git_branch="hotfix-y")) == 1

    # Явный session_id продолжает нужную сессию
    res_explicit = step_flow(
        action="advance",
        session_id="flow-ambig-feat",
        human_confirmed=True,
        project_root=root,
        storage=store,
    )
    assert res_explicit["session_id"] == "flow-ambig-feat"
    assert res_explicit["stage"]["id"] == "plan_draft"


@pytest.mark.asyncio
async def test_mcp_step_flow_envelope_errors(tmp_path: Path) -> None:
    """Проверяет маппинг ошибок FSM (STAGE_MISMATCH, AMBIGUOUS_SESSION) в MCP envelope."""
    server = create_server(project_root=tmp_path)

    # 1. Запуск флоу
    start_res = await server.call_tool("step_flow", {"flow": "feature-delivery"})
    start_data = start_res.structured_content or json.loads(start_res.content[0].text)
    session_id = start_data["session_id"]

    # 2. Несовпадение expected_stage_id -> STAGE_MISMATCH
    mismatch_res = await server.call_tool(
        "step_flow",
        {
            "action": "advance",
            "session_id": session_id,
            "expected_stage_id": "nonexistent_stage",
        },
    )
    mismatch_data = mismatch_res.structured_content or json.loads(mismatch_res.content[0].text)
    assert mismatch_data.get("error") is True
    assert mismatch_data.get("reason") == "STAGE_MISMATCH"
    assert mismatch_data.get("error_type") == "StageMismatchError"


@pytest.mark.asyncio
async def test_mcp_step_flow_forwards_options_and_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP-схема и фасад пропускают настройки старта и выбранный переход в движок."""
    calls: list[dict] = []

    def fake_step_flow(**kwargs: object) -> dict[str, str]:
        calls.append(kwargs)
        return {"status": "active"}

    monkeypatch.setattr("mcp_skills.server.step_flow", fake_step_flow)
    server = create_server(project_root=tmp_path)
    start = await server.call_tool(
        "step_flow", {"flow": "feature-delivery", "flow_options": {"autonomy": "auto"}}
    )
    assert not start.is_error
    assert calls[-1]["flow_options"] == {"autonomy": "auto"}

    advance = await server.call_tool(
        "step_flow", {"action": "advance", "transition": "repeat_task"}
    )
    assert not advance.is_error
    assert calls[-1]["transition"] == "repeat_task"


def test_search_skills_overview_and_types(project_setup: dict) -> None:
    """Проверяет режим сводки каталога (Overview) и фильтрацию type в search_skills."""
    root = project_setup["root"]
    skills_store = project_setup["skills_store"]
    sync = project_setup["sync"]

    # 1. Zero-query Overview
    res_overview = search_skills(
        query="", type="all", storage=skills_store, synchronizer=sync, project_root=root
    )
    assert res_overview.get("mode") == "overview"
    assert "categories" in res_overview
    assert "flows" in res_overview
    assert any(f["name"] == "feature-delivery" for f in res_overview["flows"])
    assert all("title" not in f for f in res_overview["flows"])  # компактный формат

    # 2. Фильтр type="flow" без query (нейтральный листинг без флага is_ambiguous)
    res_flows = search_skills(
        type="flow", storage=skills_store, synchronizer=sync, project_root=root
    )
    assert "flows" in res_flows
    assert "skills" not in res_flows
    assert res_flows.get("is_ambiguous") is not True

    # 3. Фильтр type="flow" с неоднозначностью (несколько подходящих flows)
    flows_dir = root / ".agents" / "flows"
    flows_dir.mkdir(parents=True, exist_ok=True)
    (flows_dir / "plan-execution-flow.md").write_text(
        "---\ntitle: Plan Execution Flow\naliases:\n  - выполни план\n  - execution\n---\n# Plan Execution Flow\n## 1. Inspect [id: s1]\nCheck plan",
        encoding="utf-8",
    )
    (flows_dir / "plan-execution-auto-flow.md").write_text(
        "---\ntitle: Plan Execution Auto Flow\naliases:\n  - выполни план\n  - auto execution\n---\n# Plan Execution Auto Flow\n## 1. Inspect [id: s1]\nCheck plan",
        encoding="utf-8",
    )

    res_ambiguous = search_skills(
        query="выполни план",
        type="flow",
        storage=skills_store,
        synchronizer=sync,
        project_root=root,
        # Изоляция от кэша пользователя: без skills_dir list_available_flows
        # подтягивает ~/.cache/mcp-skills/repo/flows (symlink на живой каталог),
        # и каталогный parallel-execute-flow с фразовым алиасом «выполни план …»
        # перебивает фикстуры этого теста — арбитраж гасится.
        skills_dir=Path(project_setup["root"]) / "empty-skills-dir",
    )
    assert res_ambiguous["is_ambiguous"] is True
    assert "recommendation" in res_ambiguous
    assert len(res_ambiguous["arbitration_options"]) >= 2
    assert any("plan-execution" in opt["flow"] for opt in res_ambiguous["arbitration_options"])
    assert all("intent_group" in opt for opt in res_ambiguous["arbitration_options"])
    assert all("when_to_use" in opt for opt in res_ambiguous["arbitration_options"])

    # 4. Однозначное совпадение (unambiguous match) - без флага is_ambiguous
    res_single = search_skills(
        query="feature-delivery",
        type="flow",
        storage=skills_store,
        synchronizer=sync,
        project_root=root,
    )
    assert res_single["count"] == 1
    assert res_single.get("is_ambiguous") is not True
    assert res_single["flows"][0]["name"] == "feature-delivery"

    # 5. Кросс-языковой поиск (RU -> EN синонимы: "реализация" -> delivery/execution/implement)
    res_ru_cross = search_skills(
        query="реализация", type="flow", storage=skills_store, synchronizer=sync, project_root=root
    )
    assert res_ru_cross["count"] >= 1
    assert any(
        "delivery" in f["name"] or "delivery" in f["title"].lower() for f in res_ru_cross["flows"]
    )

    # 6. Фильтр type="skill"
    res_skills = search_skills(
        query="sample", type="skill", storage=skills_store, synchronizer=sync, project_root=root
    )
    assert "results" in res_skills
    assert res_skills["count"] >= 1
    assert res_skills["results"][0]["name"] == "sample-skill"


def test_circuit_breaker_recovery_and_zombie_prevention(project_setup: dict) -> None:
    """Проверяет C1 (recovery без session_id) и C2 (предотвращение создания зомби-сессий)."""
    root = project_setup["root"]
    store = project_setup["flow_store"]

    # 1. Запуск feature-delivery (этап spec_analysis)
    start_res = step_flow(flow="feature-delivery", project_root=root, storage=store)
    sid = start_res["session_id"]

    # spec_analysis -> plan_draft
    step_flow(
        action="advance", session_id=sid, human_confirmed=True, project_root=root, storage=store
    )

    # 2. plan_draft -> plan_review_loop с файлом плана
    plan_file = root / "implementation_plan.md"
    plan_file.write_text("# Plan\n## План\nКонтент", encoding="utf-8")
    step_flow(action="advance", session_id=sid, project_root=root, storage=store)

    # 3. 3 раза needs_work -> срабатывание Circuit Breaker
    step_flow(
        action="advance", session_id=sid, verdict="needs_work", project_root=root, storage=store
    )
    step_flow(
        action="advance", session_id=sid, verdict="needs_work", project_root=root, storage=store
    )
    cb_res = step_flow(
        action="advance", session_id=sid, verdict="needs_work", project_root=root, storage=store
    )
    assert cb_res["status"] == "blocked_escalate"

    # 4. Проверка C1: step_flow(action="status") БЕЗ session_id находит заблокированную сессию
    status_res = step_flow(action="status", project_root=root, storage=store)
    assert status_res["session"]["status"] == "blocked_escalate"
    assert status_res["session"]["id"] == sid
    assert "directive" in status_res

    # 5. Проверка C2: попытка начать новый flow при заблокированной сессии отклоняется (нет зомби-сессий)
    with pytest.raises(SessionConflictError) as exc_info:
        step_flow(flow="feature-delivery", project_root=root, storage=store)
    assert "Circuit Breaker" in str(exc_info.value) or "уже выполняется" in str(exc_info.value)

    # 6. Проверка C1: step_flow(action="escalation_resolve") БЕЗ session_id восстанавливает сессию
    resolve_res = step_flow(
        action="escalation_resolve",
        resolution="retry",
        project_root=root,
        storage=store,
    )
    assert resolve_res["stage"]["id"] == "plan_review_loop"
    assert resolve_res["session_id"] == sid


def test_retry_on_terminal_session_fails(project_setup: dict) -> None:
    """Проверка M6: retry для завершённой или прерванной сессии вызывает ошибку."""
    from mcp_skills.errors import FlowError

    root = project_setup["root"]
    store = project_setup["flow_store"]

    start_res = step_flow(flow="feature-delivery", project_root=root, storage=store)
    sid = start_res["session_id"]

    # Прерываем сессию
    step_flow(action="abort", session_id=sid, notes="Отмена", project_root=root, storage=store)

    # Попытка retry на aborted сессии должна падать
    with pytest.raises(FlowError):
        step_flow(action="retry", session_id=sid, project_root=root, storage=store)


@pytest.mark.asyncio
async def test_mcp_step_flow_ambiguous_session(tmp_path: Path) -> None:
    """Проверка ошибки AMBIGUOUS_SESSION в MCP error-envelope при нескольких активных сессиях."""
    import time

    server = create_server(project_root=tmp_path)
    store = FlowStorage(project_root=tmp_path)
    now = time.time()

    # Создаём две активные сессии вручную
    s1 = FlowSession(
        id="flow-11111111",
        project_root=str(tmp_path),
        git_branch="main",
        flow_name="feature-delivery",
        current_stage_id="spec_analysis",
        status=FlowStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    s2 = FlowSession(
        id="flow-22222222",
        project_root=str(tmp_path),
        git_branch="main",
        flow_name="feature-delivery",
        current_stage_id="spec_analysis",
        status=FlowStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    store.save_session(s1)
    store.save_session(s2)

    res = await server.call_tool("step_flow", {"action": "advance"})
    data = res.structured_content or json.loads(res.content[0].text)
    assert data.get("error") is True
    assert data.get("reason") == "AMBIGUOUS_SESSION"
    assert data.get("error_type") == "AmbiguousSessionError"


def test_flow_search_scoring_when_to_use_and_arbitration_options(project_setup: dict) -> None:
    """Проверяет скоринг when_to_use (+5 один раз за флоу) и структуру arbitration_options."""
    from mcp_skills.server import _score_flows

    root = project_setup["root"]
    skills_store = project_setup["skills_store"]
    sync = project_setup["sync"]

    flows_dir = root / ".agents" / "flows"
    flows_dir.mkdir(parents=True, exist_ok=True)

    # Флоу 1: совпадение по when_to_use
    (flows_dir / "audit-deep.md").write_text(
        """---
title: Deep Audit
intent_group: "audit-group"
when_to_use:
  - "проверка безопасности и изоляции"
---
# Deep Audit
## 1. Step [id: s1]
Inspect
""",
        encoding="utf-8",
    )

    # Флоу 2: совпадение только по описанию (score = 1 за слово)
    (flows_dir / "audit-light.md").write_text(
        """---
title: Light Audit
description: "проверка без деталей"
intent_group: "audit-group"
---
# Light Audit
## 1. Step [id: s1]
Inspect
""",
        encoding="utf-8",
    )

    res = search_skills(
        query="безопасности",
        type="flow",
        storage=skills_store,
        synchronizer=sync,
        project_root=root,
    )
    # audit-deep имеет слово "безопасности" в when_to_use (+5)
    assert res["count"] >= 1
    assert res["flows"][0]["name"] == "audit-deep"
    assert res["flows"][0]["intent_group"] == "audit-group"
    assert "проверка безопасности и изоляции" in res["flows"][0]["when_to_use"]

    # Проверка, что многословный запрос не дает N x 5 бонусов за when_to_use (ровно +5 один раз)
    mock_flows = [
        {
            "name": "f1",
            "title": "Alpha",
            "description": "",
            "when_to_use": ["проверка безопасности изоляции процессов"],
        }
    ]
    # Запрос с тремя словами из when_to_use
    _, scored_multi = _score_flows(mock_flows, "проверка безопасности изоляции")
    # Должен быть ровно 5 (флаг when_matched=True сработал один раз), а не 15
    assert scored_multi[0][0] == 5
