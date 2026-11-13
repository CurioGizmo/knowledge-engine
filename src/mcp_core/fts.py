"""Унифицированный конструктор FTS5 таблиц, детектор токенизатора и атомарная миграция схем."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable

logger = logging.getLogger("mcp_core.fts")

__all__ = [
    "SCHEMA_VERSION",
    "ensure_schema",
    "fts5_table_sql",
    "resolve_tokenizer",
    "round_bm25_score",
]

# Целевая версия схемы FTS5 (v4).
# Рантайм-версионирование схемы подключено через PRAGMA user_version / ensure_schema
# во всех поисковых хранилищах (mcp_knowledge, mcp_skills, mcp_audit).
# v4 (R6, 2026-10-19): изменение нормализации токенов стеммера (запрет среза
# -ат/-ят; одиночные гласные не срезаются 2-м проходом) меняет содержимое
# indexing-side стемов в mcp_skills — все FTS-индексы пересобираются.
# v6 (2026-11): добавление task_id в body artifacts_fts, created_at/updated_at
# в audit_index и очистка шаблонных заголовков из knowledge body.
SCHEMA_VERSION: int = 6


def round_bm25_score(value: float) -> float:
    """Округляет BM25-скора для публичного вывода, не обращая её в 0.0.

    Ревью шага 3 (п. 5): прежний round(value, 6) мог округлить ненулевой
    скора (например, 4.6e-09) до 0.0, теряя сигнал релевантности. Теперь:
    - обычные значения округляются до 6 знаков;
    - если округление до 6 знаков даёт 0.0 при ненулевом value, значение
      не округляется и возвращается как есть (repr-точность float).
    """
    if value <= 0.0:
        return round(value, 6)
    rounded = round(value, 6)
    if rounded == 0.0:
        return value
    return rounded


def resolve_tokenizer(tokenizer_type: str = "trigram") -> str:
    """Определяет токенизатор SQLite FTS5.

    - tokenizer_type == 'unicode61': 'unicode61 remove_diacritics 2'.
      Обеспечивает точный поиск по границам слов и корректную работу префиксов (word*).
      Используется в knowledge_index.
    - tokenizer_type == 'trigram' (по умолчанию для универсального FTS):
      - >= (3, 45, 0): 'trigram remove_diacritics 2'
      - (3, 34, 0) .. (3, 44, x): 'trigram' (с предупреждением об отсутствии диакритики)
      - < (3, 34, 0): 'unicode61 remove_diacritics 2' (фолбэк)
    """
    if tokenizer_type == "unicode61":
        return "unicode61 remove_diacritics 2"

    version = sqlite3.sqlite_version_info
    if version >= (3, 45, 0):
        return "trigram remove_diacritics 2"
    elif version >= (3, 34, 0):
        logger.warning(
            "SQLite %s поддерживают trigram, но remove_diacritics 2 требует >=3.45.0. Диакритика сохраняется.",
            sqlite3.sqlite_version,
        )
        return "trigram"
    else:
        logger.warning(
            "SQLite %s устарел (<3.34.0) и не поддерживает trigram. Используется fallback: unicode61.",
            sqlite3.sqlite_version,
        )
        return "unicode61 remove_diacritics 2"


def fts5_table_sql(
    table: str,
    columns: list[str],
    *,
    unindexed: list[str] | None = None,
    tokenizer: str,
    content: str | None = None,
    content_rowid: str | None = None,
) -> str:
    """Генерирует DDL оператор создания FTS5 виртуальной таблицы.

    Соблюдает строгий стиль без пробелов вокруг '=': tokenize='...', content='...'.
    """
    unindexed_set = set(unindexed or [])
    col_defs: list[str] = []

    for col in columns:
        if col in unindexed_set:
            col_defs.append(f"{col} UNINDEXED")
        else:
            col_defs.append(col)

    options: list[str] = []
    if content:
        options.append(f"content='{content}'")
    if content_rowid:
        options.append(f"content_rowid='{content_rowid}'")
    options.append(f"tokenize='{tokenizer}'")

    body_items = col_defs + options
    body = ",\n    ".join(body_items)
    return f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING fts5(\n    {body}\n);"


def ensure_schema(
    conn: sqlite3.Connection,
    *,
    version: int,
    rebuild: Callable[[sqlite3.Connection], None],
) -> None:
    """Проверяет PRAGMA user_version и атомарно выполняет rebuild при несовпадении.

    Использует conn.isolation_level = None с явным BEGIN IMMEDIATE / COMMIT / ROLLBACK.
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA user_version;")
    row = cursor.fetchone()
    current_version = row[0] if row else 0

    if current_version == version:
        return

    old_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        cursor.execute("BEGIN IMMEDIATE;")
        rebuild(conn)
        cursor.execute(f"PRAGMA user_version = {version};")
        cursor.execute("COMMIT;")
    except Exception:
        cursor.execute("ROLLBACK;")
        raise
    finally:
        conn.isolation_level = old_isolation
