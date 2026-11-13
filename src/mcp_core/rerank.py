"""Переранжирование результатов поиска и учет бонусов по колонкам."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

__all__ = [
    "SearchHit",
    "matched_column_flags",
    "rerank_hits",
]


@dataclass(frozen=True)
class SearchHit:
    """Кандидат выдачи поиска после извлечения из базы."""

    id: str
    raw_bm25: float  # Отрицательный BM25 из SQLite (чем меньше/отрицательнее, тем лучше)
    matched_columns: int  # Число совпавших колонок (частотный тай-брейкер)
    field_matches: dict[str, bool]  # Флаги совпадения по колонкам (title, symptom, etc.)
    trust_rank: int  # Порядковый номер доверия из первичной SQL сортировки


def matched_column_flags(
    conn: sqlite3.Connection,
    table: str,
    rowid_col_values: dict[str, Any] | int | str,
    weights_by_column: dict[str, tuple[float, ...]],
    query: str,
) -> dict[str, bool]:
    """Зондирует совпадение отдельных колонок FTS5 таблицы через единичные bm25-вектора.

    Для каждой колонки в weights_by_column вычисляется bm25:
    flag = (val != 0.0) по сырому float (без round).
    Оптимизировано: выполняет единый SELECT вместо N запросов по числу колонок.
    """
    where_id = "rowid = ?" if isinstance(rowid_col_values, int) else "id = ?"
    target_val = rowid_col_values

    results: dict[str, bool] = {col: False for col in weights_by_column}
    if not weights_by_column:
        return results

    cursor = conn.cursor()
    col_names = list(weights_by_column.keys())
    select_items = [
        f"bm25({table}, {', '.join(str(w) for w in weights_by_column[col])})" for col in col_names
    ]
    sql = f"""
        SELECT {", ".join(select_items)}
        FROM {table}
        WHERE {table} MATCH ? AND {where_id};
    """
    try:
        cursor.execute(sql, (query, target_val))
        row = cursor.fetchone()
        if row is not None:
            for idx, col_name in enumerate(col_names):
                val = row[idx]
                results[col_name] = val is not None and float(val) != 0.0
    except sqlite3.OperationalError:
        pass

    return results


def rerank_hits(
    hits: list[SearchHit],
    *,
    field_bonus: dict[str, float] | None = None,
) -> list[SearchHit]:
    """Переранжирует кандидатов с учетом raw_bm25 и field_bonus.

    Ключ сортировки:
    (trust_rank, -(score), hit.id)
    где score = max(-raw_bm25, 0.0) + sum(field_bonuses)
    """
    bonus_map = field_bonus or {}

    def sort_key(hit: SearchHit) -> tuple[int, float, int, str]:
        base_score = max(-hit.raw_bm25, 0.0)
        bonuses = sum(
            bonus_map.get(col, 0.0) for col, matched in hit.field_matches.items() if matched
        )
        total_score = base_score + bonuses
        return (hit.trust_rank, -round(total_score, 6), -hit.matched_columns, hit.id)

    return sorted(hits, key=sort_key)
