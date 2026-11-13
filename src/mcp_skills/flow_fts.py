"""FTS5-индекс сценариев (flows) для поиска по естественному запросу.

Почему отдельный модуль и in-memory индекс
------------------------------------------
Сценарии — это Markdown-файлы каталога, а не записи БД: их ~13, суммарный объём
текста ~54 КБ. Постоянная таблица здесь не нужна, а её отсутствие снимает
главный риск миграции схемы FTS: пересборка in-memory индекса стоит ~5 мс
(измерено: 13 flow, SQLite 3.45.1, ``trigram remove_diacritics 2``), поэтому
невозможна ситуация отката транслита, когда bump версии схемы обнулял живые
индексы (``skills_count: 0``). Арность существующих FTS-таблиц не меняется.

Кэша индекса нет намеренно: полный цикл (перечитать каталог + построить индекс)
~17 мс, что несравнимо с латентностью хоста, а отсутствие кэша исключает
устаревшую выдачу после правки ``flows/*.md``.

Что индексируется
-----------------
``name`` + мета-блоб (``title``, ``aliases``, ``intent_group``, ``when_to_use``,
``description``) + тело сценария (заголовки и инструкции этапов). Тело включено,
потому что доменная лексика живёт именно в этапах: на бенчмарке 30 запросов
индекс без тела находил 97 % целей, с телом — 100 %.

Семантика MATCH
---------------
OR-по-триграммам, а не AND. AND требует присутствия *каждого* токена запроса в
индексе: на том же бенчмарке это роняло полноту до 61 % — любой «лишний» токен
(«i want to add a task storage feature») обнулял выдачу. OR сохраняет полноту
100 % и поднимает top-1 до 83 %.

Индекс НЕ заменяет пословный скоринг: замена скоринга чистым FTS регрессировала
полноту 97 % → 61 %. FTS лишь добавляет позиционный бонус к базе скоринга.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
from collections.abc import Sequence
from typing import Any

from mcp_core.fts import fts5_table_sql, resolve_tokenizer

__all__ = [
    "FLOW_FTS_BONUS",
    "FLOW_FTS_LIMIT",
    "FlowFtsResult",
    "build_flow_fts_index",
    "flow_trigram_match",
    "fts_bonus_rank",
]

logger = logging.getLogger("mcp_skills.flow_fts")

# Позиционный бонус: ранг i получает FLOW_FTS_BONUS / (i + 1). На публичном
# каталоге unified flow значение 6 сохраняет 28/29 верных top-1, а 20 перебивает
# лексическое совпадение для двух запросов на ревью (26/29).
FLOW_FTS_BONUS: float = 6.0
FLOW_FTS_LIMIT: int = 20

_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9_]+")
# Токены короче 3 символов не дают триграмм — trigram-токенизатор их не видит.
# Это не потеря полноты (recall): такие токены («qa», «id») закрыты пословным
# скорингом intent_group, который работает по границам слов, а не по триграммам.
_MIN_TRIGRAM_TOKEN = 3


class FlowFtsResult:
    """In-memory FTS5-индекс сценариев + метаданные построения.

    ``conn is None`` — индекс недоступен (пустой список, ошибка FTS5, битые
    данные). Это штатная деградация: поиск обязан продолжить работу на
    пословном скоринге, а не вернуть пустую выдачу.
    """

    __slots__ = ("conn", "error", "flows", "indexed", "tokenizer")

    def __init__(
        self,
        conn: sqlite3.Connection | None,
        flows: Sequence[dict[str, Any]],
        tokenizer: str = "",
        indexed: int = 0,
        error: str | None = None,
    ) -> None:
        self.conn = conn
        self.flows = flows
        self.tokenizer = tokenizer
        self.indexed = indexed
        self.error = error

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None


def flow_trigram_match(query: str) -> str:
    """Строит FTS5 MATCH-выражение: OR-по-триграммам каждого токена запроса.

    Пустая строка означает «MATCH построить нельзя» (нет токенов длины >= 3) —
    вызывающий код обязан трактовать это как отсутствие FTS-сигнала, а не как
    нулевой результат поиска.
    """
    groups: list[str] = []
    for raw in _TOKEN_RE.findall(query.lower()):
        token = re.sub(r"[^a-zа-яё0-9_]", "", raw)
        if len(token) < _MIN_TRIGRAM_TOKEN:
            continue
        trigrams = sorted({token[i : i + 3] for i in range(len(token) - 2)})
        if trigrams:
            # Каждая триграмма — отдельный FTS-термин; кавычки защищают символы
            # от интерпретации как операторов MATCH.
            groups.append("(" + " OR ".join(f'"{t}"' for t in trigrams) + ")")
    return " OR ".join(groups)


def _meta_blob(flow: dict[str, Any]) -> str:
    return " ".join(
        [
            str(flow.get("title") or ""),
            " ".join(str(a) for a in (flow.get("aliases") or [])),
            str(flow.get("intent_group") or ""),
            " ".join(str(t) for t in (flow.get("when_to_use") or [])),
            str(flow.get("description") or ""),
        ]
    )


def build_flow_fts_index(flows: Sequence[dict[str, Any]]) -> FlowFtsResult:
    """Строит in-memory FTS5-индекс по списку сценариев.

    Тело этапов берётся из ключа ``stage_text``, если вызывающий код его
    предоставил (``list_available_flows(include_stage_text=True)``); иначе
    индексируются только метаданные. Любая ошибка построения (нет FTS5, нет
    trigram, битые данные) приводит к ``FlowFtsResult(conn=None, error=...)``:
    поиск продолжает работать на пословном скоринге.
    """
    if not flows:
        return FlowFtsResult(None, flows, error="empty_flow_list")

    tokenizer = resolve_tokenizer()
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE flow_meta (rowid INTEGER PRIMARY KEY, name TEXT, meta TEXT, body TEXT);"
        )
        conn.execute(
            fts5_table_sql(
                "flow_index",
                ["name", "meta", "body"],
                content="flow_meta",
                content_rowid="rowid",
                tokenizer=tokenizer,
            )
        )
        indexed = 0
        for rowid, flow in enumerate(flows, start=1):
            name = str(flow.get("name") or "")
            if not name:
                continue
            meta = _meta_blob(flow)
            body = str(flow.get("stage_text") or "")
            conn.execute(
                "INSERT INTO flow_meta (rowid, name, meta, body) VALUES (?, ?, ?, ?);",
                (rowid, name, meta, body),
            )
            conn.execute(
                "INSERT INTO flow_index (rowid, name, meta, body) VALUES (?, ?, ?, ?);",
                (rowid, name, meta, body),
            )
            indexed += 1
        conn.commit()
    except Exception as err:
        # Широкий перехват намеренный: FTS — усиление поиска, а не обязательный
        # путь. Любая ошибка построения индекса (нет FTS5, нет trigram, битые
        # данные) обязана приводить к деградации на пословный скоринг, а не
        # ронять весь search_skills.
        logger.debug("FTS-индекс сценариев не построен (%s); поиск без FTS", err)
        with contextlib.closing(conn):
            pass
        return FlowFtsResult(None, flows, tokenizer, 0, f"build_failed: {err}")

    return FlowFtsResult(conn, flows, tokenizer, indexed)


def fts_bonus_rank(result: FlowFtsResult, query: str) -> dict[str, float]:
    """Позиционный бонус FTS: ``{name: FLOW_FTS_BONUS / (rank + 1)}``.

    Пустой словарь означает «FTS-сигнала нет» (индекс не построен, MATCH не
    собирается, 0 совпадений, ошибка SQLite) — вызывающий код обязан оставить
    выдачу пословного скоринга без изменений, а не обнулить её.
    """
    if result.conn is None:
        return {}
    match_expr = flow_trigram_match(query)
    if not match_expr:
        return {}
    try:
        rows = result.conn.execute(
            "SELECT name FROM flow_index WHERE flow_index MATCH ? "
            "ORDER BY bm25(flow_index) LIMIT ?;",
            (match_expr, FLOW_FTS_LIMIT),
        ).fetchall()
    except sqlite3.Error as err:
        logger.debug("FTS MATCH по сценариям не выполнен: %s", err)
        return {}
    return {str(name): FLOW_FTS_BONUS / (position + 1) for position, (name,) in enumerate(rows)}
