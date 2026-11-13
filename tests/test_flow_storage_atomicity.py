"""Тесты атомарности перехода этапа и записи истории (задача A2 плана review-fixes).

Дефект: update_session и record_history выполнялись в разных соединениях/коммитах.
При сбое INSERT в flow_history этап уже сменился, а событие истории потеряно —
клиентский retry продвигал флоу дальше.

Фикс: update_session(history_events=[...]) пишет сессию и историю в одной
транзакции BEGIN IMMEDIATE / COMMIT / ROLLBACK (прецедент: _transaction
в src/mcp_knowledge/synchronizer.py).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import mcp_skills.flow_storage as flow_storage_module
from mcp_skills.flow_engine import step_flow
from mcp_skills.flow_storage import FlowStorage
from mcp_skills.models import FlowDefinition, FlowStage, FlowStageMeta, FlowTransition


class _FailingCursor:
    """Курсор-обёртка: INSERT INTO flow_history падает, остальное делегируется."""

    def __init__(self, real: sqlite3.Cursor) -> None:
        self._real = real

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        if "INSERT INTO flow_history" in sql:
            msg = "инжектированный сбой записи истории (тест A2)"
            raise sqlite3.OperationalError(msg)
        return self._real.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _FailingHistoryConnection:
    """Обёртка над реальным соединением: INSERT INTO flow_history падает по инжекту."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        if "INSERT INTO flow_history" in sql:
            msg = "инжектированный сбой записи истории (тест A2)"
            raise sqlite3.OperationalError(msg)
        return self._real.execute(sql, params)

    def cursor(self) -> sqlite3.Cursor:
        return _FailingCursor(self._real.cursor())

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def commit(self) -> None:
        self._real.commit()

    def close(self) -> None:
        self._real.close()


class _FailingHistoryGetConnection:
    """Подмена `core_get_connection`: реальные соединения с инжектом сбоя на INSERT в историю."""

    def __init__(self, original) -> None:
        self._original = original

    def __call__(self, db_path: Path | str, timeout: float = 5.0) -> sqlite3.Connection:
        real = self._original(db_path, timeout=timeout)
        return _FailingHistoryConnection(real)  # type: ignore[return-value]


@pytest.fixture
def store(tmp_path: Path) -> FlowStorage:
    return FlowStorage(db_path=tmp_path / "flows.db")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path


def _start_and_advance_once(root: Path, store: FlowStorage) -> str:
    """Стартует флоу и делает один успешный переход, возвращая session_id."""
    res = step_flow(flow="feature-delivery", project_root=root, storage=store)
    session_id = res["session_id"]
    step_flow(
        action="advance",
        session_id=session_id,
        expected_stage_id=res["stage"]["id"],
        human_confirmed=True,
        project_root=root,
        storage=store,
    )
    return session_id


def test_advance_rolls_back_session_when_history_fails(
    root: Path, store: FlowStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой записи истории не должен менять current_stage в БД (полный откат)."""
    res = step_flow(flow="feature-delivery", project_root=root, storage=store)
    session_id = res["session_id"]
    session = store.get_session(session_id)
    assert session is not None
    stage_before = session.current_stage_id
    version_before = session.version
    history_before = store.get_history(session_id)

    original_get_connection = flow_storage_module.core_get_connection
    monkeypatch.setattr(
        flow_storage_module,
        "core_get_connection",
        _FailingHistoryGetConnection(original_get_connection),
    )

    # Симуляция перехода: сессия мутируется в памяти, а запись истории падает.
    # Ожидание: обе операции (UPDATE flow_sessions + INSERT flow_history) откатятся.
    session.current_stage_id = "next_stage"
    with pytest.raises(sqlite3.OperationalError):
        store.update_session(
            session,
            history_events=[
                {
                    "stage_id": stage_before,
                    "iteration": session.iteration_count,
                    "event": "advance_auto",
                    "verdict": "auto",
                    "notes": "переход, который упадёт на записи истории",
                }
            ],
        )

    after = store.get_session(session_id)
    assert after is not None
    # Состояние сессии откатилось: этап и версия не изменились.
    assert after.current_stage_id == stage_before
    assert after.version == version_before
    # Событие истории не потерялось молча и не задвоилось: история не выросла.
    assert store.get_history(session_id) == history_before


def test_update_session_history_events_atomic_success(root: Path, store: FlowStorage) -> None:
    """Штатный путь: сессия и события истории пишутся одной транзакцией."""
    res = step_flow(flow="feature-delivery", project_root=root, storage=store)
    session_id = res["session_id"]
    first_stage = res["stage"]["id"]

    adv = step_flow(
        action="advance",
        session_id=session_id,
        expected_stage_id=first_stage,
        human_confirmed=True,
        project_root=root,
        storage=store,
    )
    assert adv["status"] == "active"

    updated = store.get_session(session_id)
    assert updated is not None
    assert updated.current_stage_id != first_stage
    history = store.get_history(session_id)
    events = [entry["event"] for entry in history]
    assert "start_flow" in events
    assert "advance_auto" in events


def test_named_transition_count_and_history_roll_back_together(
    root: Path, store: FlowStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An INSERT failure must not consume a named transition's max_uses."""
    import mcp_skills.flow_engine as flow_engine_module

    definition = FlowDefinition(
        name="bounded-flow",
        stages={
            "start": FlowStage(
                id="start",
                title="Start",
                instruction="Go",
                meta=FlowStageMeta(
                    transition_required=True,
                    transitions={"next": FlowTransition(target="end", max_uses=1)},
                ),
            ),
            "end": FlowStage(id="end", title="End", instruction="Done"),
        },
        stage_order=["start", "end"],
    )
    monkeypatch.setattr(
        flow_engine_module, "load_flow_definition", lambda *_args, **_kw: definition
    )
    monkeypatch.setattr(flow_engine_module, "get_current_branch", lambda _root: "test-branch")
    started = step_flow(flow="bounded-flow", project_root=root, storage=store)
    sid = started["session_id"]
    before = store.get_session(sid)
    before_history = store.get_history(sid)
    assert before is not None

    original_get_connection = flow_storage_module.core_get_connection
    monkeypatch.setattr(
        flow_storage_module,
        "core_get_connection",
        _FailingHistoryGetConnection(original_get_connection),
    )
    with pytest.raises(sqlite3.OperationalError):
        step_flow(session_id=sid, transition="next", project_root=root, storage=store)
    after = store.get_session(sid)
    assert after is not None
    assert after.current_stage_id == "start"
    assert after.version == before.version
    assert after.context_data.get("transition_counts") is None
    assert store.get_history(sid) == before_history
