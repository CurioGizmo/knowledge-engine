"""Тесты миграции SQL-слоя knowledge: переименование domain → category (Задача 2).

Проверяют новую схему FTS5-кэша (category UNINDEXED без domain), позицию веса
category в BM25_WEIGHTS, очистку от legacy-ключей domain, а также пересоздание
кэша при дрейфе старой схемы (с колонкой domain).
"""

import sqlite3
from pathlib import Path

from mcp_knowledge.models import (
    CardRecordRequest,
    CategoryOverviewItem,
    KnowledgeCategory,
    KnowledgeSource,
    KnowledgeType,
    ProjectOverview,
)
from mcp_knowledge.parser import serialize_card_markdown
from mcp_core.fts import fts5_table_sql, resolve_tokenizer
from mcp_knowledge.storage import (
    BM25_WEIGHTS,
    KNOWLEDGE_FTS_COLUMNS,
    KNOWLEDGE_FTS_UNINDEXED,
    REQUIRED_FTS_COLUMNS,
    KnowledgeStorage,
)


def _write_card(knowledge_dir: Path, card_id: str, title: str = "Тестовая карточка") -> None:
    req = CardRecordRequest(
        title=title,
        category=KnowledgeCategory.AUDIO,
        source=KnowledgeSource.USER_CONFIRMED,
        type=KnowledgeType.LESSON,
        symptom=f"Симптом карточки {card_id} для проверки миграции",
        root_cause="Переименование domain в category в SQL-слое",
        solution="Переход на category на уровне выходных моделей полностью и навсегда без алиасов",
        rationale="Каноническая модель без устаревших алиасов",
    )
    card_path = knowledge_dir / f"{card_id}.md"
    card_path.write_text(serialize_card_markdown(card_id, req), encoding="utf-8")


def test_knowledge_schema_has_category_unindexed(tmp_path: Path):
    """
    Вход: Инициализация KnowledgeStorage на чистом temp-кэше.
    Выход: Схема таблицы knowledge_index.
    Проверка: Схема содержит category UNINDEXED и не содержит domain;
    REQUIRED_FTS_COLUMNS содержит 'category'.
    """
    cache_db = tmp_path / "knowledge_fts.db"
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir(parents=True)
    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)

    with storage.get_connection() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(knowledge_index);").fetchall()]
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'knowledge_index';"
        ).fetchone()[0]

    assert "category" in cols
    assert "domain" not in cols
    # DDL содержит category UNINDEXED и не содержит domain-колонку
    assert "category UNINDEXED" in sql
    assert "domain UNINDEXED" not in sql
    # Drift-check видит новую колонку как обязательную
    assert "category" in REQUIRED_FTS_COLUMNS
    assert "domain" not in REQUIRED_FTS_COLUMNS


def test_knowledge_bm25_weight_position():
    """
    Вход: Кортеж BM25_WEIGHTS.
    Выход: Позиция веса колонки category.
    Проверка: Вес category стоит на позиции 2 (0-based), как и в DDL FTS5-схемы;
    число весов совпадает с числом колонок FTS5-таблицы (20).
    """
    assert len(BM25_WEIGHTS) == 20
    assert BM25_WEIGHTS[2] == 0.0

    ddl = fts5_table_sql(
        "knowledge_index",
        KNOWLEDGE_FTS_COLUMNS,
        unindexed=KNOWLEDGE_FTS_UNINDEXED,
        tokenizer=resolve_tokenizer(),
    )
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(ddl)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(knowledge_index);").fetchall()]
    finally:
        conn.close()

    assert len(cols) == len(BM25_WEIGHTS)
    assert cols[2] == "category"


def test_knowledge_cards_and_snippets(tmp_path: Path):
    """
    Вход: Каталог с одной карточкой; вызовы get_cards_by_ids и search_faceted.
    Выход: Словари карточек и сниппеты.
    Проверка: Каждый dict карточки содержит ключ 'category' и не содержит 'domain';
    сниппеты search_faceted содержат категорию.
    """
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir(parents=True)
    _write_card(knowledge_dir, "audio-test-dual-key")

    cache_db = tmp_path / "knowledge_fts.db"
    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)

    cards = storage.get_cards_by_ids(["audio-test-dual-key"])
    assert len(cards) == 1
    card = cards[0]
    assert "category" in card
    assert "domain" not in card
    assert card["category"] == "audio"

    response = storage.search_faceted(
        query="миграции", categories=["audio"], include_overview=False
    )
    snippets = response.query_results + response.category_results.get("audio", [])
    assert snippets
    for snippet in snippets:
        assert snippet.category == "audio"
        assert not hasattr(snippet, "domain")

    list_cards = storage.list_recent_cards(since="2000-01-01", source=None)
    assert list_cards and "category" in list_cards[0] and "domain" not in list_cards[0]


def test_knowledge_drift_check(tmp_path: Path):
    """
    Вход: База SQLite со старой схемой (колонка domain, без category) и карточка на диске.
    Выход: Повторная инициализация KnowledgeStorage.
    Проверка: Хранилище пересоздаёт эфемерный кэш на новую схему (category):
    колонка domain отсутствует, category присутствует, поиск работает.
    """
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir(parents=True)
    _write_card(knowledge_dir, "audio-test-drift")
    cache_db = tmp_path / "knowledge_fts.db"

    # Создаем старую схему вручную (полный набор v-колонок, но с domain вместо category)
    ddl = fts5_table_sql(
        "knowledge_index",
        KNOWLEDGE_FTS_COLUMNS,
        unindexed=KNOWLEDGE_FTS_UNINDEXED,
        tokenizer=resolve_tokenizer(),
    )
    legacy_sql = ddl.replace("category UNINDEXED", "domain UNINDEXED")
    conn = sqlite3.connect(cache_db)
    try:
        conn.execute(legacy_sql)
        conn.execute(
            "CREATE TABLE file_sync (path TEXT PRIMARY KEY, mtime REAL, size INTEGER, card_id TEXT);"
        )
        # Сеем маркерную строку в старую схему: без drift-check она бы осталась
        conn.execute(
            """
            INSERT INTO knowledge_index (
                id, title, domain, type, status, source, generated_by, created_at,
                stale_after, verified_count, confidence, task_id, aliases, symptom,
                root_cause, solution, rationale, refutation_reason, tags, body
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                "legacy-drift-marker",
                "Легаси-маркер старой схемы",
                "audio",
                "lesson",
                "active",
                "user_confirmed",
                "process",
                "2026-10-14",
                None,
                0,
                0.75,
                None,
                "",
                "легаси симптом дрейфа схемы",
                "",
                "",
                "",
                "",
                "",
                "",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # Инициализация обязана увидеть дрейф схемы и пересоздать кэш
    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)
    storage.sync()

    with storage.get_connection() as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(knowledge_index);").fetchall()]
        legacy_rows = c.execute(
            "SELECT count(*) FROM knowledge_index WHERE id = 'legacy-drift-marker';"
        ).fetchone()[0]

    assert "category" in cols
    assert "domain" not in cols
    assert legacy_rows == 0

    results = storage.search(query="миграции")
    assert len(results) == 1
    assert results[0].id == "audio-test-drift"
    assert results[0].category == "audio"


def test_project_overview(tmp_path: Path) -> None:
    """Проверяет каноническую модель ProjectOverview и CategoryOverviewItem."""
    item_cat = CategoryOverviewItem(category="backend", count=10, sample_titles=["Title 1"])
    assert item_cat.category == "backend"
    assert not hasattr(item_cat, "domain")

    overview_cat = ProjectOverview(total_cards=10, categories=[item_cat])
    assert len(overview_cat.categories) == 1
    assert overview_cat.categories[0].category == "backend"
    assert not hasattr(overview_cat, "domains")

    # Проверка вызова storage
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    _write_card(knowledge_dir, "audio-test-overview", title="Тест overview")

    cache_db = tmp_path / "cache.db"
    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)
    storage.sync()

    overview = storage.get_project_overview()

    assert overview.total_cards == 1
    assert len(overview.categories) == 1
    assert overview.categories[0].category == "audio"
    assert overview.categories[0].sample_titles == ["Тест overview"]

    # search_faceted с include_overview=True
    res_ov = storage.search_faceted(include_overview=True)
    assert res_ov.overview is not None
    assert not hasattr(res_ov, "domain_results")
    assert res_ov.overview.total_cards == 1

    # search_faceted с include_overview=False
    res_no_ov = storage.search_faceted(include_overview=False)
    assert res_no_ov.overview is None
