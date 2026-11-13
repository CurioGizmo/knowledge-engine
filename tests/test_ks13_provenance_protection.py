"""Тесты защиты происхождения карточек знаний (KS13, Вариант Б).

Проверяет:
1. Предикат is_human_protected у CardFrontmatter и его краевые случаи.
2. Матрицу операций для Группы A (карточки человека, generated.by="human").
3. Матрицу операций для Группы B (продвинутые карточки, promoted to active).
4. Разрешённые операции для человека (by="human", by="developer", source="user_confirmed").
5. Двойной барьер вытеснения под card_lock и откат (unlink) созданного файла при конфликте.
"""

from datetime import date
from pathlib import Path
import pytest
from mcp.types import CallToolResult

from mcp_knowledge.coordinator import CardMutationCoordinator
from mcp_knowledge.errors import (
    ForbiddenUserConfirmedActionError,
    UserConfirmedConflictError,
)
from mcp_knowledge.models import (
    CardFrontmatter,
    CardRecordRequest,
    CardStatusUpdateRequest,
    GeneratedBy,
    GeneratedMetadata,
    HypothesisResolution,
    KnowledgeCategory,
    KnowledgeSource,
    KnowledgeStatus,
    KnowledgeType,
    VerificationRecord,
)
from mcp_knowledge.parser import serialize_card_markdown
from mcp_knowledge.repository import CardRepository
from mcp_knowledge.server import create_server
from mcp_knowledge.service import KnowledgeService
from mcp_knowledge.storage import KnowledgeStorage
from mcp_knowledge.synchronizer import KnowledgeSynchronizer


# ==============================================================================
# 1. Модульные тесты предиката is_human_protected
# ==============================================================================


def test_is_human_protected_group_a_human():
    """Карточки от человека (generated.by='human' или source=USER_CONFIRMED) защищены."""
    fm = CardFrontmatter(
        id="backend-auth-jwt",
        title="JWT Auth",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.DECISION,
        status=KnowledgeStatus.ACTIVE,
        generated=GeneratedMetadata(by=GeneratedBy.HUMAN, created_at="2026-10-30"),
    )
    assert fm.source == KnowledgeSource.USER_CONFIRMED
    assert fm.is_human_protected is True


def test_is_human_protected_group_b_promoted():
    """Карточки, продвинутые в active через promote, защищены."""
    fm = CardFrontmatter(
        id="backend-cache-redis",
        title="Redis Caching",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.DECISION,
        status=KnowledgeStatus.ACTIVE,
        generated=GeneratedMetadata(by=GeneratedBy.PROCESS, created_at="2026-10-30"),
        verified=[
            VerificationRecord(
                at="2026-10-30",
                by="human",
                note="Promoted to active via user consent",
                action="promote",
            )
        ],
    )
    assert fm.source == KnowledgeSource.AUTO_INFERRED
    assert fm.is_human_protected is True


def test_is_human_protected_promoted_developer():
    """Продвижение кем-то из HUMAN_CALLER_NAMES (developer/admin) защищено."""
    fm = CardFrontmatter(
        id="backend-cache-redis",
        title="Redis Caching",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.DECISION,
        status=KnowledgeStatus.ACTIVE,
        generated=GeneratedMetadata(by=GeneratedBy.PROCESS, created_at="2026-10-30"),
        verified=[
            VerificationRecord(
                at="2026-10-30",
                by="developer",
                note="promoted to active by lead dev",
                action="promote",
            )
        ],
    )
    assert fm.is_human_protected is True


def test_is_human_protected_promoted_by_agent_with_user_confirmed():
    """Дефект 1: Промоушен агентом от имени пользователя (source=user_confirmed, by=agent) защищён."""
    fm = CardFrontmatter(
        id="backend-cache-redis",
        title="Redis Caching",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.DECISION,
        status=KnowledgeStatus.ACTIVE,
        generated=GeneratedMetadata(by=GeneratedBy.PROCESS, created_at="2026-10-30"),
        verified=[
            VerificationRecord(
                at="2026-10-30",
                by="agent",
                note="Promoted to active: user approved",
                action="promote",
            )
        ],
    )
    assert fm.is_human_protected is True


def test_is_human_protected_multiple_verification_records():
    """Предикат корректно работает при наличии нескольких записей в verified."""
    fm = CardFrontmatter(
        id="backend-cache-redis",
        title="Redis Caching",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.DECISION,
        status=KnowledgeStatus.ACTIVE,
        generated=GeneratedMetadata(by=GeneratedBy.PROCESS, created_at="2026-10-30"),
        verified=[
            VerificationRecord(
                at="2026-10-30",
                by="agent",
                note="Automated bench test",
                action="verify",
            ),
            VerificationRecord(
                at="2026-10-30",
                by="human",
                note="Promoted to active with approval",
                action="promote",
            ),
            VerificationRecord(
                at="2026-10-30",
                by="admin",
                note="Subsequent audit verification",
                action="verify",
            ),
        ],
    )
    assert fm.is_human_protected is True


def test_is_human_protected_ordinary_verify_does_not_enable_protection():
    """Дефект 2: Обычный verify активной карточки процесса с note='Promoted to active: checklist'
    не создаёт ложную защиту.
    """
    fm = CardFrontmatter(
        id="backend-cache-redis",
        title="Redis Caching",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.DECISION,
        status=KnowledgeStatus.ACTIVE,
        generated=GeneratedMetadata(by=GeneratedBy.PROCESS, created_at="2026-10-30"),
        verified=[
            VerificationRecord(
                at="2026-10-30",
                by="human",
                note="Promoted to active: checklist",
                action="verify",
            )
        ],
    )
    assert fm.is_human_protected is False


def test_is_human_protected_legacy_verification_not_protected():
    """Обычная верификация человеком без промоушена не даёт защиту Группы B."""
    fm = CardFrontmatter(
        id="backend-cache-redis",
        title="Redis Caching",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.DECISION,
        status=KnowledgeStatus.ACTIVE,
        generated=GeneratedMetadata(by=GeneratedBy.PROCESS, created_at="2026-10-30"),
        verified=[
            VerificationRecord(
                at="2026-10-30",
                by="human",
                note="Verified from legacy record",
                action="verify",
            )
        ],
    )
    assert fm.is_human_protected is False


def test_is_human_protected_ordinary_draft_not_protected():
    """Обычный черновик без промоушена не защищён."""
    fm = CardFrontmatter(
        id="backend-cache-redis",
        title="Redis Caching",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.DECISION,
        status=KnowledgeStatus.DRAFT,
        generated=GeneratedMetadata(by=GeneratedBy.PROCESS, created_at="2026-10-30"),
    )
    assert fm.is_human_protected is False


# ==============================================================================
# 2. Интеграционные тесты матрицы операций через MCP Server
# ==============================================================================


@pytest.fixture
def test_setup(tmp_path: Path):
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    k_dir = project_root / ".agents" / "knowledge"
    (k_dir / "backend").mkdir(parents=True)
    (project_root / ".agents" / "hypotheses" / "backend").mkdir(parents=True)
    server = create_server(project_root=project_root)
    return project_root, server


@pytest.mark.asyncio
async def test_ks13_matrix_group_a_human(test_setup):
    """Матрица операций для Группы A (человек):
    - dispute: REJECT
    - verify: REJECT
    - suspect: OK
    - append_note: OK
    - supersedes (auto): REJECT
    - merge_into (auto): REJECT
    """
    project_root, server = test_setup

    # 1. Создаем карточку человека
    rec_res = await server.call_tool(
        "record_knowledge",
        {
            "title": "Архитектура аутентификации JWT",
            "category": "backend",
            "type": "decision",
            "source": "user_confirmed",
            "symptom": "Необходима масштабируемая аутентификация в микросервисах",
            "root_cause": "Сессионная аутентификация перегружает общую БД сессий",
            "solution": "Использование stateless JWT токенов с асимметричной подписью RS256",
            "rationale": "RS256 позволяет валидировать токен локально без обращения к Auth-сервису",
            "slug": "backend-jwt-auth-test",
            "project_path": str(project_root),
        },
    )
    assert not rec_res.is_error
    card_id = "backend-jwt-auth-test"

    # 2. dispute (auto) -> REJECT
    res_dispute = await server.call_tool(
        "update_knowledge_status",
        {
            "id": card_id,
            "action": "dispute",
            "note": "Автоматическая попытка оспорить карточку",
            "project_path": str(project_root),
        },
    )
    assert res_dispute.is_error

    # 3. verify (auto) -> REJECT
    res_verify = await server.call_tool(
        "update_knowledge_status",
        {
            "id": card_id,
            "action": "verify",
            "note": "Автоматическая попытка подтвердить карточку",
            "project_path": str(project_root),
        },
    )
    assert res_verify.is_error

    # 4. suspect -> OK
    res_suspect = await server.call_tool(
        "update_knowledge_status",
        {
            "id": card_id,
            "action": "suspect",
            "note": "Появились сомнения в актуальности размера токена",
            "project_path": str(project_root),
        },
    )
    assert not res_suspect.is_error

    # 5. append_note -> OK
    res_note = await server.call_tool(
        "update_knowledge_status",
        {
            "id": card_id,
            "action": "append_note",
            "note": "Добавлено примечание по ротации ключей RS256",
            "project_path": str(project_root),
        },
    )
    assert not res_note.is_error

    # 6. supersedes (auto) -> REJECT
    res_super = await server.call_tool(
        "record_knowledge",
        {
            "title": "Переход на Paseto токены",
            "category": "backend",
            "type": "decision",
            "supersedes": card_id,
            "symptom": "JWT имеет уязвимости с выбором некорректных алгоритмов",
            "root_cause": "Сложность валидации заголовков алгоритмов в JWT библиотеках",
            "solution": "Переход на PASETO v4 с фиксированными безопасными шифрами",
            "rationale": "Исключает ошибки конфигурирования криптографических примитивов",
            "slug": "backend-paseto-auth-test",
            "project_path": str(project_root),
        },
    )
    assert res_super.is_error

    # 7. merge_into (auto) -> REJECT
    res_hypo = await server.call_tool(
        "propose_hypothesis",
        {
            "title": "Симметричные JWT HMAC-SHA256",
            "category": "backend",
            "symptom": "Замедление при проверке асимметричных ключей",
            "hypothesis": "HMAC-SHA256 работает быстрее и проще в реализации",
            "slug": "backend-jwt-hmac-hypo",
            "project_path": str(project_root),
        },
    )
    assert not res_hypo.is_error

    res_resolve = await server.call_tool(
        "resolve_hypotheses",
        {
            "resolutions": [
                {
                    "id": "backend-jwt-hmac-hypo",
                    "action": "refute",
                    "refutation_reason": "Общий секрет небезопасен в распределённой сети микросервисов",
                    "merge_into": card_id,
                }
            ],
            "project_path": str(project_root),
        },
    )
    assert res_resolve.is_error


@pytest.mark.asyncio
async def test_ks13_matrix_group_b_promoted(test_setup):
    """Матрица операций для Группы B (promoted карточка):
    - dispute: REJECT
    - verify: REJECT
    - suspect: OK
    - append_note: OK
    - supersedes (auto): REJECT
    - merge_into (auto): REJECT
    """
    project_root, server = test_setup

    # 1. Создаем draft карточку от агента и продвигаем ее человеком
    rec_res = await server.call_tool(
        "record_knowledge",
        {
            "title": "Кэширование сессий в Redis",
            "category": "backend",
            "type": "lesson",
            "symptom": "Высокая задержка ответа сервиса при пиковых нагрузках",
            "root_cause": "Каждый запрос выполняет синхронное чтение профиля из СУБД",
            "solution": "Внедрение двухслойного кэша Redis с TTL 300 секунд",
            "rationale": "Снижает нагрузку на основную БД более чем в 10 раз",
            "slug": "backend-redis-cache-test",
            "project_path": str(project_root),
        },
    )
    assert not rec_res.is_error
    card_id = "backend-redis-cache-test"

    # Promote человеком
    promote_res = await server.call_tool(
        "update_knowledge_status",
        {
            "id": card_id,
            "action": "promote",
            "by": "human",
            "note": "Одобрено архитектором в активную базу",
            "project_path": str(project_root),
        },
    )
    assert not promote_res.is_error

    # 2. dispute (auto) -> REJECT
    res_dispute = await server.call_tool(
        "update_knowledge_status",
        {
            "id": card_id,
            "action": "dispute",
            "note": "Автоматическая попытка оспорить продвинутую карточку",
            "project_path": str(project_root),
        },
    )
    assert res_dispute.is_error

    # 3. verify (auto) -> REJECT
    res_verify = await server.call_tool(
        "update_knowledge_status",
        {
            "id": card_id,
            "action": "verify",
            "note": "Автоматическая попытка верифицировать продвинутую карточку",
            "project_path": str(project_root),
        },
    )
    assert res_verify.is_error

    # 4. suspect -> OK
    res_suspect = await server.call_tool(
        "update_knowledge_status",
        {
            "id": card_id,
            "action": "suspect",
            "note": "Обнаружен сбой при инвалидации ключей Redis",
            "project_path": str(project_root),
        },
    )
    assert not res_suspect.is_error

    # 5. append_note -> OK
    res_note = await server.call_tool(
        "update_knowledge_status",
        {
            "id": card_id,
            "action": "append_note",
            "note": "Добавлена информация про Redis Cluster",
            "project_path": str(project_root),
        },
    )
    assert not res_note.is_error

    # 6. supersedes (auto) -> REJECT
    res_super = await server.call_tool(
        "record_knowledge",
        {
            "title": "Переход на Memcached",
            "category": "backend",
            "type": "lesson",
            "supersedes": card_id,
            "symptom": "Redis требует больше оперативной памяти под служебные структуры",
            "root_cause": "Модель данных Redis сложнее чистого slab allocator Memcached",
            "solution": "Использование Memcached для простых ключей",
            "rationale": "Меньшее потребление RAM",
            "slug": "backend-memcached-test",
            "project_path": str(project_root),
        },
    )
    assert res_super.is_error

    # 7. merge_into (auto) -> REJECT
    res_hypo = await server.call_tool(
        "propose_hypothesis",
        {
            "title": "Локальный in-memory кэш процессов",
            "category": "backend",
            "symptom": "Задержка сетевых обращений к Redis",
            "hypothesis": "In-memory dict в процессе быстрее",
            "slug": "backend-local-cache-hypo",
            "project_path": str(project_root),
        },
    )
    assert not res_hypo.is_error

    res_resolve = await server.call_tool(
        "resolve_hypotheses",
        {
            "resolutions": [
                {
                    "id": "backend-local-cache-hypo",
                    "action": "refute",
                    "refutation_reason": "In-memory кэш не синхронизируется между подами",
                    "merge_into": card_id,
                }
            ],
            "project_path": str(project_root),
        },
    )
    assert res_resolve.is_error


@pytest.mark.asyncio
async def test_ks13_human_operations_allowed(test_setup):
    """Человеку (by='human' или source='user_confirmed') разрешено выполнять dispute, verify и supersedes."""
    project_root, server = test_setup

    # 1. Создаем promoted карточку
    rec_res = await server.call_tool(
        "record_knowledge",
        {
            "title": "Шардирование базы данных",
            "category": "backend",
            "type": "decision",
            "symptom": "Объем таблицы заказов превысил допустимые лимиты узла",
            "root_cause": "Монолитный экземпляр PostgreSQL исчерпал ресурсы диска",
            "solution": "Шардирование по hash(tenant_id) с использованием Citus",
            "rationale": "Позволяет линейно масштабировать чтение и запись",
            "slug": "backend-db-sharding",
            "project_path": str(project_root),
        },
    )
    assert not rec_res.is_error
    card_id = "backend-db-sharding"

    await server.call_tool(
        "update_knowledge_status",
        {
            "id": card_id,
            "action": "promote",
            "by": "human",
            "note": "Утверждено",
            "project_path": str(project_root),
        },
    )

    # 2. dispute человеком с by="human" -> РАЗРЕШЕНО
    res_dispute = await server.call_tool(
        "update_knowledge_status",
        {
            "id": card_id,
            "action": "dispute",
            "by": "human",
            "note": "Человек оспорил карточку после нагрузочных тестов",
            "project_path": str(project_root),
        },
    )
    assert not res_dispute.is_error

    # 3. verify человеком с by="developer" -> РАЗРЕШЕНО
    res_verify = await server.call_tool(
        "update_knowledge_status",
        {
            "id": card_id,
            "action": "verify",
            "by": "developer",
            "note": "Разработчик верифицировал карточку повторно",
            "project_path": str(project_root),
        },
    )
    assert not res_verify.is_error

    # 4. supersedes человеком с source="user_confirmed" -> РАЗРЕШЕНО
    res_supersede = await server.call_tool(
        "record_knowledge",
        {
            "title": "Шардирование через CockroachDB",
            "category": "backend",
            "type": "decision",
            "source": "user_confirmed",
            "supersedes": card_id,
            "symptom": "Citus усложняет обслуживание и бекапы кластера",
            "root_cause": "Необходимость ручного перебалансирования шардов при сбоях",
            "solution": "Переход на CockroachDB с автоматическим Range-реплицированием",
            "rationale": "Автоматическая балансировка и поддержка распределённых транзакций",
            "slug": "backend-cockroach-sharding",
            "project_path": str(project_root),
        },
    )
    assert not res_supersede.is_error


def test_supersedes_race_condition_rollback(tmp_path: Path):
    """Тест на гонку и откат: если между пред-чеком и _apply_supersede карточка стала promoted,
    _apply_supersede выбрасывает UserConfirmedConflictError и удаляет созданный файл новой карточки.
    """
    k_dir = tmp_path / "k"
    c_db = tmp_path / "c.db"
    (k_dir / "backend").mkdir(parents=True)

    storage = KnowledgeStorage(db_path=c_db, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    # 1. Создаем целевую карточку (draft)
    req1 = CardRecordRequest(
        title="Оригинальная карточка",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.DECISION,
        symptom="Симптом исходной проблемы в коде",
        root_cause="Первопричина исходной проблемы в коде более сорока символов",
        solution="Решение исходной проблемы в коде более пятидесяти символов",
        rationale="Обоснование правильности решения более тридцати символов",
        slug="backend-orig-race-test",
    )
    resp1 = service.record(req1)
    orig_id = resp1["id"]

    # 2. Создаем use_case
    use_case = service._record_use_case

    # Фейковый supersede_handler, который симулирует параллельный promote перед _apply_supersede
    def fake_handler_with_race(new_id: str, old_id: str) -> str | None:
        # Прямо перед apply_supersede кто-то делает promote старой карточки!
        service.update_status(
            CardStatusUpdateRequest(
                id="backend-orig-race-test", action="promote", by="human", note="Promoted in race"
            )
        )
        # Теперь вызываем реальный _apply_supersede
        new_file = service._find_card_file(new_id)
        assert new_file is not None
        return use_case._apply_supersede(
            new_id,
            old_id,
            req_source=KnowledgeSource.AUTO_INFERRED,
            new_file_path=new_file,
        )

    req2 = CardRecordRequest(
        title="Новая карточка вытеснения",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.DECISION,
        supersedes=orig_id,
        symptom="Новый симптом проблемы для вытеснения",
        root_cause="Новая первопричина проблемы для вытеснения более 40 символов",
        solution="Новое решение проблемы для вытеснения более пятидесяти символов",
        rationale="Новое обоснование правильности решения более 30 символов",
        slug="backend-new-race-test",
    )

    with pytest.raises(UserConfirmedConflictError):
        use_case.execute(req2, supersede_handler=fake_handler_with_race)

    # Проверяем, что файл новой карточки был удален (rollback)
    new_card_file = service._find_card_file("backend-new-race-test")
    assert new_card_file is None


def test_ks13_service_promote_by_agent_protects_against_auto_dispute(tmp_path: Path):
    """Дефект 1: воспроизведение через KnowledgeService:
    draft -> promote(source='user_confirmed', by='agent') -> автоматический dispute отклоняется.
    """
    k_dir = tmp_path / "k"
    c_db = tmp_path / "c.db"
    (k_dir / "backend").mkdir(parents=True)
    storage = KnowledgeStorage(db_path=c_db, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    # 1. Запись draft карточки (auto_inferred)
    resp = service.record(
        CardRecordRequest(
            title="Кэширование сессий",
            category=KnowledgeCategory.BACKEND,
            type=KnowledgeType.LESSON,
            symptom="Высокая задержка ответа сервиса при пиковых нагрузках",
            root_cause="Каждый запрос выполняет синхронное чтение профиля из СУБД более 40 символов",
            solution="Внедрение двухслойного кэша Redis с TTL 300 секунд более 50 символов",
            rationale="Снижает нагрузку на основную БД более чем в 10 раз более 30 символов",
            slug="backend-cache-service-test",
            source=KnowledgeSource.AUTO_INFERRED,
        )
    )
    card_id = resp["id"]
    card = service.get([card_id]).cards[0]
    assert card.status == KnowledgeStatus.DRAFT

    # 2. Promote с source=user_confirmed, by=agent
    p_resp = service.update_status(
        CardStatusUpdateRequest(
            id=card_id,
            action="promote",
            source=KnowledgeSource.USER_CONFIRMED,
            by="agent",
            note="User consented to promote",
        )
    )
    assert p_resp["status"] == "updated"
    assert p_resp["new_status"] == KnowledgeStatus.ACTIVE.value

    # Проверяем, что карточка защищена
    card_promoted = service.get([card_id]).cards[0]
    assert card_promoted.frontmatter.is_human_protected is True

    # 3. Автоматический dispute ДОЛЖЕН БЫТЬ ОТКЛОНЁН ошибкой ForbiddenUserConfirmedActionError
    with pytest.raises(ForbiddenUserConfirmedActionError):
        service.update_status(
            CardStatusUpdateRequest(
                id=card_id,
                action="dispute",
                source=KnowledgeSource.AUTO_INFERRED,
                by="agent",
                note="Автоматическая попытка оспорить карточку",
            )
        )

    # Карточка осталась active
    assert service.get([card_id]).cards[0].status == KnowledgeStatus.ACTIVE


def test_ks13_service_ordinary_verify_does_not_enable_protection(tmp_path: Path):
    """Дефект 2: воспроизведение через KnowledgeService:
    active карточка процесса -> ordinary verify с note='Promoted to active: checklist'
    -> не создаёт защиту -> автоматический dispute проходит.
    """
    k_dir = tmp_path / "k"
    c_db = tmp_path / "c.db"
    (k_dir / "backend").mkdir(parents=True)
    storage = KnowledgeStorage(db_path=c_db, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    # 1. Запись активной карточки процесса напрямую на диск
    card_id = "backend-verify-service-test"
    card_file = k_dir / "backend" / f"{card_id}.md"
    card_file.write_text(
        """---
id: backend-verify-service-test
title: Кэширование сессий
category: backend
type: lesson
status: active
generated:
  by: process
  agent: null
  created_at: '2026-10-30'
verified: []
stale_after: null
supersedes: null
superseded_by: null
related_to: []
derived_from: null
aliases: []
tags: []
confidence: 0.75
---

### Симптом / Контекст
Высокая задержка ответа сервиса при пиковых нагрузках более 40 символов.

### Первопричина (Root Cause)
Каждый запрос выполняет синхронное чтение профиля из СУБД более сорока символов.

### Решение / Выводы
Внедрение двухслойного кэша Redis с TTL 300 секунд более пятидесяти символов.

### Обоснование / Почему именно так (Rationale)
Снижает нагрузку на основную БД более чем в 10 раз более тридцати символов.
""",
        encoding="utf-8",
    )

    card = service.get([card_id]).cards[0]
    assert card.status == KnowledgeStatus.ACTIVE
    assert card.frontmatter.is_human_protected is False

    # 2. Обычный verify человеком с note='Promoted to active: checklist'
    v_resp = service.update_status(
        CardStatusUpdateRequest(
            id=card_id,
            action="verify",
            by="human",
            note="Promoted to active: checklist",
        )
    )
    assert v_resp["status"] == "updated"
    assert v_resp["new_status"] == KnowledgeStatus.VERIFIED.value

    # Защита НЕ должна включиться
    card_verified = service.get([card_id]).cards[0]
    assert card_verified.frontmatter.is_human_protected is False

    # 3. Автоматический dispute разрешён и переводит карточку в disputed
    d_resp = service.update_status(
        CardStatusUpdateRequest(
            id=card_id,
            action="dispute",
            source=KnowledgeSource.AUTO_INFERRED,
            by="agent",
            note="Автоматический dispute после повторного замера",
        )
    )
    assert d_resp["status"] == "updated"
    assert d_resp["new_status"] == KnowledgeStatus.DISPUTED.value
    assert service.get([card_id]).cards[0].status == KnowledgeStatus.DISPUTED
