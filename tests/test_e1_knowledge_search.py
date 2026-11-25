"""TDD тесты для пункта E1: улучшение поиска knowledge.

1. unicode61 основным токенизатором (+ проверка границ слов vs подстроки): «дан*» не матчит «преданный».
2. Стоп-слова LLM-запросов («как», «мне», «нужно», «пожалуйста», «подскажи», «что делать» и т.п.) не ломают поиск.
3. AND -> OR фолбэк: строгий AND FTS при 0 результатов автоматически повторяется с OR.
4. aliases в CTE: убрать N+1 при поиске — limit=5 -> 0 зондирующих запросов.
5. Ревизия MAX_TOTAL_SNIPPETS.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mcp_core.fts import fts5_table_sql, resolve_tokenizer
from mcp_core.sanitizer import LLM_QUERY_STOP_WORDS, sanitize_fts_query
from mcp_knowledge.models import (
    CardRecordRequest,
    KnowledgeCategory,
    KnowledgeSource,
    KnowledgeStatus,
    KnowledgeType,
)
from mcp_knowledge.storage import (
    MAX_TOTAL_SNIPPETS,
    KnowledgeStorage,
)
from mcp_knowledge.parser import serialize_card_markdown


def _create_card(
    storage: KnowledgeStorage,
    card_id: str,
    title: str,
    category: KnowledgeCategory = KnowledgeCategory.BACKEND,
    card_type: KnowledgeType = KnowledgeType.LESSON,
    status: KnowledgeStatus = KnowledgeStatus.ACTIVE,
    source: KnowledgeSource = KnowledgeSource.USER_CONFIRMED,
    symptom: str = "Симптом проблемы и контекст ошибки",
    solution: str | None = None,
    aliases: list[str] | None = None,
    task_id: str | None = None,
) -> None:
    sol = (
        solution
        or "Подробное и детальное описание решения проблемы, занимающее более пятидесяти символов для прохождения валидатора"
    )
    req = CardRecordRequest(
        title=title,
        category=category,
        type=card_type,
        status=status,
        source=source,
        symptom=symptom,
        root_cause="Коренная причина ошибки заключается в некорректной конфигурации",
        solution=sol,
        rationale="Обоснование решения с подробным объяснением архитектурных причин",
        aliases=aliases or [],
        task_id=task_id,
    )
    card_path = storage.knowledge_dir / f"{card_id}.md"
    card_path.write_text(serialize_card_markdown(card_id, req), encoding="utf-8")
    storage.sync()


# ---------------------------------------------------------------------------
# 1. unicode61 и границы слов vs подстроки
# ---------------------------------------------------------------------------


def test_tokenizer_is_unicode61_for_knowledge() -> None:
    """Токенизатор для knowledge — unicode61 remove_diacritics 2."""
    tokenizer = resolve_tokenizer("unicode61")
    assert tokenizer.startswith("unicode61")
    assert "remove_diacritics 2" in tokenizer


def test_unicode61_word_boundary_does_not_match_substring(tmp_path: Path) -> None:
    """unicode61 уважает границы слов: 'дан*' матчит 'данные', но НЕ матчит 'преданный'."""
    db_path = tmp_path / "knowledge.db"
    k_dir = tmp_path / "knowledge"
    k_dir.mkdir()
    storage = KnowledgeStorage(db_path, knowledge_dir=k_dir)

    _create_card(
        storage,
        card_id="card-loyal",
        title="Преданный друг проекта",
        symptom="Случайное слово преданный в тексте",
    )
    _create_card(
        storage,
        card_id="card-data",
        title="База данных и миграции",
        symptom="Данные повреждены при сбое",
    )

    # Проверяем, что запрос "дан*" находит "данные", но НЕ находит "преданный"
    res = storage.search("дан")
    res_ids = {c.id for c in res}
    assert "card-data" in res_ids, "Запрос 'дан' должен находить 'данные'"
    assert "card-loyal" not in res_ids, "Запрос 'дан' НЕ должен матчить подстроку в 'преданный'"


# ---------------------------------------------------------------------------
# 2. Стоп-слова LLM-запросов
# ---------------------------------------------------------------------------


def test_llm_stop_words_empty_or_frozenset() -> None:
    """Стоп-слова упразднены в пользу BM25 и длины токенов; frozenset пуст."""
    assert isinstance(LLM_QUERY_STOP_WORDS, frozenset)
    assert len(LLM_QUERY_STOP_WORDS) == 0


def test_sanitizer_filters_short_tokens_and_preserves_terms() -> None:
    """Санитайзер отсекает короткие предлоги (< 3 символов), сохраняя смысловые термины."""
    cleaned = sanitize_fts_query("в на по to in on sqlite дедлок")
    assert "sqlite" in cleaned
    assert "дедлок" in cleaned
    assert "в*" not in cleaned
    assert "на*" not in cleaned
    assert "to*" not in cleaned


def test_sanitizer_preserves_query_when_conversational() -> None:
    """Разговорный запрос формирует валидное FTS-выражение без вырождения в пустую строку."""
    cleaned = sanitize_fts_query("что делать")
    assert cleaned and cleaned != '""'
    assert "что" in cleaned or "делать" in cleaned


def test_llm_conversational_query_finds_relevant_card(tmp_path: Path) -> None:
    """Разговорный запрос агента ('подскажи пожалуйста как исправить...') находит нужную карточку."""
    db_path = tmp_path / "knowledge.db"
    k_dir = tmp_path / "knowledge"
    k_dir.mkdir()
    storage = KnowledgeStorage(db_path, knowledge_dir=k_dir)

    _create_card(
        storage,
        card_id="card-sqlite-deadlock",
        title="Дедлок в SQLite при транзакциях",
        symptom="Ошибка database is locked при параллельной записи",
        solution="Использовать WAL режим и настроить busy_timeout на пять тысяч миллисекунд для предотвращения блокировок",
    )

    results = storage.search("подскажи пожалуйста как исправить дедлок sqlite")
    assert len(results) >= 1
    assert results[0].id == "card-sqlite-deadlock"


# ---------------------------------------------------------------------------
# 3. AND -> OR фолбэк
# ---------------------------------------------------------------------------


def test_and_to_or_fallback_behavior(tmp_path: Path) -> None:
    """Если строгий AND ничего не находит, поиск автоматически повторяется с OR."""
    db_path = tmp_path / "knowledge.db"
    k_dir = tmp_path / "knowledge"
    k_dir.mkdir()
    storage = KnowledgeStorage(db_path, knowledge_dir=k_dir)

    _create_card(
        storage,
        card_id="card-sqlite",
        title="SQLite оптимизация",
        symptom="Тормозит база данных при выборке",
    )
    _create_card(
        storage,
        card_id="card-postgres",
        title="PostgreSQL миграции",
        symptom="Ошибка в alembic",
    )

    # Запрос со словами из разных карточек: ни одна карточка не содержит оба слова
    # Строгий AND даст 0 результатов, но OR фолбэк должен найти обе карточки!
    results = storage.search("sqlite postgresql")
    found_ids = {r.id for r in results}
    assert "card-sqlite" in found_ids
    assert "card-postgres" in found_ids


def test_and_prioritizes_exact_multi_term_matches(tmp_path: Path) -> None:
    """Если строгий AND находит совпадения, OR-фолбэк не подмешивает лишний шум."""
    db_path = tmp_path / "knowledge.db"
    k_dir = tmp_path / "knowledge"
    k_dir.mkdir()
    storage = KnowledgeStorage(db_path, knowledge_dir=k_dir)

    _create_card(
        storage,
        card_id="card-both",
        title="SQLite WAL дедлок",
        symptom="Взаимная блокировка потоков",
    )
    _create_card(
        storage,
        card_id="card-sqlite-only",
        title="SQLite базовые команды",
        symptom="Как создать таблицу",
    )

    # Запрос "wal sqlite" должен строго вернуть только card-both
    results = storage.search("wal sqlite", limit=5)
    found_ids = [r.id for r in results]
    assert "card-both" in found_ids
    assert "card-sqlite-only" not in found_ids, "Строгий AND не должен подмешивать card-sqlite-only"


# ---------------------------------------------------------------------------
# 4. aliases в CTE: убрать N+1 при поиске (limit=5 -> 0 зондирующих запросов)
# ---------------------------------------------------------------------------


def test_zero_probing_queries_during_search(tmp_path: Path) -> None:
    """limit=5 дает ровно 0 зондирующих SQL-запросов (SELECT bm25 по отдельным колонкам)."""
    db_path = tmp_path / "knowledge.db"
    k_dir = tmp_path / "knowledge"
    k_dir.mkdir()
    storage = KnowledgeStorage(db_path, knowledge_dir=k_dir)

    for i in range(5):
        _create_card(
            storage,
            card_id=f"card-{i}",
            title=f"Тестовая карточка {i} по архитектуре",
            aliases=[f"алиас-{i}", "архитектура"],
            symptom=f"Симптом ошибки номер {i}",
        )

    # Считаем количество выполненных SQL запросов при вызове search()
    with storage.get_connection() as conn:
        executed_queries: list[str] = []

        def trace_callback(sql: str) -> None:
            executed_queries.append(sql)

        conn.set_trace_callback(trace_callback)
        # Вызываем search
        results = storage.search("архитектура", limit=5)
        conn.set_trace_callback(None)

    assert len(results) == 5

    # Проверяем, что нет зондирующих SELECT bm25(knowledge_index, ...) по единичным строкам
    probing_queries = [
        q
        for q in executed_queries
        if "WHERE knowledge_index MATCH ? AND" in q
        or ("bm25(knowledge_index," in q and "id = ?" in q)
    ]
    assert len(probing_queries) == 0, (
        f"Ожидалось 0 зондирующих запросов, получено {len(probing_queries)}"
    )


def test_field_bonus_applied_via_cte_flags(tmp_path: Path) -> None:
    """Флаги колонок (title, aliases, symptom) корректно вычисляются и влияют на реранк."""
    db_path = tmp_path / "knowledge.db"
    k_dir = tmp_path / "knowledge"
    k_dir.mkdir()
    storage = KnowledgeStorage(db_path, knowledge_dir=k_dir)

    # Карточка 1: совпадение в title (бонус +1.0)
    _create_card(
        storage,
        card_id="card-title-match",
        title="Оптимизация кеша Redis",
        symptom="Обычный текст без ключевых слов",
    )
    # Карточка 2: совпадение только в теле/solution (без бонуса поля)
    _create_card(
        storage,
        card_id="card-body-match",
        title="Другая тема совсем",
        symptom="Обычный текст",
        solution="Тут подробно и детально описывается оптимизация кеша в распределенных системах для ускорения запросов",
    )

    results = storage.search("кеша", limit=5)
    assert len(results) >= 2
    # card-title-match должна быть первой благодаря field bonus title
    assert results[0].id == "card-title-match"


# ---------------------------------------------------------------------------
# 5. Ревизия MAX_TOTAL_SNIPPETS
# ---------------------------------------------------------------------------


def test_max_total_snippets_constant() -> None:
    """Константа MAX_TOTAL_SNIPPETS равна 12, ограничивая размер контекста LLM."""
    assert MAX_TOTAL_SNIPPETS == 12


# ---------------------------------------------------------------------------
# 6. K-SEARCH: подсказка о черновиках (draft) при пустой видимой выдаче
# ---------------------------------------------------------------------------


def test_search_hint_when_only_matching_drafts(tmp_path: Path) -> None:
    """При пустой видимой выдаче и совпадениях только в draft возвращается hint с советом повторить."""
    db_path = tmp_path / "knowledge.db"
    k_dir = tmp_path / "knowledge"
    k_dir.mkdir()
    storage = KnowledgeStorage(db_path, knowledge_dir=k_dir)

    _create_card(
        storage,
        card_id="card-draft-wal",
        title="SQLite WAL mode deadlock",
        symptom="Блокировка базы при высокой нагрузке sqlite wal",
        status=KnowledgeStatus.DRAFT,
    )

    # 1. Поиск без include_drafts: видимая выдача пуста, но hint содержит совет
    res = storage.search_faceted(query="sqlite wal")
    assert len(res.query_results) == 0
    assert len(res.category_results) == 0
    assert res.hint is not None
    assert "include_drafts=true" in res.hint.lower()
    assert "task_id" in res.hint.lower()

    # 2. Поиск с include_drafts=True: черновик виден, hint о черновиках не нужен
    res_drafts = storage.search_faceted(query="sqlite wal", include_drafts=True)
    assert len(res_drafts.query_results) == 1
    assert res_drafts.hint is None or "include_drafts=true" not in res_drafts.hint.lower()

    # 3. Добавляем активную карточку: видимая выдача не пуста -> нет hint о черновиках
    _create_card(
        storage,
        card_id="card-active-wal",
        title="SQLite WAL settings active",
        symptom="Настройка режима sqlite wal в продакшене",
        status=KnowledgeStatus.ACTIVE,
    )
    res_active = storage.search_faceted(query="sqlite wal")
    assert len(res_active.query_results) >= 1
    assert res_active.hint is None or "include_drafts=true" not in res_active.hint.lower()

    # 4. Запрос, которого нет ни в active, ни в draft -> нет hint о черновиках
    res_none = storage.search_faceted(query="абсолютнонесуществующийтермин")
    assert len(res_none.query_results) == 0
    assert res_none.hint is None or "include_drafts=true" not in res_none.hint.lower()


def test_search_hint_respects_task_and_category(tmp_path: Path) -> None:
    """Подсказка о черновиках учитывает переданные category и task_id."""
    db_path = tmp_path / "knowledge.db"
    k_dir = tmp_path / "knowledge"
    k_dir.mkdir()
    storage = KnowledgeStorage(db_path, knowledge_dir=k_dir)

    _create_card(
        storage,
        card_id="card-draft-backend",
        title="Backend draft solution",
        category=KnowledgeCategory.BACKEND,
        symptom="Ошибка при обработке запроса backend draft worker",
        status=KnowledgeStatus.DRAFT,
        task_id="task-100",
    )

    # 1. Запрос по категории frontend: черновик в backend не должен вызывать hint
    res_front = storage.search_faceted(
        query="worker",
        categories=["frontend"],
    )
    assert len(res_front.query_results) == 0
    assert res_front.hint is None or "include_drafts=true" not in res_front.hint.lower()

    # 2. Запрос со своим task_id: черновик сразу виден в результатах
    res_own_task = storage.search_faceted(
        query="worker",
        task_id="task-100",
    )
    assert len(res_own_task.query_results) == 1
    assert res_own_task.hint is None or "include_drafts=true" not in res_own_task.hint.lower()

    # 3. Запрос с чужим task_id: черновик скрыт -> появляется hint
    res_other_task = storage.search_faceted(
        query="worker",
        task_id="task-200",
    )
    assert len(res_other_task.query_results) == 0
    assert res_other_task.hint is not None
    assert "include_drafts=true" in res_other_task.hint.lower()
    assert "task_id" in res_other_task.hint.lower()


# ---------------------------------------------------------------------------
# 7. K-INDEX: шаблонные заголовки секций не индексируются в body
# ---------------------------------------------------------------------------


def test_template_headers_not_indexed_in_body(tmp_path: Path) -> None:
    """Запрос по шаблонному заголовку секции (напр. 'Обоснование') не матчит карточки по заголовку шаблона."""
    db_path = tmp_path / "knowledge.db"
    k_dir = tmp_path / "knowledge"
    k_dir.mkdir()
    storage = KnowledgeStorage(db_path, knowledge_dir=k_dir)

    _create_card(
        storage,
        card_id="card-alpha",
        title="Альфа сервис и настройка пула",
        symptom="Сбой таймера в сетевом клиенте",
        solution="Увеличен интервал опроса соединений на сервере базы данных",
    )
    _create_card(
        storage,
        card_id="card-beta",
        title="Бета сервис и конфигурация памяти",
        symptom="Утечка дескрипторов в фоновом воркере",
        solution="Закрытие файловых потоков в блоке очистки ресурсов",
    )

    # 1. Поиск по шаблонным словам из заголовков Markdown ('Rationale', 'Первопричина')
    # Не должен находить карточки только из-за заголовка секции (### Первопричина (Root Cause), ### Обоснование ... (Rationale))
    res_rationale = storage.search("Rationale")
    assert len(res_rationale) == 0, (
        f"Шаблонный заголовок Rationale дал ложные матчи: {[r.id for r in res_rationale]}"
    )

    res_root = storage.search("Первопричина")
    assert len(res_root) == 0, (
        f"Шаблонный заголовок Первопричина дал ложные матчи: {[r.id for r in res_root]}"
    )

    # 2. Поиск по реальному содержимому решения находит нужную карточку с сохранением релевантности
    res_content = storage.search("интервал опроса")
    assert len(res_content) == 1
    assert res_content[0].id == "card-alpha"


def test_all_empty_sections_body_is_empty(tmp_path: Path) -> None:
    """Ревью шага 3 (дополнение): карточка с полностью пустыми секциями не тащит
    сырой контент (frontmatter + шаблонные ###-заголовки) в body индекса.

    Прежний fallback возвращал весь content, включая '---\\nid: ...' и
    '### Симптом (Symptom)' — мусор в FTS. Теперь body пуст.
    """
    from mcp_knowledge.synchronizer import _card_body_content
    from mcp_knowledge.parser import parse_card_markdown

    content = """---
id: test-empty-card
title: Test
category: backend
type: lesson
status: draft
---

### Симптом (Symptom)

### Решение / Выводы
"""
    fm, sections = parse_card_markdown(content)
    body = _card_body_content(sections, content)
    assert body == ""
    # Шаблонные заголовки и frontmatter не попадают в индекс
    assert "Симптом" not in body
    assert "---" not in body

    # Смешанный случай: непустая секция по-прежнему индексируется
    content_mixed = content.replace(
        "### Решение / Выводы\n", "### Решение / Выводы\nнастоящее решение\n"
    )
    fm2, sections2 = parse_card_markdown(content_mixed)
    body2 = _card_body_content(sections2, content_mixed)
    assert body2 == "настоящее решение"


def test_knowledge_search_score_precision_non_zero(tmp_path: Path) -> None:
    """SCORE (41e9): knowledge search сохраняет ненулевой score для малых значений BM25."""
    db_path = tmp_path / "knowledge.db"
    k_dir = tmp_path / "knowledge"
    k_dir.mkdir()
    storage = KnowledgeStorage(db_path, knowledge_dir=k_dir)

    long_solution = "микросекунда " + "обычный текст решения проблемы базы данных " * 300
    _create_card(
        storage,
        card_id="card-micro",
        title="Тест точности ранжирования",
        symptom="Обычный симптом",
        solution=long_solution,
    )

    res = storage.search("микросекунда")
    assert len(res) == 1
    assert res[0].score is not None
    assert res[0].score > 0.0, f"Score не должен округляться в 0.0, получено {res[0].score}"
