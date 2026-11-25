"""Ранжирование, скоринг и fuzzy-догон кандидатов поиска базы знаний (MCP Knowledge Engine)."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from typing import Any

from mcp_core.db import build_bm25_weights_sql
from mcp_core.fuzzy import FUZZY_CANDIDATE_LIMIT, FUZZY_THRESHOLD, jaccard_word_best
from mcp_core.rerank import SearchHit, matched_column_flags, rerank_hits
from mcp_core.sanitizer import extract_short_acronym_tokens
from mcp_core.text import short_acronym_matches
from mcp_knowledge.models import KnowledgeStatus

__all__ = [
    "ALIASES_PROBE_SQL",
    "FUZZY_SCAN_ROW_BUDGET",
    "KNOWLEDGE_FIELD_BONUS",
    "KNOWLEDGE_PROBE_WEIGHTS",
    "SYMPTOM_PROBE_SQL",
    "TITLE_PROBE_SQL",
    "_collect_fuzzy_knowledge_rows",
    "_collect_short_acronym_knowledge",
    "_fuzzy_scan_all_pages",
    "_rerank_knowledge_rows",
]

FUZZY_SCAN_ROW_BUDGET: int = 2000

# Векторы зондирования совпадения колонок строгой арности 20
KNOWLEDGE_PROBE_WEIGHTS: dict[str, tuple[float, ...]] = {
    "title": (
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
    ),
    "aliases": (
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
    ),
    "symptom": (
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
    ),
}
TITLE_PROBE_SQL = build_bm25_weights_sql(KNOWLEDGE_PROBE_WEIGHTS["title"], 20)
ALIASES_PROBE_SQL = build_bm25_weights_sql(KNOWLEDGE_PROBE_WEIGHTS["aliases"], 20)
SYMPTOM_PROBE_SQL = build_bm25_weights_sql(KNOWLEDGE_PROBE_WEIGHTS["symptom"], 20)

KNOWLEDGE_FIELD_BONUS: dict[str, float] = {
    "title": 1.0,
    "symptom": 0.5,
    "aliases": 0.5,
}


def _collect_short_acronym_knowledge(
    cursor: sqlite3.Cursor,
    short_tokens: set[str],
    extra_conditions: list[str],
    extra_params: list[Any],
    all_seen_ids: set[str],
    needed: int,
) -> list[tuple[Any, ...]]:
    """Точный fallback по 2-буквенным акронимам (акронимы <3 символов не индексируются trigram)."""
    short_where_parts = []
    if extra_conditions:
        short_where_parts.extend(extra_conditions)
    short_where = f"WHERE {' AND '.join(short_where_parts)}" if short_where_parts else ""
    s_sql = f"""
        SELECT
            id, title, category, type, status, source, confidence, task_id, symptom,
            stale_after, refutation_reason, body, generated_by, solution
        FROM knowledge_index
        {short_where}
        LIMIT ?;
    """  # noqa: S608
    try:
        cursor.execute(s_sql, [*extra_params, 100])
        s_candidates = cursor.fetchall()
    except sqlite3.OperationalError:
        return []

    collected: list[tuple[Any, ...]] = []
    for cr in s_candidates:
        cid = cr[0]
        if cid in all_seen_ids:
            continue
        doc_blob = f"{cr[0] or ''} {cr[1] or ''} {cr[8] or ''}"
        if short_acronym_matches(short_tokens, doc_blob):
            f_row = (
                cr[0],
                cr[1],
                cr[2],
                cr[3],
                cr[4],
                cr[5],
                cr[6],
                cr[7],
                cr[8],
                cr[9],
                cr[10],
                -1.0,
                cr[11],
                cr[12],
            )
            collected.append(f_row)
            all_seen_ids.add(cid)
            if len(collected) >= needed:
                break
    return collected


def _fuzzy_scan_all_pages(
    cursor: sqlite3.Cursor,
    score_rows: Callable[[list[tuple[Any, ...]]], None],
    extra_where: str,
    extra_params: list[Any],
) -> None:
    """Постраничный полный скан knowledge_index устойчивым порядком (ORDER BY rowid)."""
    page_size = FUZZY_CANDIDATE_LIMIT
    offset = 0
    while True:
        page_sql = f"""
            SELECT
                id, title, category, type, status, source, confidence, task_id, symptom,
                stale_after, refutation_reason, body, generated_by, solution
            FROM knowledge_index
            WHERE {extra_where}
            ORDER BY rowid
            LIMIT {page_size} OFFSET {offset};
        """  # noqa: S608
        try:
            cursor.execute(page_sql, extra_params)
            scan_rows = cursor.fetchall()
        except sqlite3.OperationalError:
            return
        if not scan_rows:
            return
        score_rows(scan_rows)
        if len(scan_rows) < page_size or offset + page_size >= FUZZY_SCAN_ROW_BUDGET:
            return
        offset += page_size


def _collect_fuzzy_knowledge_rows(
    cursor: sqlite3.Cursor,
    where_clause: str,
    params: list[Any],
    query: str,
    existing_rows: list[tuple[Any, ...]],
    limit: int,
) -> list[tuple[Any, ...]]:
    """Выполняет fuzzy-догон кандидатов при нехватке MATCH-хитов."""
    tokens_3 = [t for t in re.findall(r"[A-Za-zА-Яа-яЁё0-9_]+", query) if len(t) >= 3]
    short_tokens = extract_short_acronym_tokens(query)
    if len(existing_rows) >= limit or (not tokens_3 and not short_tokens):
        return []

    existing_ids = {r[0] for r in existing_rows}
    extra_conditions = where_clause.split(" AND ")[1:]
    extra_params = list(params[1:])
    extra_where = " AND ".join(extra_conditions) if extra_conditions else "1=1"

    fuzzy_cands: dict[str, tuple[tuple[Any, ...], float]] = {}

    def _score_rows(rows: list[tuple[Any, ...]]) -> None:
        for cr in rows:
            cid = cr[0]
            if cid in existing_ids:
                continue
            doc_text = f"{cr[1] or ''} {cr[8] or ''} {cr[13] or ''} {cr[11] or ''}"
            for tok in tokens_3:
                jaccard = jaccard_word_best(tok, doc_text)
                if jaccard >= FUZZY_THRESHOLD and (
                    cid not in fuzzy_cands or jaccard > fuzzy_cands[cid][1]
                ):
                    fuzzy_cands[cid] = (cr, jaccard)

    for tok in tokens_3:
        like_pattern = f"%{tok.lower()}%"
        like_conditions = [extra_where] if extra_where != "1=1" else []
        like_conditions.append(
            "(lower(title) LIKE ? OR lower(symptom) LIKE ? OR lower(solution) LIKE ? "
            "OR lower(body) LIKE ?)"
        )
        like_where = " AND ".join(like_conditions)
        like_sql = f"""
            SELECT
                id, title, category, type, status, source, confidence, task_id, symptom,
                stale_after, refutation_reason, body, generated_by, solution
            FROM knowledge_index
            WHERE {like_where}
            ORDER BY rowid
            LIMIT {FUZZY_CANDIDATE_LIMIT};
        """  # noqa: S608
        try:
            cursor.execute(
                like_sql, [*extra_params, like_pattern, like_pattern, like_pattern, like_pattern]
            )
            _score_rows(cursor.fetchall())
        except sqlite3.OperationalError:
            continue

    _fuzzy_scan_all_pages(cursor, _score_rows, extra_where, list(extra_params))

    needed = limit - len(existing_rows)
    sorted_cands = sorted(fuzzy_cands.values(), key=lambda item: (-item[1], item[0][0]))
    fuzzy_rows: list[tuple[Any, ...]] = []
    for cr, jaccard in sorted_cands[:needed]:
        f_row = (
            cr[0],
            cr[1],
            cr[2],
            cr[3],
            cr[4],
            cr[5],
            cr[6],
            cr[7],
            cr[8],
            cr[9],
            cr[10],
            -jaccard,
            cr[11],
            cr[12],
        )
        fuzzy_rows.append(f_row)

    remaining_needed = limit - len(existing_rows) - len(fuzzy_rows)
    if short_tokens and remaining_needed > 0:
        all_seen_ids = {r[0] for r in existing_rows} | {r[0] for r in fuzzy_rows}
        short_rows = _collect_short_acronym_knowledge(
            cursor, short_tokens, extra_conditions, extra_params, all_seen_ids, remaining_needed
        )
        fuzzy_rows.extend(short_rows)

    return fuzzy_rows


def _rerank_knowledge_rows(
    conn: sqlite3.Connection,
    rows: list[tuple[Any, ...]],
    fuzzy_rows: list[tuple[Any, ...]],
    sanitized_query: str,
) -> list[tuple[Any, ...]]:
    """Переранжирует MATCH и fuzzy хиты с учетом тиров доверия и field bonuses."""
    status_ranks = {
        KnowledgeStatus.VERIFIED.value: 1,
        KnowledgeStatus.ACTIVE.value: 2,
        KnowledgeStatus.SUSPECT.value: 3,
        KnowledgeStatus.HYPOTHESIS.value: 4,
    }

    hits: list[SearchHit] = []
    row_map: dict[str, tuple[Any, ...]] = {}

    for r in rows:
        row_map[r[0]] = r
        gen_by = r[13] if len(r) > 13 else None
        status = r[4] if len(r) > 4 else None
        human_tier = 1 if gen_by == "human" else 2
        status_tier = status_ranks.get(str(status) if status else "", 5)
        trust_rank = human_tier * 10 + status_tier

        if len(r) > 17:
            flags = {
                "title": bool(r[15]),
                "aliases": bool(r[16]),
                "symptom": bool(r[17]),
            }
        else:
            flags = matched_column_flags(
                conn,
                "knowledge_index",
                r[0],
                KNOWLEDGE_PROBE_WEIGHTS,
                sanitized_query,
            )
        matched_cols = sum(1 for v in flags.values() if v)
        raw_bm25 = float(r[11]) if r[11] is not None else 0.0
        hits.append(
            SearchHit(
                id=r[0],
                raw_bm25=raw_bm25,
                matched_columns=matched_cols,
                field_matches=flags,
                trust_rank=trust_rank,
            )
        )

    for r in fuzzy_rows:
        row_map[r[0]] = r
        gen_by = r[13] if len(r) > 13 else None
        status = r[4] if len(r) > 4 else None
        human_tier = 1 if gen_by == "human" else 2
        status_tier = status_ranks.get(str(status) if status else "", 5)
        trust_rank = 1000 + human_tier * 10 + status_tier

        raw_bm25 = float(r[11]) if r[11] is not None else 0.0
        hits.append(
            SearchHit(
                id=r[0],
                raw_bm25=raw_bm25,
                matched_columns=0,
                field_matches={},
                trust_rank=trust_rank,
            )
        )

    reranked_hits = rerank_hits(hits, field_bonus=KNOWLEDGE_FIELD_BONUS)
    return [row_map[h.id] for h in reranked_hits]
