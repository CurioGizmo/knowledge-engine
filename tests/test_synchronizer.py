"""Тесты для KnowledgeSynchronizer (оркестратор синхронизации Markdown и SQLite)."""

from pathlib import Path

from mcp_knowledge.models import (
    CardRecordRequest,
    KnowledgeCategory,
    KnowledgeSource,
    KnowledgeStatus,
    KnowledgeType,
)
from mcp_knowledge.parser import serialize_card_markdown
from mcp_knowledge.repository import CardRepository, _atomic_write_text
from mcp_knowledge.storage import KnowledgeStorage
from mcp_knowledge.synchronizer import KnowledgeSynchronizer


def test_synchronizer_full_sync_and_dirty_check(tmp_path: Path):
    """Проверка полного sync, dirty-check и инкрементального обновления."""
    k_dir = tmp_path / "knowledge"
    k_dir.mkdir(parents=True)
    db_path = tmp_path / "fts.db"

    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    repo = CardRepository(knowledge_dir=k_dir)
    synchronizer = KnowledgeSynchronizer(storage=storage, repo=repo)
    storage.set_synchronizer(synchronizer)

    # 1. Записываем карточку на диск
    card_file = k_dir / "backend" / "backend-test-card.md"
    req = CardRecordRequest(
        title="Test Card",
        category=KnowledgeCategory.BACKEND,
        source=KnowledgeSource.USER_CONFIRMED,
        type=KnowledgeType.DECISION,
        symptom="Тестовый симптом для синхронизации",
        root_cause="Первопричина для теста синхронизации карточки длиной более 40",
        solution="Решение для теста синхронизации карточки длиной более 50 символов",
        rationale="Обоснование для теста синхронизации карточки более 30 символов",
        slug="backend-test-card",
    )
    _atomic_write_text(card_file, serialize_card_markdown("backend-test-card", req))

    # До синхронизации кэш dirty
    assert synchronizer._is_cache_dirty() is True

    # Синхронизация
    synchronizer.sync()

    # После синхронизации кэш clean
    assert synchronizer._is_cache_dirty() is False

    # Поиск находит карточку
    res = storage.search("Тестовый симптом")
    assert len(res) == 1
    assert res[0].id == "backend-test-card"


def test_synchronizer_index_single_card(tmp_path: Path):
    """Проверка точечной переиндексации карточки через index_single_card."""
    k_dir = tmp_path / "knowledge"
    k_dir.mkdir(parents=True)
    db_path = tmp_path / "fts.db"

    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    repo = CardRepository(knowledge_dir=k_dir)
    synchronizer = KnowledgeSynchronizer(storage=storage, repo=repo)

    card_file = k_dir / "platform" / "platform-point-index.md"
    req = CardRecordRequest(
        title="Point Index Card",
        category=KnowledgeCategory.PLATFORM,
        type=KnowledgeType.LESSON,
        symptom="Симптом точечной индексации",
        root_cause="Первопричина точечной индексации карточки длиной более 40 символов",
        solution="Решение проблемы точечной индексации длиной более 50 символов",
        rationale="Обоснование решения точечной индексации более 30 символов",
        slug="platform-point-index",
    )
    _atomic_write_text(card_file, serialize_card_markdown("platform-point-index", req))

    # Точечная индексация
    synchronizer.index_single_card("platform-point-index", card_file)

    # Проверяем, что запись появилась в file_sync и knowledge_index
    with storage.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT card_id FROM file_sync WHERE card_id = ?;", ("platform-point-index",)
        )
        assert cursor.fetchone() is not None

        cursor.execute("SELECT id FROM knowledge_index WHERE id = ?;", ("platform-point-index",))
        assert cursor.fetchone() is not None
