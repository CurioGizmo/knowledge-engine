"""Хранилище SQLite FTS5 кэша для MCP Audit Engine."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mcp_audit.models import (
    AuditIssue,
    AuditStatus,
    KnownDebtCheckResponse,
    PlanArtifact,
    ReviewArtifact,
    TaskContextResponse,
)
from mcp_core.db import build_bm25_weights_sql
from mcp_core.db import get_connection as core_get_connection
from mcp_core.fts import SCHEMA_VERSION, fts5_table_sql, resolve_tokenizer, round_bm25_score
from mcp_core.sanitizer import (
    extract_short_acronym_tokens,
    normalize_category_slug,
    sanitize_fts_query,
)
from mcp_core.schema_guard import (
    FtsSpec,
    RebuildDependency,
    StorageSchemaSpec,
    ensure_storage_schema,
)
from mcp_core.sync import drop_with_move_protection

DEFAULT_LIMIT = 10

__all__ = ["AUDIT_BM25_WEIGHTS", "AuditStorage", "_rebuild_audit"]

# 15 весов для колонок audit_index:
# 0.id, 1.title, 2.symptom, 3.root_cause, 4.solution, 5.category, 6.status, 7.priority,
# 8.blocked_by, 9.target_file, 10.symbol, 11.task_id, 12.aliases, 13.created_at, 14.updated_at
AUDIT_BM25_WEIGHTS: tuple[float, ...] = (
    0.0,  # 0. id
    3.0,  # 1. title
    3.0,  # 2. symptom
    1.5,  # 3. root_cause
    1.0,  # 4. solution
    0.0,  # 5. category (UNINDEXED)
    0.0,  # 6. status (UNINDEXED)
    0.0,  # 7. priority (UNINDEXED)
    0.0,  # 8. blocked_by (UNINDEXED)
    2.5,  # 9. target_file
    2.0,  # 10. symbol
    0.0,  # 11. task_id (UNINDEXED)
    2.5,  # 12. aliases
    0.0,  # 13. created_at (UNINDEXED)
    0.0,  # 14. updated_at (UNINDEXED)
)

AUDIT_BM25_WEIGHTS_SQL = build_bm25_weights_sql(AUDIT_BM25_WEIGHTS, 15)

# 2 веса для artifacts_fts: 1.title, 2.body
ARTIFACTS_BM25_WEIGHTS: tuple[float, ...] = (
    2.0,  # title
    1.0,  # body
)
ARTIFACTS_BM25_WEIGHTS_SQL = build_bm25_weights_sql(ARTIFACTS_BM25_WEIGHTS, 2)

AUDIT_FTS_COLUMNS = [
    "id",
    "title",
    "symptom",
    "root_cause",
    "solution",
    "category",
    "status",
    "priority",
    "blocked_by",
    "target_file",
    "symbol",
    "task_id",
    "aliases",
    "created_at",
    "updated_at",
]

AUDIT_FTS_UNINDEXED = [
    "id",
    "category",
    "status",
    "priority",
    "blocked_by",
    "task_id",
    "created_at",
    "updated_at",
]

FILE_SYNC_AUDIT_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS file_sync (
        path TEXT PRIMARY KEY,
        mtime REAL,
        size INTEGER,
        card_id TEXT,
        location TEXT
    );
"""

ARTIFACTS_FILE_SYNC_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS artifacts_file_sync (
        path TEXT PRIMARY KEY,
        artifact_type TEXT NOT NULL,
        mtime_ns INTEGER NOT NULL,
        size INTEGER NOT NULL,
        artifact_id TEXT NOT NULL,
        location TEXT NOT NULL
    );
"""

ARTIFACTS_INDEX_CREATE_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS artifacts_index (
        artifact_id TEXT PRIMARY KEY,
        artifact_type TEXT NOT NULL,
        task_id TEXT,
        category TEXT NOT NULL,
        status TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        pr_url TEXT,
        file_path TEXT NOT NULL UNIQUE,
        content_version INTEGER NOT NULL DEFAULT 1,
        mtime_ns INTEGER NOT NULL,
        steps_total INTEGER DEFAULT 0,
        steps_done INTEGER DEFAULT 0,
        current_step_title TEXT,
        latest_verdict TEXT,
        unresolved_comments INTEGER DEFAULT 0,
        payload_json TEXT
    );""",
    "CREATE INDEX IF NOT EXISTS idx_art_task_type ON artifacts_index(task_id, artifact_type);",
    "CREATE INDEX IF NOT EXISTS idx_art_task_status ON artifacts_index(task_id, status);",
    "CREATE INDEX IF NOT EXISTS idx_art_pr ON artifacts_index(pr_url) WHERE pr_url IS NOT NULL;",
]

ART_TRIGGERS_DROP_SQL = [
    "DROP TRIGGER IF EXISTS art_ai;",
    "DROP TRIGGER IF EXISTS art_ad;",
    "DROP TRIGGER IF EXISTS art_au;",
]

ART_TRIGGERS_CREATE_SQL = [
    """CREATE TRIGGER art_ai AFTER INSERT ON artifacts_index BEGIN
           INSERT INTO artifacts_fts(rowid, title, body)
           VALUES(new.rowid, new.title, new.body);
       END;""",
    """CREATE TRIGGER art_ad AFTER DELETE ON artifacts_index BEGIN
           INSERT INTO artifacts_fts(artifacts_fts, rowid, title, body)
           VALUES('delete', old.rowid, old.title, old.body);
       END;""",
    """CREATE TRIGGER art_au AFTER UPDATE ON artifacts_index BEGIN
           INSERT INTO artifacts_fts(artifacts_fts, rowid, title, body)
           VALUES('delete', old.rowid, old.title, old.body);
           INSERT INTO artifacts_fts(rowid, title, body)
           VALUES(new.rowid, new.title, new.body);
       END;""",
]

# Сообщение об отсутствии совпадений в check_known_debt (общее для обеих веток).
_NO_MATCH_MESSAGE = "Совпадений в реестре технического долга не обнаружено."

# R6: служебные/предлоги, не несущие смысла симптома (для порога значимого покрытия).
_INSIGNIFICANT_WORDS = frozenset(
    {
        "при",
        "как",
        "что",
        "это",
        "для",
        "или",
        "но",
        "не",
        "на",
        "по",
        "из",
        "от",
        "до",
        "за",
        "чем",
        "так",
        "там",
        "все",
        "его",
        "она",
        "они",
        "под",
        "the",
        "and",
        "for",
        "with",
        "this",
        "that",
        "from",
        "into",
        "when",
        "then",
    }
)


_RU_ADJ_ENDINGS = (
    "иями",
    "ыми",
    "ями",
    "ами",
    "ого",
    "его",
    "ому",
    "ему",
    "ых",
    "их",
    "ую",
    "юю",
    "ое",
    "ее",
    "ые",
    "ие",
    "ом",
    "ем",
    "ым",
    "им",
    "ой",
    "ей",
    "ий",
    "ый",
    "ая",
    "яя",
)
_RU_NOUN_ENDINGS = ("ах", "ям", "ях", "ом", "ем", "ам", "а", "я", "у", "ю", "е", "ы", "и")
_RU_PARTICIPLE_SUFFIXES = ("ующ", "ющ", "ащ", "ящ", "вш", "ован", "еван")


def _morpho_normalize(token: str) -> str:
    """5бис-2: морфо-нормализация токена для overlap-сравнения.

    Русские флексии: лёгкий стемминг (stem_russian) — «зависание» и
    «зависания» дают общую основу; снятие возвратных постфиксов (-ся/-сь:
    «дублируются»~«дублирует»), причастных/глагольных суффиксов и окончаний
    прилагательных/существительных; финальное ы/и сводятся к «и».
    Английские plural (A15): -es → - (processes → process), -s после
    согласной → - (errors → error); ss/us не трогаем (process, status).
    """
    from mcp_core.sanitizer import stem_russian

    t = token
    # Снятие возвратных постфиксов -ся/-сь (дублируются -> дублируют)
    if len(t) > 5 and (t.endswith("ся") or t.endswith("сь")):
        t = t[:-2]

    stem = stem_russian(t)
    if stem is None:
        stem = t
        for adj in _RU_ADJ_ENDINGS:
            if stem.endswith(adj) and len(stem) - len(adj) >= 3:
                stem = stem[: -len(adj)]
                break
        else:
            for noun in _RU_NOUN_ENDINGS:
                if stem.endswith(noun) and len(stem) - len(noun) >= 4:
                    stem = stem[: -len(noun)]
                    break

    for part_suf in _RU_PARTICIPLE_SUFFIXES:
        if stem.endswith(part_suf) and len(stem) - len(part_suf) >= 4:
            stem = stem[: -len(part_suf)]
            break

    if stem.endswith("иру") and len(stem) > 4:
        stem = stem[:-1]  # дублиру -> дублир

    if stem.endswith("es") and len(stem) > 4:
        stem = stem[:-2]
    if stem.endswith("s") and len(stem) > 3 and not stem.endswith(("ss", "us", "is")):
        stem = stem[:-1]
    if stem.endswith("ы") or stem.endswith("и"):
        stem = stem[:-1] + "и"
    return stem


def _significant_tokens(text: str) -> set[str]:
    """Значимые токены текста симптома: длина >= 4, не служебные, лоуэркейс.

    Дефисные термины разбиваются на под-токены (user-auth → user, auth).
    """
    raw = re.findall(r"[а-яёa-z0-9-]+", text.lower())
    result: set[str] = set()
    for tok in raw:
        if "-" in tok:
            result.update(part for part in tok.split("-") if len(part) >= 4)
        elif len(tok) >= 4 and tok not in _INSIGNIFICANT_WORDS:
            result.add(tok)
    return result


def _rebuild_audit(c: sqlite3.Connection) -> None:
    """Пакетный drift-rebuild аудита (module-level callback для StorageSchemaSpec).

    Выполняется ТОЛЬКО внутри guard-транзакции (BEGIN IMMEDIATE): DROP-ы
    устаревших объектов, FTS-DDL, служебные таблицы, триггеры и полная
    реиндексация artifacts_fts. Атомарность обеспечена guard'ом — здесь
    без транзакционных примитивов и без скриптового API курсора.
    artifacts_index НЕ дропается (CREATE IF NOT EXISTS): строки артефактов
    переживают rebuild, artifacts_fts репопулируется из content-таблицы.
    """
    cur = c.cursor()
    cur.execute("DROP TABLE IF EXISTS audit_index;")
    cur.execute("DROP TABLE IF EXISTS file_sync;")
    for drop_sql in ART_TRIGGERS_DROP_SQL:
        cur.execute(drop_sql)
    # wrong-kind артефакты (имя триггера, но type='table') тоже сносим.
    for name in ("art_ai", "art_ad", "art_au"):
        cur.execute(f"DROP TABLE IF EXISTS {name};")
    cur.execute("DROP TABLE IF EXISTS artifacts_fts;")

    cur.execute(
        fts5_table_sql(
            "audit_index",
            AUDIT_FTS_COLUMNS,
            unindexed=AUDIT_FTS_UNINDEXED,
            tokenizer=resolve_tokenizer(),
        )
    )
    cur.execute(FILE_SYNC_AUDIT_CREATE_SQL)
    cur.execute(ARTIFACTS_FILE_SYNC_CREATE_SQL)
    for stmt in ARTIFACTS_INDEX_CREATE_STATEMENTS:
        cur.execute(stmt)

    cur.execute(
        fts5_table_sql(
            "artifacts_fts",
            ["title", "body"],
            content="artifacts_index",
            content_rowid="rowid",
            tokenizer=resolve_tokenizer(),
        )
    )
    for create_sql in ART_TRIGGERS_CREATE_SQL:
        cur.execute(create_sql)
    cur.execute("INSERT INTO artifacts_fts(artifacts_fts) VALUES('rebuild');")
    cur.execute("UPDATE artifacts_file_sync SET mtime_ns = -1;")


# Спецификация схемы аудита для ensure_storage_schema (mcp_core.schema_guard).
# fts_checks — ordered-контракт: порядок колонок позиционно важен для bm25-весов.
AUDIT_STORAGE_SCHEMA_SPEC = StorageSchemaSpec(
    schema_version=SCHEMA_VERSION,
    fts_checks={
        "audit_index": FtsSpec(expected_columns=tuple(AUDIT_FTS_COLUMNS)),
        "artifacts_fts": FtsSpec(expected_columns=("title", "body")),
    },
    service_ddl=[
        FILE_SYNC_AUDIT_CREATE_SQL,
        ARTIFACTS_FILE_SYNC_CREATE_SQL,
        *ARTIFACTS_INDEX_CREATE_STATEMENTS,
    ],
    rebuild=_rebuild_audit,
    rebuild_on_missing=frozenset(
        {
            RebuildDependency("artifacts_index", "table"),
            RebuildDependency("art_ai", "trigger"),
            RebuildDependency("art_ad", "trigger"),
            RebuildDependency("art_au", "trigger"),
        }
    ),
)


class AuditStorage:
    """Управление эфемерным SQLite FTS5 кэшем для задач аудита и техдолга."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection]:
        """Контекстный менеджер подключения к SQLite."""
        conn = core_get_connection(self.db_path, timeout=5.0)
        try:
            yield conn
        finally:
            conn.close()

    def close(self) -> None:
        """Закрывает ресурсы хранилища (no-op, соединения закрываются в get_connection)."""

    def _init_db(self) -> None:
        """Инициализирует схему через mcp_core ensure_storage_schema (guard).

        Единственный guard-вызов: drift-check, атомарный rebuild при drift,
        безусловное service-самолечение — всё внутри контракта guard'а.
        """
        with self.get_connection() as conn:
            ensure_storage_schema(conn, AUDIT_STORAGE_SCHEMA_SPEC)

    @staticmethod
    def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
        """Собирает dict-представление карточки из строки audit_index (status/priority — строки)."""
        res = {
            "id": row[0],
            "title": row[1],
            "symptom": row[2],
            "root_cause": row[3],
            "solution": row[4],
            "category": row[5],
            "status": row[6],
            "priority": row[7],
            "blocked_by": json.loads(row[8]) if row[8] else [],
            "target_file": row[9],
            "symbol": row[10],
        }
        if len(row) > 11 and row[11] is not None:
            res["task_id"] = row[11]
        if len(row) > 12 and row[12] is not None:
            aliases_val = row[12]
            res["aliases"] = (
                aliases_val.split() if isinstance(aliases_val, str) else list(aliases_val)
            )
        if len(row) > 13 and row[13] is not None:
            res["created_at"] = row[13]
        if len(row) > 14 and row[14] is not None:
            res["updated_at"] = row[14]
        if len(row) > 15 and row[15] is not None:
            val = -float(row[15])
            res["score"] = round_bm25_score(val)
        return res

    @staticmethod
    def _row_to_issue(row: tuple[Any, ...]) -> AuditIssue:
        """Собирает AuditIssue из строки таблицы audit_index."""
        return AuditIssue(**AuditStorage._row_to_dict(row))

    def get_all_ids(self) -> set[str]:
        """Возвращает все зарегистрированные идентификаторы задач."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM audit_index;")
            return {row[0] for row in cursor.fetchall()}

    def get_issue(self, issue_id: str) -> AuditIssue | None:
        """Получает карточку аудита по её ID."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, title, symptom, root_cause, solution, category, status, priority, blocked_by, target_file, symbol, task_id, aliases, created_at, updated_at
                FROM audit_index
                WHERE id = ?
                LIMIT 1;
                """,
                (issue_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None

            return self._row_to_issue(row)

    def list_all_issues(
        self,
        category: str | None = None,
        status: str | None = None,
    ) -> list[AuditIssue]:
        """Возвращает список всех задач с фильтрацией по категории и/или статусу."""
        query = "SELECT id, title, symptom, root_cause, solution, category, status, priority, blocked_by, target_file, symbol, task_id, aliases, created_at, updated_at FROM audit_index WHERE 1=1"
        params: list[Any] = []

        if category:
            norm_cat = normalize_category_slug(category)
            query += " AND category = ?"
            params.append(norm_cat)
        if status:
            query += " AND status = ?"
            params.append(status)

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return [self._row_to_issue(row) for row in cursor.fetchall()]

    def search_issues(
        self,
        query: str = "",
        category: str | None = None,
        status: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Полнотекстовый поиск по бэклогу с фильтрацией по категории и ранжированием BM25.

        R6 (2026-10-19): при пустом MATCH-результате и наличии 2-символьных
        акронимов в запросе включается точный канал коротких терминов —
        Python-токенизация карточек с точным пересечением множеств
        (как _short_acronym_fallback в knowledge-поиске), без substring-FP.
        """
        safe_q = sanitize_fts_query(query) if query and query.strip() else ""

        sql_parts = [
            f"SELECT id, title, symptom, root_cause, solution, category, status, priority, blocked_by, target_file, symbol, task_id, aliases, created_at, updated_at, bm25(audit_index, {AUDIT_BM25_WEIGHTS_SQL}) AS rank",
            "FROM audit_index",
        ]
        where_clauses: list[str] = []
        params: list[Any] = []

        if safe_q:
            where_clauses.append("audit_index MATCH ?")
            params.append(safe_q)
        if category:
            norm_cat = normalize_category_slug(category)
            where_clauses.append("category = ?")
            params.append(norm_cat)
        if status:
            where_clauses.append("status = ?")
            params.append(status)

        if where_clauses:
            sql_parts.append("WHERE " + " AND ".join(where_clauses))

        if safe_q:
            sql_parts.append("ORDER BY rank")
        else:
            sql_parts.append("ORDER BY id DESC")

        effective_limit = max(1, min(limit, 100)) if limit > 0 else DEFAULT_LIMIT
        sql_parts.append("LIMIT ?;")
        params.append(effective_limit)

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("\n".join(sql_parts), params)
            rows = cursor.fetchall()

            # R6: точный канал коротких терминов — только если MATCH ничего не нашёл
            if not rows:
                short_rows = self._short_acronym_search(
                    cursor, query, category, status, effective_limit
                )
                return short_rows

            return [self._row_to_dict(row) for row in rows]

    def _short_acronym_search(
        self,
        cursor: sqlite3.Cursor,
        query: str,
        category: str | None,
        status: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Точный fallback по 2-символьным акронимам (trigram не индексирует токены <3).

        Токенизирует id/title/symptom карточек и ищет ТОЧНОЕ пересечение с
        короткими терминами запроса (включая под-токены через - и _).
        Никаких подстрочных LIKE — только совпадение целых токенов.
        """
        short_tokens = extract_short_acronym_tokens(query)
        if not short_tokens:
            return []

        where_parts: list[str] = []
        params: list[Any] = []
        if category:
            where_parts.append("category = ?")
            params.append(normalize_category_slug(category))
        if status:
            where_parts.append("status = ?")
            params.append(status)
        where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

        sql = (
            "SELECT id, title, symptom, root_cause, solution, category, status, priority, "
            f"blocked_by, target_file, symbol, task_id, aliases, created_at, updated_at FROM audit_index {where_sql} LIMIT 200;"
        )
        try:
            cursor.execute(sql, params)
            candidates = cursor.fetchall()
        except sqlite3.OperationalError:
            return []

        collected: list[dict[str, Any]] = []
        for cr in candidates:
            doc_blob = f"{cr[0] or ''} {cr[1] or ''} {cr[2] or ''}"
            doc_tokens = {tok.lower() for tok in re.findall(r"[A-Za-zА-Яа-я0-9_-]+", doc_blob)}
            split_subtokens: set[str] = set()
            for tok in doc_tokens:
                if "-" in tok or "_" in tok:
                    split_subtokens.update(re.split(r"[-_]+", tok))
            if short_tokens & (doc_tokens | split_subtokens):
                collected.append(self._row_to_dict(cr))
                if len(collected) >= limit:
                    break
        return collected

    @staticmethod
    def _matches_file_scope(card_file: str, file_path: str) -> bool:
        """Сопоставляет target_file карточки с запрашиваемым файлом.

        2-компонентное суффиксное сравнение с fallback на имя файла при 1 компоненте.
        """
        card_p = Path(card_file)
        query_p = Path(file_path)
        if len(card_p.parts) >= 2 and len(query_p.parts) >= 2:
            return card_p.parts[-2:] == query_p.parts[-2:]
        return card_p.name == query_p.name

    def check_known_debt(
        self,
        symptom: str,
        file_path: str | None = None,
        symbol: str | None = None,
        category: str | None = None,
    ) -> KnownDebtCheckResponse:
        """Проверяет симптом на наличие известного техдолга для code-review."""
        tokens = [symptom]
        if symbol:
            tokens.append(symbol)
        if file_path:
            tokens.append(Path(file_path).stem)

        query_str = " ".join(t for t in tokens if t)
        if not query_str.strip():
            return KnownDebtCheckResponse(
                found=False,
                message=_NO_MATCH_MESSAGE,
            )
        matches = self.search_issues(query=query_str, category=category, limit=5)
        if not matches:
            return KnownDebtCheckResponse(
                found=False,
                message=_NO_MATCH_MESSAGE,
            )

        # R6 (2026-10-19): порог значимого покрытия. Первый BM25-хит по общим
        # словам («при», «повторном», file stem) ложно подхватывал чужие
        # карточки (12/22 ошибок exp=new в отчёте измерений). Кандидат
        # валиден только если >= 0.6 значимых токенов симптома (длина >= 4,
        # не стоп-слова) найдены в его тексте. Порог 0.6 подобран на корпусе
        # known-debt-cases (0.4→24/36, 0.5→25/36, 0.6→27/36, 0.7→26/36).
        # При равном покрытии предпочтение отдаётся completed-карточке:
        # регрессия важнее known.
        # Значимые токены симптома (длина >= 4, не стоп-слова).
        # Вычисляются из симптома (а не всего query_str со symbol/file_path),
        # чтобы переданные file_path/symbol не раздували знаменатель overlap
        # для дефектов, формулируемых естественным языком.
        significant = _significant_tokens(symptom) or _significant_tokens(query_str)
        min_overlap = 0.6 if significant else 0.0

        # File Scope Matching + порог покрытия; выбор лучшего кандидата
        best = self._select_known_debt_candidate(matches, significant, min_overlap, file_path)

        # R6: различаем причину пустого результата — file-scope (контракт
        # «привязаны к другим файлам») vs порог значимого покрытия.
        if best is None:
            file_scope_rejected = any(
                card_file
                and file_path
                and not self._matches_file_scope(card_file, file_path)
                and self._candidate_meets_overlap(candidate, significant, min_overlap)
                for candidate in matches
                if (card_file := candidate.get("target_file"))
            )
            message = (
                "Похожие симптомы найдены, но привязаны к другим файлам (совпадение отклонено)."
                if file_scope_rejected
                else _NO_MATCH_MESSAGE
            )
            return KnownDebtCheckResponse(
                found=False,
                message=message,
            )

        candidate, card_file = best

        # Формируем ответ по выбранному кандидату
        status_val = candidate.get("status", "planned")
        is_completed = status_val == AuditStatus.COMPLETED.value
        is_open = status_val in (AuditStatus.PLANNED.value, AuditStatus.IN_PROGRESS.value)
        is_obsolete = status_val == AuditStatus.OBSOLETE.value

        if is_obsolete:
            msg = f"Проблема ранее признана неактуальной (obsolete) в [{candidate['id']}]."
        elif is_open:
            msg = f"Зафиксирован как известный техдолг [{candidate['id']}] со статусом '{status_val}'."
        else:
            msg = f"⚠️ ВНИМАНИЕ: Регрессия! Проблема была ранее решена в [{candidate['id']}]."

        return KnownDebtCheckResponse(
            found=True,
            id=candidate["id"],
            title=candidate["title"],
            status=status_val,
            is_known_debt=is_open,
            is_regression=is_completed,
            target_file=card_file,
            symbol=candidate.get("symbol"),
            message=msg,
            details=candidate,
        )

    def _select_known_debt_candidate(
        self,
        matches: list[dict[str, Any]],
        significant: set[str],
        min_overlap: float,
        file_path: str | None,
    ) -> tuple[dict[str, Any], str | None] | None:
        """Выбирает лучший кандидат known-debt по file-scope и порогу покрытия.

        R6: file-scope фильтр + значимое покрытие (>= min_overlap с симметричным
        разбиением составных терминов) + приоритет completed-карточки при
        близком покрытии (регрессия важнее known).
        """
        valid: list[tuple[float, dict[str, Any], str | None]] = []
        for candidate in matches:
            card_file = candidate.get("target_file")

            if card_file and file_path and not self._matches_file_scope(card_file, file_path):
                continue

            overlap = self._candidate_overlap(candidate, significant)
            if overlap is None or overlap < min_overlap:
                continue
            valid.append((overlap, candidate, card_file))

        if not valid:
            return None

        def _priority(item: tuple[float, dict[str, Any], str | None]) -> tuple[float, int]:
            overlap, candidate, _ = item
            is_completed = candidate.get("status") == AuditStatus.COMPLETED.value
            return (overlap, 1 if is_completed else 0)

        _overlap, candidate, card_file = max(valid, key=_priority)
        return candidate, card_file

    @staticmethod
    def _candidate_overlap(candidate: dict[str, Any], significant: set[str]) -> float | None:
        """Return significant-token coverage for one known-debt candidate."""
        if not significant:
            return 0.0
        card_text = " ".join(
            str(candidate.get(field) or "")
            for field in ("title", "symptom", "root_cause", "solution", "symbol")
        ).lower()
        card_tokens = set(re.findall(r"[а-яёa-z0-9_-]+", card_text))
        # Составные термины карточки (anti-spam, record_debt) разбиваем
        # на под-токены — симметрично разбиению _significant_tokens.
        split_subtokens: set[str] = set()
        for token in card_tokens:
            if "-" in token or "_" in token:
                split_subtokens.update(part for part in re.split(r"[-_]+", token))
        card_tokens |= split_subtokens
        if not card_tokens:
            return None
        # 5бис-2: морфо-нормализация обеих сторон — русские флексии
        # («зависание» в симптоме ~ «зависания» в карточке) и англ. plural
        # (A15: «errors» ~ «error») дают общую основу.
        sig_norm = {_morpho_normalize(t) for t in significant}
        card_norm = {_morpho_normalize(t) for t in card_tokens}
        return len(sig_norm & card_norm) / len(sig_norm)

    @classmethod
    def _candidate_meets_overlap(
        cls,
        candidate: dict[str, Any],
        significant: set[str],
        min_overlap: float,
    ) -> bool:
        overlap = cls._candidate_overlap(candidate, significant)
        return overlap is not None and overlap >= min_overlap

    def index_card(
        self,
        issue: AuditIssue,
        file_path: str,
        mtime: float,
        size: int,
        location: str,
    ) -> None:
        """Индексирует карточку в audit_index и обновляет её запись в file_sync (одна транзакция)."""
        blocked_by_json = json.dumps(issue.blocked_by)
        aliases_str = " ".join(issue.aliases) if issue.aliases else ""

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM audit_index WHERE id = ?;", (issue.id,))
            cursor.execute(
                """
                INSERT INTO audit_index (
                    id, title, symptom, root_cause, solution, category, status, priority, blocked_by, target_file, symbol, task_id, aliases, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    issue.id,
                    issue.title,
                    issue.symptom,
                    issue.root_cause,
                    issue.solution,
                    issue.category,
                    issue.status.value,
                    issue.priority.value,
                    blocked_by_json,
                    issue.target_file,
                    issue.symbol,
                    issue.task_id,
                    aliases_str,
                    issue.created_at,
                    issue.updated_at,
                ),
            )
            cursor.execute(
                """
                INSERT INTO file_sync (path, mtime, size, card_id, location)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    mtime = excluded.mtime,
                    size = excluded.size,
                    card_id = excluded.card_id,
                    location = excluded.location;
                """,
                (file_path, mtime, size, issue.id, location),
            )
            conn.commit()

    def get_sync_state(self) -> dict[str, tuple[float, int, str]]:
        """Возвращает снимок состояния file_sync: путь -> (mtime, size, card_id)."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT path, mtime, size, card_id FROM file_sync;")
            return {row[0]: (row[1], row[2], row[3]) for row in cursor.fetchall()}

    def drop_path(self, path: str, card_id: str | None = None) -> None:
        """Удаляет путь из file_sync; при переданном card_id — также карточку из audit_index."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            drop_with_move_protection(
                cursor,
                path,
                sync_table="file_sync",
                index_table="audit_index",
                id_column="id",
                card_id=card_id,
            )
            conn.commit()

    def find_card_path(self, card_id: str) -> str | None:
        """Возвращает путь файла карточки из file_sync по её ID (или None)."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT path FROM file_sync WHERE card_id = ? LIMIT 1;", (card_id,))
            row = cursor.fetchone()
            return row[0] if row else None

    # =========================================================================
    # Методы жизненного цикла планов и ревью (artifacts_index / artifacts_file_sync)
    # =========================================================================

    def index_plan(
        self,
        plan: PlanArtifact,
        file_path: str,
        mtime_ns: int,
        size: int,
        location: str,
    ) -> None:
        """Индексирует план в artifacts_index и обновляет запись в artifacts_file_sync."""
        steps_total = len(plan.steps)
        steps_done = sum(1 for s in plan.steps if s.completed)
        current_step_title = None
        for s in plan.steps:
            if not s.completed:
                current_step_title = s.title
                break

        body_parts = []
        if plan.task_id:
            body_parts.append(plan.task_id)
        body_parts.append(plan.content)
        for s in plan.steps:
            body_parts.append(s.title)
            if s.note:
                body_parts.append(s.note)
        body = "\n".join(body_parts)
        payload_json = plan.model_dump_json()

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM artifacts_index WHERE file_path = ? AND artifact_id != ?;",
                (file_path, plan.id),
            )
            cursor.execute(
                """
                INSERT INTO artifacts_index (
                    artifact_id, artifact_type, task_id, category, status, title, body,
                    pr_url, file_path, content_version, mtime_ns,
                    steps_total, steps_done, current_step_title, latest_verdict,
                    unresolved_comments, payload_json
                ) VALUES (?, 'plan', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?)
                ON CONFLICT(artifact_id) DO UPDATE SET
                    artifact_type = 'plan',
                    task_id = excluded.task_id,
                    category = excluded.category,
                    status = excluded.status,
                    title = excluded.title,
                    body = excluded.body,
                    pr_url = excluded.pr_url,
                    file_path = excluded.file_path,
                    content_version = artifacts_index.content_version + 1,
                    mtime_ns = excluded.mtime_ns,
                    steps_total = excluded.steps_total,
                    steps_done = excluded.steps_done,
                    current_step_title = excluded.current_step_title,
                    payload_json = excluded.payload_json;
                """,
                (
                    plan.id,
                    plan.task_id,
                    plan.category,
                    plan.status.value,
                    plan.title,
                    body,
                    plan.pr_url,
                    file_path,
                    plan.schema_version,
                    mtime_ns,
                    steps_total,
                    steps_done,
                    current_step_title,
                    payload_json,
                ),
            )
            cursor.execute(
                """
                INSERT INTO artifacts_file_sync (path, artifact_type, mtime_ns, size, artifact_id, location)
                VALUES (?, 'plan', ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    artifact_type = excluded.artifact_type,
                    mtime_ns = excluded.mtime_ns,
                    size = excluded.size,
                    artifact_id = excluded.artifact_id,
                    location = excluded.location;
                """,
                (file_path, mtime_ns, size, plan.id, location),
            )
            conn.commit()

    def index_review(
        self,
        review: ReviewArtifact,
        file_path: str,
        mtime_ns: int,
        size: int,
        location: str,
    ) -> None:
        """Индексирует ревью в artifacts_index и обновляет запись в artifacts_file_sync."""
        unresolved = sum(1 for c in review.comments if c.status == "open")
        body_parts = []
        if review.task_id:
            body_parts.append(review.task_id)
        body_parts.append(review.summary)
        for c in review.comments:
            body_parts.append(f"{c.file_path}: {c.symptom}")
        body = "\n".join(body_parts)
        payload_json = review.model_dump_json()

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM artifacts_index WHERE file_path = ? AND artifact_id != ?;",
                (file_path, review.id),
            )
            cursor.execute(
                """
                INSERT INTO artifacts_index (
                    artifact_id, artifact_type, task_id, category, status, title, body,
                    pr_url, file_path, content_version, mtime_ns,
                    steps_total, steps_done, current_step_title, latest_verdict,
                    unresolved_comments, payload_json
                ) VALUES (?, 'review', ?, ?, ?, ?, ?, ?, ?, 1, ?, 0, 0, NULL, ?, ?, ?)
                ON CONFLICT(artifact_id) DO UPDATE SET
                    artifact_type = 'review',
                    task_id = excluded.task_id,
                    category = excluded.category,
                    status = excluded.status,
                    title = excluded.title,
                    body = excluded.body,
                    pr_url = excluded.pr_url,
                    file_path = excluded.file_path,
                    content_version = artifacts_index.content_version + 1,
                    mtime_ns = excluded.mtime_ns,
                    latest_verdict = excluded.latest_verdict,
                    unresolved_comments = excluded.unresolved_comments,
                    payload_json = excluded.payload_json;
                """,
                (
                    review.id,
                    review.task_id,
                    review.category,
                    review.verdict.value,
                    f"Review {review.id}",
                    body,
                    review.pr_url,
                    file_path,
                    mtime_ns,
                    review.verdict.value,
                    unresolved,
                    payload_json,
                ),
            )
            cursor.execute(
                """
                INSERT INTO artifacts_file_sync (path, artifact_type, mtime_ns, size, artifact_id, location)
                VALUES (?, 'review', ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    artifact_type = excluded.artifact_type,
                    mtime_ns = excluded.mtime_ns,
                    size = excluded.size,
                    artifact_id = excluded.artifact_id,
                    location = excluded.location;
                """,
                (file_path, mtime_ns, size, review.id, location),
            )
            conn.commit()

    def get_artifacts_sync_state(self) -> dict[str, tuple[int, int, str, str]]:
        """Возвращает снимок artifacts_file_sync: path -> (mtime_ns, size, artifact_id, artifact_type)."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT path, mtime_ns, size, artifact_id, artifact_type FROM artifacts_file_sync;"
            )
            return {row[0]: (row[1], row[2], row[3], row[4]) for row in cursor.fetchall()}

    def drop_artifact_path(self, path: str, artifact_id: str | None = None) -> None:
        """Удаляет путь из artifacts_file_sync; при переданном artifact_id — запись из artifacts_index."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            drop_with_move_protection(
                cursor,
                path,
                sync_table="artifacts_file_sync",
                index_table="artifacts_index",
                id_column="artifact_id",
                card_id=artifact_id,
            )
            conn.commit()

    def find_artifact_path(self, artifact_id: str) -> str | None:
        """Возвращает путь к файлу артефакта из artifacts_file_sync по его ID (или None)."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT path FROM artifacts_file_sync WHERE artifact_id = ? LIMIT 1;",
                (artifact_id,),
            )
            row = cursor.fetchone()
            return row[0] if row else None

    def find_plans_by_task_id(self, task_id: str) -> list[tuple[str, str]]:
        """Возвращает ВСЕ (artifact_id, file_path) планов task_id из индекса.

        A13: без LIMIT 1 — произвольный выбор первого хита скрывал
        неоднозначность (несколько планов одного task_id); решение
        «1 / много / категория» принимает вызывающая сторона.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT artifact_id, file_path FROM artifacts_index
                WHERE task_id = ? AND artifact_type = 'plan'
                ORDER BY mtime_ns DESC;
                """,
                (task_id,),
            )
            return [(row[0], row[1]) for row in cursor.fetchall()]

    def _find_related_debt_rows(
        self,
        cursor: sqlite3.Cursor,
        plan_row: tuple[Any, ...] | None,
        eff_task_id: str | None,
    ) -> list[tuple[Any, ...]]:
        """A5: долги по task_id, id карточки и related_debts плана."""
        related_debt_ids: list[str] = []
        if plan_row and plan_row[10]:
            try:
                plan_payload = json.loads(plan_row[10])
                raw_related = plan_payload.get("related_debts") or []
                if isinstance(raw_related, str):
                    raw_related = [raw_related]
                related_debt_ids = [str(d) for d in raw_related]
            except (ValueError, TypeError):
                related_debt_ids = []
        if not (eff_task_id or related_debt_ids):
            return []
        where_parts = []
        params: list[Any] = []
        if eff_task_id:
            where_parts.append("(task_id = ? OR id = ?)")
            params.extend([eff_task_id, eff_task_id])
        for debt_id in related_debt_ids:
            where_parts.append("id = ?")
            params.append(debt_id)
        cursor.execute(
            f"""
            SELECT id, title, symptom, root_cause, solution, category, status, priority, blocked_by, target_file, symbol, task_id, aliases, created_at, updated_at
            FROM audit_index
            WHERE {" OR ".join(where_parts)};
            """,
            params,
        )
        return cursor.fetchall()

    def _find_plan_row(
        self,
        cursor: sqlite3.Cursor,
        task_id: str | None,
        pr_url: str | None,
    ) -> tuple[Any, ...] | None:
        """Поиск строки плана: сначала по task_id, затем (fallback) по pr_url."""
        plan_select = """
            SELECT artifact_id, task_id, category, status, title, pr_url, file_path,
                   steps_total, steps_done, current_step_title, payload_json
            FROM artifacts_index
        """
        if task_id:
            cursor.execute(
                plan_select + " WHERE task_id = ? AND artifact_type = 'plan' "
                "ORDER BY mtime_ns DESC LIMIT 1;",
                (task_id,),
            )
            plan_row = cursor.fetchone()
            if plan_row:
                return plan_row
        if pr_url:
            cursor.execute(
                plan_select + " WHERE pr_url = ? AND artifact_type = 'plan' "
                "ORDER BY mtime_ns DESC LIMIT 1;",
                (pr_url,),
            )
            return cursor.fetchone()
        return None

    def get_task_context(
        self,
        task_id: str | None = None,
        pr_url: str | None = None,
        view: str = "summary",
    ) -> TaskContextResponse:
        """Собирает агрегированный контекст задачи: план, ревью и связанные долги."""
        if not task_id and not pr_url:
            raise ValueError("Необходимо указать хотя бы task_id или pr_url")

        with self.get_connection() as conn:
            cursor = conn.cursor()

            # 1. Поиск плана
            plan_row = self._find_plan_row(cursor, task_id, pr_url)

            # Вычисляем эффективный task_id и pr_url
            eff_task_id = task_id or (plan_row[1] if plan_row else None)
            eff_pr_url = pr_url or (plan_row[5] if plan_row else None)

            # 2. Поиск связанных ревью
            rev_params: list[Any] = []
            rev_where = ["artifact_type = 'review'"]
            if eff_task_id and eff_pr_url:
                rev_where.append("(task_id = ? OR pr_url = ?)")
                rev_params.extend([eff_task_id, eff_pr_url])
            elif eff_task_id:
                rev_where.append("task_id = ?")
                rev_params.append(eff_task_id)
            elif eff_pr_url:
                rev_where.append("pr_url = ?")
                rev_params.append(eff_pr_url)

            cursor.execute(
                f"""
                SELECT artifact_id, task_id, category, status, title, pr_url, file_path,
                       latest_verdict, unresolved_comments, payload_json
                FROM artifacts_index
                WHERE {" AND ".join(rev_where)}
                ORDER BY mtime_ns ASC;
                """,
                rev_params,
            )
            review_rows = cursor.fetchall()

            # 3. Поиск связанных долгов (audit_index)
            debt_rows = self._find_related_debt_rows(cursor, plan_row, eff_task_id)

            # Собираем ответ
            if not plan_row and not review_rows and not debt_rows:
                return TaskContextResponse(
                    found=False,
                    task_id=task_id,
                    pr_url=pr_url,
                    status="unknown",
                    plan=None,
                    reviews=[],
                    debts=[],
                )

            # Формируем структуру плана
            plan_data = None
            plan_status = "unknown"
            if plan_row:
                plan_status = plan_row[3]
                if view == "full" and plan_row[10]:
                    plan_data = json.loads(plan_row[10])
                else:
                    plan_data = {
                        "id": plan_row[0],
                        "task_id": plan_row[1],
                        "title": plan_row[4],
                        "category": plan_row[2],
                        "status": plan_row[3],
                        "pr_url": plan_row[5],
                        "file_path": plan_row[6],
                        "steps_total": plan_row[7],
                        "steps_done": plan_row[8],
                        "current_step": plan_row[9],
                    }

            # Формируем структуру ревью
            reviews_data: list[dict[str, Any]] = []
            for r in review_rows:
                if view == "full" and r[9]:
                    reviews_data.append(json.loads(r[9]))
                else:
                    reviews_data.append(
                        {
                            "id": r[0],
                            "task_id": r[1],
                            "category": r[2],
                            "verdict": r[7],
                            "unresolved_comments": r[8],
                            "pr_url": r[5],
                            "file_path": r[6],
                        }
                    )

            # Формируем структуру долгов
            debts_data = [self._row_to_dict(d) for d in debt_rows]

            # Вычисляем общий статус задачи
            valid_statuses = ("discussed", "approved", "in_progress", "done", "cancelled")
            status_val: Any = plan_status if plan_status in valid_statuses else "unknown"

            return TaskContextResponse(
                found=True,
                task_id=eff_task_id,
                pr_url=eff_pr_url,
                status=status_val,
                plan=plan_data,
                reviews=reviews_data,
                debts=debts_data,
            )

    def search_artifacts(
        self,
        query: str = "",
        artifact_type: str = "all",
        category: str | None = None,
        status: str = "all",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search across plans, reviews and debt cards.

        Compatibility: ``artifact_type='audit'`` is accepted as an input alias
        for ``'debt'``; the canonical value in results is always ``'debt'``.

        Ranks: with a text query, candidates from the artifacts index
        (plans/reviews) and the debt index are merged via reciprocal rank
        fusion (score = sum 1/(k + rank), k=60) instead of comparing raw BM25
        scores from different indexes. With an empty query, both sources are
        sorted together by mtime (newest first) with a stable id tie-break.
        A status value that is invalid for every requested artifact type is
        rejected with a clear error instead of returning a silent empty result.
        """
        from mcp_audit.models import AuditStatus, PlanStatus, ReviewVerdict

        # Входной alias: 'audit' -> канонический 'debt'
        eff_type = "debt" if artifact_type == "audit" else artifact_type

        # Валидация status: неизвестный для всех запрошенных типов статус — ошибка
        self._validate_artifacts_status(eff_type, status, PlanStatus, AuditStatus, ReviewVerdict)

        safe_q = sanitize_fts_query(query) if query and query.strip() else ""
        norm_cat = normalize_category_slug(category) if category else None
        effective_limit = max(1, min(limit, 100)) if limit > 0 else DEFAULT_LIMIT

        plan_review_results: list[dict[str, Any]] = []
        if eff_type in ("all", "plan", "review"):
            plan_review_results = self._search_plan_review_candidates(
                eff_type=eff_type,
                safe_q=safe_q,
                norm_cat=norm_cat,
                status=status,
                effective_limit=effective_limit,
            )

        debt_results: list[dict[str, Any]] = []
        if eff_type in ("all", "debt"):
            debt_status = status if status != "all" else None
            debt_candidates = self.search_issues(
                query=query,
                category=norm_cat,
                status=debt_status,
                limit=effective_limit,
            )
            for d in debt_candidates:
                debt_results.append(
                    {
                        "id": d["id"],
                        "artifact_type": "debt",
                        "task_id": d.get("task_id") or d["id"],
                        "category": d.get("category") or "general",
                        "status": d.get("status", "planned"),
                        "title": d["title"],
                        "pr_url": None,
                        "file_path": self.find_card_path(d["id"]),
                        "symptom": d.get("symptom"),
                        "target_file": d.get("target_file"),
                    }
                )

        def _strip_internal(item: dict[str, Any]) -> dict[str, Any]:
            res = {k: v for k, v in item.items() if not k.startswith("_")}
            return res

        # Наполнение mtime для долгов (file_sync хранит REAL mtime) — общая сортировка.
        for item in debt_results:
            item["_mtime_ns"] = self._card_mtime_ns(item["id"])

        if safe_q:
            # RRF: слияние рангов обоих источников до общего лимита
            rrf_k = 60
            fused: dict[str, float] = {}
            entries: dict[str, dict[str, Any]] = {}
            for source in (plan_review_results, debt_results):
                for rank, item in enumerate(source, 1):
                    key = item["id"]
                    fused[key] = fused.get(key, 0.0) + 1.0 / (rrf_k + rank)
                    entries[key] = item
            ordered = sorted(
                entries.values(),
                key=lambda it: (-fused[it["id"]], -(it.get("_mtime_ns") or 0), it["id"]),
            )
            results = [_strip_internal(it) for it in ordered[:effective_limit]]
        else:
            merged = [*plan_review_results, *debt_results]
            if eff_type == "all":
                # Общая сортировка по времени со стабильным tie-break по id
                merged.sort(key=lambda it: (-(it.get("_mtime_ns") or 0), it["id"]))
            else:
                # Один источник: сохранить порядок источника (уже отсортирован по времени)
                pass
            results = [_strip_internal(it) for it in merged[:effective_limit]]

        return results

    def _search_plan_review_candidates(
        self,
        eff_type: str,
        safe_q: str,
        norm_cat: str | None,
        status: str,
        effective_limit: int,
    ) -> list[dict[str, Any]]:
        """Выбирает кандидатов планов/ревью из artifacts_index (бюджет — не больше общего лимита)."""
        results: list[dict[str, Any]] = []
        sql_parts = [
            """
            SELECT a.artifact_id, a.artifact_type, a.task_id, a.category, a.status, a.title,
                   a.pr_url, a.file_path, a.steps_total, a.steps_done, a.current_step_title,
                   a.latest_verdict, a.unresolved_comments, a.mtime_ns
            FROM artifacts_index a
            """
        ]
        where: list[str] = []
        params: list[Any] = []

        if safe_q:
            sql_parts.append("JOIN artifacts_fts f ON f.rowid = a.rowid")
            where.append("artifacts_fts MATCH ?")
            params.append(safe_q)

        if eff_type != "all":
            where.append("a.artifact_type = ?")
            params.append(eff_type)

        if norm_cat:
            where.append("a.category = ?")
            params.append(norm_cat)

        if status and status != "all":
            where.append("a.status = ?")
            params.append(status)

        if where:
            sql_parts.append("WHERE " + " AND ".join(where))

        if safe_q:
            sql_parts.append(f"ORDER BY bm25(artifacts_fts, {ARTIFACTS_BM25_WEIGHTS_SQL})")
        else:
            sql_parts.append("ORDER BY a.mtime_ns DESC, a.artifact_id ASC")
        sql_parts.append("LIMIT ?")
        params.append(effective_limit)

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("\n".join(sql_parts), params)
            for row in cursor.fetchall():
                results.append(
                    {
                        "id": row[0],
                        "artifact_type": row[1],
                        "task_id": row[2],
                        "category": row[3],
                        "status": row[4],
                        "title": row[5],
                        "pr_url": row[6],
                        "file_path": row[7],
                        "steps_total": row[8],
                        "steps_done": row[9],
                        "current_step": row[10],
                        "latest_verdict": row[11],
                        "unresolved_comments": row[12],
                        "_mtime_ns": row[13] or 0,
                    }
                )
        return results

    @staticmethod
    def _validate_artifacts_status(
        eff_type: str,
        status: str,
        plan_enum: type,
        debt_enum: type,
        review_enum: type,
    ) -> None:
        """Raises ValueError if the status is unknown for every requested artifact type.

        Статус должен существовать хотя бы для одного запрошенного типа
        артефактов; полностью неизвестный статус — ошибка, не пустой результат.
        """
        if not status or status == "all":
            return
        known_statuses: set[str] = set()
        if eff_type in ("all", "plan"):
            known_statuses.update(s.value for s in plan_enum)  # type: ignore[attr-defined]
        if eff_type in ("all", "review"):
            known_statuses.update(s.value for s in review_enum)  # type: ignore[attr-defined]
        if eff_type in ("all", "debt"):
            known_statuses.update(s.value for s in debt_enum)  # type: ignore[attr-defined]
        if status not in known_statuses:
            msg = (
                f"Unknown status '{status}'. Valid statuses: "
                f"plan: {', '.join(s.value for s in plan_enum)}; "  # type: ignore[attr-defined]
                f"debt: {', '.join(s.value for s in debt_enum)}; "  # type: ignore[attr-defined]
                f"review verdicts: {', '.join(s.value for s in review_enum)}. "  # type: ignore[attr-defined]
                "Status applies only to compatible artifact types."
            )
            raise ValueError(msg)

    def _card_mtime_ns(self, card_id: str) -> int:
        """Returns the file mtime (ns) of a debt card by its id, or 0 if unknown."""
        path = self.find_card_path(card_id)
        if not path:
            return 0
        try:
            return int(Path(path).stat().st_mtime_ns)
        except OSError:
            return 0
