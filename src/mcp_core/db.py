"""Утилиты подключения и настройки SQLite WAL для MCP Agent Platform."""

import atexit
import contextlib
import sqlite3
from pathlib import Path

__all__ = [
    "build_bm25_weights_sql",
    "cleanup_temp_dbs",
    "get_connection",
    "register_temp_db",
]

# Реестр эфемерных баз SQLite для незакреплённых корней (авто-очистка при выходе)
_TEMP_DBS: set[Path] = set()


def register_temp_db(path: Path | str) -> Path:
    """Регистрирует путь временной SQLite базы для гарантированной очистки при завершении процесса."""
    db_path = Path(path).resolve()
    _TEMP_DBS.add(db_path)
    return db_path


def cleanup_temp_dbs() -> None:
    """Удаляет все зарегистрированные временные базы SQLite и связанные WAL/SHM файлы."""
    for db_file in list(_TEMP_DBS):
        for path in (
            db_file,
            db_file.with_name(f"{db_file.name}-wal"),
            db_file.with_name(f"{db_file.name}-shm"),
        ):
            with contextlib.suppress(Exception):
                path.unlink(missing_ok=True)
    _TEMP_DBS.clear()


atexit.register(cleanup_temp_dbs)


def get_connection(db_path: Path | str, timeout: float = 5.0) -> sqlite3.Connection:
    """Создаёт и настраивает SQLite соединение с режимом WAL и безопасными тайм-аутами.

    Канонический набор PRAGMA для всех хранилищ платформы (P2):
    WAL, busy_timeout=5000, synchronous=NORMAL, foreign_keys=ON (в SQLite FK
    по умолчанию ВЫКЛ на каждое соединение; нужно flow_storage для
    ON DELETE CASCADE, для хранилищ без FK-констрейнтов — no-op).
    """
    if isinstance(db_path, Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        path_str = str(db_path)
    else:
        path_obj = Path(db_path)
        if str(path_obj) != ":memory:":
            path_obj.parent.mkdir(parents=True, exist_ok=True)
        path_str = db_path

    conn = sqlite3.connect(path_str, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.create_function(
        "lower", 1, lambda s: s.lower() if isinstance(s, str) else s, deterministic=True
    )
    conn.create_function(
        "upper", 1, lambda s: s.upper() if isinstance(s, str) else s, deterministic=True
    )
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def build_bm25_weights_sql(weights: tuple[float, ...], expected_len: int) -> str:
    """Формирует безопасную SQL-строку весов BM25 с проверкой длины."""
    if len(weights) != expected_len:
        msg = f"Количество весов BM25 ({len(weights)}) должно строго соответствовать {expected_len} колонкам схемы"
        raise RuntimeError(msg)
    return ", ".join(str(w) for w in weights)
