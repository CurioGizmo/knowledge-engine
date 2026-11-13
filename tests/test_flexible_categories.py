"""Тесты для поддержки динамических гибких категорий (Flexible Categories)."""

from pathlib import Path
import pytest

from mcp_knowledge.models import (
    CardRecordRequest,
    KnowledgeCategory,
    KnowledgeSource,
    KnowledgeType,
    normalize_category_slug,
    safe_category_slug,
)
from mcp_knowledge.server import create_server
from mcp_knowledge.service import KnowledgeService
from mcp_knowledge.storage import KnowledgeStorage


def test_normalize_category_slug():
    """Проверка нормализации строк категорий в безопасные слоги."""
    assert normalize_category_slug("audio") == "audio"
    assert normalize_category_slug("AUDIO") == "audio"
    assert normalize_category_slug(KnowledgeCategory.AUDIO) == "audio"
    assert normalize_category_slug("game dev") == "game-dev"
    assert normalize_category_slug("net/code") == "net-code"
    assert normalize_category_slug("net\\code") == "net-code"
    assert normalize_category_slug("billing_sub") == "billing_sub"
    assert normalize_category_slug(" smart-contracts ") == "smart-contracts"

    with pytest.raises(ValueError, match="только латинские буквы"):
        normalize_category_slug("платформа")

    with pytest.raises(ValueError):
        normalize_category_slug("")

    with pytest.raises(ValueError):
        normalize_category_slug("   ")

    with pytest.raises(ValueError):
        normalize_category_slug("---")

    with pytest.raises(ValueError):
        normalize_category_slug(123)


def test_safe_category_slug():
    """Мягкая нормализация: те же слоги, что normalize_category_slug, но None вместо raise."""
    # Валидные входы — идентичны normalize_category_slug
    assert safe_category_slug("audio") == "audio"
    assert safe_category_slug("AUDIO") == "audio"
    assert safe_category_slug(KnowledgeCategory.AUDIO) == "audio"
    assert safe_category_slug("game dev") == "game-dev"
    assert safe_category_slug("net/code") == "net-code"
    assert safe_category_slug("net\\code") == "net-code"
    assert safe_category_slug("billing_sub") == "billing_sub"
    assert safe_category_slug(" smart-contracts ") == "smart-contracts"
    assert safe_category_slug("---") is None

    # Невалидные входы — None вместо ValueError
    assert safe_category_slug("платформа") is None
    assert safe_category_slug("") is None
    assert safe_category_slug("   ") is None
    assert safe_category_slug(123) is None


def test_custom_project_categories_record_and_search(tmp_path: Path):
    """Проверка создания и поиска карточек с произвольными проектными категориями (billing, netcode)."""
    k_dir = tmp_path / "k"
    c_db = tmp_path / "c.db"
    storage = KnowledgeStorage(db_path=c_db, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    # 1. Создаем карточку с категорией 'billing'
    req_billing = CardRecordRequest(
        title="Идемпотентность webhook Stripe",
        category="billing",
        source=KnowledgeSource.USER_CONFIRMED,
        type=KnowledgeType.DECISION,
        symptom="Повторные списания средств при сетевых ретраях вебхуков Stripe",
        root_cause="Обработчик вебхуков не сохранял stripe_event_id в распределенном кэше Redis",
        solution="Реализация паттерна Idempotency Key в Redis с TTL 24 часа перед списанием",
        rationale="Гарантирует однократную обработку транзакции при любых сетевых аномалиях",
        slug="stripe-idempotency",
    )
    resp = service.record(req_billing)
    assert resp["status"] == "created"
    assert "billing" in resp["id"]
    card_path = Path(resp["path"])
    assert card_path.exists()
    assert card_path.parent.name == "billing"

    # 2. Создаем карточку с категорией 'netcode'
    req_net = CardRecordRequest(
        title="Синхронизация тиков в Netcode",
        category="netcode",
        source=KnowledgeSource.USER_CONFIRMED,
        type=KnowledgeType.LESSON,
        symptom="Рассинхронизация физики персонажей при пакетном джиттере UDP",
        root_cause="Клиентский предикт интерполировал координаты без учета джиттер-буфера",
        solution="Внедрение адаптивного Snapshot Buffer с буферизацией в 3 тика сервера",
        rationale="Сглаживает сетевые задержки без визуальных телепортаций персонажей",
        slug="udp-snapshot-buffer",
    )
    resp_net = service.record(req_net)
    assert resp_net["status"] == "created"
    assert "netcode" in resp_net["id"]

    # 3. Поиск по конкретной кастомной категории
    results_billing = service.search(query="stripe", category="billing")
    assert len(results_billing) == 1
    assert results_billing[0].category == "billing"
    assert results_billing[0].id == "billing-stripe-idempotency"

    # 4. Поиск без указания категории находит обе
    results_all = service.search(query="буфера")
    assert len(results_all) >= 1
    assert any(r.category == "netcode" for r in results_all)


@pytest.mark.asyncio
async def test_mcp_server_flexible_category_call(tmp_path: Path):
    """Проверка вызова MCP-инструментов с произвольной категорией."""
    k_dir = tmp_path / "k"
    c_db = tmp_path / "c.db"
    storage = KnowledgeStorage(db_path=c_db, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)
    server = create_server(service=service)

    from mcp.types import CallToolResult

    rec_res = await server.call_tool(
        "record_knowledge",
        {
            "title": "Vulkan Pipeline Barriers Optimization",
            "category": "rendering",
            "type": "lesson",
            "symptom": "Просадки FPS при частой смене графических конвейеров на GPU",
            "root_cause": "Неэффективное использование барьеров памяти и частая перекомпиляция шейдеров",
            "solution": "Сортировка объектов сцены по PSO и использование Indirect Draw буферов",
            "rationale": "Минимизирует обращения к видеодрайверу и удерживает стабильный фреймрейт",
            "slug": "vulkan-pso-sort",
        },
    )
    assert isinstance(rec_res, CallToolResult)
    assert not rec_res.is_error

    search_res = await server.call_tool(
        "search_knowledge",
        {"query": "шейдеров", "categories": ["rendering"]},
    )
    assert isinstance(search_res, CallToolResult)
    assert not search_res.is_error
