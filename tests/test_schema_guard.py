"""Контрактные тесты mcp_core.schema_guard (план v5, Этап 1).

Один набор на все пакеты: drift-варианты (missing/лишняя/недостающая/
переставленные колонки/legacy), транзакционный контракт, конкуренция,
rebuild_on_missing, service-самолечение, идемпотентность N×open.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from mcp_core.schema_guard import (
    FtsSpec,
    RebuildDependency,
    SchemaGuardContractError,
    StorageSchemaSpec,
    ensure_storage_schema,
)

EXPECTED_COLUMNS: tuple[str, ...] = ("id", "title", "body")
SERVICE_DDL: list[str] = [
    "CREATE TABLE IF NOT EXISTS file_sync (path TEXT PRIMARY KEY, mtime REAL);",
]
DEPS: frozenset[RebuildDependency] = frozenset(
    {
        RebuildDependency("artifacts_index", "table"),
        RebuildDependency("art_ai", "trigger"),
        RebuildDependency("art_ad", "trigger"),
        RebuildDependency("art_au", "trigger"),
    }
)


def _rebuild_ok(c: sqlite3.Connection) -> None:
    cur = c.cursor()
    cur.execute("DROP TABLE IF EXISTS test_fts;")
    cur.execute("DROP TABLE IF EXISTS artifacts_index;")
    # Артефакты wrong-kind (имя триггера, но table) тоже сносим: DROP TRIGGER их не берёт.
    for name in ("art_ai", "art_ad", "art_au"):
        cur.execute(f"DROP TABLE IF EXISTS {name};")
        cur.execute(f"DROP TRIGGER IF EXISTS {name};")
    cur.execute("CREATE VIRTUAL TABLE test_fts USING fts5(id, title, body);")
    cur.execute("CREATE TABLE artifacts_index (artifact_id TEXT PRIMARY KEY);")
    cur.execute("CREATE TRIGGER art_ai AFTER INSERT ON artifacts_index BEGIN SELECT 1; END;")
    cur.execute("CREATE TRIGGER art_ad AFTER DELETE ON artifacts_index BEGIN SELECT 1; END;")
    cur.execute("CREATE TRIGGER art_au AFTER UPDATE ON artifacts_index BEGIN SELECT 1; END;")


def _build_spec(
    *,
    expected_columns: tuple[str, ...] = EXPECTED_COLUMNS,
    rebuild=_rebuild_ok,
    service_ddl: list[str] | None = None,
    rebuild_on_missing: frozenset[RebuildDependency] = DEPS,
    schema_version: int = 4,
) -> StorageSchemaSpec:
    return StorageSchemaSpec(
        schema_version=schema_version,
        fts_checks={"test_fts": FtsSpec(expected_columns=expected_columns)},
        service_ddl=service_ddl if service_ddl is not None else SERVICE_DDL,
        rebuild=rebuild,
        rebuild_on_missing=rebuild_on_missing,
    )


def _open(path: Path, timeout: float = 5.0) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=timeout, isolation_level=None)  # autocommit
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def _cols(conn: sqlite3.Connection, table: str = "test_fts") -> tuple[str, ...]:
    return tuple(row[1] for row in conn.execute(f"PRAGMA table_info({table});").fetchall())


# --- drift-детекция -------------------------------------------------------


def test_fresh_db_is_drift_and_rebuilds(tmp_path: Path) -> None:
    conn = _open(tmp_path / "t.db")
    ensure_storage_schema(conn, _build_spec())
    assert _cols(conn) == EXPECTED_COLUMNS
    assert conn.execute("PRAGMA user_version;").fetchone()[0] == 4
    conn.close()


@pytest.mark.parametrize(
    ("create_sql", "expected"),
    [
        ("CREATE VIRTUAL TABLE test_fts USING fts5(id, title);", ("id", "title")),
        (
            "CREATE VIRTUAL TABLE test_fts USING fts5(id, title, body, extra);",
            ("id", "title", "body", "extra"),
        ),
        (
            "CREATE VIRTUAL TABLE test_fts USING fts5(id, title, legacy_col);",
            ("id", "title", "legacy_col"),
        ),
    ],
    ids=["missing-column", "extra-column", "legacy-column"],
)
def test_drift_variants_trigger_rebuild(
    tmp_path: Path, create_sql: str, expected: tuple[str, ...]
) -> None:
    conn = _open(tmp_path / "t.db")
    conn.execute(create_sql)
    conn.execute("PRAGMA user_version = 4;")
    conn.commit()
    ensure_storage_schema(conn, _build_spec())
    assert _cols(conn) == EXPECTED_COLUMNS
    conn.close()


def test_reordered_columns_are_drift_ordered_contract(tmp_path: Path) -> None:
    """Перестановка колонок = drift: веса bm25 позиционны."""
    conn = _open(tmp_path / "t.db")
    conn.execute("CREATE VIRTUAL TABLE test_fts USING fts5(title, id, body);")
    conn.execute("PRAGMA user_version = 4;")
    conn.commit()
    ensure_storage_schema(conn, _build_spec())
    assert _cols(conn) == EXPECTED_COLUMNS
    conn.close()


# --- транзакционный контракт ------------------------------------------------


def test_active_transaction_is_contract_error(tmp_path: Path) -> None:
    conn = _open(tmp_path / "t.db")
    ensure_storage_schema(conn, _build_spec())
    # Внутри активной транзакции создаём drift (сносим FTS-таблицу):
    conn.execute("BEGIN;")
    conn.execute("DROP TABLE test_fts;")
    assert conn.in_transaction
    with pytest.raises(SchemaGuardContractError):
        ensure_storage_schema(conn, _build_spec())
    conn.rollback()
    conn.close()


def test_begin_failure_propagates_original_error(tmp_path: Path) -> None:
    """Сбой BEGIN IMMEDIATE → исходная ошибка без маскировки 'cannot rollback'."""
    db = tmp_path / "t.db"
    conn = _open(db)

    # Конкурент держит EXCLUSIVE lock → BEGIN IMMEDIATE падает OperationalError.
    blocker = _open(db)
    blocker.execute("BEGIN IMMEDIATE;")
    blocker.execute("DROP TABLE IF EXISTS never_existing;")  # write-lock

    try:
        with pytest.raises(sqlite3.OperationalError, match="locked") as exc_info:
            ensure_storage_schema(conn, _build_spec())
        # Исходная ошибка прокинута как есть, НЕ маскирована rollback-ошибкой.
        assert "rollback" not in str(exc_info.value).lower()
    finally:
        blocker.rollback()
        blocker.close()
    assert not conn.in_transaction
    conn.close()


def test_rebuild_failure_full_rollback(tmp_path: Path) -> None:
    calls = []

    def _failing_rebuild(c: sqlite3.Connection) -> None:
        calls.append(1)
        c.execute("CREATE TABLE rebuild_marker (x);")
        raise RuntimeError("boom")

    conn = _open(tmp_path / "t.db")
    with pytest.raises(RuntimeError, match="boom"):
        ensure_storage_schema(conn, _build_spec(rebuild=_failing_rebuild))
    assert not conn.in_transaction
    assert calls == [1]
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table';")}
    assert "rebuild_marker" not in tables
    conn.close()


def test_second_check_under_lock_prevents_double_rebuild(tmp_path: Path) -> None:
    """Drift исчезает под lock (конкурент пересобрал) → callback НЕ вызывается, пустой COMMIT."""
    calls = []

    def _spy_rebuild(c: sqlite3.Connection) -> None:
        calls.append(1)
        _rebuild_ok(c)

    original = ensure_storage_schema.__globals__["_has_drift"]
    calls_to_drift = []

    def _drift_wrapper(c: sqlite3.Connection, spec: StorageSchemaSpec) -> bool:
        calls_to_drift.append(1)
        if len(calls_to_drift) == 1:
            return True  # быстрый путь: видит drift
        return original(c, spec)  # под lock: конкурент уже пересобрал

    import mcp_core.schema_guard as sg

    conn = _open(tmp_path / "t.db")
    # Здоровая база (конкурент «уже пересобрал» до нашего входа в guard).
    ensure_storage_schema(conn, _build_spec())
    # имитируем «конкурент пересобрал» между check-ами: база здорова,
    # но первый (быстрый) check возвращает drift.
    try:
        sg._has_drift = _drift_wrapper
        ensure_storage_schema(conn, _build_spec(rebuild=_spy_rebuild))
    finally:
        sg._has_drift = original

    assert calls == [], "rebuild не должен вызываться, если drift исчез под lock"
    assert not conn.in_transaction
    conn.close()


def test_concurrent_two_threads_callback_once(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    calls = []
    lock = threading.Lock()

    def _counting_rebuild(c: sqlite3.Connection) -> None:
        with lock:
            calls.append(1)
        _rebuild_ok(c)

    spec = _build_spec(rebuild=_counting_rebuild)
    barriers = []

    def worker() -> None:
        conn = _open(db, timeout=30.0)
        barriers.append(threading.Event())
        try:
            ensure_storage_schema(conn, spec)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads)

    assert calls == [1], f"callback должен выполниться ровно один раз, выполнений: {len(calls)}"


# --- rebuild_on_missing ------------------------------------------------------


@pytest.mark.parametrize(
    "drop_sql",
    [
        "DROP TABLE artifacts_index;",
        "DROP TRIGGER art_ai;",
        "DROP TRIGGER art_ad;",
        "DROP TRIGGER art_au;",
    ],
    ids=["missing-table", "missing-trigger-ai", "missing-trigger-ad", "missing-trigger-au"],
)
def test_missing_dependency_is_drift_rebuild(tmp_path: Path, drop_sql: str) -> None:
    conn = _open(tmp_path / "t.db")
    ensure_storage_schema(conn, _build_spec())
    conn.execute(drop_sql)
    conn.commit()
    ensure_storage_schema(conn, _build_spec())
    # всё восстановлено
    for dep in DEPS:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ? AND type = ?;", (dep.name, dep.kind)
        ).fetchone()
        assert row is not None, f"{dep.name} ({dep.kind}) должен существовать после rebuild"
    conn.close()


def test_wrong_object_kind_is_drift(tmp_path: Path) -> None:
    """Объект правильного имени, но неправильного типа (table vs trigger) → drift."""
    conn = _open(tmp_path / "t.db")
    conn.execute("CREATE VIRTUAL TABLE test_fts USING fts5(id, title, body);")
    # art_ai существует, но как table, а не trigger
    conn.execute("CREATE TABLE art_ai (x);")
    conn.execute("PRAGMA user_version = 4;")
    conn.commit()

    rebuild_calls = []

    def _spy(c: sqlite3.Connection) -> None:
        rebuild_calls.append(1)
        _rebuild_ok(c)

    ensure_storage_schema(conn, _build_spec(rebuild=_spy))
    assert rebuild_calls == [1]
    kind = conn.execute("SELECT type FROM sqlite_master WHERE name = 'art_ai';").fetchone()[0]
    assert kind == "trigger"
    conn.close()


# --- service_ddl -------------------------------------------------------------


def test_service_ddl_self_heal_on_healthy_schema(tmp_path: Path) -> None:
    conn = _open(tmp_path / "t.db")
    ensure_storage_schema(conn, _build_spec())
    conn.execute("DROP TABLE file_sync;")
    conn.commit()
    rebuild_calls = []

    def _spy(c: sqlite3.Connection) -> None:
        rebuild_calls.append(1)
        _rebuild_ok(c)

    ensure_storage_schema(conn, _build_spec(rebuild=_spy))
    assert rebuild_calls == [], "отсутствие service-таблицы — самолечение, НЕ drift-ребилд"
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='file_sync';").fetchone()
    conn.close()


def test_service_ddl_failure_does_not_rollback_rebuild(tmp_path: Path) -> None:
    """Сбой service_ddl: rebuild остаётся закоммиченным, service-таблицы отсутствуют."""
    conn = _open(tmp_path / "t.db")
    # Готовим drift вне guard'а (user_version=0 в свежей базе).
    bad_ddl = ["CREATE TABLE broken (x)"]  # не идемпотентен без IF NOT EXISTS
    conn.execute("CREATE TABLE broken (x);")

    spec = _build_spec(service_ddl=SERVICE_DDL + bad_ddl)
    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        ensure_storage_schema(conn, spec)

    # rebuild закоммичен: FTS и user_version валидны
    assert _cols(conn) == EXPECTED_COLUMNS
    assert conn.execute("PRAGMA user_version;").fetchone()[0] == 4
    conn.close()

    # детектируется следующим открытием: без bad DDL схема здорова
    conn2 = _open(tmp_path / "t.db")
    ensure_storage_schema(conn2, _build_spec())
    assert _cols(conn2) == EXPECTED_COLUMNS
    conn2.close()


def test_service_ddl_failure_detected_by_next_open(tmp_path: Path) -> None:
    """«FTS валиден, служебные таблицы отсутствуют» → следующий вызов создаёт их."""
    conn = _open(tmp_path / "t.db")
    ensure_storage_schema(conn, _build_spec())
    conn.execute("DROP TABLE file_sync;")
    conn.commit()
    rebuild_calls = []

    def _spy(c: sqlite3.Connection) -> None:
        rebuild_calls.append(1)
        _rebuild_ok(c)

    ensure_storage_schema(conn, _build_spec(rebuild=_spy))
    assert rebuild_calls == []
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='file_sync';").fetchone()
    conn.close()


# --- идемпотентность ----------------------------------------------------------


def test_n_open_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    calls = []

    def _spy(c: sqlite3.Connection) -> None:
        calls.append(1)
        _rebuild_ok(c)

    spec = _build_spec(rebuild=_spy)
    for _ in range(5):
        conn = _open(db)
        ensure_storage_schema(conn, spec)
        conn.close()
    # Ровно один rebuild — на первом open (свежая БД), последующие — no-op.
    assert calls == [1], "повторные открытия не должны вызывать rebuild повторно"
