"""Утилиты подключения и настройки SQLite WAL для MCP Agent Platform."""

import sqlite3
from pathlib import Path

__all__ = ["build_bm25_weights_sql", "get_connection"]


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
