"""Тесты для гипотез, антипаттернов, весов доверия (confidence) и резолюции."""

import json
from datetime import date
from pathlib import Path
import pytest
from pydantic import ValidationError

from contextlib import contextmanager


@contextmanager
def monkeypatch_global(obj, attr, replacement):
    """Тестовый перехват метода объекта (глобальная замена с восстановлением)."""
    original = getattr(obj, attr)
    setattr(obj, attr, replacement)
    try:
        yield
    finally:
        setattr(obj, attr, original)


from mcp_knowledge.models import (
    CardFrontmatter,
    CardRecordRequest,
    CardStatusUpdateRequest,
    FacetedSearchResponse,
    GeneratedBy,
    GeneratedMetadata,
    HypothesesResolveRequest,
    HypothesisRecordRequest,
    HypothesisResolution,
    KnowledgeCategory,
    KnowledgeSnippet,
    KnowledgeSource,
    KnowledgeStatus,
    KnowledgeType,
    VerificationRecord,
)
from typing import Any
from mcp_knowledge.parser import (
    parse_card_markdown,
    serialize_card_from_parts,
    serialize_card_markdown,
    serialize_hypothesis_markdown,
)
from mcp_knowledge.repository import CardRepository
from mcp_knowledge.server import create_server
from mcp_knowledge.service import (
    CardNotFoundError,
    ForbiddenUserConfirmedActionError,
    KnowledgeService,
)
from mcp_knowledge.storage import DuplicateCardError, KnowledgeStorage
from mcp.types import CallToolResult, TextContent


def extract_data(res: Any) -> Any:
    if hasattr(res, "structured_content") and res.structured_content:
        return res.structured_content.get("result", res.structured_content)
    if hasattr(res, "content") and res.content:
        first_item = res.content[0]
        if isinstance(first_item, TextContent):
            return json.loads(first_item.text)
    return {}


# ==========================================
# 1. ТЕСТЫ МОДЕЛЕЙ И ВАЛИДАЦИИ
# ==========================================


def test_confidence_validation():
    """Проверка валидации поля confidence в CardFrontmatter и HypothesisRecordRequest."""
    # Валидные значения
    fm = CardFrontmatter(
        id="backend-test",
        title="Test Card",
        category="backend",
        type=KnowledgeType.LESSON,
        confidence=0.8,
        generated=GeneratedMetadata(created_at="2026-09-22"),
    )
    assert fm.confidence == 0.8

    # По умолчанию 0.75 в CardFrontmatter
    fm_default = CardFrontmatter(
        id="backend-test-2",
        title="Test Card",
        category="backend",
        type=KnowledgeType.LESSON,
        generated=GeneratedMetadata(created_at="2026-09-22"),
    )
    assert fm_default.confidence == 0.75

    # None преобразуется в 0.75
    fm_none = CardFrontmatter(
        id="backend-test-3",
        title="Test Card",
        category="backend",
        type=KnowledgeType.LESSON,
        confidence=None,
        generated=GeneratedMetadata(created_at="2026-09-22"),
    )
    assert fm_none.confidence == 0.75

    # Невалидные значения (<0 или >1) вызывают ValidationError
    with pytest.raises(ValidationError):
        CardFrontmatter(
            id="backend-test-4",
            title="Test Card",
            category="backend",
            type=KnowledgeType.LESSON,
            confidence=1.5,
            generated=GeneratedMetadata(created_at="2026-09-22"),
        )

    with pytest.raises(ValidationError):
        CardFrontmatter(
            id="backend-test-5",
            title="Test Card",
            category="backend",
            type=KnowledgeType.LESSON,
            confidence=-0.1,
            generated=GeneratedMetadata(created_at="2026-09-22"),
        )


def test_hypothesis_record_request_validation():
    """Проверка валидации полей в HypothesisRecordRequest."""
    # Успешное создание
    req = HypothesisRecordRequest(
        title="Тестовая гипотеза",
        category="backend",
        symptom="Симптом сбоя сервиса",
        hypothesis="Возможная причина в блокировке дескриптора",
        test_plan="Проверить lsof",
        task_id="task-123",
    )
    assert req.confidence == 0.20
    assert req.task_id == "task-123"

    # Слишком короткая гипотеза (<15 символов)
    with pytest.raises(ValidationError):
        HypothesisRecordRequest(
            title="Тестовая гипотеза",
            category="backend",
            symptom="Симптом сбоя сервиса",
            hypothesis="Коротко",  # < 15
        )

    # Слишком короткий симптом (<10 символов)
    with pytest.raises(ValidationError):
        HypothesisRecordRequest(
            title="Тестовая гипотеза",
            category="backend",
            symptom="Сбой",  # < 10
            hypothesis="Возможная причина в блокировке дескриптора",
        )


def test_hypothesis_resolution_validation():
    """Проверка валидации действий confirm, refute, drop в HypothesisResolution."""
    # confirm без solution отклоняется
    with pytest.raises(ValidationError):
        HypothesisResolution(id="hypo-1", action="confirm")

    # confirm с короткими solution (>=10) и rationale принимается
    res_short_confirm = HypothesisResolution(
        id="hypo-1",
        action="confirm",
        solution="Короткое решение",
        rationale="Коротко",
    )
    assert res_short_confirm.action == "confirm"

    # confirm с подробными solution и rationale также валиден
    res_confirm = HypothesisResolution(
        id="hypo-1",
        action="confirm",
        solution="Надежное решение проблемы с подробным описанием шагов исправления",
        rationale="Обоснование правильности решения",
    )
    assert res_confirm.action == "confirm"

    # refute без refutation_reason отклоняется
    with pytest.raises(ValidationError):
        HypothesisResolution(id="hypo-2", action="refute")

    # refute с валидным refutation_reason (>=15) принимается
    res_refute = HypothesisResolution(
        id="hypo-2",
        action="refute",
        refutation_reason="Гипотеза опровергнута тестами производительности",
        merge_into="backend-main-card",
    )
    assert res_refute.action == "refute"
    assert res_refute.merge_into == "backend-main-card"

    # drop не требует дополнительных полей
    res_drop = HypothesisResolution(id="hypo-3", action="drop")
    assert res_drop.action == "drop"


# ==========================================
# 2. ТЕСТЫ ПАРСЕРА И СЕРИАЛИЗАЦИИ
# ==========================================


def test_hypothesis_serialization_and_parsing():
    """Проверка сериализации HypothesisRecordRequest и обратного парсинга."""
    req = HypothesisRecordRequest(
        title="SQLite WAL Shared Memory Locking",
        category="platform",
        symptom="База данных возвращает busy_timeout при конкурентных чтениях",
        hypothesis="Файл wal-shm не синхронизируется между процессами в контейнере",
        test_plan="Запустить два процесса с strace fcntl",
        task_id="session-42",
        tags=["sqlite", "wal", "locking"],
        aliases=["wal lock", "sqlite concurrency"],
        slug="sqlite-wal-shm",
    )

    md = serialize_hypothesis_markdown("platform-sqlite-wal-shm", req, created_at="2026-09-22")
    assert "status: hypothesis" in md
    assert "task_id: session-42" in md
    assert "### Симптом / Контекст" in md
    assert "### Рабочая гипотеза (Hypothesis)" in md
    assert "### План проверки (Test Plan)" in md

    fm, sections = parse_card_markdown(md)
    assert fm.id == "platform-sqlite-wal-shm"
    assert fm.status == KnowledgeStatus.HYPOTHESIS
    assert fm.confidence == 0.20
    assert fm.task_id == "session-42"
    assert "Рабочая гипотеза (Hypothesis)" in sections
    assert "План проверки (Test Plan)" in sections


def test_antipattern_serialization_and_parsing():
    """Проверка парсинга карточки-антипаттерна со статусом refuted."""
    fm = CardFrontmatter(
        id="backend-bad-lock",
        title="Global File Locking for Concurrency",
        category="backend",
        type=KnowledgeType.ANTIPATTERN,
        status=KnowledgeStatus.REFUTED,
        confidence=0.10,
        refutation_reason="Приводит к полной деградации RPS и дедлокам при росте нагрузки",
        generated=GeneratedMetadata(created_at="2026-09-22"),
    )
    sections = {
        "Симптом / Контекст": "Попытка синхронизировать воркеры через flock",
        "Опровержение / Почему это тупик (Refutation Reason)": "Приводит к полной деградации RPS и дедлокам при росте нагрузки",
    }
    md = serialize_card_from_parts(fm, sections)
    parsed_fm, parsed_sections = parse_card_markdown(md)

    assert parsed_fm.type == KnowledgeType.ANTIPATTERN
    assert parsed_fm.status == KnowledgeStatus.REFUTED
    assert (
        parsed_fm.refutation_reason
        == "Приводит к полной деградации RPS и дедлокам при росте нагрузки"
    )
    assert "Опровержение / Почему это тупик (Refutation Reason)" in parsed_sections


# ==========================================
# 3. ТЕСТЫ ХРАНИЛИЩА, ДИНАМИЧЕСКОГО CONFIDENCE И ПОИСКА
# ==========================================


def test_storage_confidence_calculation_and_search_isolation(tmp_path: Path):
    """Проверка вычисления confidence и изоляции гипотез/антипаттернов от боевого поиска."""
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    # 1. Создаем активную карточку user_confirmed (confidence = 1.0)
    card1_req = CardRecordRequest(
        title="Postgres Connection Pooling",
        category="backend",
        type=KnowledgeType.DECISION,
        source=KnowledgeSource.USER_CONFIRMED,
        symptom="Высокая задержка при открытии новых TCP соединений к PostgreSQL",
        root_cause="Каждое соединение форкает процесс backend в PostgreSQL с накладными расходами памяти",
        solution="Использовать встроенный или внешний connection pooler вроде PgBouncer в режиме транзакций",
        rationale="Снижает количество активных форков процессов и переиспользует TCP соединения к СУБД",
        slug="pg-bouncer",
    )
    b_dir = k_dir / "backend"
    b_dir.mkdir(parents=True, exist_ok=True)
    (b_dir / "backend-pg-bouncer.md").write_text(
        serialize_card_markdown("backend-pg-bouncer", card1_req), encoding="utf-8"
    )

    # 2. Создаем гипотезу (confidence = 0.20, status = hypothesis)
    hypo_req = HypothesisRecordRequest(
        title="Postgres Index Scan Latency",
        category="backend",
        symptom="Медленные запросы к таблице orders при миллионах строк",
        hypothesis="Возможно, планировщик выбирает seq scan вместо index scan из-за устаревшей статистики",
        task_id="task-investigate-pg",
        slug="pg-stat-hypothesis",
    )
    (b_dir / "backend-pg-stat-hypothesis.md").write_text(
        serialize_hypothesis_markdown("backend-pg-stat-hypothesis", hypo_req), encoding="utf-8"
    )

    # 3. Создаем антипаттерн (status = refuted, type = antipattern, содержит Postgres в title)
    antipattern_fm = CardFrontmatter(
        id="backend-pg-no-index",
        title="Disable Postgres Indexes for Fast Bulk Insert",
        category="backend",
        type=KnowledgeType.ANTIPATTERN,
        status=KnowledgeStatus.REFUTED,
        confidence=0.10,
        refutation_reason="Удаление индексов блокирует аналитические запросы и требует дорогого REINDEX",
        generated=GeneratedMetadata(created_at="2026-09-22"),
    )
    antipattern_sections = {
        "Симптом / Контекст": "Медленный bulk insert в таблицу orders",
        "Опровержение / Почему это тупик (Refutation Reason)": "Удаление индексов блокирует аналитические запросы и требует дорогого REINDEX",
    }
    (b_dir / "backend-pg-no-index.md").write_text(
        serialize_card_from_parts(antipattern_fm, antipattern_sections), encoding="utf-8"
    )

    storage.sync()

    # Проверка 1: Обычный поиск search() по умолчанию находит ТОЛЬКО боевое решение
    normal_results = storage.search(query="Postgres")
    assert len(normal_results) == 1
    assert normal_results[0].id == "backend-pg-bouncer"
    assert normal_results[0].confidence == 1.0

    # Проверка 2: Поиск с task_id находит и боевую карточку, и связанную гипотезу
    task_results = storage.search(query="Postgres", task_id="task-investigate-pg")
    found_ids = {r.id for r in task_results}
    assert "backend-pg-bouncer" in found_ids
    assert "backend-pg-stat-hypothesis" in found_ids
    # Антипаттерн по-прежнему изолирован от основного пула
    assert "backend-pg-no-index" not in found_ids

    # Проверка 3: Фасетный поиск search_faceted выносит антипаттерн в блок antipatterns
    faceted_resp = storage.search_faceted(query="Postgres", task_id="task-investigate-pg")
    assert any(c.id == "backend-pg-bouncer" for c in faceted_resp.query_results)
    assert any(c.id == "backend-pg-stat-hypothesis" for c in faceted_resp.hypotheses)
    assert len(faceted_resp.antipatterns) == 1
    assert faceted_resp.antipatterns[0].id == "backend-pg-no-index"
    assert "⛔ ОПРОВЕРГНУТО" in (faceted_resp.antipatterns[0].warning or "")


# ==========================================
# 4. ТЕСТЫ СЕРВИСА: ПОЛНЫЙ ЖИЗНЕННЫЙ ЦИКЛ ГИПОТЕЗ
# ==========================================


def test_hypothesis_lifecycle_confirm(tmp_path: Path):
    """Сценарий: создание гипотезы -> подтверждение (confirm) -> карточка становится боевой."""
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    # Шаг 1: Предложение гипотезы
    hypo_req = HypothesisRecordRequest(
        title="AsyncIO High CPU in ThreadPoolExecutor",
        category="backend",
        symptom="Высокая утилизация CPU при вызовах run_in_executor",
        hypothesis="Бесконечное создание дефолтных потоков из-за отсутствия лимита max_workers",
        test_plan="Замерить количество тредов через threading.enumerate()",
        task_id="task-thread-pool",
        slug="asyncio-thread-leak",
    )
    prop_res = service.propose_hypothesis(hypo_req)
    card_id = prop_res["id"]
    assert prop_res["status"] == "proposed"

    # До подтверждения карточка не видна в обычном поиске
    assert len(service.search(query="ThreadPoolExecutor")) == 0

    # Шаг 2: Подтверждение гипотезы
    res_req = HypothesesResolveRequest(
        task_id="task-thread-pool",
        resolutions=[
            HypothesisResolution(
                id=card_id,
                action="confirm",
                solution="Явно инициализировать ThreadPoolExecutor с ограничением max_workers=cpu_count*2 и передавать в loop.set_default_executor",
                rationale="Предотвращает неограниченный спавн потоков операционной системы и стабилизирует нагрузку CPU",
            )
        ],
    )
    resolve_res = service.resolve_hypotheses(res_req)
    assert resolve_res["status"] == "resolved"
    assert resolve_res["resolved_count"] == 1

    # Карантин (план B): агентское confirm оставляет карточку в draft
    # (не видна в общем поиске), в active её переводит только промоушен
    # с человеческим подтверждением.
    assert len(service.search(query="ThreadPoolExecutor")) == 0
    quarantine_hits = service.search(
        query="ThreadPoolExecutor", include_drafts=True, task_id="task-thread-pool"
    )
    assert any(hit.id == card_id for hit in quarantine_hits)
    quarantine_card = service.get([card_id]).cards[0]
    assert quarantine_card.status == KnowledgeStatus.DRAFT
    assert quarantine_card.type == KnowledgeType.LESSON

    # Шаг 3: Промоушен по явному согласию человека
    promote_res = service.update_status(
        CardStatusUpdateRequest(
            id=card_id,
            action="promote",
            note="Утверждено пользователем после проверки",
            by="human",
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )
    assert promote_res["new_status"] == "active"

    # Теперь карточка стала active lesson и находится обычным поиском
    search_res = service.search(query="ThreadPoolExecutor")
    assert len(search_res) == 1
    confirmed_card = search_res[0]
    assert confirmed_card.id == card_id
    assert confirmed_card.status == KnowledgeStatus.ACTIVE
    assert confirmed_card.type == KnowledgeType.LESSON


def test_hypothesis_lifecycle_refute_and_merge(tmp_path: Path):
    """Сценарий: опровержение гипотезы со слиянием в нюансы целевой карточки."""
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    # 1. Создаем существующую целевую карточку
    target_req = CardRecordRequest(
        title="FastAPI Lifespan Context Manager",
        category="backend",
        type=KnowledgeType.DECISION,
        symptom="Предупреждение о депрекейшене on_event startup и shutdown в FastAPI",
        root_cause="Starlette и FastAPI стандартизировали asynccontextmanager lifespan вместо split-событий",
        solution="Использовать @asynccontextmanager async def lifespan(app: FastAPI): yield для инициализации ресурсов",
        rationale="Гарантирует атомарную очистку ресурсов в блоке finally даже при возникновении исключений",
        slug="fastapi-lifespan",
    )
    service.record(target_req)

    # 2. Создаем ошибочную гипотезу
    hypo_req = HypothesisRecordRequest(
        title="FastAPI Sub-App Lifespan Chaining",
        category="backend",
        symptom="Суб-приложения FastAPI не вызывают родительский lifespan",
        hypothesis="Можно вручную вызывать __aenter__ родительского контекста в цикле суб-приложений",
        task_id="task-lifespan",
        slug="subapp-lifespan",
    )
    prop = service.propose_hypothesis(hypo_req)
    hypo_id = prop["id"]

    # 3. Опровергаем со слиянием в целевую карточку
    ref_req = HypothesesResolveRequest(
        task_id="task-lifespan",
        resolutions=[
            HypothesisResolution(
                id=hypo_id,
                action="refute",
                refutation_reason="Приводит к двойному выполнению teardown соединений с базой данных",
                merge_into="backend-fastapi-lifespan",
            )
        ],
    )
    service.resolve_hypotheses(ref_req)

    # Проверяем, что файл гипотезы физически удален
    hypo_file = service._find_card_file(hypo_id)
    assert hypo_file is None

    # Проверяем, что в целевой карточке появились нюансы с опровержением
    target_card = service.get(["backend-fastapi-lifespan"]).cards[0]
    assert "⛔ Опровергнутый подход" in target_card.content
    assert "двойному выполнению teardown" in target_card.content


def test_hypothesis_lifecycle_drop(tmp_path: Path):
    """Сценарий: удаление черновика гипотезы (drop)."""
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    hypo_req = HypothesisRecordRequest(
        title="Random Hypothesis to Drop",
        category="general",
        symptom="Случайный симптом для теста удаления",
        hypothesis="Гипотеза, которая оказалась полностью нерелевантной",
        slug="drop-me",
    )
    prop = service.propose_hypothesis(hypo_req)
    card_id = prop["id"]

    # Дропаем гипотезу
    res = service.resolve_hypotheses(
        HypothesesResolveRequest(resolutions=[HypothesisResolution(id=card_id, action="drop")])
    )
    assert res["status"] == "resolved"
    assert service._find_card_file(card_id) is None


# ==========================================
# 5. ИНТЕГРАЦИОННЫЕ ТЕСТЫ MCP СЕРВЕРА
# ==========================================


@pytest.mark.asyncio
async def test_mcp_server_propose_and_resolve(tmp_path: Path):
    """Проверка работы инструментов propose_hypothesis и resolve_hypotheses через MCP сервер."""
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    server = create_server(cache_db=db_path, knowledge_dir=k_dir)

    # Проверяем наличие инструментов
    tools = await server.list_tools()
    tool_names = {t.name for t in tools}
    assert "propose_hypothesis" in tool_names
    assert "resolve_hypotheses" in tool_names
    assert "search_knowledge" in tool_names

    # 1. Вызов propose_hypothesis
    res_prop = await server.call_tool(
        "propose_hypothesis",
        {
            "title": "MCP Server Hypothesis Test",
            "category": "platform",
            "symptom": "Симптом проверки MCP сервера на корректность",
            "hypothesis": "Предполагаем корректную работу всех оберток инструментов",
            "task_id": "mcp-task-1",
            "slug": "mcp-hypo-test",
        },
    )
    assert isinstance(res_prop, CallToolResult)
    assert not res_prop.is_error
    prop_data = extract_data(res_prop)
    assert prop_data.get("status") == "proposed"
    hypo_id = prop_data["id"]

    # 2. Поиск без include_hypotheses пуст
    res_clean = await server.call_tool("search_knowledge", {"query": "MCP Server Hypothesis Test"})
    clean_data = extract_data(res_clean)
    assert len(clean_data["query_results"]) == 0

    # 3. Поиск с task_id возвращает гипотезу
    res_task = await server.call_tool(
        "search_knowledge",
        {"query": "MCP Server Hypothesis Test", "task_id": "mcp-task-1"},
    )
    task_data = extract_data(res_task)
    assert len(task_data["hypotheses"]) == 1
    assert task_data["hypotheses"][0]["id"] == hypo_id
    assert "🧪 РАБОЧАЯ ГИПОТЕЗА" in task_data["hypotheses"][0]["warning"]

    # 4. Резолюция через resolve_hypotheses (refute -> antipattern)
    res_resolve = await server.call_tool(
        "resolve_hypotheses",
        {
            "task_id": "mcp-task-1",
            "resolutions": [
                {
                    "id": hypo_id,
                    "action": "refute",
                    "refutation_reason": "Доказано неприменимым в боевой среде из-за ограничений песочницы",
                }
            ],
        },
    )
    assert isinstance(res_resolve, CallToolResult)
    assert not res_resolve.is_error
    resolve_data = extract_data(res_resolve)
    assert resolve_data.get("status") == "resolved"

    # 5. Фасетный поиск теперь возвращает карточку в блоке antipatterns
    res_faceted = await server.call_tool(
        "search_knowledge",
        {"query": "MCP Server Hypothesis Test", "include_overview": False},
    )
    faceted_data = extract_data(res_faceted)
    assert len(faceted_data["antipatterns"]) == 1
    assert faceted_data["antipatterns"][0]["id"] == hypo_id
    assert "⛔ ОПРОВЕРГНУТО" in faceted_data["antipatterns"][0]["warning"]

    # 6. Защита user_confirmed карточек от удаления через MCP resolve_hypotheses
    await server.call_tool(
        "record_knowledge",
        {
            "title": "Critical User Decision",
            "category": "backend",
            "type": "decision",
            "source": "user_confirmed",
            "symptom": "Критический симптом для теста защиты сервера",
            "root_cause": "Анализ первопричины сбоя для теста сервера более 40 символов",
            "solution": "Законченное техническое решение для теста сервера более 50 символов",
            "rationale": "Обоснование правильности решения более 30 символов",
            "slug": "server-user-card",
        },
    )
    res_malicious = await server.call_tool(
        "resolve_hypotheses",
        {"resolutions": [{"id": "backend-server-user-card", "action": "drop"}]},
    )
    malicious_data = extract_data(res_malicious)
    assert malicious_data.get("status") == "rejected"
    assert malicious_data.get("reason") in ("FORBIDDEN_USER_CONFIRMED", "VALIDATION_FAILED")


def test_resolve_hypotheses_security_guardrails(tmp_path: Path):
    """Проверка защитных барьеров (Guardrails): невозможность модифицировать или удалить не-гипотезы."""
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    # 1. Создаем боевую карточку со статусом active и source=user_confirmed
    active_req = CardRecordRequest(
        title="Protected User Confirmed Architecture Card",
        category="backend",
        type=KnowledgeType.DECISION,
        source=KnowledgeSource.USER_CONFIRMED,
        symptom="Критически важный симптом архитектурного соглашения команды",
        root_cause="Ограничение сетевой топологии защищенного контура",
        solution="Использовать mTLS аутентификацию для всех внутренних RPC вызовов между микросервисами",
        rationale="Гарантирует нулевое доверие (Zero Trust) и криптографическую изоляцию сервисов",
        slug="mtls-decision",
    )
    rec_res = service.record(active_req)
    active_id = rec_res["id"]

    # Попытка дропнуть боевую карточку через resolve_hypotheses ДОЛЖНА блокироваться
    with pytest.raises(ValueError, match="не является рабочей гипотезой"):
        service.resolve_hypotheses(
            HypothesesResolveRequest(
                resolutions=[HypothesisResolution(id=active_id, action="drop")]
            )
        )

    # Файл должен остаться в целости на диске
    assert service._find_card_file(active_id) is not None

    # Попытка confirm / refute боевой карточки также блокируется
    with pytest.raises(ValueError, match="не является рабочей гипотезой"):
        service.resolve_hypotheses(
            HypothesesResolveRequest(
                resolutions=[
                    HypothesisResolution(
                        id=active_id,
                        action="refute",
                        refutation_reason="Попытка опровергнуть утвержденное решение",
                    )
                ]
            )
        )

    # 2. Проверка защиты merge_into: нельзя объединять гипотезу саму в себя
    hypo_req = HypothesisRecordRequest(
        title="Test Loop Hypothesis",
        category="backend",
        symptom="Тестовый симптом для проверки самослияния гипотезы",
        hypothesis="Предполагаем некоторую причину для теста слияния",
        slug="self-merge",
    )
    prop = service.propose_hypothesis(hypo_req)
    hypo_id = prop["id"]

    with pytest.raises(ValueError, match="Нельзя объединить гипотезу.*саму в себя"):
        service.resolve_hypotheses(
            HypothesesResolveRequest(
                resolutions=[
                    HypothesisResolution(
                        id=hypo_id,
                        action="refute",
                        refutation_reason="Причина опровержения для проверки самослияния",
                        merge_into=hypo_id,
                    )
                ]
            )
        )

    # 3. Проверка защиты merge_into: нельзя сливать в вытесненную (superseded) карточку
    # Создаем вторую карточку и вытесняем первую
    active_req2 = CardRecordRequest(
        title="Modern Replacement for Architecture Decision",
        category="backend",
        type=KnowledgeType.DECISION,
        source=KnowledgeSource.USER_CONFIRMED,
        symptom="Критически важный симптом архитектурного соглашения команды версия 2",
        root_cause="Ограничение сетевой топологии защищенного контура версия 2",
        solution="Использовать SPIFFE/SPIRE для динамической выдачи x509 сертификатов mTLS",
        rationale="Автоматизирует ротацию ключей и снижает операционные накладные расходы",
        supersedes=active_id,
        force=True,
    )
    service.record(active_req2)

    # Теперь active_id находится в статусе superseded
    with pytest.raises(ValueError, match="вытеснена карточкой"):
        service.resolve_hypotheses(
            HypothesesResolveRequest(
                resolutions=[
                    HypothesisResolution(
                        id=hypo_id,
                        action="refute",
                        refutation_reason="Причина опровержения для проверки слияния в вытесненную карточку",
                        merge_into=active_id,
                    )
                ]
            )
        )


def test_competing_hypotheses_and_force_flag(tmp_path: Path):
    """Проверка создания конкурирующих гипотез по одному симптому и работы флага force."""
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    common_symptom = "Высокая задержка и таймауты при открытии соединений к PostgreSQL"

    # Гипотеза 1: DNS резолвинг
    hypo1 = HypothesisRecordRequest(
        title="PostgreSQL DNS Resolution Timeout",
        category="backend",
        symptom=common_symptom,
        hypothesis="Задержка вызвана медленным резолвингом доменного имени через локальный systemd-resolved",
        slug="pg-dns-latency",
    )
    res1 = service.propose_hypothesis(hypo1)
    assert res1["status"] == "proposed"

    # Гипотеза 2: Конкурирующая гипотеза по тому же симптому (исчерпание дескрипторов)
    hypo2 = HypothesisRecordRequest(
        title="PostgreSQL File Descriptors Exhaustion",
        category="backend",
        symptom=common_symptom,
        hypothesis="Задержка вызвана исчерпанием лимита ulimit open files на стороне воркера приложения",
        slug="pg-fd-exhaustion",
    )
    # Должна успешно сохраниться, так как суть гипотезы принципиально иная!
    res2 = service.propose_hypothesis(hypo2)
    assert res2["status"] == "proposed"

    # Гипотеза 3: Дубликат первой гипотезы (тот же симптом и та же суть гипотезы)
    hypo3 = HypothesisRecordRequest(
        title="PostgreSQL DNS Resolution Delay Duplicate",
        category="backend",
        symptom=common_symptom,
        hypothesis="Задержка вызвана медленным резолвингом доменного имени через systemd-resolved DNS",
        slug="pg-dns-duplicate",
    )
    # Без force должно выбросить DuplicateCardError
    with pytest.raises(DuplicateCardError):
        service.propose_hypothesis(hypo3)

    # С force=True должно успешно создаться
    hypo3.force = True
    res3 = service.propose_hypothesis(hypo3)
    assert res3["status"] == "proposed"


def test_record_r10_tech_differentiator_in_symptom_not_duplicate(tmp_path: Path):
    """Верификация r10 (остаточный JSvsTS): различитель в СИМПТОМЕ.

    «утечка памяти js-приложения...» vs «...ts-приложения...» при
    идентичном root_cause: после канонизации (js→javascript,
    ts→typescript) overlap симптома 4/5 >= 0.6, но различитель
    технологий в уникальных токенах симптомов не объясняется
    морфологией/синонимией — это не доказанный дубликат.
    """
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    common_solution = "Отписывать слушатели событий при размонтировании компонента рендера"
    common_rationale = "Профилировщик heap показал рост без сборки мусора"

    service.record(
        CardRecordRequest(
            title="JS Memory Leak Lesson",
            category="backend",
            type=KnowledgeType.LESSON,
            symptom="утечка памяти js-приложения при долгой сессии",
            root_cause="накопление слушателей событий в компоненте рендера",
            solution=common_solution,
            rationale=common_rationale,
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )

    # ts-карточка с тем же root_cause — НЕ дубль (разный стек)
    res = service.record(
        CardRecordRequest(
            title="TS Memory Leak Lesson",
            category="backend",
            type=KnowledgeType.LESSON,
            symptom="утечка памяти ts-приложения при долгой сессии",
            root_cause="накопление слушателей событий в компоненте рендера",
            solution=common_solution,
            rationale=common_rationale,
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )
    assert res["status"] == "created"

    # Контроль: настоящий дубль симптома (js/js) — ловится
    with pytest.raises(DuplicateCardError):
        service.record(
            CardRecordRequest(
                title="JS Memory Leak Dup",
                category="backend",
                type=KnowledgeType.LESSON,
                symptom="утечка памяти js-приложения при долгой сессии",
                root_cause="накопление слушателей событий в компоненте рендера",
                solution=common_solution,
                rationale=common_rationale,
                source=KnowledgeSource.USER_CONFIRMED,
            )
        )

    # Контроль: морфология в симптоме (кэш/кэши) — дубль
    service.record(
        CardRecordRequest(
            title="Cache Growth Lesson",
            category="backend",
            type=KnowledgeType.LESSON,
            symptom="рост памяти при переполнении кэша сессий",
            root_cause="ограниченный размер кэша и отсутствие вытеснения",
            solution="Включить вытеснение LRU и ограничить время жизни записей кэша",
            rationale="Замеры подтвердили монотонный рост кэша при равной нагрузке",
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )
    with pytest.raises(DuplicateCardError):
        service.record(
            CardRecordRequest(
                title="Cache Growth Dup",
                category="backend",
                type=KnowledgeType.LESSON,
                symptom="рост памяти при переполнении кэши сессий",
                root_cause="ограниченный размер кэша и отсутствие вытеснения",
                solution="Включить вытеснение LRU и ограничить время жизни записей кэша",
                rationale="Замеры подтвердили монотонный рост кэша при равной нагрузке",
                source=KnowledgeSource.USER_CONFIRMED,
            )
        )


def test_record_r10_nondictionary_rephrase_in_symptom_is_duplicate(tmp_path: Path):
    """Верификация r10: несловарное перефразирование в симптоме — дубль.

    «переполнен» vs «забит», «медленный» vs «долгий» — перефразирования
    вне словарей синонимов. Различитель симптомов считается только для
    пары известных tech-терминов (postgres vs mysql), поэтому
    прилагательные из словарей причин не должны отсекать настоящие дубли.
    """
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    common_cause = "слишком маленький размер пула соединений без таймаутов простоя"
    common_solution = "Ограничить пул соединений и включить таймауты простоя в драйвере"
    common_rationale = "Замеры показали исчерпание слотов ожидания под пиковую нагрузку"

    service.record(
        CardRecordRequest(
            title="Pool Overflow Lesson",
            category="backend",
            type=KnowledgeType.LESSON,
            symptom="пул соединений переполнен при пиковой нагрузке сервера",
            root_cause=common_cause,
            solution=common_solution,
            rationale=common_rationale,
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )

    # «забит» — несловарное перефразирование «переполнен» → дубль
    with pytest.raises(DuplicateCardError):
        service.record(
            CardRecordRequest(
                title="Pool Overflow Dup",
                category="backend",
                type=KnowledgeType.LESSON,
                symptom="пул соединений забит при пиковой нагрузке сервера",
                root_cause=common_cause,
                solution=common_solution,
                rationale=common_rationale,
                source=KnowledgeSource.USER_CONFIRMED,
            )
        )


def test_record_r10_known_tech_terms_in_symptom_are_distinct(tmp_path: Path):
    """Верификация r10: пара известных tech-терминов — различитель симптомов.

    «исчерпание пула postgres при длинных транзакциях» vs «...mysql...»:
    оба термина известны (postgres/mysql) и не связаны алиасом — карточки
    не склеиваются. Проверяется в отдельном окружении, чтобы шум от
    похожих симптомов других уроков не маскировал поведение гейта.
    """
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    common_cause = "утечка транзакций без commit из-за незакрываемых курсоров"
    common_solution = "Закрывать курсоры в finally и включить таймауты транзакций"
    common_rationale = "Логи показали рост открытых курсоров до лимита сервера"

    res = service.record(
        CardRecordRequest(
            title="PG Pool Lesson",
            category="backend",
            type=KnowledgeType.LESSON,
            symptom="исчерпание пула postgres при длинных транзакциях очереди задач",
            root_cause=common_cause,
            solution=common_solution,
            rationale=common_rationale,
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )
    assert res["status"] == "created"
    res_mysql = service.record(
        CardRecordRequest(
            title="MySQL Pool Lesson",
            category="backend",
            type=KnowledgeType.LESSON,
            symptom="исчерпание пула mysql при длинных транзакциях очереди задач",
            root_cause=common_cause,
            solution=common_solution,
            rationale=common_rationale,
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )
    assert res_mysql["status"] == "created"


def test_resolve_hypotheses_confirm_without_strict_lengths(tmp_path: Path):
    """Проверка, что confirm работает без требования строгих 50/30 символов Anti-Spam Gate, но требует законченного solution."""
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    # 1. Создаем гипотезу
    hypo_req = HypothesisRecordRequest(
        title="Asyncio Loop Hang on Windows",
        category="platform",
        symptom="Зависание цикла событий asyncio при использовании ProactorEventLoop в Windows",
        hypothesis="ProactorEventLoop не закрывает корректно pipe handles при завершении сабпроцесса",
        slug="asyncio-loop-hang",
    )
    prop = service.propose_hypothesis(hypo_req)
    hypo_id = prop["id"]

    # 2. Подтверждаем с коротким решением (без строгих 50 символов)
    res = service.resolve_hypotheses(
        HypothesesResolveRequest(
            resolutions=[
                HypothesisResolution(
                    id=hypo_id,
                    action="confirm",
                    solution="Использовать закрытие pipe handles перед join",
                )
            ]
        )
    )
    assert res["status"] == "resolved"
    assert res["resolved_count"] == 1

    # Карантин (план B): карточка создана в draft и не видна в общем поиске
    card = service.get([hypo_id]).cards[0]
    assert card.status == KnowledgeStatus.DRAFT
    assert card.type == KnowledgeType.LESSON
    assert len(service.search(query="pipe handles")) == 0
    assert "Использовать закрытие pipe handles" in card.content

    # 3. Создаем вторую гипотезу и подтверждаем с короткими solution и rationale
    hypo2 = HypothesisRecordRequest(
        title="Quick Fix Hypothesis",
        category="platform",
        symptom="Короткий тестовый симптом для быстрой проверки резолюции",
        hypothesis="Суть быстрой рабочей теории для проверки решения",
        slug="quick-fix-test",
    )
    prop2 = service.propose_hypothesis(hypo2)
    hypo2_id = prop2["id"]

    res2 = service.resolve_hypotheses(
        HypothesesResolveRequest(
            resolutions=[
                HypothesisResolution(
                    id=hypo2_id,
                    action="confirm",
                    solution="Исправление бага в рантайме",
                    rationale="Проверено на стенде",
                )
            ]
        )
    )
    assert res2["status"] == "resolved"
    card2 = service.get([hypo2_id]).cards[0]
    assert card2.status == KnowledgeStatus.DRAFT
    assert "Исправление бага в рантайме" in card2.content
    assert "Проверено на стенде" in card2.content


# ===========================================================================
# 6. K-HYP: Разрешение гипотез при похожем знании (Step 3: AUDIT-REMEDIATION-2026-11-V2)
# ===========================================================================


def test_confirm_distinguishes_different_root_cause_for_same_symptom(tmp_path: Path):
    """Один симптом с DNS и исчерпанным пулом: confirm обычным путём создаёт draft."""
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    common_symptom = "Высокая задержка и таймауты при открытии соединений к PostgreSQL"

    # 1. Существующий урок в базе (DNS)
    rec = service.record(
        CardRecordRequest(
            title="PostgreSQL DNS Resolution Delay",
            category="backend",
            type=KnowledgeType.LESSON,
            symptom=common_symptom,
            root_cause="Задержка вызвана медленным резолвингом доменного имени через локальный systemd-resolved",
            solution="Использовать статический IP адрес или настроить кэширующий DNS сервер локально в контейнере",
            rationale="Проверено замерами latency до и после настройки кэширования DNS",
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )
    existing_id = rec["id"]

    # 2. Новая гипотеза с тем же симптомом, но другой причиной (исчерпание пула)
    hypo = service.propose_hypothesis(
        HypothesisRecordRequest(
            title="PostgreSQL Connection Pool Exhaustion",
            category="backend",
            symptom=common_symptom,
            hypothesis="Задержка вызвана исчерпанием пула соединений pgbouncer из-за утечки клиентских транзакций",
            slug="pg-pool-exhaustion",
        )
    )
    hypo_id = hypo["id"]

    # 3. Confirm гипотезы: причины доказанно различны -> успешно переходит в draft
    res = service.resolve_hypotheses(
        HypothesesResolveRequest(
            resolutions=[
                HypothesisResolution(
                    id=hypo_id,
                    action="confirm",
                    solution="Увеличить max_connections в pgbouncer и настроить idle_transaction_timeout",
                )
            ]
        )
    )
    assert res["status"] == "resolved"
    assert res["resolved_count"] == 1

    card = service.get([hypo_id]).cards[0]
    assert card.status == KnowledgeStatus.DRAFT
    assert card.type == KnowledgeType.LESSON
    assert "Увеличить max_connections" in card.content


def test_confirm_matching_symptom_and_cause_yields_duplicate_found(tmp_path: Path):
    """Совпадающие симптом и причина: confirm отклоняется с DuplicateCardError."""
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    common_symptom = "Высокая задержка и таймауты при открытии соединений к PostgreSQL"
    common_cause = (
        "Задержка вызвана медленным резолвингом доменного имени через локальный systemd-resolved"
    )

    rec = service.record(
        CardRecordRequest(
            title="PostgreSQL DNS Delay Lesson",
            category="backend",
            type=KnowledgeType.LESSON,
            symptom=common_symptom,
            root_cause=common_cause,
            solution="Использовать локальный dnsmasq для надежного кэширования DNS ответов в сети",
            rationale="Позволяет снизить задержки резолвинга DNS при повторных запросах к базе данных",
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )
    existing_id = rec["id"]

    hypo = service.propose_hypothesis(
        HypothesisRecordRequest(
            title="PostgreSQL DNS Timeout Duplicate Hypothesis",
            category="backend",
            symptom=common_symptom,
            hypothesis=common_cause,
            slug="pg-dns-duplicate-hypo",
        )
    )
    hypo_id = hypo["id"]

    # При confirm совпадающие симптом и причина дают DuplicateCardError
    with pytest.raises(DuplicateCardError) as exc_info:
        service.resolve_hypotheses(
            HypothesesResolveRequest(
                resolutions=[
                    HypothesisResolution(
                        id=hypo_id,
                        action="confirm",
                        solution="Повторное решение для проверки дубликата",
                    )
                ]
            )
        )
    assert exc_info.value.existing_id == existing_id

    # Исходная гипотеза осталась в статусе hypothesis
    card = service.get([hypo_id]).cards[0]
    assert card.status == KnowledgeStatus.HYPOTHESIS


def test_confirm_insufficient_tokens_yields_uncertain(tmp_path: Path):
    """Пустая или короткая причина (< MIN_HYPO_TOKENS_FOR_OVERLAP): uncertain без падения и без записи."""
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    common_symptom = "Сбой синхронизации сокета в распределённой очереди"

    rec = service.record(
        CardRecordRequest(
            title="Socket Sync Issue Existing",
            category="platform",
            type=KnowledgeType.LESSON,
            symptom=common_symptom,
            root_cause="Недостаточный таймаут подтверждения TCP ACK пакетов",
            solution="Увеличение TCP keepalive интервала в конфигурации ядра операционной системы на всех серверах",
            rationale="Устраняет таймауты при потере сетевых пакетов на высоконагруженных каналах связи",
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )
    existing_id = rec["id"]

    # Создаём гипотезу с тем же симптомом, но короткой причиной в токенах (< 3 токенов: 'Непонятный сбой -- --', длина > 15 символов)
    hypo = service.propose_hypothesis(
        HypothesisRecordRequest(
            title="Socket Sync Issue Too Short Cause",
            category="platform",
            symptom=common_symptom,
            hypothesis="Непонятный сбой -- --",
            slug="socket-sync-short",
        )
    )
    hypo_id = hypo["id"]

    with pytest.raises(ValueError) as exc_info:
        service.resolve_hypotheses(
            HypothesesResolveRequest(
                resolutions=[
                    HypothesisResolution(
                        id=hypo_id,
                        action="confirm",
                        solution="Исправление сокета через ретрай коннекта",
                    )
                ]
            )
        )
    assert "uncertain" in str(exc_info.value).lower()
    assert existing_id in str(exc_info.value)

    # Гипотеза не изменена
    card = service.get([hypo_id]).cards[0]
    assert card.status == KnowledgeStatus.HYPOTHESIS


def test_confirm_symptom_polluted_causes_are_distinct(tmp_path: Path):
    """K-HYP (ревью шага 3): причины, пересыпанные общими словами симптома,
    не дают ложного DuplicateCardError при confirm.

    Сценарий отзыва: «задержка из-за DNS» против «исчерпан пул PostgreSQL»,
    где оба текста причин содержат слова симптома — прежний overlap >= 0.45
    не срабатывал как сигнал «разные причины», и классификатор звал duplicate.
    """
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    common_symptom = "задержка ответа API"

    service.record(
        CardRecordRequest(
            title="API Latency DNS Lesson",
            category="backend",
            type=KnowledgeType.LESSON,
            symptom=common_symptom,
            root_cause="задержка ответа API из-за медленного DNS-резолвера",
            solution="Настроить кэширующий DNS-резолвер локально и проверять кэш перед запросом",
            rationale="Замеры показали снижение p99-задержки после включения DNS-кэша",
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )

    hypo = service.propose_hypothesis(
        HypothesisRecordRequest(
            title="API Latency Pool Exhaustion Hypothesis",
            category="backend",
            symptom=common_symptom,
            hypothesis="задержка ответа API из-за исчерпания пула соединений PostgreSQL",
            slug="api-latency-pool",
        )
    )
    hypo_id = hypo["id"]

    # Прямая проверка классификатора: разные причины → distinct
    outcome = storage.classify_confirm_candidate(
        hypo_symptom=common_symptom,
        hypo_reason="задержка ответа API из-за исчерпания пула соединений PostgreSQL",
        cand_symptom=common_symptom,
        cand_root_cause="задержка ответа API из-за медленного DNS-резолвера",
    )
    assert outcome == "distinct"

    # Confirm проходит без DuplicateCardError
    res = service.resolve_hypotheses(
        HypothesesResolveRequest(
            resolutions=[
                HypothesisResolution(
                    id=hypo_id,
                    action="confirm",
                    solution="Увеличить пул соединений и включить таймауты простоя в драйвере",
                )
            ]
        )
    )
    assert res["status"] == "resolved"
    card = service.get([hypo_id]).cards[0]
    assert card.status == KnowledgeStatus.DRAFT


def test_confirm_r2_shared_connective_words_not_duplicate(tmp_path: Path):
    """Ревью r2: разные причины с общими связками («таймаута DNS» vs «таймаута пула»).

    Прежний distinct-фильтр не исключал связочные слова: в коротких наборах
    «из-за таймаута DNS» vs «из-за таймаута пула» оставшиеся 2 общих токена
    давали overlap 0.5 >= 0.45 → ложный duplicate.
    """
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    common_symptom = "задержка ответа сервера"
    service.record(
        CardRecordRequest(
            title="Server Latency Pool Lesson",
            category="backend",
            type=KnowledgeType.LESSON,
            symptom=common_symptom,
            root_cause="задержка ответа сервера из-за таймаута пула",
            solution="Расширить пул соединений и ограничить время ожидания клиента",
            rationale="Замеры пула подтвердили исчерпание слотов ожидания",
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )

    hypo = service.propose_hypothesis(
        HypothesisRecordRequest(
            title="Server Latency DNS Hypothesis",
            category="backend",
            symptom=common_symptom,
            hypothesis="задержка ответа сервера из-за таймаута DNS",
            slug="server-latency-dns",
        )
    )

    # Прямая проверка: общие связки не делают причины одинаковыми
    outcome = storage.classify_confirm_candidate(
        hypo_symptom=common_symptom,
        hypo_reason="задержка ответа сервера из-за таймаута DNS",
        cand_symptom=common_symptom,
        cand_root_cause="задержка ответа сервера из-за таймаута пула",
    )
    assert outcome == "distinct"

    res = service.resolve_hypotheses(
        HypothesesResolveRequest(
            resolutions=[
                HypothesisResolution(
                    id=hypo["id"],
                    action="confirm",
                    solution="Включить кэширующий DNS-резолвер и проверить таймауты резолвинга",
                )
            ]
        )
    )
    assert res["status"] == "resolved"


def test_confirm_r2_symptom_only_reason_is_uncertain(tmp_path: Path):
    """Ревью r2: причина гипотезы из одних слов симптома → uncertain, не duplicate.

    Прежний fallthrough возвращал duplicate, когда полные overlap-тексты
    причин пересекались (кандидат тоже пересыпан симптомом), а собственных
    содержательных токенов у причины гипотезы не было.
    """
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    s = "задержка ответа сервера"
    # Причина гипотезы дословно = симптом; причина кандидата тоже содержит
    # слова симптома + свои (полный overlap высок, собственных токенов у
    # гипотезы нет).
    outcome = storage.classify_confirm_candidate(
        hypo_symptom=s,
        hypo_reason="задержка ответа сервера",
        cand_symptom=s,
        cand_root_cause="задержка ответа сервера и исчерпание пула",
    )
    assert outcome == "uncertain"

    # Обе причины = симптом дословно: причинных данных нет вовсе —
    # совпадение пустых по смыслу текстов не доказывает дубликат
    # (ревью r6, G1). Прежний duplicate позволял закрывать гипотезы
    # как дубликаты без единого причинного токена.
    outcome2 = storage.classify_confirm_candidate(
        hypo_symptom=s, hypo_reason=s, cand_symptom=s, cand_root_cause=s
    )
    assert outcome2 == "uncertain"


def test_confirm_r2_morphological_forms_are_duplicate(tmp_path: Path):
    """Ревью r2: морфологические формы одного слова (пуст/пустой) — дубликат, не distinct."""
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    outcome = storage.classify_confirm_candidate(
        hypo_symptom="задержка ответа сервера",
        hypo_reason="медленный DNS-резолвер, кэш пуст",
        cand_symptom="задержка ответа сервера",
        cand_root_cause="медленный DNS-резолвер и пустой кэш",
    )
    assert outcome == "duplicate"


def test_confirm_r2_differentiator_in_symptom_is_distinct(tmp_path: Path):
    """Ревью r2 (дефект A): различающие термины лежат в СИМПТОМАХ.

    «...из-за таймаута DNS-резолвера» vs «...таймаута пула соединений»:
    термины dns/пула входят и в тексты симптомов, поэтому вычитание
    symptom_tokens опустошает distinct-наборы обеих причин. Прежний код
    падал в ветку «обе причины = симптом → duplicate».
    """
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    outcome = storage.classify_confirm_candidate(
        hypo_symptom="задержка ответа сервера при пике нагрузки из-за таймаута DNS-резолвера",
        hypo_reason="задержка ответа сервера из-за таймаута DNS-резолвера",
        cand_symptom="задержка ответа сервера при пике нагрузки из-за таймаута пула соединений",
        cand_root_cause="задержка ответа сервера из-за таймаута пула",
    )
    assert outcome == "distinct"


def test_confirm_r2_plural_morphology_full_rephrase_is_duplicate(tmp_path: Path):
    """Ревью r2 (дефект B): перефразированный дубль с множественной морфологией.

    «кэш переполняется и вытесняет» vs «кэши переполняются и вытесняют» —
    одна причина, но сырой overlap < 0.45 давал ложный distinct.
    """
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    outcome = storage.classify_confirm_candidate(
        hypo_symptom="деградация производительности кэша",
        hypo_reason="кэш переполняется и вытесняет горячие записи",
        cand_symptom="деградация производительности кэша",
        cand_root_cause="кэши переполняются и вытесняют горячие записи",
    )
    assert outcome == "duplicate"


def test_confirm_r5_paronym_of_symptom_token_is_distinct(tmp_path: Path):
    """Ревью r5 (D1): дифференциатор — пароним токена, вычтенного как симптомный.

    «кэш ответов переписывается» vs «кэш ответов переполняется»: глагольная
    форма «переполняется» входит в СИМПТОМ и вычитается из наборов причин,
    поэтому пароним-гейт на unique-токенах её не видел. Сравнение unique
    против ПОЛНОГО набора другой причины ловит замену похожего, но другого
    слова → distinct.
    """
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    outcome = storage.classify_confirm_candidate(
        hypo_symptom="Кэш ответов переполняется",
        hypo_reason="кэш ответов переписывается из-за отсутствия вытеснения",
        cand_symptom="Кэш ответов переполняется",
        cand_root_cause="кэш ответов переполняется из-за отсутствия вытеснения",
    )
    assert outcome == "distinct"


def test_confirm_r5_antonym_single_difference_is_not_duplicate(tmp_path: Path):
    """Ревью r5 (D2): антонимы при единственном расхождении — НЕ дубль.

    «медленный диск подсистемы хранения» vs «быстрый диск подсистемы
    хранения»: поверхности не связаны морфологией и не входят в словарь
    синонимов — позитивного доказательства синонимии нет, значит
    ``duplicate`` объявлять нельзя (uncertain).
    """
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    outcome = storage.classify_confirm_candidate(
        hypo_symptom="Загрузка страницы",
        hypo_reason="медленный диск подсистемы хранения",
        cand_symptom="Загрузка страницы",
        cand_root_cause="быстрый диск подсистемы хранения",
    )
    assert outcome == "uncertain"

    outcome_en = storage.classify_confirm_candidate(
        hypo_symptom="Page load",
        hypo_reason="slow disk subsystem causes stalls",
        cand_symptom="Page load",
        cand_root_cause="fast disk subsystem causes stalls",
    )
    assert outcome_en == "uncertain"


def test_confirm_r5_synonym_pair_order_independent(tmp_path: Path):
    """Ревью r5: пары словаря синонимов нормализованы к sorted-порядку.

    Обратный порядок расхождения («исчерпан» vs «переполнен») должен
    классифицироваться так же, как прямой, — одной причиной.
    """
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    direct = storage.classify_confirm_candidate(
        hypo_symptom="Запросы к базе данных",
        hypo_reason="Пул PostgreSQL переполнен из-за утечки транзакций",
        cand_symptom="Запросы к базе данных",
        cand_root_cause="Пул PostgreSQL исчерпан из-за утечки транзакций",
    )
    reverse = storage.classify_confirm_candidate(
        hypo_symptom="Запросы к базе данных",
        hypo_reason="Пул PostgreSQL исчерпан из-за утечки транзакций",
        cand_symptom="Запросы к базе данных",
        cand_root_cause="Пул PostgreSQL переполнен из-за утечки транзакций",
    )
    assert direct == "duplicate"
    assert reverse == "duplicate"


def test_confirm_r6_short_synonym_pair_is_duplicate(tmp_path: Path):
    """Ревью r6 (B1): словарная пара в короткой формулировке — дубль.

    «пул переполнен из-за утечки» vs «пул исчерпан из-за утечки»:
    единственное расхождение — словарные синонимы «переполнен»/«исчерпан».
    Ранний overlap-гейт (2/3 < 0.75) не должен давать ложный distinct:
    confirm создавал draft рядом с существующим active уроком.
    """
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    outcome = storage.classify_confirm_candidate(
        hypo_symptom="Запросы к базе данных",
        hypo_reason="пул переполнен из-за утечки",
        cand_symptom="Запросы к базе данных",
        cand_root_cause="пул исчерпан из-за утечки",
    )
    assert outcome == "duplicate"


def test_confirm_r6_one_sided_extra_mechanism_is_uncertain(tmp_path: Path):
    """Ревью r6 (B2): кандидат добавляет независимый механизм — не дубль.

    «кэш переполняется из-за отсутствия вытеснения» vs «... при утечке
    соединений»: у кандидата независимые термины, отсутствующие у гипотезы.
    Совпадение причинных признаков не доказано → uncertain.
    """
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    outcome = storage.classify_confirm_candidate(
        hypo_symptom="Деградация производительности кэша",
        hypo_reason="кэш переполняется из-за отсутствия вытеснения",
        cand_symptom="Деградация производительности кэша",
        cand_root_cause="кэш переполняется из-за отсутствия вытеснения при утечке соединений",
    )
    assert outcome == "uncertain"


def test_confirm_r6_verb_form_rephrase_of_symptom_token_is_duplicate(
    tmp_path: Path,
):
    """Ревью r6 (N1): родственные глагольные формы — перефразирование, дубль.

    «кэш переполняется из-за отсутствия вытеснения» vs «кэш переполнен
    из-за отсутствия вытеснения»: «переполняется» вычтено как симптомный
    токен, «переполнен» — его морфологическая форма (вложенный стем
    «переполн»). Пароним-гейт на таких парах срабатывать не должен.
    """
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    outcome = storage.classify_confirm_candidate(
        hypo_symptom="Кэш переполняется",
        hypo_reason="кэш переполняется из-за отсутствия вытеснения",
        cand_symptom="Кэш переполняется",
        cand_root_cause="кэш переполнен из-за отсутствия вытеснения",
    )
    assert outcome == "duplicate"


def test_confirm_r6_cross_form_synonym_pairs_are_duplicate(tmp_path: Path):
    """Верификация r6 (D1): кросс-форменные пары словарных синонимов.

    «пул переполняется из-за утечки» vs «пул исчерпан из-за утечки»:
    ранее словарь содержал только одноимённые формы (переполняется/
    исчерпается, переполнен/исчерпан), а stem_russian("исчерпан") = None
    не склеивал формы — кросс-комбинации давали ложный distinct.
    Словарь перестроен на классы форм: любые формы синонимичных классов
    связаны.
    """
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    outcome = storage.classify_confirm_candidate(
        hypo_symptom="Запросы к базе данных",
        hypo_reason="пул переполняется из-за утечки",
        cand_symptom="Запросы к базе данных",
        cand_root_cause="пул исчерпан из-за утечки",
    )
    assert outcome == "duplicate"

    outcome2 = storage.classify_confirm_candidate(
        hypo_symptom="Запросы к базе данных",
        hypo_reason="Пул PostgreSQL переполняется из-за утечки транзакций",
        cand_symptom="Запросы к базе данных",
        cand_root_cause="Пул PostgreSQL исчерпан из-за утечки транзакций",
    )
    assert outcome2 == "duplicate"

    # Контроль: антонимы остаются НЕ-дублями (классы не связаны)
    antonym = storage.classify_confirm_candidate(
        hypo_symptom="Загрузка страницы",
        hypo_reason="медленный диск подсистемы хранения",
        cand_symptom="Загрузка страницы",
        cand_root_cause="быстрый диск подсистемы хранения",
    )
    assert antonym != "duplicate"


def test_confirm_r8_synonym_pair_in_symptom_only_reasons(tmp_path: Path):
    """Ревью r8 (P1) → уточнение r11 (п. 2): причины-повторения симптома
    со словарной заменой — НЕ доказанный дубль.

    Симптом «пул переполнен или исчерпан при утечке» содержит оба слова;
    причины «пул переполнен при утечке» и «пул исчерпан при утечке» не
    имеют собственных токенов. Прежний код (r8) сравнивал полные наборы
    только строгой морфологией → ложный distinct и draft рядом с
    active-карточкой; фикс r8 сделал duplicate. Ревью r11 (п. 2)
    уточняет контракт: без собственных причинных токенов замена
    синонима не доказывает совпадение причин (гипотеза не должна
    закрываться duplicate) → uncertain, как и дословное совпадение
    пустых причин (G1).
    """
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    outcome = storage.classify_confirm_candidate(
        hypo_symptom="пул переполнен или исчерпан при утечке",
        hypo_reason="пул переполнен при утечке",
        cand_symptom="пул переполнен или исчерпан при утечке",
        cand_root_cause="пул исчерпан при утечке",
    )
    assert outcome == "uncertain"

    # Контроль: настоящие разные термины в тех же условиях — distinct
    distinct_case = storage.classify_confirm_candidate(
        hypo_symptom="задержка из-за dns или пула соединений при таймауте",
        hypo_reason="задержка из-за dns при таймауте",
        cand_symptom="задержка из-за dns или пула соединений при таймауте",
        cand_root_cause="задержка из-за пула при таймауте",
    )
    assert distinct_case == "distinct"


def test_confirm_r8_db_alias_terms_are_duplicate(tmp_path: Path):
    """Ревью r8 (P2): алиасы СУБД (postgres/postgresql) — один термин.

    Прежний пароним-гейт (префикс >= 5) считал «postgres» и «postgresql»
    разными словами → ложный distinct и draft-дубль рядом с active-уроком.
    Алиасы учитываются в ``_reason_tokens_related`` и не проходят как
    паронимы.
    """
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    outcome = storage.classify_confirm_candidate(
        hypo_symptom="Задержка ответа сервера",
        hypo_reason="пул postgres исчерпан из-за утечки транзакций",
        cand_symptom="Задержка ответа сервера",
        cand_root_cause="пул postgresql исчерпан из-за утечки транзакций",
    )
    assert outcome == "duplicate"

    # Контроль: разные СУБД — разные причины
    diff_db = storage.classify_confirm_candidate(
        hypo_symptom="Задержка ответа сервера",
        hypo_reason="пул postgres исчерпан из-за утечки транзакций",
        cand_symptom="Задержка ответа сервера",
        cand_root_cause="пул mysql исчерпан из-за утечки транзакций",
    )
    assert diff_db != "duplicate"


def test_confirm_r9_short_alias_canonicalization_no_false_duplicate(
    tmp_path: Path,
):
    """Верификация r9 (JSvsTS): 2-символьные алиасы видны токенизатору.

    Токенайзер отбрасывает слова короче 3 символов, поэтому «js»/«ts»
    выпадали из наборов токенов и причины разных технологий склеивались
    в ложный duplicate (confirm блокировался рядом с JS-уроком при
    TS-гипотезе). Канонизация ``_SHORT_ALIAS_CANON`` до токенизации
    сохраняет различитель: js → javascript, ts → typescript.
    """
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    # js vs ts — РАЗНЫЕ технологии, не дубль
    diff_stack = storage.classify_confirm_candidate(
        hypo_symptom="Переполнение heap",
        hypo_reason="heap js переполнен из-за утечки слушателей",
        cand_symptom="Переполнение heap",
        cand_root_cause="heap ts переполнен из-за утечки слушателей",
    )
    assert diff_stack != "duplicate"

    # js vs javascript — один термин, дубль (алиас канонизирован)
    alias_same = storage.classify_confirm_candidate(
        hypo_symptom="Переполнение heap",
        hypo_reason="heap js переполнен из-за утечки слушателей",
        cand_symptom="Переполнение heap",
        cand_root_cause="heap javascript переполнен из-за утечки слушателей",
    )
    assert alias_same == "duplicate"


def test_confirm_r9_extended_db_and_tech_aliases_are_duplicate(tmp_path: Path):
    """Верификация r9: дополнительные алиасы технологий/СУБД.

    mongo/mongodb, sqlite/sqlite3, memcache/memcached, pg/postgres,
    memory/ram — один термин. pg (2 символа) канонизируется в postgres.
    """
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    pairs = [
        ("память mongo переполнена из-за курсоров", "память mongodb переполнена из-за курсоров"),
        (
            "база sqlite заблокирована из-за длинных транзакций",
            "база sqlite3 заблокирована из-за длинных транзакций",
        ),
        (
            "кэш memcache переполнен из-за утечки ключей",
            "кэш memcached переполнен из-за утечки ключей",
        ),
        (
            "пул pg исчерпан из-за утечки соединений",
            "пул postgres исчерпан из-за утечки соединений",
        ),
        ("память memory переполнена из-за утечки", "память ram переполнена из-за утечки"),
    ]
    for hypo_reason, cand_reason in pairs:
        outcome = storage.classify_confirm_candidate(
            hypo_symptom="Деградация производительности",
            hypo_reason=hypo_reason,
            cand_symptom="Деградация производительности",
            cand_root_cause=cand_reason,
        )
        assert outcome == "duplicate", (hypo_reason, cand_reason)

    # Контроль: mariadb ≠ postgres (разные СУБД, не связаны алиасом)
    cross = storage.classify_confirm_candidate(
        hypo_symptom="Деградация производительности",
        hypo_reason="пул postgres исчерпан из-за утечки транзакций",
        cand_symptom="Деградация производительности",
        cand_root_cause="пул mariadb исчерпан из-за утечки транзакций",
    )
    assert cross != "duplicate"


def test_confirm_r11_mysql_mariadb_are_distinct(tmp_path: Path):
    """Ревью r11 (п. 1): MySQL и MariaDB — РАЗНЫЕ СУБД, не алиасы.

    Прежняя пара в _REASON_ALIAS_PAIRS позволяла confirm считать разные
    причины дублем и закрывать гипотезу duplicate-действием как
    superseded в пользу «канонического» урока другой СУБД.
    """
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    outcome = storage.classify_confirm_candidate(
        hypo_symptom="Задержка ответа сервера",
        hypo_reason="пул mysql исчерпан из-за утечки транзакций",
        cand_symptom="Задержка ответа сервера",
        cand_root_cause="пул mariadb исчерпан из-за утечки транзакций",
    )
    assert outcome == "distinct"

    reverse = storage.classify_confirm_candidate(
        hypo_symptom="Задержка ответа сервера",
        hypo_reason="пул mariadb исчерпан из-за утечки транзакций",
        cand_symptom="Задержка ответа сервера",
        cand_root_cause="пул mysql исчерпан из-за утечки транзакций",
    )
    assert reverse == "distinct"


def test_confirm_r11_different_tech_terms_reasons_are_distinct(tmp_path: Path):
    """Ревью r11 (п. 3): разные СУБД в причинах — distinct, не uncertain.

    «пул postgres исчерпан...» vs «пул mysql исчерпан...»: оба термина
    известны и не связаны алиасом — различие доказуемо, confirm должен
    создавать draft (distinct-путь), а не блокироваться uncertain.
    """
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    outcome = storage.classify_confirm_candidate(
        hypo_symptom="Задержка ответа сервера",
        hypo_reason="пул postgres исчерпан из-за утечки транзакций",
        cand_symptom="Задержка ответа сервера",
        cand_root_cause="пул mysql исчерпан из-за утечки транзакций",
    )
    assert outcome == "distinct"


def test_resolve_duplicate_target_must_not_be_open_hypothesis(tmp_path: Path):
    """Ревью шага 3: duplicate_of не принимает открытую гипотезу как каноническую цель."""
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    common_symptom = "задержка ответа API"
    common_cause = "медленный DNS-резолвер кэш пустой"

    # Две открытые гипотезы с близкими, но разными симптомами (чтобы
    # propose_hypothesis не отклонил вторую как дубль первой)
    hypo_a = service.propose_hypothesis(
        HypothesisRecordRequest(
            title="DNS Hypo A",
            category="backend",
            symptom=common_symptom,
            hypothesis=common_cause,
            slug="dns-hypo-a",
        )
    )
    hypo_b = service.propose_hypothesis(
        HypothesisRecordRequest(
            title="DNS Hypo B",
            category="backend",
            symptom="падение throughput в очереди задач при старте воркеров",
            hypothesis="недостаточный размер prefetch-буфера потребителя очереди",
            slug="dns-hypo-b",
        )
    )

    # duplicate_of на открытую гипотезу B → отказ
    with pytest.raises(ValueError) as exc_info:
        service.resolve_hypotheses(
            HypothesesResolveRequest(
                resolutions=[
                    HypothesisResolution(
                        id=hypo_a["id"],
                        action="duplicate",
                        duplicate_of=hypo_b["id"],
                    )
                ]
            )
        )
    assert "не может быть канонической целью duplicate_of" in str(exc_info.value)

    # Обе карточки остались гипотезами
    assert service.get([hypo_a["id"]]).cards[0].status == KnowledgeStatus.HYPOTHESIS
    assert service.get([hypo_b["id"]]).cards[0].status == KnowledgeStatus.HYPOTHESIS


def test_resolve_duplicate_target_must_not_be_refuted(tmp_path: Path):
    """Ревью r2: duplicate_of не принимает опровергнутую (refuted) карточку
    как каноническую цель — дубликат закрывается в пользу карточки-факта."""
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    # Опровергнутая карточка: гипотеза → refute (без merge) → refuted/antipattern
    refuted = service.propose_hypothesis(
        HypothesisRecordRequest(
            title="Refuted Lesson Target",
            category="backend",
            symptom="задержка ответа API",
            hypothesis="медленный DNS-резолвер кэш пустой",
            slug="refuted-lesson-target",
        )
    )
    service.resolve_hypotheses(
        HypothesesResolveRequest(
            resolutions=[
                HypothesisResolution(
                    id=refuted["id"],
                    action="refute",
                    refutation_reason="Замеры показали, что DNS-резолв мгновенный, причина не в DNS",
                )
            ]
        )
    )
    assert service.get([refuted["id"]]).cards[0].status == KnowledgeStatus.REFUTED

    # Открытая гипотеза с той же формулировкой
    hypo = service.propose_hypothesis(
        HypothesisRecordRequest(
            title="Same Idea Hypo",
            category="backend",
            symptom="задержка ответа API и потери пакетов при пике нагрузки",
            hypothesis="медленный DNS-резолвер и пустой кэш записей",
            slug="same-idea-hypo",
        )
    )

    # duplicate_of на refuted-карточку → отказ
    with pytest.raises(ValueError) as exc_info:
        service.resolve_hypotheses(
            HypothesesResolveRequest(
                resolutions=[
                    HypothesisResolution(
                        id=hypo["id"],
                        action="duplicate",
                        duplicate_of=refuted["id"],
                    )
                ]
            )
        )
    assert "не может быть канонической целью duplicate_of" in str(exc_info.value)
    assert "refuted" in str(exc_info.value) or "статус" in str(exc_info.value)

    # Гипотеза осталась гипотезой
    assert service.get([hypo["id"]]).cards[0].status == KnowledgeStatus.HYPOTHESIS


def test_resolve_duplicate_target_must_not_be_disputed(tmp_path: Path):
    """Ревью r4 (п. 3): duplicate_of не принимает оспоренную (disputed) карточку.

    Явный список допустимых статусов цели: active/verified/suspect/draft.
    """
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    # Disputed-карточка: урок, помеченный оспоренным, напрямую в репозитории
    rec = service.record(
        CardRecordRequest(
            title="Disputed Lesson",
            category="backend",
            type=KnowledgeType.LESSON,
            symptom="задержка ответа API и потери пакетов при пике нагрузки",
            root_cause="медленный DNS-резолвер и пустой кэш записей",
            solution="Включить кэширующий DNS-резолвер и увеличить TTL записей",
            rationale="Оспаривается: замеры не подтвердили эффект от DNS-кэша",
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )
    disputed_id = rec["id"]
    # Переводим в disputed напрямую (симметрично ревью-репро)
    repo = CardRepository(knowledge_dir=k_dir)
    card_file = repo.find_card_file(disputed_id)
    fm, sections = repo.read_card_file(card_file)
    fm.status = KnowledgeStatus.DISPUTED
    repo.save_card(card_file, fm, sections)
    assert service.get([disputed_id]).cards[0].status == KnowledgeStatus.DISPUTED

    hypo = service.propose_hypothesis(
        HypothesisRecordRequest(
            title="Same Idea Disputed Hypo",
            category="backend",
            symptom="задержка ответа API и потери пакетов при пике нагрузки",
            hypothesis="медленный DNS-резолвер и пустой кэш записей",
            slug="same-idea-disputed-hypo",
        )
    )

    with pytest.raises(ValueError) as exc_info:
        service.resolve_hypotheses(
            HypothesesResolveRequest(
                resolutions=[
                    HypothesisResolution(
                        id=hypo["id"],
                        action="duplicate",
                        duplicate_of=disputed_id,
                    )
                ]
            )
        )
    assert "не может быть канонической целью duplicate_of" in str(exc_info.value)
    assert "disputed" in str(exc_info.value)

    # Гипотеза осталась гипотезой
    assert service.get([hypo["id"]]).cards[0].status == KnowledgeStatus.HYPOTHESIS


def test_resolve_duplicate_target_suspect_is_allowed(tmp_path: Path):
    """Ревью r6 (тупик suspect): suspect-карточка — допустимая цель duplicate_of.

    classify_confirm ищет кандидатов среди ACTIVE_STATUS_SQL (включает
    suspect) и блокирует confirm гипотезы-дубля DuplicateCardError. Если
    suspect не допускается как цель duplicate_of, гипотеза застревает:
    ни подтвердиться, ни закрыться дубликатом.
    """
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    # Suspect-карточка: урок, помеченный сомнительным, напрямую в репозитории
    rec = service.record(
        CardRecordRequest(
            title="Suspect Lesson",
            category="backend",
            type=KnowledgeType.LESSON,
            symptom="задержка ответа API и потери пакетов при пике нагрузки",
            root_cause="медленный DNS-резолвер и пустой кэш записей",
            solution="Включить кэширующий DNS-резолвер и увеличить TTL записей",
            rationale="Под сомнением: часть замеров не воспроизводится",
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )
    suspect_id = rec["id"]
    repo = CardRepository(knowledge_dir=k_dir)
    card_file = repo.find_card_file(suspect_id)
    fm, sections = repo.read_card_file(card_file)
    fm.status = KnowledgeStatus.SUSPECT
    repo.save_card(card_file, fm, sections)
    assert service.get([suspect_id]).cards[0].status == KnowledgeStatus.SUSPECT

    hypo = service.propose_hypothesis(
        HypothesisRecordRequest(
            title="Same Idea Suspect Hypo",
            category="backend",
            symptom="задержка ответа API и потери пакетов при пике нагрузки",
            hypothesis="медленный DNS-резолвер и пустой кэш записей",
            slug="same-idea-suspect-hypo",
        )
    )

    # duplicate_of на suspect-карточку → закрытие гипотезы (не тупик)
    res = service.resolve_hypotheses(
        HypothesesResolveRequest(
            resolutions=[
                HypothesisResolution(
                    id=hypo["id"],
                    action="duplicate",
                    duplicate_of=suspect_id,
                )
            ]
        )
    )
    assert res["status"] == "resolved"
    card = service.get([hypo["id"]]).cards[0]
    assert card.status == KnowledgeStatus.SUPERSEDED
    assert card.superseded_by == suspect_id


def test_resolve_refute_rejects_already_closed_card_under_lock(tmp_path: Path):
    """Ревью шага 3: конкурентная резолюция, закрывшая карточку между проходами,
    не позволяет второму батчу перезаписать закрытую карточку.

    Эмулируется подменой карточки между Pass 1 и Pass 2: сначала валидируем
    батч (кэширует снимки), затем конкурентно закрываем карточку напрямую
    в репозитории, затем применяем батч — должен быть отказ, а не слепая
    перезапись статуса.
    """
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    hypo = service.propose_hypothesis(
        HypothesisRecordRequest(
            title="Concurrent Refute Target",
            category="backend",
            symptom="задержка ответа API",
            hypothesis="медленный DNS-резолвер кэш пустой",
            slug="concurrent-refute-target",
        )
    )
    hypo_id = hypo["id"]

    from mcp_knowledge.use_cases.resolve_hypotheses import ResolveHypothesesUseCase as _UC
    from mcp_knowledge.repository import CardRepository
    from mcp_knowledge.synchronizer import KnowledgeSynchronizer

    repo = CardRepository(knowledge_dir=k_dir)
    sync = KnowledgeSynchronizer(storage=storage, repo=repo)
    use_case = _UC(repo=repo, storage=storage, synchronizer=sync)

    req = HypothesesResolveRequest(
        task_id="t",
        resolutions=[
            HypothesisResolution(
                id=hypo_id,
                action="refute",
                refutation_reason="Замеры показали, что DNS не при чём: резолв мгновенный",
            )
        ],
    )

    # Конкурентное закрытие карточки МЕЖДУ Pass 1 и Pass 2: валидация снимков
    # (Pass 1) должна пройти, а применение (Pass 2) — отказаться под локом.
    # Перехват срабатывает на ВТОРОМ чтении карточки (Pass 1 читает первый,
    # Pass 2 перечитывает под локом) — именно там конкурент закрывает карточку.
    original_read = repo.read_card_file
    read_count = {"n": 0}

    def read_and_close(path, *a, **kw):
        fm, sections = original_read(path, *a, **kw)
        if getattr(fm, "id", None) == hypo_id:
            read_count["n"] += 1
            if read_count["n"] == 2:
                # Конкурентный refute напрямую в репозиторий между проходами
                fm.status = KnowledgeStatus.REFUTED
                fm.type = KnowledgeType.ANTIPATTERN
                repo.save_card(path, fm, sections)
        return original_read(path, *a, **kw)

    with monkeypatch_global(repo, "read_card_file", read_and_close):
        with pytest.raises(ValueError) as exc_info:
            use_case.execute(req)
    assert "больше не является рабочей гипотезой" in str(exc_info.value)
    # Конкурентное закрытие действительно произошло между проходами
    assert read_count["n"] >= 2


def test_resolve_duplicate_action_on_true_duplicate(tmp_path: Path):
    """action='duplicate', duplicate_of=<id>: закрывает гипотезу как superseded, каноническую не меняет."""
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    common_symptom = "Таймаут инициализации пула соединений при старте воркера"
    common_cause = "Превышение лимита одновременных полуоткрытых соединений в операционной системе"

    rec = service.record(
        CardRecordRequest(
            title="Worker Init Timeout Canonical",
            category="backend",
            type=KnowledgeType.LESSON,
            symptom=common_symptom,
            root_cause=common_cause,
            solution="Тюнинг параметров somaxconn и tcp_max_syn_backlog в конфигурации sysctl на хосте",
            rationale="Устраняет отбрасывание SYN пакетов при параллельном старте воркеров",
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )
    canonical_id = rec["id"]
    canonical_card_before = service.get([canonical_id]).cards[0]

    hypo = service.propose_hypothesis(
        HypothesisRecordRequest(
            title="Worker Init Timeout Duplicate Hypothesis",
            category="backend",
            symptom=common_symptom,
            hypothesis=common_cause,
            slug="worker-init-dup",
        )
    )
    hypo_id = hypo["id"]

    # Разрешаем через явное action='duplicate'
    res = service.resolve_hypotheses(
        HypothesesResolveRequest(
            resolutions=[
                HypothesisResolution(
                    id=hypo_id,
                    action="duplicate",
                    duplicate_of=canonical_id,
                )
            ]
        )
    )
    assert res["status"] == "resolved"
    assert res["resolved_count"] == 1
    assert res["details"][0]["action"] == "duplicate"
    assert res["details"][0]["superseded_by"] == canonical_id

    # Гипотеза переведена в superseded и хранит ссылку
    hypo_card = service.get([hypo_id]).cards[0]
    assert hypo_card.status == KnowledgeStatus.SUPERSEDED
    assert hypo_card.superseded_by == canonical_id
    assert "DUPLICATE: Superseded by" in hypo_card.content

    # Каноническая карточка не мутирована
    canonical_card_after = service.get([canonical_id]).cards[0]
    assert canonical_card_after.content == canonical_card_before.content
    assert canonical_card_after.status == KnowledgeStatus.ACTIVE


def test_resolve_duplicate_action_fails_on_distinct_or_invalid_target(tmp_path: Path):
    """duplicate_of на карточку с иной причиной, неполными данными, несуществующую или вытесненную отклоняется."""
    db_path = tmp_path / "cache.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    common_symptom = "Таймаут инициализации пула соединений при старте воркера"

    rec_distinct = service.record(
        CardRecordRequest(
            title="Worker Init Timeout Distinct Cause",
            category="backend",
            type=KnowledgeType.LESSON,
            symptom=common_symptom,
            root_cause="Блокировка файлового дескриптора другим процессом монопольно",
            solution="Проверка механизма flock перед повторным открытием дескриптора сокета в цикле",
            rationale="Исключает состояние гонки при инициализации нескольких процессов воркеров",
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )
    distinct_id = rec_distinct["id"]

    hypo = service.propose_hypothesis(
        HypothesisRecordRequest(
            title="Worker Init Timeout Memory Cause",
            category="backend",
            symptom=common_symptom,
            hypothesis="Нехватка виртуальной памяти при аллокации буфера сокета",
            slug="worker-init-mem",
        )
    )
    hypo_id = hypo["id"]

    # 1. Ошибочный duplicate_of на карточку с другой причиной -> отказ
    with pytest.raises(ValueError) as exc1:
        service.resolve_hypotheses(
            HypothesesResolveRequest(
                resolutions=[
                    HypothesisResolution(
                        id=hypo_id,
                        action="duplicate",
                        duplicate_of=distinct_id,
                    )
                ]
            )
        )
    assert "distinct" in str(exc1.value).lower()

    # 2. duplicate_of на самого себя -> отказ
    with pytest.raises(ValueError) as exc2:
        service.resolve_hypotheses(
            HypothesesResolveRequest(
                resolutions=[
                    HypothesisResolution(
                        id=hypo_id,
                        action="duplicate",
                        duplicate_of=hypo_id,
                    )
                ]
            )
        )
    assert "саму себя" in str(exc2.value).lower()

    # 3. duplicate_of на несуществующую карточку -> CardNotFoundError
    with pytest.raises(CardNotFoundError):
        service.resolve_hypotheses(
            HypothesesResolveRequest(
                resolutions=[
                    HypothesisResolution(
                        id=hypo_id,
                        action="duplicate",
                        duplicate_of="non-existent-card-id",
                    )
                ]
            )
        )

    # Гипотеза осталась целой и невредимой
    card = service.get([hypo_id]).cards[0]
    assert card.status == KnowledgeStatus.HYPOTHESIS
