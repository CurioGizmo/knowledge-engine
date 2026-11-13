"""Тесты Шага 6 плана skills-correctness-and-search: устранение SQL N+1 при реранке.

1. При поиске с несколькими кандидатами НЕ выполняется отдельный probe-запрос
   (``WHERE rowid = ?`` + ``bm25``) на каждого кандидата: флаги совпадения
   колонок (match_name/match_title/...) вычисляются в SELECT основного запроса.
2. Ранжирование сохраняет корректный порядок с учётом весов колонок и
   local_match.
3. Сценарии: пустой запрос (без bm25 — иначе ошибка SQLite вне MATCH),
   запрос с категорией, запрос с project_stack.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mcp_core.sanitizer import sanitize_fts_query
from mcp_skills.models import SkillCard
from mcp_skills.storage import (
    BLOCKS_SECTION_MARKER,
    SkillsStorage,
    _build_search_sql,
    _section_header,
    _skill_hit_flags,
)

_SKILL_MD_TMPL = """---
name: {name}
title: {title}
description: {description}
category: {category}
stack: {stack}
---

# {title}

Body: {body}
"""


def _add_skill(
    storage: SkillsStorage,
    name: str,
    title: str,
    description: str,
    category: str = "general",
    stack: list[str] | None = None,
    body: str = "unrelated body text",
    requires_stack: list[str] | dict[str, list[str]] | None = None,
) -> None:
    card = SkillCard(
        name=name,
        title=title,
        description=description,
        category=category,
        stack=stack or [],
        requires_stack=requires_stack or [],
    )
    content = _SKILL_MD_TMPL.format(
        name=name,
        title=title,
        description=description,
        category=category,
        stack=" ".join(stack or []),
        body=body,
    )
    storage.index_skill(card, content)


def _make_storage(tmp_path: Path) -> SkillsStorage:
    return SkillsStorage(tmp_path / "skills.db")


def _make_multi_candidate_storage(tmp_path: Path) -> SkillsStorage:
    """Каталог из 10 карточек, где все находятся по запросу 'deploy'."""
    storage = _make_storage(tmp_path)
    for i in range(10):
        _add_skill(
            storage,
            name=f"deploy-helper-{i}",
            title=f"Deploy Helper {i}",
            description="Tooling that mentions deploy workflows and pipelines",
        )
    return storage


def _probe_query_count(queries: list[str]) -> int:
    """Число probe-запросов: WHERE rowid = ? с bm25 или одиночный bm25-SELECT."""
    return sum(
        1
        for q in queries
        if "bm25" in q.lower() and ("rowid = ?" in q or re.search(r"SELECT\s+bm25", q, re.I))
    )


@contextmanager
def _traced_connection(storage: SkillsStorage, queries: list[str]) -> Iterator[None]:
    """Подменяет get_connection хранилища на соединение с trace-колбэком.

    search_skills открывает собственное соединение к тому же файлу — прямой
    перехват невозможен, поэтому подменяем фабрику соединений.
    """
    orig_get_connection = storage.get_connection

    @contextmanager
    def _patched() -> Iterator[Any]:
        with orig_get_connection() as conn:
            conn.set_trace_callback(queries.append)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    storage.get_connection = _patched  # type: ignore[method-assign]
    try:
        yield
    finally:
        storage.get_connection = orig_get_connection  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# 1. Устранение SQL N+1
# ---------------------------------------------------------------------------


def test_no_per_candidate_probe_queries(tmp_path: Path) -> None:
    """Поиск по 10 кандидатам не порождает probe-запросов WHERE rowid = ?/bm25."""
    storage = _make_multi_candidate_storage(tmp_path)
    queries: list[str] = []

    with _traced_connection(storage, queries):
        res = storage.search_skills("deploy", limit=10)

    assert not isinstance(res, tuple)
    assert len(res) >= 5, "кандидаты не найдены"
    probe_count = _probe_query_count(queries)
    assert probe_count == 0, (
        f"SQL N+1 не устранён: выполнено {probe_count} probe-запросов "
        f"(WHERE rowid = ? / bm25) на {len(res)} кандидатов"
    )
    # Основной поисковый запрос содержит встроенные probe-выражения.
    main_selects = [q for q in queries if "AS match_name" in q]
    assert main_selects, "match_name не вычисляется в основном SELECT"
    assert "AS match_title" in main_selects[0]
    assert "AS match_description" in main_selects[0]
    assert "AS match_stack" in main_selects[0]


def test_probe_flags_computed_in_single_select(tmp_path: Path) -> None:
    """Флаги совпадения колонок читаются из строки результата без доп. запросов."""
    storage = _make_multi_candidate_storage(tmp_path)
    query = "deploy"
    clean_q = sanitize_fts_query(query)
    sql, params = _build_search_sql(clean_q, None, with_offset=False)
    params.append(40)

    queries: list[str] = []
    with storage.get_connection() as conn:
        conn.set_trace_callback(queries.append)
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.set_trace_callback(None)

    assert len(rows) == 10
    r = rows[0]
    assert "match_name" in r.keys()
    assert "match_title" in r.keys()
    assert bool(r["match_name"]) is True  # 'deploy' в name: deploy-helper-N

    with storage.get_connection() as conn:
        conn.set_trace_callback(queries.append)
        try:
            flags = _skill_hit_flags(conn, r, query, clean_q, r["name"])
        finally:
            conn.set_trace_callback(None)

    assert flags["name"] is True
    assert flags["title"] is True
    assert flags["description"] is True
    assert flags["stack"] is False
    assert flags["local_match"] is False
    # Ни одного probe-запроса. Trace ловит и внутренние statement'ы FTS5
    # (PRAGMA/segments) — приложение выполнило ровно один SELECT с
    # probe-выражениями, и _skill_hit_flags не добавил ни одного запроса.
    assert _probe_query_count(queries) == 0
    app_selects = [q for q in queries if "AS match_name" in q]
    assert len(app_selects) == 1, f"ожидался единственный основной SELECT, получено: {app_selects}"


def test_probe_query_counter_helper() -> None:
    """Счётчик probe-запросов корректно распознаёт старый N+1-паттерн."""
    legacy_n_plus_one = (
        "SELECT bm25(t, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0) FROM t WHERE t MATCH ? AND rowid = ?"
    )
    assert _probe_query_count([legacy_n_plus_one]) == 1
    assert _probe_query_count(["SELECT name FROM skills_index"]) == 0
    # Новый встроенный SELECT (MATCH-ветка) не считается probe-запросом.
    inline = "SELECT rowid, name, bm25(skills_index, 1.0) != 0.0 AS match_name FROM skills_index WHERE skills_index MATCH ?"
    assert _probe_query_count([inline]) == 0


# ---------------------------------------------------------------------------
# 2. Ранжирование сохраняет порядок (веса колонок и local_match)
# ---------------------------------------------------------------------------


def test_ranking_prefers_name_column_matches(tmp_path: Path) -> None:
    """Совпадение по name (вес 1.5) выше совпадения только по description (0.25)."""
    storage = _make_storage(tmp_path)
    # Карточка со совпадением 'deploy' только в description.
    _add_skill(
        storage,
        name="z-card-desc-match",
        title="Unrelated Title",
        description="deploy в описании",
    )
    # Карточка со совпадением 'deploy' в name (колонка FTS name хранит имя
    # скилла) — бонус 1.5 перекрывает базовый bm25-балл карточки-соперника.
    _add_skill(
        storage,
        name="deploy-master-card",
        title="Unrelated Title",
        description="совсем другое описание",
    )

    res = storage.search_skills("deploy", limit=10)
    assert not isinstance(res, tuple)
    names = [c.name for c in res]
    assert "deploy-master-card" in names and "z-card-desc-match" in names
    assert names.index("deploy-master-card") < names.index("z-card-desc-match")


def test_ranking_local_match_bonus(tmp_path: Path) -> None:
    """local_match (бонус 0.5) поднимает карточку с локальной секцией выше."""
    storage = _make_storage(tmp_path)
    # Две карточки: у одной блочная секция с 'replicator' (local), у другой нет.
    _add_skill(
        storage,
        name="x-card-plain",
        title="Replicator Setup",
        description="description about replicator",
    )
    _add_skill(
        storage,
        name="y-card-local",
        title="Replicator Guide",
        description="guide about replicator",
    )
    # Добавляем локальную секцию в content карточки y-card-local напрямую
    # (синхронизатор собрал бы её из blocks/, здесь проще вставить вручную).
    card = SkillCard(
        name="y-card-local",
        title="Replicator Guide",
        description="guide about replicator",
    )
    blocks_payload = (
        _section_header(
            {"type": "slot", "slot": "core", "variant": "default", "origin": "local:default.md"}
        )
        + "\nreplicator deep dive text"
    )
    content = (
        _SKILL_MD_TMPL.format(
            name="y-card-local",
            title="Replicator Guide",
            description="guide about replicator",
            category="general",
            stack="",
            body="base body",
        ).rstrip()
        + "\n\n"
        + BLOCKS_SECTION_MARKER
        + "\n"
        + blocks_payload
    )
    storage.delete_skill("y-card-local")
    storage.index_skill(card, content)

    res = storage.search_skills("replicator", limit=10)
    assert not isinstance(res, tuple)
    names = [c.name for c in res]
    assert names[0] == "y-card-local", (
        f"карточка с local_match должна быть первой, получено: {names}"
    )
    # Секция — slot (не resource): matched_resources пуст, но local_match
    # учтён флагами реранка.
    y_card = next(c for c in res if c.name == "y-card-local")
    assert y_card.matched_resources == []


# ---------------------------------------------------------------------------
# 3. Сценарии: пустой запрос, категория, project_stack
# ---------------------------------------------------------------------------


def test_empty_query_no_bm25_calls(tmp_path: Path) -> None:
    """Пустой запрос: сортировка по name, bm25 не вызывается вовсе."""
    storage = _make_multi_candidate_storage(tmp_path)
    queries: list[str] = []

    with _traced_connection(storage, queries):
        res = storage.search_skills("", limit=5)

    assert not isinstance(res, tuple)
    assert len(res) == 5
    names = [c.name for c in res]
    assert names == sorted(names), "сортировка по name"
    assert all("bm25" not in q.lower() for q in queries), (
        "bm25 вызван при пустом запросе (SQLite вернул бы ошибку вне MATCH)"
    )


def test_query_with_category_no_probes(tmp_path: Path) -> None:
    """Запрос с категорией: фильтр в основном запросе, probe N+1 отсутствует."""
    storage = _make_storage(tmp_path)
    _add_skill(
        storage,
        name="cat-a-deploy",
        title="Deploy A",
        description="deploy description a",
        category="backend-db",
    )
    _add_skill(
        storage,
        name="cat-a-other",
        title="Other A",
        description="other description",
        category="backend-db",
    )
    _add_skill(
        storage,
        name="cat-b-deploy",
        title="Deploy B",
        description="deploy description b",
        category="frontend",
    )
    queries: list[str] = []

    with _traced_connection(storage, queries):
        res = storage.search_skills("deploy", category="backend-db", limit=10)

    assert not isinstance(res, tuple)
    names = [c.name for c in res]
    assert "cat-a-deploy" in names
    assert "cat-b-deploy" not in names
    assert _probe_query_count(queries) == 0


def test_query_with_project_stack_no_probes(tmp_path: Path) -> None:
    """Запрос с project_stack: стек-ветка тоже без probe N+1, результат корректен."""
    storage = _make_storage(tmp_path)
    _add_skill(
        storage,
        name="stack-match",
        title="Deploy Stack",
        description="deploy with fastapi stack",
        stack=["fastapi"],
    )
    _add_skill(
        storage,
        name="stack-nope",
        title="Deploy Nope",
        description="deploy with something else",
    )
    queries: list[str] = []

    with _traced_connection(storage, queries):
        res = storage.search_skills("deploy", limit=10, project_stack=["fastapi"])

    assert isinstance(res, tuple)
    cards, _truncated = res
    names = [c.name for c in cards]
    assert "stack-match" in names
    assert _probe_query_count(queries) == 0, "probe N+1 в стек-ветке поиска"


def test_project_stack_incompatible_filtered(tmp_path: Path) -> None:
    """Стек-ветка: несовместимые карточки вымываются, совместимые остаются."""
    storage = _make_storage(tmp_path)
    _add_skill(
        storage,
        name="ok-skill",
        title="Deploy Ok",
        description="deploy description ok",
    )
    _add_skill(
        storage,
        name="incomp-skill",
        title="Deploy Incomp",
        description="deploy description incomp",
        requires_stack={"none_of": ["tauri"]},
    )

    res = storage.search_skills("deploy", limit=10, project_stack=["tauri"])
    assert isinstance(res, tuple)
    cards, _truncated = res
    names = [c.name for c in cards]
    assert "ok-skill" in names
    assert "incomp-skill" not in names
