"""Изолированное SQLite-хранилище сессий флоу для MCP Skills Engine.

Сессии хранятся отдельно от кэша скиллов в файле flows.db внутри .agents/.cache.
Поддерживает optimistic locking (колонка version) для защиты от параллельных
модификаций и NULL-safe поиск активных сессий по git-ветке.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mcp_core.db import get_connection as core_get_connection
from mcp_core.paths import _find_default_project_root, get_cache_dir
from mcp_skills.errors import SessionConflictError
from mcp_skills.models import FlowSession, FlowStatus

__all__ = ["FlowStorage"]

FLOW_SESSIONS_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS flow_sessions (
        id TEXT PRIMARY KEY,
        task_id TEXT,
        project_root TEXT NOT NULL,
        git_branch TEXT,
        flow_name TEXT NOT NULL,
        current_stage_id TEXT NOT NULL,
        iteration_count INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL CHECK (status IN ('active', 'blocked_escalate', 'completed', 'aborted')),
        version INTEGER NOT NULL DEFAULT 1,
        context_data TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
"""

FLOW_SESSIONS_INDEX_SQL = """
    CREATE INDEX IF NOT EXISTS idx_flow_sessions_lookup
    ON flow_sessions(project_root, git_branch, status, updated_at);
"""

FLOW_HISTORY_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS flow_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        stage_id TEXT NOT NULL,
        iteration INTEGER NOT NULL,
        event TEXT NOT NULL,
        verdict TEXT,
        notes TEXT,
        created_at REAL NOT NULL,
        FOREIGN KEY(session_id) REFERENCES flow_sessions(id) ON DELETE CASCADE
    );
"""


def _row_to_session(row: sqlite3.Row) -> FlowSession:
    """Десериализует строку flow_sessions в FlowSession (context_data из JSON)."""
    data: dict[str, Any] = dict(row)
    raw_context = data.get("context_data")
    data["context_data"] = (
        json.loads(raw_context) if isinstance(raw_context, str) else (raw_context or {})
    )
    # Fail-closed: невалидный статус из БД падает сразу с понятной ошибкой enum.
    data["status"] = FlowStatus(data["status"])
    return FlowSession(**data)


class FlowStorage:
    """Управление SQLite-хранилищем сессий флоу с optimistic locking."""

    def __init__(self, db_path: Path | None = None, project_root: Path | None = None) -> None:
        if db_path is None:
            db_path = get_cache_dir(project_root or _find_default_project_root()) / "flows.db"
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        # PRAGMA foreign_keys = ON задаёт канонический core_get_connection (P2) —
        # нужен для ON DELETE CASCADE (flow_events → flow_sessions).
        conn = core_get_connection(self.db_path, timeout=5.0)
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.get_connection() as conn:
            conn.execute(FLOW_SESSIONS_CREATE_SQL)
            conn.execute(FLOW_SESSIONS_INDEX_SQL)
            conn.execute(FLOW_HISTORY_CREATE_SQL)
            conn.commit()

    def save_session(self, session: FlowSession) -> None:
        """Создаёт новую запись сессии в flow_sessions."""
        context_json = json.dumps(session.context_data, ensure_ascii=False)
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO flow_sessions (
                    id, task_id, project_root, git_branch, flow_name, current_stage_id,
                    iteration_count, status, version, context_data, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    session.task_id,
                    session.project_root,
                    session.git_branch,
                    session.flow_name,
                    session.current_stage_id,
                    session.iteration_count,
                    session.status.value,
                    session.version,
                    context_json,
                    session.created_at,
                    session.updated_at,
                ),
            )
            conn.commit()

    def get_session(self, session_id: str) -> FlowSession | None:
        """Возвращает сессию по id или None, если сессия не найдена."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM flow_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_session(row)

    def update_session(
        self,
        session: FlowSession,
        history_events: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        """Атомарно обновляет сессию с проверкой optimistic locking по version.

        Если передан ``history_events``, события истории (flow_history) пишутся
        в той же транзакции ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK``
        (прецедент: ``_transaction`` в mcp_knowledge.synchronizer). При сбое
        любой из операций (UPDATE flow_sessions или INSERT в flow_history)
        вся транзакция откатывается: этап не меняется, событие истории
        не теряется и не остаётся висеть без смены этапа.

        git_branch пишется вместе с переходом этапа в той же транзакции:
        смена ветки сессии (продолжение работы в новой ветке после checkout,
        D5) атомарна с продвижением этапа — не бывает состояния «ветка
        обновлена, а этап нет» и наоборот.

        Args:
            session: сессия с изменённым состоянием (мутация version/updated_at
                применяется только после успешного COMMIT).
            history_events: события истории в виде dict с ключами
                ``stage_id``, ``iteration``, ``event`` и опциональными
                ``verdict``, ``notes``.

        Raises:
            SessionConflictError: если запись с текущей версией не найдена
                (параллельная модификация или удаление сессии).
        """
        now = time.time()
        context_json = json.dumps(session.context_data, ensure_ascii=False)
        events = list(history_events) if history_events else []
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE;")
            try:
                updated = cursor.execute(
                    """
                    UPDATE flow_sessions
                    SET current_stage_id = ?,
                        iteration_count = ?,
                        status = ?,
                        version = version + 1,
                        context_data = ?,
                        git_branch = ?,
                        updated_at = ?
                    WHERE id = ? AND version = ?
                    """,
                    (
                        session.current_stage_id,
                        session.iteration_count,
                        session.status.value,
                        context_json,
                        session.git_branch,
                        now,
                        session.id,
                        session.version,
                    ),
                )
                if updated.rowcount == 0:
                    raise SessionConflictError(
                        f"Конфликт параллельного изменения сессии {session.id} (версия {session.version})"
                    )
                for event in events:
                    cursor.execute(
                        """
                        INSERT INTO flow_history (
                            session_id, stage_id, iteration, event, verdict, notes, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session.id,
                            event["stage_id"],
                            event["iteration"],
                            event["event"],
                            event.get("verdict"),
                            event.get("notes", ""),
                            time.time(),
                        ),
                    )
            except BaseException:
                cursor.execute("ROLLBACK;")
                raise
            else:
                cursor.execute("COMMIT;")
        session.version += 1
        session.updated_at = now

    def record_history(
        self,
        session_id: str,
        stage_id: str,
        iteration: int,
        event: str,
        verdict: str | None = None,
        notes: str = "",
    ) -> None:
        """Добавляет запись о событии этапа в flow_history."""
        self.update_session_history_only(
            session_id,
            [
                {
                    "stage_id": stage_id,
                    "iteration": iteration,
                    "event": event,
                    "verdict": verdict,
                    "notes": notes,
                }
            ],
        )

    def update_session_history_only(
        self,
        session_id: str,
        history_events: Sequence[dict[str, Any]],
    ) -> None:
        """Записывает события истории в отдельной короткой транзакции.

        Для переходов этапа используйте ``update_session(history_events=[...])``
        — он меняет сессию и историю атомарно.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE;")
            try:
                for event in history_events:
                    cursor.execute(
                        """
                        INSERT INTO flow_history (
                            session_id, stage_id, iteration, event, verdict, notes, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            event["stage_id"],
                            event["iteration"],
                            event["event"],
                            event.get("verdict"),
                            event.get("notes", ""),
                            time.time(),
                        ),
                    )
            except BaseException:
                cursor.execute("ROLLBACK;")
                raise
            else:
                cursor.execute("COMMIT;")

    def get_history(self, session_id: str) -> list[dict[str, Any]]:
        """Возвращает историю событий сессии в хронологическом порядке."""
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM flow_history WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def find_active_sessions(
        self,
        project_root: str,
        git_branch: str | None = None,
        statuses: tuple[str, ...] = ("active", "blocked_escalate"),
        allow_project_fallback: bool = False,
    ) -> list[FlowSession]:
        """Ищет незавершённые сессии (active, blocked_escalate) по (project_root, git_branch) с NULL-safe сравнением.

        Если по указанной ветке совпадений нет — выполняет fallback-поиск
        только по project_root среди незавершённых сессий. Без явного
        ``allow_project_fallback=True`` fallback действует только при
        ``git_branch is None`` (вне git / detached HEAD), сохраняя строгую
        изоляцию веток (H1); opt-in включает fallback и для конкретной
        ветки — продолжение единственной сессии проекта после checkout
        (D5).
        """
        placeholders = ",".join("?" for _ in statuses)
        with self.get_connection() as conn:
            query_exact = f"""
                SELECT * FROM flow_sessions
                WHERE project_root = ? AND git_branch IS ? AND status IN ({placeholders})
                ORDER BY updated_at DESC
            """  # noqa: S608
            rows = conn.execute(
                query_exact,
                (project_root, git_branch, *statuses),
            ).fetchall()
            # Если точных совпадений нет: вне git (branch=None) fallback работает
            # всегда; для конкретной ветки — только по явному opt-in (H1/D5)
            if not rows and (git_branch is None or allow_project_fallback):
                query_fallback = f"""
                    SELECT * FROM flow_sessions
                    WHERE project_root = ? AND status IN ({placeholders})
                    ORDER BY updated_at DESC
                """  # noqa: S608
                rows = conn.execute(
                    query_fallback,
                    (project_root, *statuses),
                ).fetchall()
        return [_row_to_session(row) for row in rows]
