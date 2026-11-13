"""P4: Edge-case тесты Flow State Machine (дополнение к tests/test_skills_flows.py).

Покрытые гэпы (дедуп с существующими тестами):
- advance_flow несуществующей сессии → FlowNotFoundError
- невалидный verdict / resolution → ValueError
- resolve_flow_escalation на не-blocked сессии → SessionConflictError
- resolve_flow_escalation(resolution=abort) → терминальный статус + история
- get_flow_status без session_id при 0 / ≥2 активных сессиях
- advance_flow на completed-сессии (у существующего теста — только aborted)

Уже покрыты в test_skills_flows.py (не дублируются):
deliverable >64КБ, blocked_escalate на advance → SessionConflictError,
abort_flow на терминальной сессии.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_skills.errors import FlowError, FlowNotFoundError, SessionConflictError
from mcp_skills.flow_engine import FlowEngine, advance_flow, get_flow_status, start_flow
from mcp_skills.flow_storage import FlowStorage
from mcp_skills.models import FlowStatus


@pytest.fixture
def engine_env(tmp_path: Path) -> tuple[Path, FlowStorage, FlowEngine]:
    """Изолированное окружение флоу-движка на tmp_path."""
    store = FlowStorage(db_path=tmp_path / "flows.db")
    engine = FlowEngine(project_root=tmp_path, storage=store)
    return tmp_path, store, engine


def _start_feature_delivery(engine: FlowEngine) -> str:
    """Стартует feature-delivery и возвращает session_id."""
    res = engine.start_flow(name="feature-delivery")
    assert res["status"] == "active"
    return res["session_id"]


def _drive_to_review_loop(tmp_path: Path, engine: FlowEngine, session_id: str) -> None:
    """Переводит сессию на ревью-этап plan_review_loop (meta.agent установлен)."""
    engine.advance_flow(session_id=session_id, human_confirmed=True)  # spec_analysis -> plan_draft
    plan_file = tmp_path / "implementation_plan.md"
    plan_file.write_text("# Plan\n\n## План\nКонтент\n", encoding="utf-8")
    res = engine.advance_flow(session_id=session_id)  # plan_draft -> plan_review_loop
    assert res["stage"]["id"] == "plan_review_loop"


# ---------------------------------------------------------------------------
# 1. advance_flow несуществующей сессии → FlowNotFoundError
# ---------------------------------------------------------------------------


def test_advance_flow_missing_session(engine_env: tuple[Path, FlowStorage, FlowEngine]) -> None:
    _tmp, _store, engine = engine_env
    with pytest.raises(FlowNotFoundError, match="не найдена"):
        engine.advance_flow(session_id="flow-nonexistent")


def test_advance_flow_missing_session_facade(tmp_path: Path) -> None:
    """Фасадная функция advance_flow пробрасывает FlowNotFoundError (маппится в FLOW_NOT_FOUND)."""
    with pytest.raises(FlowNotFoundError):
        advance_flow(session_id="flow-nonexistent", project_root=tmp_path)


# ---------------------------------------------------------------------------
# 2. Невалидные verdict / resolution → ValueError
# ---------------------------------------------------------------------------


def test_advance_flow_invalid_verdict(
    engine_env: tuple[Path, FlowStorage, FlowEngine],
) -> None:
    tmp_path, _store, engine = engine_env
    session_id = _start_feature_delivery(engine)
    _drive_to_review_loop(tmp_path, engine, session_id)
    with pytest.raises(ValueError, match="Недопустимый вердикт"):
        engine.advance_flow(session_id=session_id, verdict="maybe")


def test_advance_flow_verdict_required_on_review_stage(
    engine_env: tuple[Path, FlowStorage, FlowEngine],
) -> None:
    tmp_path, _store, engine = engine_env
    session_id = _start_feature_delivery(engine)
    _drive_to_review_loop(tmp_path, engine, session_id)
    with pytest.raises(ValueError, match="требует вердикт ревьюера"):
        engine.advance_flow(session_id=session_id, verdict=None)


def test_resolve_flow_escalation_invalid_resolution(
    engine_env: tuple[Path, FlowStorage, FlowEngine],
) -> None:
    _tmp, _store, engine = engine_env
    # Валидация resolution идёт до проверки сессии — ValueError даже для несуществующей
    with pytest.raises(ValueError, match="Недопустимая резолюция"):
        engine.resolve_flow_escalation(session_id="flow-nonexistent", resolution="force-merge")


# ---------------------------------------------------------------------------
# 3. resolve_flow_escalation на не-blocked сессии → SessionConflictError
# ---------------------------------------------------------------------------


def test_resolve_escalation_on_active_session(
    engine_env: tuple[Path, FlowStorage, FlowEngine],
) -> None:
    _tmp, _store, engine = engine_env
    session_id = _start_feature_delivery(engine)
    with pytest.raises(SessionConflictError, match="не находится в статусе blocked_escalate"):
        engine.resolve_flow_escalation(session_id=session_id, resolution="retry")


def test_resolve_escalation_missing_session(
    engine_env: tuple[Path, FlowStorage, FlowEngine],
) -> None:
    _tmp, _store, engine = engine_env
    with pytest.raises(FlowNotFoundError, match="не найдена"):
        engine.resolve_flow_escalation(session_id="flow-nonexistent", resolution="retry")


# ---------------------------------------------------------------------------
# 4. resolve_flow_escalation(resolution=abort) → терминальный статус + история
# ---------------------------------------------------------------------------


def _trip_circuit_breaker(tmp_path: Path, engine: FlowEngine, session_id: str) -> None:
    """Доводит сессию до BLOCKED_ESCALATE через лимит итераций plan_review_loop."""
    _drive_to_review_loop(tmp_path, engine, session_id)
    # loop_max этапа plan_review_loop = 3: три needs_work подряд → эскалация
    r1 = engine.advance_flow(session_id=session_id, verdict="needs_work")
    assert r1["status"] == "active"
    r2 = engine.advance_flow(session_id=session_id, verdict="needs_work")
    assert r2["status"] == "active"
    r3 = engine.advance_flow(session_id=session_id, verdict="needs_work")
    assert r3["status"] == "blocked_escalate"


def test_resolve_escalation_abort(
    engine_env: tuple[Path, FlowStorage, FlowEngine],
) -> None:
    tmp_path, store, engine = engine_env
    session_id = _start_feature_delivery(engine)
    _trip_circuit_breaker(tmp_path, engine, session_id)

    res = engine.resolve_flow_escalation(
        session_id=session_id, resolution="abort", notes="Отказ оператора"
    )
    assert res["status"] == "aborted"
    assert res["session_id"] == session_id

    session = store.get_session(session_id)
    assert session is not None and session.status == FlowStatus.ABORTED

    # История содержит событие escalation_abort с notes
    history = store.get_history(session_id)
    events = [h["event"] for h in history]
    assert "escalation_abort" in events
    abort_event = next(h for h in history if h["event"] == "escalation_abort")
    assert abort_event["notes"] == "Отказ оператора"

    # Дальнейшее продвижение отвергается (терминальный статус)
    with pytest.raises(FlowError, match="терминальном статусе"):
        engine.advance_flow(session_id=session_id, verdict="approved")


# ---------------------------------------------------------------------------
# 5. get_flow_status без session_id: 0 / ≥2 активных сессий
# ---------------------------------------------------------------------------


def test_get_flow_status_no_active_session(tmp_path: Path) -> None:
    res = get_flow_status(project_root=tmp_path)
    assert res["status"] == "no_active_session"
    assert "session" not in res


def test_get_flow_status_multiple_active(
    engine_env: tuple[Path, FlowStorage, FlowEngine],
) -> None:
    _tmp, _store, engine = engine_env
    s1 = _start_feature_delivery(engine)
    s2 = _start_feature_delivery(engine)

    res = engine.get_flow_status()
    assert res["status"] == "multiple_active"
    assert res["count"] == 2
    ids = {s["id"] for s in res["sessions"]}
    assert ids == {s1, s2}


def test_get_flow_status_single_active(
    engine_env: tuple[Path, FlowStorage, FlowEngine],
) -> None:
    _tmp, _store, engine = engine_env
    session_id = _start_feature_delivery(engine)
    res = engine.get_flow_status()
    assert res["session"]["id"] == session_id
    assert res["current_stage"]["id"] == "spec_analysis"
    assert "recent_events" in res


def test_get_flow_status_missing_session_id(
    engine_env: tuple[Path, FlowStorage, FlowEngine],
) -> None:
    _tmp, _store, engine = engine_env
    with pytest.raises(FlowNotFoundError, match="не найдена"):
        engine.get_flow_status(session_id="flow-nonexistent")


# ---------------------------------------------------------------------------
# 6. advance_flow на completed-сессии
# ---------------------------------------------------------------------------


def test_advance_flow_on_completed_session(
    engine_env: tuple[Path, FlowStorage, FlowEngine],
) -> None:
    _tmp, store, engine = engine_env
    session_id = _start_feature_delivery(engine)

    # Переводим сессию в completed напрямую через хранилище
    session = store.get_session(session_id)
    assert session is not None
    session.status = FlowStatus.COMPLETED
    store.update_session(session)

    with pytest.raises(FlowError, match="терминальном статусе 'completed'"):
        engine.advance_flow(session_id=session_id)


def test_start_flow_facade_with_explicit_storage(tmp_path: Path) -> None:
    """Фасад start_flow принимает явный storage (DI) и создаёт сессию под tmp_path."""
    store = FlowStorage(db_path=tmp_path / "facade_flows.db")
    res = start_flow(name="feature-delivery", task_id="t-p4", project_root=tmp_path, storage=store)
    assert res["status"] == "active"
    session = store.get_session(res["session_id"])
    assert session is not None
    assert session.task_id == "t-p4"
    assert Path(session.project_root) == tmp_path
