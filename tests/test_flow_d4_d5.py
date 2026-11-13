"""Тесты D4 и D5 плана review-fixes (flow_engine / flow_storage).

D4 — честные сигналы стейт-машины:
- verdict='needs_work' на этапе без agent удерживает этап (счётчик итераций растёт,
  Circuit Breaker при превышении loop_max) вместо безусловного автоперехода;
- require_human_confirm проверяет явный флаг human_confirmed, а не прокси по notes.

D5 — продолжение сессии после смены git-ветки:
- update_session персистит git_branch (атомарно с переходом этапа);
- find_active_sessions(allow_project_fallback=True) — fallback по project_root
  при пустом точном поиске (изоляция веток по умолчанию сохранена, H1);
- step_flow advance без session_id после смены ветки продолжает единственную
  сессию проекта и обновляет её git_branch в той же транзакции, что и переход;
- несколько fallback-сессий → AmbiguousSessionError (зомби-сессии не создаются);
- status/retry — fallback-чтение без обновления ветки.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mcp_skills.errors import AmbiguousSessionError, SessionConflictError
from mcp_skills.flow_engine import FlowEngine, step_flow
from mcp_skills.flow_storage import FlowStorage
from mcp_skills.models import FlowSession, FlowStatus


def _write_flow(proj: Path, name: str, content: str) -> None:
    flows_dir = proj / ".agents" / "flows"
    flows_dir.mkdir(parents=True, exist_ok=True)
    (flows_dir / f"{name}.md").write_text(content, encoding="utf-8")


def _make_session(
    root: Path, sid: str, branch: str | None, stage: str = "spec_analysis"
) -> FlowSession:
    now = time.time()
    return FlowSession(
        id=sid,
        project_root=str(root),
        git_branch=branch,
        flow_name="feature-delivery",
        current_stage_id=stage,
        iteration_count=1,
        status=FlowStatus.ACTIVE,
        version=1,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# D4: verdict='needs_work' на этапе без agent
# ---------------------------------------------------------------------------

D4_AUTO_FLOW = """# D4 Auto Flow

## 1. Auto One [id: s_auto]
Instruction 1

## 2. Auto Two [id: s_next]
Instruction 2
"""


def _d4_engine(tmp_path: Path, flow_md: str = D4_AUTO_FLOW) -> tuple[FlowStorage, FlowEngine, str]:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname='d4'\n", encoding="utf-8")
    _write_flow(proj, "d4-auto", flow_md)
    store = FlowStorage(db_path=proj / "flows.db")
    engine = FlowEngine(project_root=proj, storage=store)
    res = engine.start_flow("d4-auto", project_root=proj)
    return store, engine, res["session_id"]


def test_needs_work_on_agentless_stage_holds_stage(tmp_path: Path) -> None:
    """needs_work на этапе без agent удерживает этап вместо автоперехода (D4)."""
    store, engine, sid = _d4_engine(tmp_path)

    r1 = engine.advance_flow(session_id=sid, verdict="needs_work")
    assert r1["status"] == "active"
    assert r1["stage_id"] == "s_auto"
    assert r1["iteration_count"] == 2
    assert "Требуется доработка" in r1["message"]
    assert r1["instruction"] == "Instruction 1"

    # Этап удержан и зафиксирован в БД и истории
    session = store.get_session(sid)
    assert session is not None
    assert session.current_stage_id == "s_auto"
    assert session.iteration_count == 2
    events = [h["event"] for h in store.get_history(sid)]
    assert "auto_needs_work" in events

    # Обычный advance (без needs_work) проходит дальше
    r2 = engine.advance_flow(session_id=sid)
    assert r2["stage"]["id"] == "s_next"


def test_needs_work_auto_stage_circuit_breaker(tmp_path: Path) -> None:
    """Превышение loop_max при needs_work на auto-этапе → blocked_escalate (D4)."""
    flow_md = """# D4 CB Flow

## 1. Auto One [id: s_auto]
<!-- meta: {"loop_max": 2} -->
Instruction 1

## 2. Auto Two [id: s_next]
Instruction 2
"""
    store, engine, sid = _d4_engine(tmp_path, flow_md)

    r1 = engine.advance_flow(session_id=sid, verdict="needs_work")
    assert r1["status"] == "active"
    r2 = engine.advance_flow(session_id=sid, verdict="needs_work")
    assert r2["status"] == "blocked_escalate"

    session = store.get_session(sid)
    assert session is not None
    assert session.status == FlowStatus.BLOCKED_ESCALATE
    events = [h["event"] for h in store.get_history(sid)]
    assert "circuit_breaker_tripped" in events

    with pytest.raises(SessionConflictError, match="Circuit Breaker"):
        engine.advance_flow(session_id=sid)


def test_invalid_verdict_on_auto_stage_ignored(tmp_path: Path) -> None:
    """Невалидная строка вердикта на auto-этапе трактуется как его отсутствие."""
    _store, engine, sid = _d4_engine(tmp_path)
    res = engine.advance_flow(session_id=sid, verdict="banana")
    assert res["stage"]["id"] == "s_next"


def test_spec_analysis_needs_work_hold_with_human_confirm(tmp_path: Path) -> None:
    """needs_work на spec_analysis (require_human_confirm) удерживает этап."""
    store = FlowStorage(db_path=tmp_path / "flows.db")
    engine = FlowEngine(project_root=tmp_path, storage=store)
    res = engine.start_flow(name="feature-delivery")
    sid = res["session_id"]

    r1 = engine.advance_flow(session_id=sid, verdict="needs_work", human_confirmed=True)
    assert r1["status"] == "active"
    assert r1["stage_id"] == "spec_analysis"
    assert r1["iteration_count"] == 2
    events = [h["event"] for h in store.get_history(sid)]
    assert "auto_needs_work" in events

    r2 = engine.advance_flow(session_id=sid, human_confirmed=True)
    assert r2["stage"]["id"] == "plan_draft"


# ---------------------------------------------------------------------------
# D4: require_human_confirm требует явный human_confirmed
# ---------------------------------------------------------------------------


def test_require_human_confirm_explicit_flag(tmp_path: Path) -> None:
    """Непустые notes больше не прокси: нужен явный human_confirmed=True."""
    store = FlowStorage(db_path=tmp_path / "flows.db")
    engine = FlowEngine(project_root=tmp_path, storage=store)
    res = engine.start_flow(name="feature-delivery")
    sid = res["session_id"]

    blocked = engine.advance_flow(session_id=sid, notes="ТЗ согласовано")
    assert blocked["status"] == "gate_blocked"
    assert any("human_confirmed" in b for b in blocked["blockers"])
    # gate_failed зафиксирован, этап не сменился
    events = [h["event"] for h in store.get_history(sid)]
    assert "gate_failed" in events
    session = store.get_session(sid)
    assert session is not None
    assert session.current_stage_id == "spec_analysis"

    # Явный флаг с пустыми notes проходит
    ok = engine.advance_flow(session_id=sid, human_confirmed=True)
    assert ok["stage"]["id"] == "plan_draft"


# ---------------------------------------------------------------------------
# D5: storage — персистентность git_branch и fallback-поиск
# ---------------------------------------------------------------------------


def test_update_session_persists_git_branch(tmp_path: Path) -> None:
    """update_session пишет git_branch в БД (версия растёт)."""
    store = FlowStorage(db_path=tmp_path / "flows.db")
    session = _make_session(tmp_path, "flow-persist", "main", stage="s1")
    store.save_session(session)

    loaded = store.get_session("flow-persist")
    assert loaded is not None
    loaded.git_branch = "feat/d5"
    loaded.current_stage_id = "s2"
    store.update_session(loaded)

    after = store.get_session("flow-persist")
    assert after is not None
    assert after.git_branch == "feat/d5"
    assert after.current_stage_id == "s2"
    assert after.version == 2


def test_find_active_sessions_project_fallback_flag(tmp_path: Path) -> None:
    """allow_project_fallback=True находит сессии чужих веток; по умолчанию — нет (H1)."""
    store = FlowStorage(db_path=tmp_path / "flows.db")
    store.save_session(_make_session(tmp_path, "flow-fb-1", "main"))

    # H1: строгая изоляция веток по умолчанию
    assert store.find_active_sessions(str(tmp_path), git_branch="feature-x") == []

    # Явный opt-in на fallback по project_root
    found = store.find_active_sessions(
        str(tmp_path), git_branch="feature-x", allow_project_fallback=True
    )
    assert [s.id for s in found] == ["flow-fb-1"]


# ---------------------------------------------------------------------------
# D5: step_flow advance без session_id после смены ветки
# ---------------------------------------------------------------------------


def test_step_flow_branch_switch_resumes_and_updates_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Смена ветки: advance без session_id продолжает единственную сессию и
    обновляет её git_branch в той же транзакции, что и переход этапа."""
    store = FlowStorage(db_path=tmp_path / "flows.db")
    monkeypatch.setattr("mcp_skills.flow_engine.get_current_branch", lambda _root: "main")

    res = step_flow(flow="feature-delivery", project_root=tmp_path, storage=store)
    sid = res["session_id"]
    assert store.get_session(sid).git_branch == "main"

    # Переключаемся на новую ветку
    monkeypatch.setattr("mcp_skills.flow_engine.get_current_branch", lambda _root: "feat/d5")
    res2 = step_flow(action="advance", human_confirmed=True, project_root=tmp_path, storage=store)
    assert res2["session_id"] == sid
    assert res2["stage"]["id"] == "plan_draft"

    session = store.get_session(sid)
    assert session is not None
    assert session.git_branch == "feat/d5"
    # Сессия теперь находится по новой ветке, и она одна
    assert len(store.find_active_sessions(str(tmp_path), git_branch="feat/d5")) == 1
    assert len(store.find_active_sessions(str(tmp_path), git_branch="main")) == 0


def test_step_flow_gate_block_does_not_rebranch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Если шлюз заблокировал переход — ветка не обновляется (только вместе с переходом)."""
    store = FlowStorage(db_path=tmp_path / "flows.db")
    monkeypatch.setattr("mcp_skills.flow_engine.get_current_branch", lambda _root: "main")
    res = step_flow(flow="feature-delivery", project_root=tmp_path, storage=store)
    sid = res["session_id"]

    monkeypatch.setattr("mcp_skills.flow_engine.get_current_branch", lambda _root: "feat/x")
    blocked = step_flow(action="advance", project_root=tmp_path, storage=store)
    assert blocked["status"] == "gate_blocked"

    session = store.get_session(sid)
    assert session is not None
    assert session.git_branch == "main"


def test_step_flow_fallback_ambiguous_across_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Несколько активных сессий на разных ветках → AmbiguousSessionError (D5.3)."""
    store = FlowStorage(db_path=tmp_path / "flows.db")
    store.save_session(_make_session(tmp_path, "flow-d5-a", "feat/a"))
    store.save_session(_make_session(tmp_path, "flow-d5-b", "feat/b"))

    monkeypatch.setattr("mcp_skills.flow_engine.get_current_branch", lambda _root: "main")
    with pytest.raises(AmbiguousSessionError):
        step_flow(action="advance", project_root=tmp_path, storage=store)

    # Зомби-сессии не создались; все прежние на месте
    assert len(store.find_active_sessions(str(tmp_path), git_branch="feat/a")) == 1
    assert len(store.find_active_sessions(str(tmp_path), git_branch="feat/b")) == 1
    assert (
        len(
            store.find_active_sessions(
                str(tmp_path), git_branch="main", allow_project_fallback=True
            )
        )
        == 2
    )

    # status тоже видит неоднозначность через fallback
    status_res = step_flow(action="status", project_root=tmp_path, storage=store)
    assert status_res["status"] == "multiple_active"
    assert status_res["count"] == 2


def test_step_flow_explicit_session_id_does_not_rebranch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Явный session_id не подтягивает ветку сессии к текущей ветке."""
    store = FlowStorage(db_path=tmp_path / "flows.db")
    store.save_session(_make_session(tmp_path, "flow-d5-main", "main"))

    monkeypatch.setattr("mcp_skills.flow_engine.get_current_branch", lambda _root: "feat/x")
    res = step_flow(
        action="advance",
        session_id="flow-d5-main",
        human_confirmed=True,
        project_root=tmp_path,
        storage=store,
    )
    assert res["stage"]["id"] == "plan_draft"
    session = store.get_session("flow-d5-main")
    assert session is not None
    assert session.git_branch == "main"


def test_status_and_retry_fallback_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """status/retry находят сессию через fallback по проекту, но ветку не меняют (D5.4)."""
    store = FlowStorage(db_path=tmp_path / "flows.db")
    store.save_session(_make_session(tmp_path, "flow-d5-ro", "main"))

    monkeypatch.setattr("mcp_skills.flow_engine.get_current_branch", lambda _root: "feat/x")

    status_res = step_flow(action="status", project_root=tmp_path, storage=store)
    assert status_res["session"]["id"] == "flow-d5-ro"
    assert status_res["current_stage"]["id"] == "spec_analysis"

    retry_res = step_flow(action="retry", project_root=tmp_path, storage=store)
    assert retry_res["stage"]["id"] == "spec_analysis"

    session = store.get_session("flow-d5-ro")
    assert session is not None
    assert session.git_branch == "main"
