"""Хранилище SQLite FTS5 кэша и автосинхронизации для MCP Knowledge Engine.

Обеспечивает полнотекстовый поиск, BM25-ранжирование, авто-переиндексацию по mtime,
детекцию дубликатов и стандарт SQLite Concurrency (WAL, busy_timeout=5000).
"""

import re
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Literal

from mcp_core.db import build_bm25_weights_sql
from mcp_core.db import get_connection as core_get_connection
from mcp_core.fts import SCHEMA_VERSION, fts5_table_sql, resolve_tokenizer, round_bm25_score
from mcp_core.fuzzy import jaccard_word_best
from mcp_core.schema_guard import FtsSpec, StorageSchemaSpec, ensure_storage_schema
from mcp_core.text import (
    morpho_diff as _morpho_diff,
)
from mcp_core.text import (
    tokens_morpho_equal as _tokens_morpho_equal,
)
from mcp_knowledge.db_guard import _raise_sqlite_unavailable
from mcp_knowledge.errors import DuplicateCardError
from mcp_knowledge.hypothesis_classifier import (
    ANTIPATTERN_CONFIRMED_THRESHOLD,
    ANTIPATTERN_REFUTED_THRESHOLD,
    DUP_SYMPTOM_THRESHOLD,
    MIN_HYPO_TOKENS_FOR_OVERLAP,
    MIN_TOKENS_FOR_OVERLAP,
    _symptom_similarity,
    _symptom_uniques_have_tech_differentiator,
    _tokenize,
    classify_confirm_candidate,
)
from mcp_knowledge.logger import logger
from mcp_knowledge.models import (
    CardRecordRequest,
    CategoryOverviewItem,
    FacetedSearchResponse,
    HypothesisRecordRequest,
    KnowledgeCategory,
    KnowledgeSnippet,
    KnowledgeSource,
    KnowledgeStatus,
    KnowledgeType,
    ProjectOverview,
    compute_card_badge,
)
from mcp_knowledge.sanitizer import (
    build_multi_field_fts_query,
    sanitize_fts_query,
)
from mcp_knowledge.search_scoring import (
    ALIASES_PROBE_SQL,
    KNOWLEDGE_PROBE_WEIGHTS,
    SYMPTOM_PROBE_SQL,
    TITLE_PROBE_SQL,
    _collect_fuzzy_knowledge_rows,
    _rerank_knowledge_rows,
)
from mcp_knowledge.synchronizer import calculate_card_confidence
from mcp_knowledge.telemetry import (
    TOOL_CALLS_CREATE_SQL,
    TelemetryMixin,
    read_tool_call_stats,
)

# 20 весов для колонок FTS5 таблицы knowledge_index:
# 0.id, 1.title, 2.category, 3.type, 4.status, 5.source, 6.generated_by, 7.created_at,
# 8.stale_after, 9.verified_count, 10.confidence, 11.task_id, 12.aliases, 13.symptom,
# 14.root_cause, 15.solution, 16.rationale, 17.refutation_reason, 18.tags, 19.body
BM25_WEIGHTS: tuple[float, ...] = (
    0.0,
    3.5,
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
    2.5,
    2.0,
    1.5,
    1.0,
    1.0,
    0.0,
    2.0,
    0.15,
)
BM25_WEIGHTS_SQL = build_bm25_weights_sql(BM25_WEIGHTS, 20)

KNOWLEDGE_FTS_COLUMNS = [
    "id",
    "title",
    "category",
    "type",
    "status",
    "source",
    "generated_by",
    "created_at",
    "stale_after",
    "verified_count",
    "confidence",
    "task_id",
    "aliases",
    "symptom",
    "root_cause",
    "solution",
    "rationale",
    "refutation_reason",
    "tags",
    "body",
]

KNOWLEDGE_FTS_UNINDEXED = [
    "id",
    "category",
    "type",
    "status",
    "source",
    "generated_by",
    "created_at",
    "stale_after",
    "verified_count",
    "confidence",
    "task_id",
    "refutation_reason",
]

FILE_SYNC_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS file_sync (
        path TEXT PRIMARY KEY,
        mtime REAL,
        size INTEGER,
        card_id TEXT,
        conflict INTEGER NOT NULL DEFAULT 0
    );
    """

# Колонки, отсутствие которых в существующей knowledge_index => пересоздать эфемерный кэш.
REQUIRED_FTS_COLUMNS = frozenset(
    {
        "generated_by",
        "stale_after",
        "verified_count",
        "confidence",
        "task_id",
        "refutation_reason",
        "created_at",
        "category",
    }
)

ACTIVE_STATUSES = (
    KnowledgeStatus.ACTIVE,
    KnowledgeStatus.VERIFIED,
    KnowledgeStatus.SUSPECT,
)
ACTIVE_STATUS_SQL = ", ".join(f"'{s.value}'" for s in ACTIVE_STATUSES)

ALL_SEARCHABLE_STATUSES = (
    KnowledgeStatus.ACTIVE,
    KnowledgeStatus.VERIFIED,
    KnowledgeStatus.SUSPECT,
    KnowledgeStatus.HYPOTHESIS,
)
ALL_SEARCHABLE_STATUS_SQL = ", ".join(f"'{s.value}'" for s in ALL_SEARCHABLE_STATUSES)

ACTIVE_OR_VERIFIED_STATUSES = (
    KnowledgeStatus.ACTIVE,
    KnowledgeStatus.VERIFIED,
)
ACTIVE_OR_VERIFIED_STATUS_SQL = ", ".join(f"'{s.value}'" for s in ACTIVE_OR_VERIFIED_STATUSES)

STATUS_ORDER_BY_CASE_SQL = (
    f"CASE status "
    f"WHEN '{KnowledgeStatus.VERIFIED.value}' THEN 1 "
    f"WHEN '{KnowledgeStatus.ACTIVE.value}' THEN 2 "
    f"WHEN '{KnowledgeStatus.SUSPECT.value}' THEN 3 "
    f"WHEN '{KnowledgeStatus.HYPOTHESIS.value}' THEN 4 "
    f"ELSE 5 END"
)

CATEGORY_STATUS_ORDER_BY_CASE_SQL = (
    f"CASE status "
    f"WHEN '{KnowledgeStatus.VERIFIED.value}' THEN 1 "
    f"WHEN '{KnowledgeStatus.ACTIVE.value}' THEN 2 "
    f"ELSE 3 END"
)

# Human-first префикс ORDER BY (ADR storage-split-adr.md §DRY-опция):
# вычисленные человеком карточки сортируются раньше авто-сгенерированных.
HUMAN_FIRST_ORDER_BY_CASE_SQL = "CASE generated_by WHEN 'human' THEN 1 ELSE 2 END"

__all__ = [
    "ANTIPATTERN_CONFIRMED_THRESHOLD",
    "ANTIPATTERN_REFUTED_THRESHOLD",
    "BM25_WEIGHTS",
    "BM25_WEIGHTS_SQL",
    "DUP_DETAIL_THRESHOLD",
    "DUP_PENALTY",
    "DUP_SYMPTOM_THRESHOLD",
    "HYP_DETAIL_THRESHOLD",
    "HYP_PENALTY",
    "HYP_SYMPTOM_THRESHOLD",
    "KNOWLEDGE_PROBE_WEIGHTS",
    "MAX_TOTAL_SNIPPETS",
    "DuplicateCardError",
    "KnowledgeStorage",
    "_tokens_morpho_equal",
    "jaccard_word_best",
    "read_tool_call_stats",
]

# Пороги token-overlap для детекции дубликатов (аудит H3: именованные константы
# вместо магических чисел). Значения сохранены 1:1 с прежним кодом.
DUP_DETAIL_THRESHOLD = 0.5  # check_duplicate: совпадение при сильном BM25-скоре
DUP_PENALTY = -0.1  # check_duplicate: порог «сильного» BM25-скора
HYP_SYMPTOM_THRESHOLD = 0.7  # check_duplicate_hypothesis: совпадение симптомов
HYP_DETAIL_THRESHOLD = 0.6  # check_duplicate_hypothesis: совпадение при сильном BM25-скоре
HYP_PENALTY = -0.2  # check_duplicate_hypothesis: порог «сильного» BM25-скора


# Жесткий потолок суммарной выдачи фасетного поиска (search_faceted) и
# дефолт max_total для _build_faceted_hint (аудит L-magic-12: единая константа).
MAX_TOTAL_SNIPPETS = 12

_TOKEN_PATTERN = re.compile(r"\w{3,}")

# 16 колонок полной выборки карточек (get_cards_by_ids / list_recent_cards / _rows_to_cards).
_CARD_COLUMNS = (
    "id",
    "title",
    "category",
    "type",
    "status",
    "source",
    "generated_by",
    "created_at",
    "stale_after",
    "verified_count",
    "confidence",
    "task_id",
    "symptom",
    "root_cause",
    "solution",
    "rationale",
)

# 11 колонок сниппетной выборки (совпадает с порядком row[:11] в _row_to_snippet).
_SNIPPET_COLUMNS_SQL = (
    "id, title, category, type, status, source, confidence, task_id, symptom, "
    "stale_after, refutation_reason"
)


def _rebuild_knowledge(c: sqlite3.Connection) -> None:
    """Пакетный drift-rebuild кэша знаний (module-level callback для StorageSchemaSpec).

    Выполняется ТОЛЬКО внутри guard-транзакции (BEGIN IMMEDIATE): DROP-ы
    устаревших объектов, FTS-DDL, служебная таблица file_sync. Атомарность
    обеспечивает guard — здесь без транзакционных примитивов и без скриптового
    API курсора. Кэш эфемерный: контент восстанавливается при следующем sync.
    """
    cur = c.cursor()
    cur.execute("DROP TABLE IF EXISTS knowledge_index;")
    cur.execute("DROP TABLE IF EXISTS file_sync;")
    cur.execute(
        fts5_table_sql(
            "knowledge_index",
            KNOWLEDGE_FTS_COLUMNS,
            unindexed=KNOWLEDGE_FTS_UNINDEXED,
            tokenizer=resolve_tokenizer("unicode61"),
        )
    )
    cur.execute(FILE_SYNC_CREATE_SQL)


# Спецификация схемы кэша знаний для ensure_storage_schema (mcp_core.schema_guard).
# fts_checks — ordered-контракт на ВСЕ 20 колонок: веса bm25(knowledge_index, weights)
# позиционны, а прежний REQUIRED=8 при арности 20 пропускал перестановки/потери
# колонок (дыра плана v5) — строгий контракт закрывает её бесплатно (кэш
# эфемерный, drift-ребилд беневолентен).
KNOWLEDGE_STORAGE_SCHEMA_SPEC = StorageSchemaSpec(
    schema_version=SCHEMA_VERSION,
    fts_checks={"knowledge_index": FtsSpec(expected_columns=tuple(KNOWLEDGE_FTS_COLUMNS))},
    # tool_calls — ТОЛЬКО самолечение, никогда drift (иначе бессмысленный
    # холодный реиндекс; см. план v5, этап 3).
    service_ddl=[FILE_SYNC_CREATE_SQL, TOOL_CALLS_CREATE_SQL],
    rebuild=_rebuild_knowledge,
    # Объектов, создаваемых исключительно в _rebuild (триггеры/индексы) нет —
    # file_sync и knowledge_index полностью покрываются fts_checks + service_ddl.
    rebuild_on_missing=frozenset(),
)


def _clean_categories(categories: list[str] | None, limit: int = 5) -> list[str]:
    """Очищает и нормализует список категорий: strip+lower, дедупликация, срез до limit."""
    if not categories:
        return []
    return list(dict.fromkeys(d.strip().lower() for d in categories if d and d.strip()))[:limit]


def _build_symptom_fts_query(symptom: str) -> str | None:
    """Санитизирует симптом и строит FTS5-запрос вида `symptom : (...)`.

    Возвращает None, если после санитизации запрос пуст или вырождается до '""'.
    """
    sanitized = sanitize_fts_query(symptom)
    if not sanitized or sanitized == '""':
        return None
    return f"symptom : ({sanitized})"


# UX-тексты сниппетов (_row_to_snippet). Байт-идентичны прежним inline-литералам.
WARNING_REFUTED_PREFIX = "⛔ ОПРОВЕРГНУТО: {ref_msg}"
WARNING_HYPOTHESIS = "🧪 РАБОЧАЯ ГИПОТЕЗА: Не подтверждено на практике"
WARNING_SUSPECT = "⚠️ Подсказка под сомнением: были сбои в предыдущих сессиях"
WARNING_STALE_FORMAT = "⚠️ Карточка устарела (stale_after: {stale_after})"
REFUTED_FALLBACK_REASON = "Признано тупиковым путем"

# UX-тексты поиска (_build_faceted_hint).
HINT_EMPTY_BASE = "База знаний проекта пока пуста. Зафиксируйте первые правила или решения через record_knowledge."
HINT_CATALOG_FORMAT = (
    "Каталог базы знаний проекта: {total_cards} активных карточек. "
    "Темы: {top_cats}. Используйте categories=[...] или query для точечной выборки."
)
HINT_CATALOG_NO_OVERVIEW = (
    "Каталог базы знаний пуст или сводка отключена. Используйте query или categories для поиска."
)
HINT_LIMIT_REACHED = (
    f"Достигнут лимит выдачи ({MAX_TOTAL_SNIPPETS} сниппетов). "
    "Уточните запрос или сократите список categories для более детальной выборки."
)
HINT_EMPTY_CATEGORIES_FORMAT = (
    "По категориям {truly_empty} пока нет активных карточек в базе знаний."
)
HINT_MATCHING_DRAFTS = (
    "По вашему запросу найдены черновики в карантине (draft). "
    "Повторите поиск с include_drafts=true или укажите свой task_id."
)


class KnowledgeStorage(TelemetryMixin):
    """Управление эфемерным SQLite FTS5 кэшем над Markdown-карточками."""

    _calculate_card_confidence = staticmethod(calculate_card_confidence)

    def __init__(
        self,
        db_path: Path,
        knowledge_dir: Path | None = None,
        create_dirs: bool = True,
    ):
        self.db_path = Path(db_path)
        self.knowledge_dir = (
            Path(knowledge_dir) if knowledge_dir is not None else self.db_path.parent / "knowledge"
        )
        self._create_dirs = create_dirs

        # Создаем родительские директории при необходимости
        if create_dirs and str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        self._synchronizer: Any = None

        self._init_db()

    def get_synchronizer(self) -> Any:
        """Возвращает привязанный экземпляр KnowledgeSynchronizer или создает ленивый дефолт."""
        if self._synchronizer is None:
            from mcp_knowledge.repository import CardRepository
            from mcp_knowledge.synchronizer import KnowledgeSynchronizer

            repo = CardRepository(self.knowledge_dir, create_dir=self._create_dirs)
            self._synchronizer = KnowledgeSynchronizer(self, repo)
        return self._synchronizer

    def set_synchronizer(self, synchronizer: Any) -> None:
        """Привязывает внешний экземпляр KnowledgeSynchronizer."""
        self._synchronizer = synchronizer

    @contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection]:
        """Контекстный менеджер подключения к SQLite с обязательными PRAGMA."""
        conn = core_get_connection(self.db_path, timeout=5.0)
        try:
            yield conn
        finally:
            conn.close()

    def close(self) -> None:
        """Закрывает ресурсы хранилища (no-op, соединения закрываются в get_connection)."""

    def _init_db(self) -> None:
        """Инициализирует схему через mcp_core ensure_storage_schema (guard).

        Единственный guard-вызов: drift-check (ordered-контракт всех 20 колонок
        knowledge_index), атомарный rebuild при drift, безусловное service-самолечение
        (file_sync, tool_calls) — всё внутри контракта guard'а.
        """
        with self.get_connection() as conn:
            ensure_storage_schema(conn, KNOWLEDGE_STORAGE_SCHEMA_SPEC)

    def sync(self) -> None:
        """Сверяет файлы в knowledge_dir с таблицей file_sync по mtime и размеру.

        Инкрементально переиндексирует измененные файлы и удаляет несуществующие.
        """
        self.get_synchronizer().sync()

    def _build_search_conditions(
        self,
        sanitized_query: str,
        include_archived: bool = False,
        include_hypotheses: bool = False,
        task_id: str | None = None,
        category: str | KnowledgeCategory | None = None,
        include_drafts: bool = False,
    ) -> tuple[str, list[Any]]:
        """Собирает WHERE-условия и параметры для FTS5-поиска.

        Правила видимости: без include_archived поиск идёт по активным карточкам;
        гипотезы и черновики видны только при явном task_id или соответствующих флагах.
        """
        conditions = ["knowledge_index MATCH ?"]
        params: list[Any] = [sanitized_query]

        if not include_archived:
            if task_id:
                conditions.append(
                    f"(status IN ({ACTIVE_STATUS_SQL}) "
                    f"OR (status IN ('{KnowledgeStatus.HYPOTHESIS.value}', '{KnowledgeStatus.DRAFT.value}') AND task_id = ?))"
                )
                params.append(task_id)
            elif include_drafts and include_hypotheses:
                conditions.append(
                    f"status IN ({ALL_SEARCHABLE_STATUS_SQL}, '{KnowledgeStatus.DRAFT.value}')"
                )
            elif include_drafts:
                conditions.append(
                    f"(status IN ({ACTIVE_STATUS_SQL}) OR status = '{KnowledgeStatus.DRAFT.value}')"
                )
            elif include_hypotheses:
                conditions.append(f"status IN ({ALL_SEARCHABLE_STATUS_SQL})")
            else:
                conditions.append(f"status IN ({ACTIVE_STATUS_SQL})")

        if category:
            cat_val = (
                category.value if hasattr(category, "value") else str(category).strip().lower()
            )
            conditions.append("category = ?")
            params.append(cat_val)

        return " AND ".join(conditions), params

    def search(
        self,
        query: str,
        category: str | KnowledgeCategory | None = None,
        include_archived: bool = False,
        include_hypotheses: bool = False,
        task_id: str | None = None,
        limit: int = 5,
        include_drafts: bool = False,
        sync: bool = True,
    ) -> list[KnowledgeSnippet]:
        """Выполняет полнотекстовый поиск с BM25 ранжированием и сортировкой по статусу доверия."""
        limit = max(1, min(limit, 20))
        if sync:
            self.sync()

        and_query = sanitize_fts_query(query, operator="AND")
        or_query = sanitize_fts_query(query, operator="OR")
        if not and_query or and_query == '""':
            return []

        pool_limit = max(limit * 4, limit)
        candidate_limit = max(pool_limit * 2, 30)

        def _build_search_sql(where_sql: str) -> str:
            return f"""
                WITH relevant_candidates AS (
                    SELECT
                        id, title, category, type, status, source, generated_by, symptom, stale_after, verified_count,
                        confidence, task_id, refutation_reason, body, aliases,
                        bm25(knowledge_index, {BM25_WEIGHTS_SQL}) as score,
                        bm25(knowledge_index, {TITLE_PROBE_SQL}) != 0.0 as match_title,
                        bm25(knowledge_index, {ALIASES_PROBE_SQL}) != 0.0 as match_aliases,
                        bm25(knowledge_index, {SYMPTOM_PROBE_SQL}) != 0.0 as match_symptom
                    FROM knowledge_index
                    WHERE {where_sql}
                    ORDER BY score ASC
                    LIMIT ?
                )
                SELECT
                    id, title, category, type, status, source, confidence, task_id, symptom, stale_after, refutation_reason, score, body, generated_by,
                    aliases, match_title, match_aliases, match_symptom
                FROM relevant_candidates
                ORDER BY
                    {HUMAN_FIRST_ORDER_BY_CASE_SQL},
                    {STATUS_ORDER_BY_CASE_SQL},
                    confidence DESC,
                    verified_count DESC,
                    score ASC
                LIMIT ?;
            """

        with self.get_connection() as conn:
            cursor = conn.cursor()

            where_clause, params = self._build_search_conditions(
                and_query,
                include_archived=include_archived,
                include_hypotheses=include_hypotheses,
                task_id=task_id,
                category=category,
                include_drafts=include_drafts,
            )

            sql = _build_search_sql(where_clause)
            params_sql = list(params)
            params_sql.append(candidate_limit)
            params_sql.append(pool_limit)

            used_query = and_query
            try:
                cursor.execute(sql, params_sql)
                rows = cursor.fetchall()

                # AND -> OR fallback: если строгий AND ничего не нашел, повторяем с OR
                if not rows and or_query != and_query:
                    where_clause_or, params_or = self._build_search_conditions(
                        or_query,
                        include_archived=include_archived,
                        include_hypotheses=include_hypotheses,
                        task_id=task_id,
                        category=category,
                        include_drafts=include_drafts,
                    )
                    sql_or = _build_search_sql(where_clause_or)
                    params_sql_or = list(params_or)
                    params_sql_or.append(candidate_limit)
                    params_sql_or.append(pool_limit)
                    cursor.execute(sql_or, params_sql_or)
                    rows = cursor.fetchall()
                    used_query = or_query
                    where_clause = where_clause_or
                    params = params_or
            except sqlite3.OperationalError as e:
                clean_q = sanitize_fts_query(query) if query else ""
                cat_str = str(category) if category else None
                _raise_sqlite_unavailable(
                    e,
                    warn_template=(
                        "SQLite OperationalError during search(query=%r, category=%r, limit=%r): %s"
                    ),
                    warn_params=(clean_q, cat_str, limit),
                    debug_context="search",
                    error_message="Database search failed",
                )

            if used_query == and_query and rows and and_query != or_query:
                fuzzy_rows = []
            else:
                fuzzy_rows = _collect_fuzzy_knowledge_rows(
                    cursor, where_clause, params, query, rows, limit
                )
            ordered_rows = _rerank_knowledge_rows(conn, rows, fuzzy_rows, used_query)
            return [self._row_to_snippet(r) for r in ordered_rows[:limit]]

    @staticmethod
    def _row_to_snippet(row: tuple[Any, ...]) -> KnowledgeSnippet:
        (
            c_id,
            c_title,
            c_category,
            c_type,
            c_status,
            c_source,
            c_confidence,
            c_task_id,
            c_symptom,
            c_stale_after,
            c_refutation,
        ) = row[:11]
        raw_score = row[11] if len(row) > 11 else None
        if raw_score is not None:
            val = -float(raw_score)
            score_val = round_bm25_score(val)
        else:
            score_val = None
        warnings: list[str] = []

        # K11: легаси/некорректное значение в индексе не должно валить весь
        # search — деградация до ACTIVE с warning.
        try:
            status_enum = KnowledgeStatus(c_status)
        except ValueError:
            warnings.append(f"CARD_STATUS_UNKNOWN: '{c_status}'")
            status_enum = KnowledgeStatus.ACTIVE
        try:
            type_enum = KnowledgeType(c_type)
        except ValueError:
            warnings.append(f"CARD_TYPE_UNKNOWN: '{c_type}'")
            type_enum = KnowledgeType.LESSON
        badge = compute_card_badge(status=status_enum, card_type=type_enum)

        if badge == "refuted":
            ref_msg = (
                str(c_refutation).strip() if c_refutation and str(c_refutation).strip() else ""
            ) or REFUTED_FALLBACK_REASON
            warnings.append(WARNING_REFUTED_PREFIX.format(ref_msg=ref_msg))
        elif badge == "hypothesis":
            warnings.append(WARNING_HYPOTHESIS)
        elif badge == "suspect":
            warnings.append(WARNING_SUSPECT)

        if c_stale_after:
            try:
                stale_date = date.fromisoformat(str(c_stale_after).split("T")[0])
                if stale_date < date.today():
                    warnings.append(WARNING_STALE_FORMAT.format(stale_after=c_stale_after))
            except ValueError:
                pass

        warning = "; ".join(warnings) if warnings else None
        s_text = str(c_symptom) if c_symptom else (str(c_refutation) if c_refutation else "")
        snippet_text = s_text[:140] + "..." if len(s_text) > 140 else s_text
        conf_val = float(c_confidence) if c_confidence is not None else 0.75

        return KnowledgeSnippet(
            id=str(c_id),
            title=str(c_title),
            category=str(c_category),
            type=type_enum,
            status=status_enum,
            source=(
                KnowledgeSource(c_source)
                if c_source and str(c_source).lower() in KnowledgeSource._value2member_map_
                else KnowledgeSource.AUTO_INFERRED
            ),
            badge=badge,
            confidence=conf_val,
            task_id=str(c_task_id) if c_task_id else None,
            snippet=snippet_text,
            warning=warning,
            score=score_val,
        )

    def get_project_overview(self, sync: bool = True) -> ProjectOverview:
        """Собирает динамическую сводку тем проекта (Project Overview) и распределение карточек по категориям."""
        if sync:
            self.sync()

        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(f"""
                    SELECT category, count(*) as cnt
                    FROM knowledge_index
                    WHERE status IN ({ACTIVE_STATUS_SQL})
                    GROUP BY category
                    ORDER BY cnt DESC;
                """)
                category_counts = cursor.fetchall()

                if not category_counts:
                    return ProjectOverview(total_cards=0, categories=[])

                total_cards = sum(row[1] for row in category_counts)

                cursor.execute(f"""
                    WITH ranked AS (
                        SELECT category, title,
                               ROW_NUMBER() OVER (
                                   PARTITION BY category
                                   ORDER BY
                                       {HUMAN_FIRST_ORDER_BY_CASE_SQL},
                                       {CATEGORY_STATUS_ORDER_BY_CASE_SQL},
                                       verified_count DESC
                               ) as rn
                        FROM knowledge_index
                        WHERE status IN ({ACTIVE_STATUS_SQL})
                    )
                    SELECT category, title
                    FROM ranked
                    WHERE rn <= 3;
                """)
                title_rows = cursor.fetchall()
                category_titles: dict[str, list[str]] = {}
                for cat, title in title_rows:
                    category_titles.setdefault(cat, []).append(title)

                overview_items: list[CategoryOverviewItem] = []
                for cat, cnt in category_counts:
                    samples = category_titles.get(cat, [])
                    overview_items.append(
                        CategoryOverviewItem(
                            category=cat,
                            count=cnt,
                            sample_titles=samples,
                        )
                    )

                return ProjectOverview(total_cards=total_cards, categories=overview_items)
        except sqlite3.OperationalError as err:
            _raise_sqlite_unavailable(
                err,
                warn_template="Ошибка при сборе сводки проекта (%s)",
                debug_context="get_project_overview",
                error_message="Database overview query failed",
            )

    def _collect_category_results(
        self,
        cursor: sqlite3.Cursor,
        categories: list[str],
        status_cond: str,
        clamped_facet_limit: int,
        seen_card_ids: set[str],
        max_total: int,
        status_params: list[Any] | None = None,
    ) -> dict[str, list[KnowledgeSnippet]]:
        category_results: dict[str, list[KnowledgeSnippet]] = {}
        clean_cats = _clean_categories(categories)
        params_base: list[Any] = list(status_params or [])
        for cat in clean_cats:
            sql_cat = f"""
                SELECT {_SNIPPET_COLUMNS_SQL}
                FROM knowledge_index
                WHERE {status_cond} AND category = ?
                ORDER BY
                    {HUMAN_FIRST_ORDER_BY_CASE_SQL},
                    {CATEGORY_STATUS_ORDER_BY_CASE_SQL},
                    confidence DESC,
                    verified_count DESC
                LIMIT ?;
            """
            try:
                cursor.execute(sql_cat, (*params_base, cat, clamped_facet_limit * 2))
                cat_snippets: list[KnowledgeSnippet] = []
                for row in cursor.fetchall():
                    card_id = row[0]
                    if (
                        card_id not in seen_card_ids
                        and len(seen_card_ids) < max_total
                        and len(cat_snippets) < clamped_facet_limit
                    ):
                        seen_card_ids.add(card_id)
                        cat_snippets.append(self._row_to_snippet(row))
                category_results[cat] = cat_snippets
            except sqlite3.OperationalError as err:
                _raise_sqlite_unavailable(
                    err,
                    warn_template="Ошибка при выборке категории %s (%s)",
                    warn_params=(cat,),
                    debug_context="_collect_category_results",
                    error_message=f"Database query failed for category '{cat}'",
                )
        return category_results

    def _collect_antipatterns(
        self,
        cursor: sqlite3.Cursor,
        query: str | None,
        categories: list[str] | None,
        seen_card_ids: set[str],
    ) -> list[KnowledgeSnippet]:
        """Выбирает до 2 наиболее релевантных антипаттернов / опровергнутых решений."""
        return self._collect_snippet_facet(
            cursor,
            base_condition=(
                f"(status = '{KnowledgeStatus.REFUTED.value}' "
                f"OR type = '{KnowledgeType.ANTIPATTERN.value}')"
            ),
            layer="antipatterns",
            query=query,
            categories=categories,
            seen_card_ids=seen_card_ids,
            facet_limit=2,
            max_total=None,
        )

    def _collect_hypotheses(
        self,
        cursor: sqlite3.Cursor,
        query: str | None,
        categories: list[str] | None,
        task_id: str | None,
        clamped_facet_limit: int,
        seen_card_ids: set[str],
        max_total: int,
    ) -> list[KnowledgeSnippet]:
        """Выбирает рабочие гипотезы текущей задачи или контекста."""
        return self._collect_snippet_facet(
            cursor,
            base_condition=f"status = '{KnowledgeStatus.HYPOTHESIS.value}'",
            layer="hypotheses",
            query=query,
            categories=categories,
            seen_card_ids=seen_card_ids,
            facet_limit=clamped_facet_limit,
            limit_as_param=True,
            task_id=task_id,
            max_total=max_total,
        )

    def _collect_snippet_facet(
        self,
        cursor: sqlite3.Cursor,
        base_condition: str,
        layer: str,
        query: str | None,
        categories: list[str] | None,
        seen_card_ids: set[str],
        facet_limit: int,
        max_total: int | None,
        limit_as_param: bool = False,
        task_id: str | None = None,
    ) -> list[KnowledgeSnippet]:
        """Общая реализация фасетных коллекторов антипаттернов и гипотез.

        Строит условия выборки (base_condition + task_id + query MATCH | categories),
        выбирает order_by (bm25 при активном MATCH, иначе confidence) и выполняет
        дедупликацию по seen_card_ids с потолками facet_limit / max_total.
        Параметр layer параметризует сообщения R-3 (warn/debug_context/error).
        Инвариант R-3: сбой SQLite конвертируется в StorageUnavailableError.
        """
        snippets: list[KnowledgeSnippet] = []
        conditions = [base_condition]
        params: list[Any] = []
        has_match = False
        if task_id:
            conditions.append("task_id = ?")
            params.append(task_id)
        if query and query.strip():
            sanitized_query = sanitize_fts_query(query.strip())
            if sanitized_query and sanitized_query != '""':
                conditions.append("knowledge_index MATCH ?")
                params.append(sanitized_query)
                has_match = True
        elif categories:
            clean_cats = _clean_categories(categories)
            if clean_cats:
                placeholders = ",".join("?" for _ in clean_cats)
                conditions.append(f"category IN ({placeholders})")
                params.extend(clean_cats)

        if has_match:
            order_by = f"bm25(knowledge_index, {BM25_WEIGHTS_SQL}) ASC"
        else:
            order_by = "confidence DESC, id DESC"

        limit_sql = "?" if limit_as_param else str(facet_limit)
        sql = f"""
            SELECT {_SNIPPET_COLUMNS_SQL}
            FROM knowledge_index
            WHERE {" AND ".join(conditions)}
            ORDER BY {order_by}
            LIMIT {limit_sql};
        """
        if limit_as_param:
            params.append(facet_limit)
        try:
            cursor.execute(sql, params)
            for row in cursor.fetchall():
                card_id = row[0]
                within_total = max_total is None or len(seen_card_ids) < max_total
                if card_id not in seen_card_ids and within_total and len(snippets) < facet_limit:
                    seen_card_ids.add(card_id)
                    snippets.append(self._row_to_snippet(row))
        except sqlite3.OperationalError as err:
            _raise_sqlite_unavailable(
                err,
                warn_template=f"Ошибка при выборке {layer} (%s)",
                debug_context=f"_collect_{layer}",
                error_message=f"Database query failed for {layer}",
            )
        return snippets

    @staticmethod
    def _build_faceted_hint(
        overview: ProjectOverview | None,
        query: str | None,
        categories: list[str] | None,
        category_results: dict[str, list[KnowledgeSnippet]],
        seen_card_ids: set[str],
        max_total: int = MAX_TOTAL_SNIPPETS,
    ) -> str | None:
        if overview and overview.total_cards == 0:
            return HINT_EMPTY_BASE
        if not query and not categories:
            if overview and overview.total_cards > 0:
                top_cats = ", ".join(
                    f"{item.category} ({item.count})" for item in overview.categories[:4]
                )
                return HINT_CATALOG_FORMAT.format(
                    total_cards=overview.total_cards, top_cats=top_cats
                )
            return HINT_CATALOG_NO_OVERVIEW

        if len(seen_card_ids) >= max_total:
            return HINT_LIMIT_REACHED

        if categories:
            clean_cats = [c.strip().lower() for c in categories if c and c.strip()]
            if overview:
                overview_counts = {item.category: item.count for item in overview.categories}
                truly_empty = [c for c in clean_cats if overview_counts.get(c, 0) == 0]
            else:
                truly_empty = [c for c in clean_cats if not category_results.get(c)]
            if truly_empty:
                return HINT_EMPTY_CATEGORIES_FORMAT.format(truly_empty=truly_empty)
        return None

    @staticmethod
    def _has_matching_drafts(
        cursor: sqlite3.Cursor,
        query: str | None,
        categories: list[str] | None,
        task_id: str | None,
    ) -> bool:
        clean_cats = (
            [c.strip().lower() for c in categories if c and c.strip()] if categories else []
        )
        extra_conds = ["status = 'draft'"]
        extra_params: list[Any] = []

        if task_id:
            extra_conds.append("(task_id IS NULL OR task_id != ?)")
            extra_params.append(task_id)

        if clean_cats:
            placeholders = ", ".join("?" for _ in clean_cats)
            extra_conds.append(f"category IN ({placeholders})")
            extra_params.extend(clean_cats)

        where_extra = " AND ".join(extra_conds)

        try:
            if query and query.strip():
                and_query = sanitize_fts_query(query.strip(), operator="AND")
                or_query = sanitize_fts_query(query.strip(), operator="OR")
                if not and_query or and_query == '""':
                    return False

                sql_and = f"SELECT 1 FROM knowledge_index WHERE knowledge_index MATCH ? AND {where_extra} LIMIT 1"
                cursor.execute(sql_and, [and_query, *extra_params])
                if cursor.fetchone():
                    return True

                if or_query and or_query != '""' and or_query != and_query:
                    sql_or = f"SELECT 1 FROM knowledge_index WHERE knowledge_index MATCH ? AND {where_extra} LIMIT 1"
                    cursor.execute(sql_or, [or_query, *extra_params])
                    if cursor.fetchone():
                        return True
                return False
            elif clean_cats or task_id:
                sql = f"SELECT 1 FROM knowledge_index WHERE {where_extra} LIMIT 1"
                cursor.execute(sql, extra_params)
                return cursor.fetchone() is not None
            else:
                return False
        except sqlite3.OperationalError:
            return False

    def search_faceted(
        self,
        query: str | None = None,
        categories: list[str] | None = None,
        task_id: str | None = None,
        include_hypotheses: bool = False,
        include_antipatterns: bool = True,
        limit: int = 5,
        limit_per_facet: int = 3,
        include_overview: bool = True,
        include_archived: bool = False,
        include_drafts: bool = False,
    ) -> FacetedSearchResponse:
        """Выполняет многослойный фасетный поиск с дедупликацией и защитой от переполнения контекста."""
        self.sync()

        clamped_limit = max(1, min(limit, 20))
        clamped_facet_limit = max(1, min(limit_per_facet, 5))

        seen_card_ids: set[str] = set()

        query_results: list[KnowledgeSnippet] = []
        category_results: dict[str, list[KnowledgeSnippet]] = {}
        antipatterns: list[KnowledgeSnippet] = []
        hypotheses: list[KnowledgeSnippet] = []

        antipattern_exclude_sql = f"type != '{KnowledgeType.ANTIPATTERN.value}'"
        status_params: list[Any] = []
        if include_archived:
            status_cond = "1=1"
        elif task_id:
            # task_id открывает свои draft-карточки в слое категорий.
            # Карточки со статусом hypothesis выбираются исключительно выделенным
            # слоем гипотез (_collect_hypotheses), чтобы не утекать в category_results
            # при include_hypotheses=False и не дедуплицироваться раньше времени.
            status_cond = (
                f"(status IN ({ACTIVE_STATUS_SQL}) "
                f"OR (status = '{KnowledgeStatus.DRAFT.value}' AND task_id = ?)) "
                f"AND {antipattern_exclude_sql}"
            )
            status_params.append(task_id)
        elif include_drafts:
            status_cond = (
                f"(status IN ({ACTIVE_STATUS_SQL}) OR status = '{KnowledgeStatus.DRAFT.value}') "
                f"AND {antipattern_exclude_sql}"
            )
        else:
            status_cond = f"status IN ({ACTIVE_STATUS_SQL}) AND {antipattern_exclude_sql}"

        with self.get_connection() as conn:
            cursor = conn.cursor()

            # 2. Слой текстового поиска (query_results)
            clean_cats = _clean_categories(categories)
            clean_cats_set = set(clean_cats)
            if query and query.strip():
                target_category = clean_cats[0] if clean_cats and len(clean_cats) == 1 else None
                raw_query_snippets = self.search(
                    query=query.strip(),
                    category=target_category,
                    include_archived=include_archived,
                    include_hypotheses=False,
                    include_drafts=include_drafts,
                    task_id=task_id,
                    limit=clamped_limit * 2,
                    sync=False,
                )
                for snippet in raw_query_snippets:
                    if (
                        clean_cats
                        and len(clean_cats) > 1
                        and snippet.category.lower() not in clean_cats_set
                    ):
                        continue
                    if (
                        snippet.type != KnowledgeType.ANTIPATTERN
                        and snippet.status != KnowledgeStatus.HYPOTHESIS
                        and snippet.id not in seen_card_ids
                        and len(seen_card_ids) < MAX_TOTAL_SNIPPETS
                        and len(query_results) < clamped_limit
                    ):
                        seen_card_ids.add(snippet.id)
                        query_results.append(snippet)

            # 3. Слой категорий (category_results)
            if categories:
                category_results = self._collect_category_results(
                    cursor=cursor,
                    categories=categories,
                    status_cond=status_cond,
                    status_params=status_params,
                    clamped_facet_limit=clamped_facet_limit,
                    seen_card_ids=seen_card_ids,
                    max_total=MAX_TOTAL_SNIPPETS,
                )

            # 4. Слой антипаттернов (antipatterns: максимум 2 релевантных)
            if include_antipatterns:
                antipatterns = self._collect_antipatterns(
                    cursor=cursor,
                    query=query,
                    categories=categories,
                    seen_card_ids=seen_card_ids,
                )

            # 5. Слой гипотез (hypotheses)
            if include_hypotheses or task_id:
                hypotheses = self._collect_hypotheses(
                    cursor=cursor,
                    query=query,
                    categories=categories,
                    task_id=task_id,
                    clamped_facet_limit=clamped_facet_limit,
                    seen_card_ids=seen_card_ids,
                    max_total=MAX_TOTAL_SNIPPETS,
                )

            has_matching_drafts = False
            if not seen_card_ids and not include_drafts and not include_archived:
                has_matching_drafts = self._has_matching_drafts(
                    cursor=cursor,
                    query=query,
                    categories=categories,
                    task_id=task_id,
                )

        # 6. Сводка проекта (Project Overview)
        overview: ProjectOverview | None = (
            self.get_project_overview(sync=False) if include_overview else None
        )

        # 7. Детерминированный hint
        hint: str | None
        if has_matching_drafts:
            hint = HINT_MATCHING_DRAFTS
        else:
            hint = self._build_faceted_hint(
                overview=overview,
                query=query,
                categories=categories,
                category_results=category_results,
                seen_card_ids=seen_card_ids,
                max_total=MAX_TOTAL_SNIPPETS,
            )

        return FacetedSearchResponse(
            query_results=query_results,
            results=query_results,
            category_results=category_results,
            antipatterns=antipatterns,
            hypotheses=hypotheses,
            overview=overview,
            hint=hint,
        )

    def _rows_to_cards(self, rows: list[Any]) -> list[dict[str, Any]]:
        """Преобразует строки выборки карточек в словари (единый список колонок)."""
        cards: list[dict[str, Any]] = []
        for row in rows:
            res = dict(zip(_CARD_COLUMNS, row, strict=True))
            cards.append(res)
        return cards

    def get_cards_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        """Точечная выборка активных карточек по ID (ветка ids саморевью).

        В отличие от list_recent_cards не зависит от сортировки/лимитов по created_at —
        находит карточку независимо от её возраста.
        """
        clean_ids = [i for i in ids if i]
        if not clean_ids:
            return []

        try:
            self.sync()
            with self.get_connection() as conn:
                cursor = conn.cursor()
                placeholders = ", ".join("?" for _ in clean_ids)
                cursor.execute(
                    f"""
                    SELECT {", ".join(_CARD_COLUMNS)}
                    FROM knowledge_index
                    WHERE status IN ({ACTIVE_STATUS_SQL}, '{KnowledgeStatus.DRAFT.value}') AND id IN ({placeholders});
                    """,
                    clean_ids,
                )
                rows = cursor.fetchall()
        except sqlite3.OperationalError as e:
            _raise_sqlite_unavailable(
                e,
                warn_template="SQLite OperationalError in get_cards_by_ids: %s",
                debug_context="get_cards_by_ids",
                error_message="Ошибка выборки карточек по ID",
            )

        return self._rows_to_cards(rows)

    def list_recent_cards(
        self,
        since: str | None = None,
        task_id: str | None = None,
        source: str | None = "auto_inferred",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Возвращает свежие карточки-кандидаты для саморевью (review_knowledge).

        Отбирает активные карточки (active/verified/suspect) по фильтрам:
        task_id (точная привязка к сессии) и/или created_at >= since (сравнение на уровне даты).
        Сортировка: новые сверху.
        """
        limit = max(1, min(limit, 50))
        since_date = since.split("T")[0] if since else None

        try:
            self.sync()
            with self.get_connection() as conn:
                cursor = conn.cursor()
                conditions = [f"status IN ({ACTIVE_STATUS_SQL}, '{KnowledgeStatus.DRAFT.value}')"]
                params: list[Any] = []
                if task_id:
                    conditions.append("task_id = ?")
                    params.append(task_id)
                if source:
                    conditions.append("source = ?")
                    params.append(source)

                cursor.execute(
                    f"""
                    SELECT {", ".join(_CARD_COLUMNS)}
                    FROM knowledge_index
                    WHERE {" AND ".join(conditions)}
                    ORDER BY created_at DESC
                    LIMIT 200;
                    """,
                    params,
                )
                rows = cursor.fetchall()
        except sqlite3.OperationalError as e:
            _raise_sqlite_unavailable(
                e,
                warn_template="SQLite OperationalError in list_recent_cards: %s",
                debug_context="list_recent_cards",
                error_message="Ошибка выборки свежих карточек",
            )

        result: list[dict[str, Any]] = []
        for card in self._rows_to_cards(rows):
            if since_date:
                card_date = str(card.get("created_at") or "").split("T")[0]
                if not card_date or card_date < since_date:
                    continue
            result.append(card)
            if len(result) >= limit:
                break
        return result

    def find_similar_cards(
        self,
        card_id: str,
        category: str,
        symptom: str,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Ищет похожие активные карточки по симптому в той же категории (для саморевью на дубли).

        Возвращает список кандидатов с id, title и усечённым сниппетом симптома, исключая саму карточку.
        """
        limit = max(1, min(limit, 5))
        fts_query = _build_symptom_fts_query(symptom)
        if fts_query is None:
            return []

        try:
            self.sync()
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"""
                    SELECT id, title, symptom
                    FROM knowledge_index
                    WHERE knowledge_index MATCH ? AND category = ? AND status IN ({ACTIVE_STATUS_SQL}, '{KnowledgeStatus.DRAFT.value}')
                      AND id != ?
                    ORDER BY bm25(knowledge_index, {BM25_WEIGHTS_SQL}) ASC
                    LIMIT ?;
                    """,
                    (fts_query, category, card_id, limit),
                )
                rows = cursor.fetchall()
        except sqlite3.OperationalError as e:
            _raise_sqlite_unavailable(
                e,
                warn_template="SQLite OperationalError in find_similar_cards: %s",
                debug_context="find_similar_cards",
                error_message="Ошибка поиска похожих карточек",
            )

        return [
            {
                "id": str(row[0]),
                "title": str(row[1] or ""),
                "symptom_snippet": str(row[2] or "")[:200],
            }
            for row in rows
        ]

    def list_open_hypotheses(
        self,
        task_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Возвращает открытые гипотезы (status='hypothesis') с опциональным фильтром по task_id."""
        limit = max(1, min(limit, 50))
        try:
            self.sync()
            with self.get_connection() as conn:
                cursor = conn.cursor()
                if task_id:
                    cursor.execute(
                        """
                        SELECT id, title, task_id, category, created_at
                        FROM knowledge_index
                        WHERE status = ? AND task_id = ?
                        ORDER BY created_at DESC
                        LIMIT ?;
                        """,
                        (KnowledgeStatus.HYPOTHESIS.value, task_id, limit),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT id, title, task_id, category, created_at
                        FROM knowledge_index
                        WHERE status = ?
                        ORDER BY created_at DESC
                        LIMIT ?;
                        """,
                        (KnowledgeStatus.HYPOTHESIS.value, limit),
                    )
                rows = cursor.fetchall()
        except sqlite3.OperationalError as e:
            _raise_sqlite_unavailable(
                e,
                warn_template="SQLite OperationalError in list_open_hypotheses: %s",
                debug_context="list_open_hypotheses",
                error_message="Ошибка выборки открытых гипотез",
            )

        return [
            {
                "id": str(row[0]),
                "title": str(row[1] or ""),
                "task_id": row[2],
                "category": str(row[3] or ""),
                "created_at": str(row[4] or ""),
            }
            for row in rows
        ]

    def check_duplicate(self, req: CardRecordRequest) -> str | None:
        """Проверяет наличие похожих карточек (двухстадийная дедупликация: FTS5 recall + точный overlap).

        Возвращает ID существующей карточки, если найдено высокое сходство, либо None.
        """
        if req.force and req.confirmed_force:
            return None

        cat_val = (
            req.category.value
            if hasattr(req.category, "value")
            else str(req.category).strip().lower()
        )

        try:
            self.sync()

            # Стадия 1 (Recall кандидатов через FTS5 BM25):
            # Поисковый запрос строится по связке title + symptom + aliases + tags
            fields = {
                "title": req.title,
                "symptom": req.symptom,
                "aliases": " ".join(req.aliases) if req.aliases else "",
                "tags": " ".join(req.tags) if req.tags else "",
            }
            fts_query = build_multi_field_fts_query(fields)
            if fts_query is None:
                return None

            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"""
                    SELECT id, title, symptom, aliases, category, bm25(knowledge_index, {BM25_WEIGHTS_SQL}) as score
                    FROM knowledge_index
                    WHERE knowledge_index MATCH ? AND status IN ({ACTIVE_STATUS_SQL}, '{KnowledgeStatus.DRAFT.value}')
                    ORDER BY score ASC
                    LIMIT 10;
                    """,
                    (fts_query,),
                )
                rows = cursor.fetchall()

                # Стадия 2: точное решение по token-overlap и семантическим совпадениям
                req_title_clean = req.title.strip().lower()
                req_symptom_clean = req.symptom.strip().lower()
                req_aliases_set = {a.strip().lower() for a in req.aliases if a and a.strip()}

                for row in rows:
                    candidate_id = str(row[0])
                    candidate_title = str(row[1] or "")
                    candidate_symptom = str(row[2] or "")
                    candidate_aliases_str = str(row[3] or "")
                    candidate_category = str(row[4] or "").strip().lower()
                    score = float(row[5])

                    cand_title_clean = candidate_title.strip().lower()
                    cand_sym_clean = candidate_symptom.strip().lower()

                    # 1. Точные совпадения по заголовку или симптому
                    if req_symptom_clean == cand_sym_clean:
                        return candidate_id
                    if candidate_category == cat_val and req_title_clean == cand_title_clean:
                        return candidate_id

                    # 2. Метрики сходства
                    title_overlap, min_t_tokens = _symptom_similarity(req.title, candidate_title)
                    sym_overlap, min_s_tokens = _symptom_similarity(req.symptom, candidate_symptom)

                    cand_aliases_set = {
                        a.strip().lower()
                        for a in re.split(r"[\s,]+", candidate_aliases_str)
                        if a and a.strip()
                    }
                    common_aliases = req_aliases_set & cand_aliases_set

                    # 3. Внутри своей категории
                    if candidate_category == cat_val:
                        if (
                            sym_overlap >= DUP_SYMPTOM_THRESHOLD
                            and min_s_tokens >= MIN_TOKENS_FOR_OVERLAP
                        ) or (sym_overlap >= DUP_DETAIL_THRESHOLD and score < DUP_PENALTY):
                            # Ревью r10 (остаточный JSvsTS на симптоме):
                            # высокий overlap симптома ещё не доказывает
                            # дубликат, если в уникальных токенах симптомов
                            # есть различитель ТЕХНОЛОГИЙ (javascript vs
                            # typescript — разные стеки при равном остальном).
                            # Обычные перефразирования («транскрибация» vs
                            # «обработка») различителем не считаются — их
                            # покрывает fuzzy-порог overlap. Проверяем только
                            # известные канонические термины стека/СУБД.
                            sym_uniq_req = _morpho_diff(
                                _tokenize(req.symptom), _tokenize(candidate_symptom)
                            )
                            sym_uniq_cand = _morpho_diff(
                                _tokenize(candidate_symptom), _tokenize(req.symptom)
                            )
                            if _symptom_uniques_have_tech_differentiator(
                                sym_uniq_req, sym_uniq_cand
                            ):
                                # Различитель технологий в симптоме —
                                # это не доказанный дубликат.
                                continue
                            return candidate_id

                        if title_overlap >= 0.6 and min_t_tokens >= 3:
                            return candidate_id

                        # K10: одна длинная generic-алиаса («python») — не
                        # доказательство дубликата: требуем >= 2 общих,
                        # как и в кросс-категориальном поиске (ветка 4).
                        if len(common_aliases) >= 2:
                            return candidate_id

                    # 4. Кросс-категориальный поиск (повышенный порог: совпадение и заголовка, и симптома >= 0.75, либо симптом >= 0.9)
                    else:
                        if (
                            title_overlap >= 0.75 and sym_overlap >= 0.75 and min_s_tokens >= 5
                        ) or (sym_overlap >= 0.9 and min_s_tokens >= 5):
                            return candidate_id
                        if len(common_aliases) >= 2:
                            return candidate_id

        except sqlite3.OperationalError as e:
            clean_symptom = sanitize_fts_query(req.symptom)
            _raise_sqlite_unavailable(
                e,
                warn_template=(
                    "SQLite OperationalError during check_duplicate(symptom=%r, category=%r): %s"
                ),
                warn_params=(clean_symptom, cat_val),
                debug_context="check_duplicate",
                error_message="Ошибка проверки дубликатов",
            )

        return None

    def check_duplicate_hypothesis(self, req: HypothesisRecordRequest) -> str | None:
        """Проверяет наличие похожих активных гипотез по симптому и сути гипотезы в той же категории."""
        if req.force:
            return None

        cat_val = (
            req.category.value
            if hasattr(req.category, "value")
            else str(req.category).strip().lower()
        )

        try:
            self.sync()

            fts_query = _build_symptom_fts_query(req.symptom)
            if fts_query is None:
                return None

            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"""
                    SELECT id, symptom, root_cause, bm25(knowledge_index) as score
                    FROM knowledge_index
                    WHERE knowledge_index MATCH ? AND category = ? AND status = '{KnowledgeStatus.HYPOTHESIS.value}'
                    ORDER BY score ASC
                    LIMIT 5;
                    """,
                    (fts_query, cat_val),
                )
                rows = cursor.fetchall()
                for row in rows:
                    candidate_id, candidate_symptom, candidate_root_cause, score = (
                        row[0],
                        row[1],
                        row[2],
                        row[3],
                    )
                    # Проверяем степень сходства симптомов
                    symptom_exact = req.symptom.strip().lower() == candidate_symptom.strip().lower()

                    symptom_matched = symptom_exact
                    if not symptom_matched:
                        overlap, min_tokens = _symptom_similarity(req.symptom, candidate_symptom)
                        if min_tokens and (
                            (
                                overlap >= HYP_SYMPTOM_THRESHOLD
                                and min_tokens >= MIN_TOKENS_FOR_OVERLAP
                            )
                            or (overlap >= HYP_DETAIL_THRESHOLD and score < HYP_PENALTY)
                        ):
                            symptom_matched = True

                    if not symptom_matched:
                        continue

                    # Если симптом совпал, проверяем суть гипотезы.
                    # Разные конкурирующие гипотезы для одного симптома НЕ являются дубликатами.
                    # K-HYP (aud-backend-7951): устраняем асимметрию с confirm-путём —
                    # вызываем classify_confirm_candidate для фильтрации токенов симптома
                    # и морфологического анализа причин.
                    classify_verdict = self.classify_confirm_candidate(
                        hypo_symptom=req.symptom,
                        hypo_reason=req.hypothesis,
                        cand_symptom=candidate_symptom,
                        cand_root_cause=str(candidate_root_cause or ""),
                    )
                    if classify_verdict == "distinct":
                        continue
                    if classify_verdict == "duplicate":
                        return str(candidate_id)

                    # Для uncertain сверяем overlap по токенам причин за вычетом симптомов
                    s_tokens = _tokenize(req.symptom) | _tokenize(candidate_symptom)
                    h_tokens_hypo = _tokenize(req.hypothesis) - s_tokens
                    h_tokens_cand = _tokenize(str(candidate_root_cause or "")) - s_tokens
                    if h_tokens_hypo and h_tokens_cand:
                        overlap = len(h_tokens_hypo & h_tokens_cand) / min(
                            len(h_tokens_hypo), len(h_tokens_cand)
                        )
                        if overlap < ANTIPATTERN_REFUTED_THRESHOLD:
                            continue

                    hypo_overlap, min_hypo_tokens = _symptom_similarity(
                        req.hypothesis, str(candidate_root_cause or "")
                    )
                    # Если гипотезы предполагают принципиально разные вещи, это не дубликат
                    if (
                        min_hypo_tokens >= MIN_HYPO_TOKENS_FOR_OVERLAP
                        and hypo_overlap < ANTIPATTERN_REFUTED_THRESHOLD
                    ):
                        continue

                    return str(candidate_id)
        except sqlite3.OperationalError as e:
            clean_symptom = sanitize_fts_query(req.symptom)
            _raise_sqlite_unavailable(
                e,
                warn_template=(
                    "SQLite OperationalError during "
                    "check_duplicate_hypothesis(symptom=%r, category=%r): %s"
                ),
                warn_params=(clean_symptom, cat_val),
                debug_context="check_duplicate_hypothesis",
                error_message="Ошибка проверки дубликатов гипотез",
            )
        return None

    classify_confirm_candidate = staticmethod(classify_confirm_candidate)

    def classify_confirm(
        self,
        category: str | KnowledgeCategory,
        symptom: str,
        hypo_reason: str,
        exclude_id: str | None = None,
    ) -> tuple[Literal["duplicate", "distinct", "uncertain"], str | None]:
        """Ищет кандидатов среди active и draft карточек и классифицирует их по симптому и причине."""
        cat_val = category.value if hasattr(category, "value") else str(category).strip().lower()
        self.sync()

        fts_query = _build_symptom_fts_query(symptom)
        if fts_query is None:
            return "distinct", None

        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"""
                    SELECT id, symptom, root_cause, bm25(knowledge_index) as score
                    FROM knowledge_index
                    WHERE knowledge_index MATCH ? AND category = ?
                      AND status IN ({ACTIVE_STATUS_SQL}, '{KnowledgeStatus.DRAFT.value}')
                    ORDER BY score ASC
                    LIMIT 10;
                    """,
                    (fts_query, cat_val),
                )
                rows = cursor.fetchall()
                uncertain_id: str | None = None

                for row in rows:
                    cand_id = str(row[0])
                    cand_symptom = str(row[1] or "")
                    cand_root_cause = str(row[2] or "")
                    if exclude_id and cand_id == exclude_id:
                        continue
                    res = self.classify_confirm_candidate(
                        hypo_symptom=symptom,
                        hypo_reason=hypo_reason,
                        cand_symptom=cand_symptom,
                        cand_root_cause=cand_root_cause,
                    )
                    if res == "duplicate":
                        return "duplicate", cand_id
                    if res == "uncertain" and uncertain_id is None:
                        uncertain_id = cand_id

                if uncertain_id is not None:
                    return "uncertain", uncertain_id
                return "distinct", None
        except sqlite3.OperationalError:
            return "distinct", None

    def check_active_card_consistency(
        self, category: str | None = None
    ) -> list[tuple[str, str, str]]:
        """Проверяет базу на наличие активных карточек с похожими симптомами в одной категории."""
        self.sync()
        inconsistencies: list[tuple[str, str, str]] = []
        with self.get_connection() as conn:
            cursor = conn.cursor()
            sql = f"""
                SELECT id, category, symptom
                FROM knowledge_index
                WHERE status IN ({ACTIVE_OR_VERIFIED_STATUS_SQL})
            """
            params: list[Any] = []
            if category:
                sql += " AND category = ?"
                params.append(category.strip().lower())
            cursor.execute(sql, params)
            cards = cursor.fetchall()

            for i in range(len(cards)):
                for j in range(i + 1, len(cards)):
                    id1, cat1, sym1 = cards[i]
                    id2, cat2, sym2 = cards[j]
                    if cat1 != cat2 or not sym1 or not sym2:
                        continue
                    overlap, min_tokens = _symptom_similarity(sym1, sym2)
                    if (
                        overlap >= ANTIPATTERN_CONFIRMED_THRESHOLD
                        and min_tokens >= MIN_TOKENS_FOR_OVERLAP
                    ):
                        inconsistencies.append((str(id1), str(id2), str(cat1)))
                        logger.info(
                            "Обнаружены похожие активные карточки в категории '%s': '%s' и '%s'",
                            cat1,
                            id1,
                            id2,
                        )
        return inconsistencies
