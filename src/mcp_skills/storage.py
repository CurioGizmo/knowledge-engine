"""Хранилище SQLite FTS5 кэша для MCP Skills Engine."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mcp_core.db import build_bm25_weights_sql
from mcp_core.db import get_connection as core_get_connection
from mcp_core.fts import SCHEMA_VERSION, fts5_table_sql, resolve_tokenizer, round_bm25_score
from mcp_core.fuzzy import (
    FUZZY_CANDIDATE_LIMIT,
    FUZZY_THRESHOLD,
    build_fuzzy_match,
    jaccard_word_best,
    short_acronym_matches,
)
from mcp_core.rerank import SearchHit, rerank_hits
from mcp_core.sanitizer import extract_short_acronym_tokens, sanitize_fts_query, stem_russian
from mcp_core.schema_guard import FtsSpec, StorageSchemaSpec, ensure_storage_schema
from mcp_skills.categories import get_category_keywords
from mcp_skills.models import SkillCard

__all__ = [
    "BLOCKS_SECTION_MARKER",
    "REQUIRED_SKILLS_FTS_COLUMNS",
    "RERANK_POOL_FACTOR",
    "SECTION_META_PREFIX",
    "SKILLS_BM25_WEIGHTS",
    "SkillsStorage",
    "check_skill_applicability",
]

# E2-2 (review-fixes): множитель пула кандидатов ДО Python-реранка.
# SQL LIMIT применяется к пулу (limit * фактор), реранк переупорядочивает
# пул, усечение до limit — после. 4x симметрично page_size стек-ветки и
# даёт реранку простор без существенного роста стоимости зондирующих
# запросов (максимум FUZZY_CANDIDATE_LIMIT).
RERANK_POOL_FACTOR: int = 4

SKILLS_BM25_WEIGHTS: tuple[float, ...] = (
    4.0,  # name
    0.0,  # category (UNINDEXED)
    3.5,  # title
    3.0,  # description
    2.5,  # stack
    0.15,  # content
    0.0,  # metadata (UNINDEXED)
)

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "backend-db": [
        "бэкенд",
        "backend",
        "хранилище",
        "api",
        "сервер",
    ],
    "frontend": [
        "ui",
        "ux",
        "интерфейс",
        "дизайн",
        "styling",
        "layout",
        "компоненты",
        "стили",
    ],
    "audio-ml": [
        "аудио",
        "голос",
        "речь",
        "speech",
        "whisper",
        "модель",
        "нейросети",
    ],
    "quality-review": [
        "ревью",
        "аудит",
        "качество",
        "review",
        "audit",
        "qa",
        "тесты",
        "инварианты",
        "проверка",
    ],
    "planning": [
        "планирование",
        "план",
        "архитектура",
        "декомпозиция",
        "planning",
        "tdd",
        "задачи",
    ],
    "desktop": [
        "десктоп",
        "gui",
        "desktop",
        "окна",
        "native",
        "tauri",
    ],
    "general": [
        "утилиты",
        "автоматизация",
        "ci",
        "cd",
        "пакеты",
        "uv",
        "git",
    ],
}

SKILLS_BM25_WEIGHTS_SQL = build_bm25_weights_sql(SKILLS_BM25_WEIGHTS, 7)

SKILLS_FTS_COLUMNS = [
    "name",
    "category",
    "title",
    "description",
    "stack",
    "content",
    "metadata",
]

SKILLS_FTS_UNINDEXED = ["category", "metadata"]

# D1 (review-fixes): маркер секции индексации блоков внутри FTS-колонки content.
# После чистого каркаса (SKILL.md) в content дописывается секция с текстами
# blocks/<slot>/<variant>.md — слова из блоков должны находиться поиском.
# Схема FTS остаётся 7-колоночной (арность весов/проб фиксирована другими тестами),
# поэтому блочный payload живёт в существующей колонке content, а get_skill
# отрезает секцию по маркеру и возвращает потребителям чистый каркас — иначе
# slot_compiler вставил бы блоки дважды (из FTS-тела и с диска при сборке
# load_skill). Маркер HTML-комментария не индексируется триграммным токенизатором
# как значимый текст и не конфликтует с настоящими SLOT-маркерами каркаса.
BLOCKS_SECTION_MARKER = "<!-- mcp-skills:blocks-index -->"

# Шаг 3 (skills-correctness-and-search): префикс строки-заголовка секции блока/ресурса
# внутри FTS-payload (после BLOCKS_SECTION_MARKER). Формат: line-anchored префикс
# + однострочный JSON (ensure_ascii=False). JSON надёжно экранирует кириллицу,
# пробелы, кавычки, '=' и квадратные скобки в значениях, а префикс '===section: '
# — обычный текст (не HTML-комментарий): FTS-колонка content триграммного
# токенизатора должна находить карточку по слову из title в заголовке секции.
# Разделение секций — только по началу строки с этим префиксом (line-anchored
# regex), что не даёт ложных сплитов на вхождения '[resource=' в Markdown-теле.
SECTION_META_PREFIX = "===section: "

# Разделитель секций: line-anchored lookahead на SECTION_META_PREFIX
# (lookahead, чтобы префикс оставался в начале части после re.split;
# ^ в MULTILINE — сплит только на начале строки, не в середине Markdown-тела).
SECTION_SPLIT_RE = re.compile(rf"(?=^{re.escape(SECTION_META_PREFIX)})", re.MULTILINE)

# Векторы зондирования совпадения колонок строгой арности 7
SKILLS_PROBE_WEIGHTS: dict[str, tuple[float, ...]] = {
    "name": (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    "title": (0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0),
    "description": (0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0),
    "stack": (0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0),
}

# Шаг 6 (skills-correctness-and-search): probe SQL выражения bm25() для каждой
# колонки — встраиваются в SELECT основного поискового запроса. Флаги совпадения
# колонок читаются из той же строки результата: SQL N+1 (по одному
# WHERE rowid = ? + bm25 на каждого кандидата в matched_column_flags) исчезает.
SKILLS_PROBE_SQL: dict[str, str] = {
    col: build_bm25_weights_sql(weights, 7) for col, weights in SKILLS_PROBE_WEIGHTS.items()
}

SKILLS_FIELD_BONUS: dict[str, float] = {
    "applicable": 0.05,  # Мягкий бонус, а не приоритет над релевантностью запроса.
    "name": 1.5,
    "title": 0.75,
    "description": 0.25,
    "stack": 0.25,
    "local_match": 0.5,
}

FILE_SYNC_SKILLS_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS file_sync (
        path TEXT NOT NULL,
        mtime REAL NOT NULL,
        size INTEGER NOT NULL,
        skill_name TEXT NOT NULL,
        PRIMARY KEY (path, skill_name)
    );
"""


REQUIRED_SKILLS_FTS_COLUMNS = frozenset(
    {
        "name",
        "category",
        "title",
        "description",
        "stack",
        "content",
        "metadata",
    }
)


def check_skill_applicability(
    card: SkillCard,
    project_stack: set[str] | list[str] | None,
) -> str:
    """F5/F1: Проверяет применимость скилла к стеку проекта.

    Возвращает:
      * "applicable" — скилл применим;
      * "incompatible" — скилл несовместим со стеком проекта (доказанное противоречие);
      * "unknown" — стек проекта не подтверждает требования скилла (неполный автодетектор
                    или технологии ещё нет в манифестах).
    """
    has_requirements = bool(card.requires_stack) or bool(
        card.activation and card.activation.lower() not in (None, "always")
    )
    if not has_requirements:
        return "applicable"

    if not project_stack:
        return "unknown"

    norm_stack = {s.lower() for s in project_stack}

    # 1. Проверка activation (ось режима: mcp / offline)
    act = card.activation.lower() if card.activation else None
    if act and act not in (None, "always"):
        mode_tags = {"mcp", "offline"} & norm_stack
        if mode_tags and act not in mode_tags:
            return "incompatible"
        if act not in norm_stack:
            return "unknown"

    # 2. Проверка requires_stack
    if isinstance(card.requires_stack, dict):
        none_of = [s.lower() for s in card.requires_stack.get("none_of", [])]
        if none_of and bool(set(none_of) & norm_stack):
            return "incompatible"

        all_of = [s.lower() for s in card.requires_stack.get("all_of", [])]
        if all_of and not set(all_of).issubset(norm_stack):
            return "unknown"

        any_of = [s.lower() for s in card.requires_stack.get("any_of", [])]
        if any_of and not bool(set(any_of) & norm_stack):
            return "unknown"
    elif isinstance(card.requires_stack, list) and card.requires_stack:
        req_set = {s.lower() for s in card.requires_stack}
        if not bool(req_set & norm_stack):
            return "unknown"

    return "applicable"


def _matches_project_stack(card: SkillCard, project_stack_set: set[str] | list[str]) -> bool:
    """Проверяет соответствие карточки скилла стеку проекта (F5/F1: контракт применимости)."""
    return check_skill_applicability(card, project_stack_set) != "incompatible"


def _rebuild_skills(c: sqlite3.Connection) -> None:
    """Пакетный drift-rebuild скиллов (module-level callback для StorageSchemaSpec).

    Выполняется ТОЛЬКО внутри guard-транзакции (BEGIN IMMEDIATE): DROP устаревшего
    индекса, пересоздание file_sync, сброс состояния синхронизации и FTS-DDL.
    Атомарность обеспечивает guard — здесь без транзакционных примитивов и без
    скриптового API курсора. Порядок CREATE→DELETE сохранён из прежнего пакетного
    _rebuild: file_sync пересоздаётся ДО сброса (DELETE, не DROP — PK-миграция
    пакета остаётся отдельным шагом ПОСЛЕ guard-вызова).
    """
    cur = c.cursor()
    cur.execute("DROP TABLE IF EXISTS skills_index;")
    cur.execute(FILE_SYNC_SKILLS_CREATE_SQL)
    cur.execute("DELETE FROM file_sync;")
    cur.execute(
        fts5_table_sql(
            "skills_index",
            SKILLS_FTS_COLUMNS,
            unindexed=SKILLS_FTS_UNINDEXED,
            tokenizer=resolve_tokenizer(),
        )
    )


# Спецификация схемы скиллов для ensure_storage_schema (mcp_core.schema_guard).
# fts_checks — ordered-контракт 7 колонок: веса bm25(skills_index, weights)
# позиционны (SKILLS_BM25_WEIGHTS = 7 весов), перестановка колонок сломала бы
# ранжирование. service_ddl = [FILE_SYNC_SKILLS_CREATE_SQL] явно (план v5, этап 4).
SKILLS_STORAGE_SCHEMA_SPEC = StorageSchemaSpec(
    schema_version=SCHEMA_VERSION,
    fts_checks={"skills_index": FtsSpec(expected_columns=tuple(SKILLS_FTS_COLUMNS))},
    service_ddl=[FILE_SYNC_SKILLS_CREATE_SQL],
    rebuild=_rebuild_skills,
    # Объектов, создаваемых исключительно в _rebuild (триггеры/индексы) нет —
    # file_sync полностью покрывается service_ddl.
    rebuild_on_missing=frozenset(),
)


def _section_header(meta: dict[str, Any]) -> str:
    """Шаг 3: сериализует метаданные секции — префикс + однострочный JSON."""
    return SECTION_META_PREFIX + json.dumps(meta, ensure_ascii=False)


def _iter_sections(blocks_payload: str) -> Generator[tuple[dict[str, Any], str]]:
    """Шаг 3: разбивает блочный payload на секции (meta, body).

    Разделение — по началу строки с SECTION_META_PREFIX (line-anchored):
    вхождение '[resource=' в Markdown-теле секции не создаёт ложный сплит.
    Секции с повреждённым JSON-заголовком пропускаются (не ломают поиск).
    """
    for raw in SECTION_SPLIT_RE.split(blocks_payload):
        if not raw.strip():
            continue
        header, _, body = raw.partition("\n")
        header = header.strip()
        if header.startswith(SECTION_META_PREFIX):
            try:
                meta = json.loads(header[len(SECTION_META_PREFIX) :])
            except (ValueError, TypeError):
                continue
        else:
            continue
        if isinstance(meta, dict):
            yield meta, body


def _linked_resource_sections(base_dir: Path, catalog_root: Path, owner: str) -> list[str]:
    from mcp_skills.resources import extract_resources_from_markdown, resolve_resource_file

    skill_md = base_dir / "SKILL.md"
    try:
        _, resources = extract_resources_from_markdown(
            skill_md.read_text(encoding="utf-8"), skill_md, catalog_root, owner
        )
    except (OSError, UnicodeError):
        return []
    sections: list[str] = []
    for resource in resources:
        rid = resource["resource_id"]
        if rid.startswith("blocks/"):
            continue
        try:
            path, _ = resolve_resource_file(rid, catalog_root)
            text = path.read_text(encoding="utf-8")
        except (OSError, ValueError, UnicodeError):
            continue
        # Шаг 3: title/owner/resource_id — в JSON-заголовке: пробелы, кириллица,
        # ']' и '=' в названии ресурса больше не рвут разбор секции.
        sections.append(
            _section_header(
                {"type": "resource", "resource_id": rid, "title": resource["title"], "owner": owner}
            )
            + "\n"
            + text
        )
    return sections


def _collect_blocks_payload(
    base_dir: Path | None,
    available_slots: list[str] | None = None,
    catalog_root: Path | None = None,
    owner_skill: str = "",
) -> tuple[str, list[str]]:
    """Индексирует локальные/общие варианты и связанные ресурсы, не меняя скелет."""
    if base_dir is None:
        return "", []
    paths = [(path, f"local:{path.name}") for path in sorted((base_dir / "blocks").rglob("*.md"))]
    if catalog_root is not None:
        for slot in available_slots or []:
            if re.fullmatch(r"[a-zA-Z0-9_-]+", slot):
                paths.extend(
                    (path, f"shared:{path.relative_to(catalog_root).as_posix()}")
                    for path in sorted((catalog_root / "blocks" / slot).glob("*.md"))
                )
    sections: list[str] = []
    variants: list[str] = []
    for path, origin in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        variant = path.stem.lower()
        if variant != "default" and variant not in variants:
            variants.append(variant)
        # Шаг 3: slot/variant/origin — в JSON-заголовке секции (точные сравнения
        # полей вместо подстрок 'origin=shared:' в заголовке).
        sections.append(
            _section_header(
                {"type": "slot", "slot": path.parent.name, "variant": variant, "origin": origin}
            )
            + "\n"
            + text
        )
    if catalog_root is not None:
        sections.extend(_linked_resource_sections(base_dir, catalog_root, owner_skill))
    return (BLOCKS_SECTION_MARKER + "\n" + "\n\n".join(sections) if sections else ""), variants


def _normalize_query_token(token: str) -> str:
    """Шаг 3: нормализация токена запроса — lowercase + русский стемминг.

    stem_russian возвращает None для коротких слов и латиницы — тогда
    используется исходный lowercase-токен.
    """
    lower = token.lower()
    return stem_russian(lower) or lower


def _section_search_text(meta: dict[str, Any], body: str) -> str:
    """Шаг 3: поисковый текст секции = значащие поля метаданных + body.

    СЛУЖЕБНЫЕ ключи ('type', 'owner', 'origin') в текст НЕ попадают —
    совпадение запроса с именем ключа не должно давать ложный local_match.
    """
    if meta.get("type") == "resource":
        fields = [meta.get("resource_id", ""), meta.get("title", "")]
    else:
        fields = [meta.get("slot", ""), meta.get("variant", "")]
    return " ".join(part for part in fields if part) + " " + body


def _section_matches_query(meta: dict[str, Any], body: str, query: str) -> bool:
    """Шаг 3: единый helper совпадения секции с запросом.

    OR-семантика (симметрично sanitize_fts_query: 'fastapi auth' находит секцию
    с 'fastapi supports auth'): достаточно совпадения хотя бы одного терма.
    Терм совпадает по префиксу С НАЧАЛА токена текста ('auth' -> 'authentication',
    'thor' != 'authorization' — отличие от trigram-поиска карточек, который
    находит 'authorization' по 'thor' как подстроку триграмм). Русские словоформы —
    через stem_russian для обеих сторон.
    """
    if not query:
        return False
    query_terms: set[str] = set()
    for tok in re.findall(r"[\w]+", query.lower(), flags=re.UNICODE):
        if len(tok) >= 2 or tok.isdigit():
            query_terms.add(_normalize_query_token(tok))
    if not query_terms:
        return False
    for tok in re.findall(r"[\w]+", _section_search_text(meta, body).lower(), flags=re.UNICODE):
        norm = _normalize_query_token(tok)
        for term in query_terms:
            if norm.startswith(term) or tok.lower().startswith(term):
                return True
    return False


def _extract_matched_resources(content: str, query: str, card_name: str) -> list[dict[str, Any]]:
    """Извлекает связанные ресурсы, совпавшие с запросом внутри FTS-payload блоков и ссылок.

    Шаг 3: секции разбираются через JSON-метаданные (полный title с пробелами и
    спецсимволами), совпадение — через единый helper _section_matches_query.
    """
    if BLOCKS_SECTION_MARKER not in content or not query:
        return []

    matched: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    blocks_payload = content.split(BLOCKS_SECTION_MARKER, 1)[1]

    for meta, body in _iter_sections(blocks_payload):
        if not _section_matches_query(meta, body, query):
            continue

        sec_type = meta.get("type")
        if sec_type == "resource":
            res_id = meta.get("resource_id", "")
            owner = meta.get("owner", card_name)
            if res_id and res_id not in seen_ids:
                seen_ids.add(res_id)
                matched.append(
                    {
                        "resource_id": res_id,
                        "title": meta.get("title") or Path(res_id).stem,
                        "owner_skill": owner,
                    }
                )
        elif sec_type == "slot":
            origin = meta.get("origin", "")
            if origin.startswith("shared:"):
                shared_path = origin.split(":", 1)[1]
                if shared_path not in seen_ids:
                    seen_ids.add(shared_path)
                    matched.append(
                        {
                            "resource_id": shared_path,
                            "title": Path(shared_path).stem,
                            "owner_skill": card_name,
                        }
                    )

    return matched


def _row_to_card(r: sqlite3.Row) -> SkillCard:
    """Десериализует строку БД в SkillCard с присвоением score."""
    meta = json.loads(r["metadata"])
    rank = r["rank"] if "rank" in r.keys() else None  # noqa: SIM118
    if rank is not None:
        val = -float(rank)
        meta["score"] = round_bm25_score(val)
    return SkillCard(**meta)


def _build_search_sql(
    clean_q: str, category: str | None, with_offset: bool = False
) -> tuple[str, list[Any]]:
    """Формирует SQL-запрос поиска скиллов и базовые параметры.

    Шаг 6: в ветке MATCH выражения зондирования колонок (match_name и т.д.)
    вычисляются в том же SELECT — отдельных probe-запросов на кандидата нет.
    В ветке пустого запроса bm25 НЕ вызывается: вне MATCH-контекста SQLite
    вернёт ошибку.
    """
    if clean_q:
        probe_select = ", ".join(
            f"bm25(skills_index, {SKILLS_PROBE_SQL[col]}) != 0.0 AS match_{col}"
            for col in SKILLS_PROBE_WEIGHTS
        )
        sql = f"""
            SELECT rowid, name, title, description, stack, content, metadata,
                   bm25(skills_index, {SKILLS_BM25_WEIGHTS_SQL}) AS rank,
                   {probe_select}
            FROM skills_index
            WHERE skills_index MATCH ?
        """
        params: list[Any] = [clean_q]
        if category:
            sql += " AND category = ?"
            params.append(category)
        sql += " ORDER BY rank, name LIMIT ? OFFSET ?" if with_offset else " ORDER BY rank LIMIT ?"
    else:
        sql = "SELECT rowid, name, title, description, stack, content, metadata FROM skills_index"
        params = []
        if category:
            sql += " WHERE category = ?"
            params.append(category)
        sql += " ORDER BY name LIMIT ? OFFSET ?" if with_offset else " ORDER BY name LIMIT ?"
    return sql, params


def _collect_short_acronym_skills(
    conn: sqlite3.Connection,
    category: str | None,
    short_tokens: set[str],
    existing_names: set[str],
    fuzzy_cards: dict[str, tuple[SkillCard, float]],
    project_stack: list[str] | None = None,
) -> None:
    """Точный fallback по 2-буквенным акронимам (акронимы <3 символов не индексируются trigram).

    E2-3: при переданном project_stack кандидаты фильтруются по стеку здесь,
    а не после набора (иначе несовместимые вымываются финальным фильтром,
    теряя слоты для совместимых).

    План E (P2): trigram-индекс не индексирует термины короче 3 символов, поэтому
    канал расширен подстрочным/префиксным LIKE-совпадением по блобу
    name/title/description/stack — акроним внутри составного токена
    ('db_backup') и в начале имени скилла ('pr-review') тоже находится.
    """
    if not short_tokens:
        return
    sql_s = "SELECT rowid, name, title, description, stack, metadata FROM skills_index"
    params_s: list[Any] = []
    if category:
        sql_s += " WHERE category = ?"
        params_s.append(category)
    try:
        s_rows = conn.execute(sql_s, params_s).fetchall()
    except sqlite3.OperationalError:
        return

    stack_set = {s.lower() for s in project_stack} if project_stack is not None else None

    for sr in s_rows:
        sname = sr["name"]
        if sname in existing_names or sname in fuzzy_cards:
            continue
        doc_blob = f"{sr['name']} {sr['title']} {sr['description']} {sr['stack']}"
        doc_lower = doc_blob.lower()
        token_hit = short_acronym_matches(short_tokens, doc_blob)
        # Подстрочный канал: короткий акроним как подслово (граница токена или
        # часть составного токена 'db_backup'/'pr-review') в тексте полей.
        substr_hit = any(f" {st} " in f" {doc_lower} " or st in doc_lower for st in short_tokens)
        if not (token_hit or substr_hit):
            continue
        meta = json.loads(sr["metadata"])
        if stack_set is not None and not _matches_project_stack(SkillCard(**meta), stack_set):
            continue
        meta["score"] = 1.0
        fuzzy_cards[sname] = (SkillCard(**meta), 1.0)


def _skill_hit_flags(
    conn: sqlite3.Connection, r: sqlite3.Row, query: str, clean_q: str, name: str
) -> dict[str, bool]:
    # Шаг 6: флаги совпадения колонок читаются из той же строки результата
    # (match_name/match_title/... вычислены в SELECT основного запроса),
    # а не отдельным probe-запросом на каждого кандидата — SQL N+1 устранён.
    # rows из ветки без MATCH (пустой запрос) не содержат match_* колонок —
    # тогда флаги остаются False, как раньше при пустом clean_q.
    row_keys = r.keys()
    flags = {
        col: bool(r[f"match_{col}"]) if f"match_{col}" in row_keys else False
        for col in SKILLS_PROBE_WEIGHTS
    }
    has_local_match = False
    if query and "content" in row_keys and BLOCKS_SECTION_MARKER in r["content"]:
        # Шаг 3: единый helper совпадения секции; локальность — точные
        # сравнения полей JSON-метаданных (origin.startswith('local:') или
        # owner == name), а не подстроки 'owner={name}' в тексте секции.
        blocks_part = r["content"].split(BLOCKS_SECTION_MARKER, 1)[1]
        for meta, body in _iter_sections(blocks_part):
            origin = meta.get("origin", "")
            is_local = origin.startswith("local:") or meta.get("owner") == name
            if is_local and _section_matches_query(meta, body, query):
                has_local_match = True
                break
    flags["local_match"] = has_local_match
    return flags


def _row_matched_resources(row: sqlite3.Row, query: str, name: str) -> list[dict[str, Any]]:
    return (
        _extract_matched_resources(row["content"], query, name)
        if query and "content" in row.keys()  # noqa: SIM118
        else []
    )


def _rerank_and_fuzzy_skills(
    conn: sqlite3.Connection,
    match_rows: list[sqlite3.Row],
    query: str,
    clean_q: str,
    category: str | None,
    limit: int,
    project_stack: list[str] | None = None,
) -> list[SkillCard]:
    """Выполняет fuzzy-догон и reranking для результатов поиска скиллов.

    E2-3 (verdict_4:89): при переданном ``project_stack`` fuzzy-кандидаты и
    fallback по акронимам фильтруются по стеку УЖЕ на этапе набора — иначе
    несовместимые карточки занимают слоты выдачи, а финальная фильтрация
    (в search_skills) их вымывает, теряя совместимые результаты ниже.
    """
    if not clean_q:
        return [_row_to_card(r) for r in match_rows]

    # E2-3: нормализованный набор стека для фильтрации fuzzy-кандидатов
    stack_norm: set[str] | None = (
        {s.lower() for s in project_stack} if project_stack is not None else None
    )

    app_match_count = (
        sum(
            1
            for r in match_rows
            if check_skill_applicability(_row_to_card(r), stack_norm) == "applicable"
        )
        if stack_norm
        else len(match_rows)
    )

    tokens_3 = [t for t in re.findall(r"[A-Za-zА-Яа-я0-9_]+", query) if len(t) >= 3]
    fuzzy_cards: dict[str, tuple[SkillCard, float]] = {}

    if (len(match_rows) < limit or app_match_count < limit) and tokens_3:
        existing_names = {r["name"] for r in match_rows}
        for tok in tokens_3:
            f_match = build_fuzzy_match(tok)
            if not f_match:
                continue
            sql_f = """
                SELECT rowid, name, title, description, stack, content, metadata
                FROM skills_index
                WHERE skills_index MATCH ?
            """
            params_f: list[Any] = [f_match]
            if category:
                sql_f += " AND category = ?"
                params_f.append(category)
            sql_f += f" LIMIT {FUZZY_CANDIDATE_LIMIT}"

            try:
                c_rows = conn.execute(sql_f, params_f).fetchall()
            except sqlite3.OperationalError:
                continue

            for cr in c_rows:
                cname = cr["name"]
                if cname in existing_names:
                    continue
                meta = json.loads(cr["metadata"])
                # E2-3: фильтр стека на этапе набора — несовместимый кандидат
                # не должен занимать слот, который затем вымоет финальный фильтр.
                if stack_norm is not None:
                    cand_card = SkillCard(**meta)
                    if not _matches_project_stack(cand_card, stack_norm):
                        continue
                # План E: fuzzy-скоринг учитывает и содержимое (content) — слово,
                # встречающееся только в теле скилла, тоже должно находиться по опечатке.
                doc_text = f"{cr['title']} {cr['description']} {cr['stack']} {cr['content'] or ''}"
                jaccard = jaccard_word_best(tok, doc_text)
                if jaccard >= FUZZY_THRESHOLD and (
                    cname not in fuzzy_cards or jaccard > fuzzy_cards[cname][1]
                ):
                    meta["score"] = round(jaccard, 3)
                    fuzzy_cards[cname] = (SkillCard(**meta), jaccard)

    # Фаза 2b: Точный fallback по 2-буквенным акронимам
    short_tokens = extract_short_acronym_tokens(query)
    if (len(match_rows) < limit or app_match_count < limit) and short_tokens:
        _collect_short_acronym_skills(
            conn,
            category,
            short_tokens,
            {r["name"] for r in match_rows},
            fuzzy_cards,
            project_stack=project_stack,
        )

    needed = max(limit - app_match_count, limit - len(match_rows), 0)
    sorted_fuzzy = sorted(fuzzy_cards.values(), key=lambda item: (-item[1], item[0].name))[:needed]

    # Фаза 3: Rerank
    hits: list[SearchHit] = []
    card_map: dict[str, SkillCard] = {}

    for r in match_rows:
        card = _row_to_card(r)
        card.matched_resources = _row_matched_resources(r, query, card.name)
        card_map[card.name] = card
        flags = _skill_hit_flags(conn, r, query, clean_q, card.name)
        matched_cols = sum(1 for v in flags.values() if v)
        raw_bm25 = float(r["rank"]) if "rank" in r.keys() else 0.0  # noqa: SIM118
        app = check_skill_applicability(card, stack_norm) if stack_norm else "applicable"
        t_rank = 0
        flags["applicable"] = app == "applicable"
        hits.append(
            SearchHit(
                id=card.name,
                raw_bm25=raw_bm25,
                matched_columns=matched_cols,
                field_matches=flags,
                trust_rank=t_rank,
            )
        )

    for card, jaccard in sorted_fuzzy:
        card_map[card.name] = card
        app = check_skill_applicability(card, stack_norm) if stack_norm else "applicable"
        t_rank = 1
        hits.append(
            SearchHit(
                id=card.name,
                raw_bm25=-jaccard,
                matched_columns=0,
                field_matches={"applicable": app == "applicable"},
                trust_rank=t_rank,
            )
        )

    reranked_hits = rerank_hits(hits, field_bonus=SKILLS_FIELD_BONUS)
    return [card_map[h.id] for h in reranked_hits]


class SkillsStorage:
    """Управление локальной базой SQLite FTS5 для поиска и извлечения скиллов."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = core_get_connection(self.db_path, timeout=5.0)
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Инициализирует схему через mcp_core ensure_storage_schema (guard).

        Единственный guard-вызов: drift-check (ordered-контракт 7 колонок
        skills_index), атомарный rebuild при drift, service-самолечение file_sync.
        Пакетная PK-миграция file_sync (pk_count==1 → DROP) идёт ПОСЛЕ guard-вызова —
        отдельным не атомарным и сегодня шагом (риск потери при переносе в guard).
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with self.get_connection() as conn:
            ensure_storage_schema(conn, SKILLS_STORAGE_SCHEMA_SPEC)

            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(file_sync);")
            fs_cols = cursor.fetchall()
            pk_count = sum(1 for col in fs_cols if col[5] > 0)
            if pk_count == 1:
                cursor.execute("DROP TABLE IF EXISTS file_sync;")
                cursor.execute(FILE_SYNC_SKILLS_CREATE_SQL)

    def get_dependency_revision(self) -> str:
        """Возвращает стабильный sha256-хэш ревизии всех отслеживаемых файлов зависимостей."""
        with self.get_connection() as conn:
            rows = conn.execute("SELECT path, mtime, size FROM file_sync ORDER BY path").fetchall()
        h = hashlib.sha256()
        for r in rows:
            p_val = r["path"] if isinstance(r, sqlite3.Row) else r[0]
            m_val = r["mtime"] if isinstance(r, sqlite3.Row) else r[1]
            s_val = r["size"] if isinstance(r, sqlite3.Row) else r[2]
            h.update(f"{p_val}:{m_val}:{s_val}\n".encode())
        return h.hexdigest()

    def index_skill(self, card: SkillCard, content: str, catalog_root: Path | None = None) -> None:
        """Индексирует или обновляет карточку скилла в FTS5."""
        stack_parts = list(card.stack)
        if card.category and card.category not in stack_parts:
            stack_parts.append(card.category)
        for cat in card.categories:
            if cat not in stack_parts:
                stack_parts.append(cat)
        for alias in card.aliases:
            if alias not in stack_parts:
                stack_parts.append(alias)
        category_kw = get_category_keywords().get(
            card.category, CATEGORY_KEYWORDS.get(card.category, [])
        )
        for kw in category_kw:
            if kw not in stack_parts:
                stack_parts.append(kw)

        if catalog_root is None and card.base_dir:
            p = card.base_dir
            while p and p != p.parent:
                if (p / "blocks").is_dir() or (p / "skills").is_dir():
                    catalog_root = p
                    break
                p = p.parent

        # D1 (review-fixes) + Step 15: тексты блоков, общих блоков слотов и связанных ресурсов — в индекс
        blocks_payload, block_variants = _collect_blocks_payload(
            card.base_dir,
            available_slots=card.available_slots,
            catalog_root=catalog_root,
            owner_skill=card.name,
        )
        for variant in block_variants:
            if variant not in stack_parts:
                stack_parts.append(variant)
        stack_str = " ".join(stack_parts)
        if blocks_payload:
            content = content.rstrip() + "\n\n" + blocks_payload

        with self.get_connection() as conn:
            # Удаляем старую запись по имени
            conn.execute("DELETE FROM skills_index WHERE name = ?", (card.name,))
            conn.execute(
                """
                INSERT INTO skills_index(name, category, title, description, stack, content, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    card.name,
                    card.category,
                    card.title,
                    card.description,
                    stack_str,
                    content,
                    json.dumps(card.model_dump(mode="json"), ensure_ascii=False),
                ),
            )
            conn.commit()

    def get_skill(self, name: str) -> tuple[SkillCard, str] | None:
        """Возвращает карточку скилла и его контент по имени.

        D1 (review-fixes): колонка content хранит каркас + блочную секцию для
        поиска, но потребители (slot_compiler/creator/bundler/server) должны
        получать чистый каркас — иначе load_skill вставил бы тексты блоков
        дважды (из FTS-тела и повторно с диска при компиляции слотов).
        Блочная секция отрезается по BLOCKS_SECTION_MARKER; rstrip() убирает
        хвостовые переводы строк, оставшиеся от стыка "каркас + секция".
        """
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT content, metadata FROM skills_index WHERE name = ?",
                (name,),
            ).fetchone()
            if not row:
                return None

            content = row["content"]
            # Отрезаем D1-блочную секцию: вернуть только каркас SKILL.md.
            content = content.split(BLOCKS_SECTION_MARKER, 1)[0].rstrip()
            meta_json = json.loads(row["metadata"])
            card = SkillCard(**meta_json)
            return card, content

    def delete_skill(self, name: str) -> None:
        """Удаляет скилл из FTS5 индекса и связанные записи синхронизации."""
        with self.get_connection() as conn:
            conn.execute("DELETE FROM skills_index WHERE name = ?", (name,))
            conn.execute("DELETE FROM file_sync WHERE skill_name = ?", (name,))
            conn.commit()

    def list_all_skills(self) -> list[SkillCard]:
        """Возвращает все зарегистрированные карточки скиллов."""
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT * FROM skills_index")
            rows = cursor.fetchall()
            return [_row_to_card(r) for r in rows]

    def search_skills(
        self,
        query: str = "",
        category: str | None = None,
        limit: int = 10,
        project_stack: list[str] | None = None,
    ) -> list[SkillCard] | tuple[list[SkillCard], bool]:
        """Полнотекстовый поиск скиллов с фильтрацией по категории и BM25 ранжированием."""
        clean_q = sanitize_fts_query(query) if query else ""

        with self.get_connection() as conn:
            if project_stack is None:
                # E2-2 (verdict_4:88): кандидаты набираются БЕЗ окончательного
                # LIMIT — SQL-LIMIT до Python-реранка отрезал документы, которые
                # реранк (флаги match_name/match_title/... из SELECT) мог
                # поднять в топ. Пул = limit * RERANK_POOL_FACTOR (максимум
                # FUZZY_CANDIDATE_LIMIT — совместим с fuzzy-догоном), реранк
                # переупорядочивает, только затем усечение до limit.
                pool_limit = max(limit * RERANK_POOL_FACTOR, limit)
                sql, params = _build_search_sql(clean_q, category, with_offset=False)
                params.append(pool_limit)
                rows = conn.execute(sql, params).fetchall()
                reranked = _rerank_and_fuzzy_skills(conn, rows, query, clean_q, category, limit)
                return reranked[:limit]

            # Фильтрация по стеку проекта с пагинированным over-fetch
            sql, base_params = _build_search_sql(clean_q, category, with_offset=True)
            page_size = max(limit * 4, 16)
            max_pages = 4
            offset = 0
            filtered_rows: list[sqlite3.Row] = []
            truncated = False
            stack_set = {s.lower() for s in project_stack}
            # E2-2: пул для реранка больше limit — релевантный по колонкам
            # документ не должен отрезаться до Python-реранка.
            pool_limit = max(limit * RERANK_POOL_FACTOR, limit)

            for _ in range(max_pages):
                page_params = [*base_params, page_size, offset]
                rows = conn.execute(sql, page_params).fetchall()
                for r in rows:
                    card = _row_to_card(r)
                    app = check_skill_applicability(card, stack_set)
                    if app != "incompatible":
                        filtered_rows.append(r)
                if (
                    sum(
                        check_skill_applicability(_row_to_card(row), stack_set) == "applicable"
                        for row in filtered_rows
                    )
                    >= pool_limit
                ):
                    break
                if len(rows) < page_size:
                    break
                offset += page_size
            else:
                truncated = True

            # E2-3: fuzzy-набор уже стековый — финальная фильтрация ниже
            # остаётся страховкой (карточки из match_rows перезанованы).
            results = _rerank_and_fuzzy_skills(
                conn, filtered_rows, query, clean_q, category, limit, project_stack
            )
            final_cards = [c for c in results if _matches_project_stack(c, stack_set)][:limit]
            for c in final_cards:
                app = check_skill_applicability(c, stack_set)
                c.applicability = app  # type: ignore[assignment]
                if app == "applicable":
                    c.applicability_reason = "Применимо к стеку проекта"
                elif app == "unknown":
                    c.applicability_reason = (
                        "Требуемые технологии не подтверждены манифестами проекта (unknown)"
                    )
                elif app == "incompatible":
                    c.applicability_reason = "Несовместимо со стеком проекта"
            return final_cards, truncated

    def get_category_counts(self, project_stack: list[str] | None = None) -> dict[str, int]:
        """Возвращает динамическое распределение скиллов по категориям с фильтрацией пустых значений.

        При project_stack=None — полный GROUP BY-подсчёт (потребители без стека:
        categories.py). При переданном стеке карточки фильтруются построчно через
        _matches_project_stack (требует activation/requires_stack из metadata —
        SQL-WHERE невозможен, стек не нормализован в колонке) — консистентно с
        search_skills, чтобы overview не расходился с drilldown (verdict_7b).
        Категории, чьи карточки отфильтрованы, возвращаются с count=0: сокрытие
        нулей — решение слоя вызова (server.py скрывает их только при переданном
        стеке). Cold-start fallback {c: 0} при пустой базе сохранён для обеих веток.
        """
        with self.get_connection() as conn:
            if project_stack is None:
                rows = conn.execute(
                    """
                    SELECT COALESCE(category, 'general') AS cat, COUNT(*) AS cnt
                    FROM skills_index
                    WHERE category IS NOT NULL AND trim(category) != ''
                    GROUP BY category
                    ORDER BY cnt DESC, cat ASC
                    """
                ).fetchall()
                if not rows:
                    from mcp_skills.categories import CATEGORIES

                    # Cold-start fallback при пустой базе
                    return {c: 0 for c in CATEGORIES}
                return {row["cat"]: row["cnt"] for row in rows}

            # Построчный проход вместо GROUP BY: карточки с несовместимым
            # requires_stack/activation исключаются из счётчиков, чтобы overview
            # совпадал с drilldown/search при том же стеке.
            all_rows = conn.execute(
                """
                SELECT category, metadata FROM skills_index
                WHERE category IS NOT NULL AND trim(category) != ''
                """
            ).fetchall()
            if not all_rows:
                from mcp_skills.categories import CATEGORIES

                # Cold-start fallback при пустой базе (и для ветки со стеком)
                return {c: 0 for c in CATEGORIES}
            stack_set = {s.lower() for s in project_stack}
            counts: dict[str, int] = {}
            for r in all_rows:
                cat = r["category"]
                counts.setdefault(cat, 0)
                card = SkillCard(**json.loads(r["metadata"]))
                if _matches_project_stack(card, stack_set):
                    counts[cat] += 1
            return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def close(self) -> None:
        """Закрывает ресурсы хранилища (no-op, соединения закрываются в get_connection)."""
