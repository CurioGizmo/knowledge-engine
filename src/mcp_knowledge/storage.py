"""Хранилище SQLite FTS5 кэша и автосинхронизации для MCP Knowledge Engine.

Обеспечивает полнотекстовый поиск, BM25-ранжирование, авто-переиндексацию по mtime,
детекцию дубликатов и стандарт SQLite Concurrency (WAL, busy_timeout=5000).
"""

import re
import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Literal

from mcp_core.db import build_bm25_weights_sql
from mcp_core.db import get_connection as core_get_connection
from mcp_core.fts import SCHEMA_VERSION, fts5_table_sql, resolve_tokenizer, round_bm25_score
from mcp_core.fuzzy import (
    FUZZY_CANDIDATE_LIMIT,
    FUZZY_THRESHOLD,
    jaccard_word_best,
    short_acronym_matches,
)
from mcp_core.rerank import SearchHit, matched_column_flags, rerank_hits
from mcp_core.sanitizer import stem_russian
from mcp_core.schema_guard import FtsSpec, StorageSchemaSpec, ensure_storage_schema
from mcp_knowledge.db_guard import _raise_sqlite_unavailable
from mcp_knowledge.errors import DuplicateCardError
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
    extract_short_acronym_tokens,
    sanitize_fts_query,
)
from mcp_knowledge.synchronizer import calculate_card_confidence
from mcp_knowledge.telemetry import (
    TOOL_CALLS_CREATE_SQL,
    TelemetryMixin,
    read_tool_call_stats,
)

# Бюджет строк постраничного fuzzy-скана (канал 2 в _collect_fuzzy_knowledge_rows):
# хвост таблицы обязан быть достижим (регрессия unordered LIMIT 200), но скан
# гигантских таблиц ограничен сверху и не безлимитен.
FUZZY_SCAN_ROW_BUDGET: int = 2000

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
    1.5,
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
    "MAX_TOTAL_SNIPPETS",
    "DuplicateCardError",
    "KnowledgeStorage",
    "read_tool_call_stats",
]

# Пороги token-overlap для детекции дубликатов (аудит H3: именованные константы
# вместо магических чисел). Значения сохранены 1:1 с прежним кодом.
DUP_SYMPTOM_THRESHOLD = 0.6  # check_duplicate: сильное совпадение симптомов
DUP_DETAIL_THRESHOLD = 0.5  # check_duplicate: совпадение при сильном BM25-скоре
DUP_PENALTY = -0.1  # check_duplicate: порог «сильного» BM25-скора
HYP_SYMPTOM_THRESHOLD = 0.7  # check_duplicate_hypothesis: совпадение симптомов
HYP_DETAIL_THRESHOLD = 0.6  # check_duplicate_hypothesis: совпадение при сильном BM25-скоре
HYP_PENALTY = -0.2  # check_duplicate_hypothesis: порог «сильного» BM25-скора
ANTIPATTERN_REFUTED_THRESHOLD = 0.45  # гипотезы: разные сути — не дубликат
ANTIPATTERN_CONFIRMED_THRESHOLD = 0.75  # check_active_card_consistency: похожие активные карточки

# Минимальная длина (в токенах) для доверия overlap-эвристике.
MIN_TOKENS_FOR_OVERLAP = 4
MIN_HYPO_TOKENS_FOR_OVERLAP = 3

# K-HYP (ревью r2): связочные/служебные слова, не доказывающие совпадение
# причин. При оценке «разные ли причины» они исключаются из собственных
# токенов причин наряду с токенами симптомов: в коротких формулировках
# («задержка из-за таймаута DNS» vs «задержка из-за таймаута пула»)
# именно такие слова создавали ложный distinct-overlap >= 0.45.
_REASON_STOPWORDS = frozenset(
    {
        # русские связки и связочные глаголы
        "и",
        "или",
        "не",
        "на",
        "из",
        "за",
        "от",
        "до",
        "по",
        "при",
        "под",
        "это",
        "весь",
        "все",
        "всех",
        "был",
        "была",
        "было",
        "быть",
        "когда",
        "тогда",
        "так",
        "также",
        "как",
        "что",
        "чтобы",
        "если",
        "возникает",
        "происходит",
        "вызван",
        "вызвана",
        "приводит",
        "приводят",
        "является",
        "появляется",
        # английские связки
        "and",
        "or",
        "not",
        "the",
        "a",
        "an",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "by",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "when",
        "then",
        "that",
        "this",
        "because",
        "due",
        "caused",
        "causes",
        "occurs",
        "happens",
    }
)

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


# Ревью r8/r9 (P2, 2-символьные алиасы): токенайзер отбрасывает слова
# короче 3 символов (`\w{3,}`), поэтому различитель «js»/«ts»/«py» в
# причинах («heap js переполнен...» vs «heap ts переполнен...») терялся
# и разные технологии давали ложный duplicate. Канонизация известных
# коротких алиасов ДО токенизации сохраняет различитель в наборе токенов.
_SHORT_ALIAS_CANON = {
    "js": "javascript",
    "ts": "typescript",
    "py": "python",
    "pg": "postgres",
}


def _canonicalize_short_aliases(text: str) -> str:
    """Заменяет известные 2-символьные алиасы каноническими формами."""
    return _SHORT_ALIAS_WORD_PATTERN.sub(lambda m: _SHORT_ALIAS_CANON[m.group(0).lower()], text)


_SHORT_ALIAS_WORD_PATTERN = re.compile(r"\b(?:js|ts|py|pg)\b", re.IGNORECASE)


def _tokenize(text: str) -> set[str]:
    """Разбивает текст на множество токенов (слова длиной 3+, lowercase).

    Короткие алиасы (js/ts/py) канонизируются до полного термина — см.
    ``_SHORT_ALIAS_CANON``: иначе они выпадают из набора токенов и причины
    разных технологий склеиваются в ложный duplicate.
    """
    return set(_TOKEN_PATTERN.findall(_canonicalize_short_aliases(text.lower())))


def _symptom_similarity(text_a: str, text_b: str) -> tuple[float, int]:
    """Считает overlap-сходство двух текстов по методу Jaccard-минимум.

    Возвращает (overlap, min_tokens). Если один из текстов пуст (0 токенов),
    возвращает (0.0, 0) — вычислять overlap нет смысла (инвариант прежнего кода).
    """
    tokens_a = _tokenize(text_a)
    tokens_b = _tokenize(text_b)
    if not tokens_a or not tokens_b:
        return 0.0, 0
    min_tokens = min(len(tokens_a), len(tokens_b))
    overlap = len(tokens_a & tokens_b) / min_tokens
    return overlap, min_tokens


def _clean_categories(categories: list[str] | None, limit: int = 5) -> list[str]:
    """Очищает и нормализует список категорий: strip+lower, дедупликация, срез до limit."""
    if not categories:
        return []
    return list(dict.fromkeys(d.strip().lower() for d in categories if d and d.strip()))[:limit]


# Ревью r4: хвосты-флексии для морфологической эквивалентности токенов.
# Флексия — короткое окончание (≤ 4 букв); общий префикс при этом должен
# оставаться значимой частью слова (≥ 3 символов). «переполнение» и
# «переписывание» имеют общий префикс «переп», но хвосты по 7-8 букв —
# это не флексии, а разные корни.
_RU_FLEXION_SUFFIXES = frozenset(
    {
        "а",
        "я",
        "ы",
        "и",
        "ов",
        "ев",
        "ах",
        "ях",
        "ей",
        "ой",
        "ам",
        "ям",
        "ом",
        "ем",
        "у",
        "ю",
        "е",
        "ь",
        "ий",
        "ый",
        "ая",
        "ые",
        "ое",
        "ее",
        "ет",
        "ут",
        "ют",
        "ат",
        "ят",
        "ся",
        "ась",
        "ось",
        "ется",
        "утся",
        "ются",
        "ится",
        "ятся",
        "ать",
        "ять",
        "ить",
        "ла",
        "ло",
        "ли",
        "л",
        "на",
        "но",
        "ны",
        "н",
        "т",
        "ешь",
        "ишь",
        "ит",
    }
)
_EN_FLEXION_SUFFIXES = frozenset({"s", "es", "ed", "ing", "en", "er", "est"})


def _unique_tokens_are_paronyms(tokens_a: set[str], tokens_b: set[str]) -> bool:
    """Проверяет, являются ли уникальные токены паронимами (похожие, но разные слова).

    Ревью r4 (п. 1/п. 2-разделитель): «переполнение» и «переписывание» —
    общий префикс «переп» (>= 5), но это разные слова (паронимы), а не
    формы одного слова. «переполнен» и «исчерпан» — несвязанные поверхности
    (префикс < 5): при высоком совпадении остального это синонимы.
    Ревью r8 (P2): алиасы одного термина (postgres/postgresql) — НЕ
    паронимы, несмотря на общий префикс: см. ``_reason_tokens_related``.
    """
    for a in tokens_a:
        for b in tokens_b:
            if _reason_tokens_related(a, b):
                # Ревью r8 (P2): алиасы/синонимы/формы одного термина
                # не являются паронимами.
                continue
            n = 0
            while n < min(len(a), len(b)) and a[n] == b[n]:
                n += 1
            if n >= 5 and not _tokens_morpho_equal(a, b):
                return True
    return False


def _has_paronym_in_full(unique_tokens: set[str], full_tokens: set[str]) -> bool:
    """Проверяет, есть ли у unique-токенов пароним в ПОЛНОМ наборе другой причины.

    Ревью r5 (D1): дифференцирующий токен («переписывается») может быть
    паронимом токена, вычтенного из другой стороны как симптомный
    («переполняется»). Причина кандидата «Кэш ответов переполняется из-за
    отсутствия вытеснения» содержит «переполняется» — пароним
    «переписывается» (префикс «переп»). Такое расхождение — замена
    похожего, но другого слова → причины различны.
    """
    for u in unique_tokens:
        for f in full_tokens:
            # Ревью r6 (N1): родственные формы (вложенные стемы:
            # «переполнен»/«переполняется», словарные синонимы) — НЕ
            # паронимы: это перефразирование одной причины.
            if _reason_tokens_related(u, f):
                continue
            n = 0
            while n < min(len(u), len(f)) and u[n] == f[n]:
                n += 1
            if n >= 5:
                return True
    return False


# Ревью r5 (D2): известные группы синонимов для позитивного доказательства
# «синонимичной перефразировки» одной причины. Расширяется по мере
# нахождения ложных uncertain в ревью; консервативный минимум.
# Ревью r6 (D1 верификации): пары заменены КЛАССАМИ — «переполняется»/
# «исчерпан» (кросс-форменные комбинации) раньше давали ложный distinct,
# потому что stem_russian("исчерпан") = None (суффикс «-ан» не срезается)
# и морфология не склеивала формы. Класс содержит все поверхностные формы
# одного термина; синонимия классов транзитивна для любых форм.
# Пример: классы {переполнен, переполнение, переполняется, ...} и
# {исчерпан, исчерпание, исчерпается, исчерпана, ...} синонимичны, значит
# «пул переполняется из-за утечки» == «пул исчерпан из-за утечки».
_REASON_SYNONYM_CLASSES: tuple[frozenset[str], ...] = (
    # ёмкость/ресурсы: переполнение == исчерпание == закончилась
    frozenset(
        {
            "переполнен",
            "переполняется",
            "переполнение",
            "переполнены",
            "переполнялся",
        }
    ),
    frozenset(
        {
            "исчерпан",
            "исчерпана",
            "исчерпание",
            "исчерпается",
            "исчерпаны",
            "исчерпался",
            "закончилась",
            "кончился",
        }
    ),
    # latency: медленный == низкий == туго
    frozenset({"медленный", "медлительный", "медленно", "низкий", "туго"}),
    # англ.
    frozenset({"slow", "sluggish", "laggy"}),
    frozenset({"fast", "quick", "rapid"}),
)
# Пары классов, между которыми любые формы считаются синонимами.
_REASON_SYNONYM_CLASS_LINKS = frozenset(
    {
        # переполнение == исчерпание (ёмкость/ресурсы)
        (0, 1),
        # медленный == низкий (внутри одного класса 2 уже), между 2 и 0/1 нет
    }
)
# Ревью r8 (P2): известные алиасы одного термина (названия технологий,
# СУБД и т.п.) — разные поверхности, обозначающие одну сущность.
# Паронимная эвристика (префикс >= 5) принимала их за разные слова.
_REASON_ALIAS_PAIRS = frozenset(
    {
        tuple(sorted(pair))
        for pair in (
            ("postgres", "postgresql"),
            ("postgres", "pgsql"),
            ("pgsql", "postgresql"),
            ("postgres", "pg"),
            ("mongo", "mongodb"),
            ("sqlite", "sqlite3"),
            ("memcache", "memcached"),
            ("node", "nodejs"),
            ("k8s", "kubernetes"),
            ("memory", "ram"),
            ("cpu", "процессор"),
            # Ревью r11 (п. 1): mysql и mariadb — РАЗНЫЕ СУБД (форк
            # разошёлся), не алиасы: confirm не должен считать разные
            # причины дублем, а duplicate_of — закрывать гипотезу как
            # superseded. Оба остаются известными tech-терминами для
            # различителя (см. _KNOWN_TECH_TERMS).
            # js/ts/py канонизируются до полных форм в _tokenize —
            # см. _SHORT_ALIAS_CANON; javascript/typescript/python
            # связаны с алиасами автоматически.
        )
    }
)
# Плоский словарь пар поверхностных форм для быстрого lookup: строится из
# декартова произведения форм связанных классов + всех пар внутри класса
# (морфология одного слова).
_REASON_SYNONYM_PAIRS = frozenset(
    pair
    for i, class_a in enumerate(_REASON_SYNONYM_CLASSES)
    for j, class_b in enumerate(_REASON_SYNONYM_CLASSES)
    if i == j or (min(i, j), max(i, j)) in _REASON_SYNONYM_CLASS_LINKS
    for a in class_a
    for b in class_b
    if a != b
    for pair in (((a, b) if a < b else (b, a)),)
)


def _all_unique_pairs_symmetrically_related(tokens_a: set[str], tokens_b: set[str]) -> bool:
    """Проверяет, что ВСЕ unique-токены ОБЕИХ сторон имеют пару.

    Ревью r5 (D2): позитивное доказательство «синонимичной перефразировки» —
    морфологическое родство (равные/вложенные стемы: «переполнен»/
    «переполняются» → «переполн») ИЛИ известная пара синонимов
    (``_REASON_SYNONYM_PAIRS``: «переполнен»/«исчерпан»). Антонимы
    («медленный»/«быстрый») и произвольные замены стемов не проходят →
    uncertain, а не duplicate.
    Ревью r6 (B2/N2): проверка симметрична — каждый unique-токен tokens_b
    тоже обязан иметь пару в tokens_a. Раньше односторонняя проверка
    объявляла дублем причину с независимым доп. термином («... при утечке
    соединений»), которого нет у гипотезы.
    """
    return _reason_tokens_all_matched(tokens_a, tokens_b) and (
        _reason_tokens_all_matched(tokens_b, tokens_a)
    )


def _verdict_equal_reasons(
    reason: str,
    hypo_symptom: str,
    cand_symptom: str,
) -> Literal["duplicate", "uncertain"]:
    """Вердикт для дословно совпадающих текстов причин.

    Ревью r6 (G1): причина, целиком состоящая из слов симптома и связок,
    причинных данных не содержит — совпадение пустых по смыслу текстов
    не доказывает дубликат. Дубль только при наличии собственных
    содержательных токенов.
    """
    equal_symptom_tokens = _tokenize(hypo_symptom) | _tokenize(cand_symptom)
    own_tokens = _tokenize(reason) - equal_symptom_tokens - _REASON_STOPWORDS
    if own_tokens:
        return "duplicate"
    return "uncertain"


def _symptom_uniques_have_tech_differentiator(uniq_req: set[str], uniq_cand: set[str]) -> bool:
    """Проверяет, есть ли в уникальных токенах симптомов различитель технологий.

    Ревью r10 (остаточный JSvsTS на симптоме): «утечка памяти
    js-приложения» vs «...ts-приложения» — после канонизации различитель
    javascript/typescript остаётся в уникальных токенах обеих сторон и
    относится к РАЗНЫМ каноническим терминам (не alias-паре, не морфологии).
    Обычные перефразирования («транскрибация» vs «обработка») — не
    различители: неизвестные гэпу поверхности покрываются fuzzy-порогом.
    """
    if not uniq_req or not uniq_cand:
        return False
    for a in uniq_req:
        for b in uniq_cand:
            if _reason_tokens_related(a, b):
                # Формы/синонимы/алиасы одного термина — не различитель.
                continue
            # Ревью r10 (верификация): различитель — ОБА токена пары
            # известные термины стека/СУБД (postgres vs mysql,
            # javascript vs typescript). Односторонняя проверка
            # («известный + произвольное перефразирование»: «переполнен»
            # vs «забит») пропускала настоящие дубли — прилагательные
            # из словарей синонимов не являются tech-различителями.
            if a in _KNOWN_TECH_TERMS and b in _KNOWN_TECH_TERMS:
                return True
    return False


# Канонические формы терминов стека/СУБД для различителя симптомов:
# только поверхности алиасов и канонические формы коротких алиасов
# (js/ts/py/pg → javascript/typescript/python/postgres). Классы
# синонимов причин (переполнен/исчерпан/медленный/...) НЕ входят —
# это описательные прилагательные, а не термины технологий.
# Ревью r11 (п. 3): пары независимых терминов (mysql без mariadb-связи)
# тоже должны быть известными различителями — отдельный список.
_ALIAS_SURFACE_FORMS = frozenset(term for pair in _REASON_ALIAS_PAIRS for term in pair) | set(
    _SHORT_ALIAS_CANON.values()
)
_INDEPENDENT_TECH_TERMS = frozenset(
    {
        # Ревью r11 (п. 1): mysql/mariadb — разные СУБД; каждый термин
        # известен независимо, но парой НЕ связан.
        "mysql",
        "mariadb",
    }
)
_KNOWN_TECH_TERMS = _ALIAS_SURFACE_FORMS | _INDEPENDENT_TECH_TERMS


def _distinct_reason_tokens(
    tokens_hypo: set[str],
    tokens_cand: set[str],
    symptom_tokens: set[str],
) -> tuple[set[str], set[str]]:
    """Вычисляет собственные содержательные токены причин (минус симптомные).

    K-HYP (ревью r2): причины часто «пересыпаны» словами симптома и
    связками («задержка ответа сервера из-за таймаута DNS-резолвера») —
    сравниваются только собственные термины каждой стороны.
    """
    return (
        tokens_hypo - symptom_tokens - _REASON_STOPWORDS,
        tokens_cand - symptom_tokens - _REASON_STOPWORDS,
    )


def _classify_empty_distinct_reasons(
    tokens_hypo: set[str], tokens_cand: set[str]
) -> Literal["duplicate", "distinct", "uncertain"]:
    """Классифицирует случай, когда обе причины не имеют собственных токенов.

    K-HYP (ревью r2, дефект A): причины повторяют симптом. Дифференцирующий
    термин может лежать В СИМПТОМЕ («...из-за таймаута DNS-резолвера» vs
    «...таймаута пула соединений»), поэтому сравниваются ПОЛНЫЕ наборы
    токенов причин. Ревью r6 (G1): полное совпадение пустых по смыслу
    причин — НЕ доказательство дубликата: причинных данных нет.
    Ревью r8 (P1): сравнение полных наборов должно учитывать и словарные
    синонимы («пул переполнен при утечке» vs «пул исчерпан при утечке»
    при симптоме, содержащем оба слова) — не только строгую морфологию.
    """
    full_diff_hypo = _morpho_diff(tokens_hypo, tokens_cand)
    full_diff_cand = _morpho_diff(tokens_cand, tokens_hypo)
    if not full_diff_hypo and not full_diff_cand:
        # Ревью r6 (G1): обе причины дословно повторяют симптом — причинных
        # данных нет вовсе, совпадение причин не доказано (нечем доказывать).
        # Прежний duplicate позволял закрыть гипотезу как дубликат без
        # единого причинного токена; правильный вердикт — uncertain.
        return "uncertain"
    if full_diff_hypo and full_diff_cand:
        # Ревью r11 (п. 2): расхождения в этой ветке — слова СИМПТОМОВ
        # (distinct-наборы обеих причин пусты, собственных терминов нет).
        # Синонимная замена («переполнен»→«исчерпан», оба слова в симптоме)
        # не добавляет причинных сведений — как и дословное совпадение
        # пустых причин (G1), это НЕ доказывает дубль → uncertain.
        # Прежний duplicate позволял закрыть гипотезу (confirm/duplicate_of)
        # без доказанной причины.
        if _all_unique_pairs_symmetrically_related(full_diff_hypo, full_diff_cand):
            return "uncertain"
        # Каждая причина содержит термин, отсутствующий у другой —
        # дифференциатор в симптоме (r2, дефект A: «dns» vs «пула»).
        return "distinct"
    # Одна причина вкладывается в другую — данных недостаточно.
    return "uncertain"


def _reason_tokens_related(a: str, b: str) -> bool:
    """Проверяет, являются ли два токена формами/синонимами одной сущности.

    Ревью r6: единый критерий «позитивного доказательства» совпадения причин:
    морфологическая эквивалентность (``_tokens_morpho_equal``), словарная
    пара синонимов (``_REASON_SYNONYM_PAIRS``) или вложенные стемы
    (``переполнен``/``переполняется`` → ``переполн`` ⊂ ``переполняетс``).
    Антонимы («медленный»/«быстрый») и паронимы («переполнение»/
    «переписывание») сюда НЕ входят — это разные термины.
    Ревью r8 (P2): алиасы одного термина (postgres/postgresql) —
    одна сущность (``_REASON_ALIAS_PAIRS``).
    """
    if _tokens_morpho_equal(a, b):
        return True
    if ((a, b) if a < b else (b, a)) in _REASON_SYNONYM_PAIRS:
        return True
    if ((a, b) if a < b else (b, a)) in _REASON_ALIAS_PAIRS:
        return True
    stem_a = stem_russian(a)
    stem_b = stem_russian(b)
    # Одинаковые или вложенные стемы (переполн/переполняетс)
    return bool(
        stem_a is not None
        and stem_b is not None
        and (stem_a == stem_b or stem_a.startswith(stem_b) or stem_b.startswith(stem_a))
    )


def _reason_tokens_all_matched(tokens: set[str], other: set[str]) -> bool:
    """Проверяет, что каждый токен одного набора имеет эквивалент в другом.

    Ревью r6 (B1/N1): пара «переполняется»/«переполнен» (родственные
    глагольные формы) и словарная пара «переполнен»/«исчерпан» — одна
    причина, но ранний overlap-гейт видел сырые разные поверхности и
    давал ложный distinct. Совпавшими считаются также пары, связанные
    морфологией или словарём синонимов (``_reason_tokens_related``).
    """
    for t in tokens:
        if t in other:
            continue
        if any(_reason_tokens_related(t, o) for o in other):
            continue
        return False
    return True


def _classify_single_difference(
    unique_hypo: set[str],
    unique_cand: set[str],
    overlap_distinct: float,
) -> Literal["duplicate", "distinct", "uncertain"]:
    """Классифицирует единственное расхождение при высоком совпадении остального.

    Ревью r4: «переполнен» vs «исчерпан» — синоним перефразирования; пароним
    («переполнение» vs «переписывание», общий префикс >= 5) — разные слова.
    Ревью r5 (D2): синонимия требует ПОЗИТИВНОГО доказательства (морфология
    или словарь ``_REASON_SYNONYM_PAIRS``); антонимы и произвольные замены
    дают ``uncertain``, а не ``duplicate``.
    Ревью r6 (B2/N2): позитивное доказательство синонимии ДОЛЖНО быть
    симметричным — каждый unique-токен ОБЕИХ сторон обязан иметь пару
    (морфологию или словарный синоним). Одностороннее совпадение, когда
    вторая причина добавляет независимый термин («...при утечке
    соединений»), доказательством совпадения причин не является →
    ``uncertain``.
    """
    # Ревью r6 (B1): overlap_distinct считается по строгому
    # морфологическому пересечению и не видит словарные синонимы:
    # «пул переполнен из-за утечки» vs «пул исчерпан из-за утечки»
    # даёт 2/3 = 0.67 < 0.75 → ложный distinct, хотя единственное
    # расхождение — словарная пара. Related-пары (морфология/синонимы/
    # вложенные стемы) снимают ранний distinct-гейт.
    related_pairs = {
        (a, b) if a < b else (b, a)
        for a in unique_hypo
        for b in unique_cand
        if a != b and _reason_tokens_related(a, b)
    }
    if overlap_distinct < ANTIPATTERN_CONFIRMED_THRESHOLD and not related_pairs:
        # Каждая причина содержит собственный термин, отсутствующий
        # у другой — причины доказуемо различны. Ревью r6 (B1): словарная
        # пара («переполнен»/«исчерпан») — тоже совпадение, гейт на ней
        # не срабатывает.
        return "distinct"
    if _unique_tokens_are_paronyms(unique_hypo, unique_cand):
        # Паронимы при высоком совпадении остального —
        # разные термины, причины различны.
        return "distinct"
    # Ревью r11 (п. 3): пара известных РАЗНЫХ терминов стека/СУБД
    # (postgres vs mysql) — доказуемое различие причин, а не
    # «недостаточность данных»: confirm должен создавать draft (или
    # блокироваться distinct-путём), а не топтаться в uncertain.
    if _symptom_uniques_have_tech_differentiator(unique_hypo, unique_cand):
        return "distinct"
    # Ревью r5 (D2): без позитивного доказательства синонимии
    # (антонимы «медленный»/«быстрый», произвольные замены)
    # объявлять дублем нельзя — но и различие не доказано.
    # Ревью r6 (B2/N2): доказательство симметрично по обеим сторонам —
    # только морфологическое родство (равные стемы) ИЛИ словарная пара
    # синонимов для КАЖДОГО unique-токена с ОБЕИХ сторон; иначе —
    # uncertain, а не duplicate.
    if _all_unique_pairs_symmetrically_related(unique_hypo, unique_cand):
        return "duplicate"
    return "uncertain"


def _tokens_morpho_equal(a: str, b: str) -> bool:
    """Проверяет, являются ли два токена формами одного слова.

    K-HYP (ревью r2/r4): «пуст» и «пустой», «кэш» и «кэши» — формы одного
    слова; «переполнение» и «переписывание» — разные слова (раньше ложно
    склеивались 5-символьным префиксом), «пул переполнен» и «пул исчерпан» —
    разные термины.

    Критерий (гибридный):
    1. Одинаковые стемы русского стеммера (``stem_russian`` из mcp_core)
       — формы одного слова.
    2. Общий префикс ≥ 3 символов, и ОБА оставшихся хвоста — известные
       флексии (≤ 4 букв): «цикл-е»/«цикл-ах», «переполня-ется»/
       «переполня-ются».
    3. Короткое слово — ЦЕЛИКОМ префикс длинного, а остаток — флексия:
       «воркер»/«воркеры», «worker»/«workers».
    """
    if a == b:
        return True

    stem_a = stem_russian(a)
    stem_b = stem_russian(b)
    if stem_a is not None and stem_b is not None and stem_a == stem_b:
        return True

    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    tail_a = a[n:]
    tail_b = b[n:]
    is_cyrillic = any("а" <= c <= "я" for c in a) and any("а" <= c <= "я" for c in b)
    flex = _RU_FLEXION_SUFFIXES if is_cyrillic else _EN_FLEXION_SUFFIXES
    # Общий префикс — значимая часть слова, оба хвоста — флексии.
    if n >= 3 and tail_a in flex and tail_b in flex:
        return True
    # Короткое слово целиком — префикс длинного, остаток — флексия.
    if len(a) < len(b) and len(a) >= 3 and a == b[: len(a)]:
        return b[len(a) :] in flex
    if len(b) < len(a) and len(b) >= 3 and b == a[: len(b)]:
        return a[len(b) :] in flex
    return False


def _morpho_diff(tokens_a: set[str], tokens_b: set[str]) -> set[str]:
    """Токены A, не имеющие морфологического эквивалента в B."""
    result: set[str] = set()
    for a in tokens_a:
        if not any(_tokens_morpho_equal(a, b) for b in tokens_b):
            result.add(a)
    return result


def _morpho_intersection(tokens_a: set[str], tokens_b: set[str]) -> set[str]:
    """Токены A, имеющие морфологический эквивалент в B."""
    result: set[str] = set()
    for a in tokens_a:
        if any(_tokens_morpho_equal(a, b) for b in tokens_b):
            result.add(a)
    return result


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
    """
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
    """Постраничный полный скан knowledge_index устойчивым порядком (ORDER BY rowid).

    Канал 2 fuzzy-догона: хвост таблицы (rowid > FUZZY_CANDIDATE_LIMIT) обязан
    быть достижим — неупорядоченный LIMIT 200 отрезал релевантные карточки.
    Скан ограничен бюджетом FUZZY_SCAN_ROW_BUDGET строк (не безлимитен).
    """
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
        """
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
    """Выполняет fuzzy-догон кандидатов при нехватке MATCH-хитов.

    Канал отбора кандидатов совместим с unicode61-индексом (измерение плана E:
    триграммный MATCH по unicode61 возвращает 0 строк — мёртвый код):
    1) подстрочный LIKE-префильтр по title/symptom/solution/body (ловит опечатки
       в середине слова без триграммного индекса);
    2) постраничный скан всей таблицы устойчивым порядком (ORDER BY rowid) —
       неупорядоченный LIMIT 200 отрезал хвост (>200 карточек недостижимы).
    Скоринг и порог (Jaccard >= FUZZY_THRESHOLD) не изменились.
    """
    tokens_3 = [t for t in re.findall(r"[A-Za-zА-Яа-я0-9_]+", query) if len(t) >= 3]
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
        # Канал 1: подстрочный префильтр LIKE '%tok%' — unicode61-совместимый
        # способ recall опечаток в середине слова (измерен на контрольном корпусе).
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
        """
        try:
            cursor.execute(
                like_sql, [*extra_params, like_pattern, like_pattern, like_pattern, like_pattern]
            )
            _score_rows(cursor.fetchall())
        except sqlite3.OperationalError:
            continue

    # Канал 2: постраничный полный скан устойчивым порядком.
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

    # Фаза 2b: Точный fallback по 2-буквенным акронимам
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
    ) -> list[KnowledgeSnippet]:
        """Выполняет полнотекстовый поиск с BM25 ранжированием и сортировкой по статусу доверия."""
        limit = max(1, min(limit, 20))
        self.sync()

        and_query = sanitize_fts_query(query, operator="AND")
        or_query = sanitize_fts_query(query, operator="OR")
        if not and_query or and_query == '""':
            return []

        candidate_limit = max(limit * 3, 15)

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
            params_sql.append(limit)

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
                    params_sql_or.append(limit)
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
            return [self._row_to_snippet(r) for r in ordered_rows]

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
            source=KnowledgeSource(c_source),
            badge=badge,
            confidence=conf_val,
            task_id=str(c_task_id) if c_task_id else None,
            snippet=snippet_text,
            warning=warning,
            score=score_val,
        )

    def get_project_overview(self) -> ProjectOverview:
        """Собирает динамическую сводку тем проекта (Project Overview) и распределение карточек по категориям."""
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
            # task_id открывает свои draft/hypothesis-карточки — тот же контракт,
            # что и в _build_search_conditions (query-слой). Иначе агент не видит
            # свои черновики в своей задаче даже при явном task_id.
            status_cond = (
                f"(status IN ({ACTIVE_STATUS_SQL}) "
                f"OR (status IN ('{KnowledgeStatus.HYPOTHESIS.value}', '{KnowledgeStatus.DRAFT.value}') AND task_id = ?)) "
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
            if query and query.strip():
                target_category = categories[0] if categories and len(categories) == 1 else None
                raw_query_snippets = self.search(
                    query=query.strip(),
                    category=target_category,
                    include_archived=include_archived,
                    include_hypotheses=False,
                    include_drafts=include_drafts,
                    task_id=task_id,
                    limit=clamped_limit * 2,
                )
                for snippet in raw_query_snippets:
                    if categories and len(categories) > 1 and snippet.category not in categories:
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
        overview: ProjectOverview | None = self.get_project_overview() if include_overview else None

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

    @staticmethod
    def classify_confirm_candidate(
        hypo_symptom: str,
        hypo_reason: str,
        cand_symptom: str,
        cand_root_cause: str,
    ) -> Literal["duplicate", "distinct", "uncertain"]:
        """Классифицирует кандидата при confirm гипотезы.

        Сравнивает симптом гипотезы с симптомом кандидата,
        а текст предполагаемой причины гипотезы — с root_cause кандидата.

        K-HYP (ревью шага 3, раунд r2): общие токены СИМПТОМА «загрязняют»
        overlap текстов причин — «задержка из-за DNS» и «исчерпан пул
        PostgreSQL» на пересыпанных симптомом формулировках дают
        overlap >= ANTIPATTERN_REFUTED_THRESHOLD и ложно классифицируются
        как duplicate. Дополнительно (раунд r2): служебные/связочные слова
        («из-за», «таймаута», «возникает») в коротких наборах причин дают
        ложный distinct-overlap, а причина гипотезы, состоящая только из
        слов симптома, падала в fallthrough-duplicate. Решение:

        1. «Разные ли причины» оценивается по СОДЕРЖАТЕЛЬНЫМ собственным
           токенам причин — за вычетом токенов симптомов (обеих сторон)
           и связочных слов (``_REASON_STOPWORDS``).
        2. Если у причины гипотезы содержательных собственных токенов нет
           (она дословно повторяет симптом), доказательства совпадения
           тоже нет → ``uncertain``; если при этом и у кандидата их нет
           (обе причины = симптом) → истинный ``duplicate``.
        """
        s_hypo = hypo_symptom.strip().lower()
        s_cand = cand_symptom.strip().lower()
        symptom_matched = s_hypo == s_cand
        if not symptom_matched:
            overlap, min_tokens = _symptom_similarity(hypo_symptom, cand_symptom)
            symptom_matched = bool(
                min_tokens >= MIN_TOKENS_FOR_OVERLAP and overlap >= DUP_SYMPTOM_THRESHOLD
            )

        if not symptom_matched:
            return "distinct"

        tokens_hypo = _tokenize(hypo_reason or "")
        tokens_cand = _tokenize(cand_root_cause or "")
        if (
            len(tokens_hypo) < MIN_HYPO_TOKENS_FOR_OVERLAP
            or len(tokens_cand) < MIN_HYPO_TOKENS_FOR_OVERLAP
        ):
            return "uncertain"

        h_hypo = hypo_reason.strip().lower()
        h_cand = cand_root_cause.strip().lower()
        if h_hypo == h_cand:
            # Ревью r6 (G1): дословное совпадение текстов причин — дубль,
            # ТОЛЬКО если причина содержит собственные содержательные
            # токены вне слов симптома и связок (см. ``_verdict_equal_reasons``).
            return _verdict_equal_reasons(hypo_reason, hypo_symptom, cand_symptom)

        overlap_hypo, min_h_tokens = _symptom_similarity(hypo_reason, cand_root_cause)
        # Ревью r6 (B1/N1): ранний distinct-гейт не должен срабатывать на
        # перефразировании одного и того же состояния: родственные
        # глагольные формы («переполняется»/«переполнен») и словарные
        # синонимы («переполнен»/«исчерпан») считаются совпавшими, иначе
        # короткие причины («пул переполнен из-за утечки» vs «пул исчерпан
        # из-за утечки») дают ложный distinct при overlap < 0.45.
        raw_overlap_is_rephrase = _reason_tokens_all_matched(
            tokens_hypo, tokens_cand
        ) and _reason_tokens_all_matched(tokens_cand, tokens_hypo)
        if (
            min_h_tokens >= MIN_HYPO_TOKENS_FOR_OVERLAP
            and overlap_hypo < ANTIPATTERN_REFUTED_THRESHOLD
            and not raw_overlap_is_rephrase
            # Ревью r2 (дефект B): ранний distinct-выход пропускал вниз
            # перефразированные дубли с морфологией («кэш переполняется» vs
            # «кэши переполняются»): сырой overlap < порога, хотя причины
            # морфологически идентичны. Если ПОЧТИ все токены имеют
            # морфологический эквивалент (уникальных нет с обеих сторон),
            # ранний distinct не срабатывает — разбираем ниже.
            and (
                bool(_morpho_diff(tokens_hypo, tokens_cand))
                or bool(_morpho_diff(tokens_cand, tokens_hypo))
            )
        ):
            return "distinct"

        # K-HYP (r2): оценка «разные ли причины» по содержательным
        # собственным токенам — минус токены симптомов и связочные слова.
        symptom_tokens = _tokenize(hypo_symptom) | _tokenize(cand_symptom)
        distinct_hypo, distinct_cand = _distinct_reason_tokens(
            tokens_hypo, tokens_cand, symptom_tokens
        )
        if not distinct_hypo and not distinct_cand:
            # Обе причины не имеют собственных содержательных токенов
            # (повторяют симптомы). Различие всё же возможно: дифференцирующий
            # термин может лежать В СИМПТОМЕ (ревью r2, дефект A:
            # «...из-за таймаута DNS-резолвера» vs «...таймаута пула
            # соединений», где «dns» и «пула» входят и в симптомы).
            # Сравниваем полные наборы токенов причин.
            return _classify_empty_distinct_reasons(tokens_hypo, tokens_cand)
        if not distinct_hypo or not distinct_cand:
            # Причина одной из сторон дословно повторяет симптом, у другой
            # есть собственные токены — совпадение недоказуемо.
            return "uncertain"
        # Морфологическое сравнение собственных токенов причин:
        # «кэш»/«кэши», «переполняется»/«переполняются» — одна причина,
        # а «dns» vs «пула» — разные (ревью r2, дефект B).
        unique_hypo = _morpho_diff(distinct_hypo, distinct_cand)
        unique_cand = _morpho_diff(distinct_cand, distinct_hypo)
        matched = _morpho_intersection(distinct_hypo, distinct_cand)
        min_distinct = min(len(distinct_hypo), len(distinct_cand))
        overlap_distinct = len(matched) / min_distinct

        # Ревью r5 (D1): дифференциатор может быть паронимом токена,
        # вычтенного из другой стороны как симптомного («переписывается»
        # vs вычтенное «переполняется»). Проверяем unique-токены против
        # ПОЛНЫХ наборов причин другой стороны (включая симптомные вхождения).
        if unique_hypo and _has_paronym_in_full(unique_hypo, tokens_cand):
            return "distinct"
        if unique_cand and _has_paronym_in_full(unique_cand, tokens_hypo):
            return "distinct"

        if unique_hypo and unique_cand:
            # Ревью r4/r5: единственное расхождение при полном совпадении
            # остального — см. ``_classify_single_difference``.
            return _classify_single_difference(unique_hypo, unique_cand, overlap_distinct)
        # Ревью r6 (B2/N2): одна сторона добавила независимые термины
        # (unique есть ТОЛЬКО у неё). Совпадение причинных признаков не
        # доказано: кандидат может описывать ДРУГУЮ причину с теми же
        # ключевыми словами («... при утечке соединений»). Консервативно —
        # uncertain, а не fallthrough-duplicate.
        # Ревью r6 (N1): исключение — все односторонние unique-токены
        # related к ПОЛНОМУ набору другой причины (морфология/синонимы/
        # вложенные стемы: «переполнен» vs вычтенное как симптомное
        # «переполняется») — это перефразирование одной причины → дубль.
        if unique_hypo or unique_cand:
            if unique_hypo and _reason_tokens_all_matched(unique_hypo, tokens_cand):
                return "duplicate"
            if unique_cand and _reason_tokens_all_matched(unique_cand, tokens_hypo):
                return "duplicate"
            return "uncertain"
        if overlap_distinct < ANTIPATTERN_REFUTED_THRESHOLD:
            return "distinct"

        # Обе причины содержательно совпадают и не имеют уникальных
        # терминов — подтверждённый дубликат.
        return "duplicate"

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
