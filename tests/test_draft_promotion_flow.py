"""Тесты для Draft Quarantine и Promote-флоу (OKF v0.3 / Phase 4).

Проверяет:
1. Автономные карточки (auto_inferred) сохраняются как draft и изолируются от поиска.
2. Поиск исключает черновики по умолчанию; включает при include_drafts=True или task_id.
3. review_knowledge помечает черновик флагом 'draft_candidate'.
4. Запрет самопродвижения агентом (ForbiddenUserConfirmedActionError).
5. Продвижение человеком (action='promote', by='human'): статус active, source user_confirmed, лог PROMOTED.
6. Человеческие записи (source='user_confirmed') минуют карантин и сразу становятся active.
"""

from pathlib import Path
import pytest

from mcp.types import CallToolResult
from mcp_knowledge.errors import ForbiddenUserConfirmedActionError
from mcp_knowledge.models import (
    CardRecordRequest,
    CardStatusUpdateRequest,
    KnowledgeCategory,
    KnowledgeSource,
    KnowledgeStatus,
    KnowledgeType,
)
from mcp_knowledge.parser import parse_card_markdown
from mcp_knowledge.server import create_server
from mcp_knowledge.service import KnowledgeService
from mcp_knowledge.storage import KnowledgeStorage


@pytest.fixture
def test_env(tmp_path: Path):
    k_dir = tmp_path / "knowledge"
    c_db = tmp_path / "cache.db"
    storage = KnowledgeStorage(db_path=c_db, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)
    server = create_server(service=service)
    return storage, service, server


def test_auto_inferred_card_records_as_draft_quarantine(test_env):
    """1. Автономная запись карточки без человека сохраняется в статусе draft."""
    _, service, _ = test_env

    req = CardRecordRequest(
        title="Ловушка буферизации fastcgi",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.LESSON,
        symptom="Буферизация Nginx блокирует chunked transfer encoding для fastcgi бэкенда",
        root_cause="Директива proxy_buffering включена по умолчанию и накапливает чанки в буферах",
        solution="Установка proxy_buffering off и proxy_cache off в конфигурации локейшна",
        rationale="Обеспечивает немедленную потоковую передачу данных клиенту без задержек",
        slug="fastcgi-buffering-trap",
        task_id="task-debug-stream-1",
    )
    resp = service.record(req)

    assert resp["status"] == "created"
    assert resp["card_status"] == "draft"
    assert "draft status (quarantine)" in resp.get("hint", "")
    assert "Never promote" in resp.get("hint", "")

    file_path = Path(resp["path"])
    assert file_path.exists()
    fm, _ = parse_card_markdown(file_path.read_text(encoding="utf-8"))
    assert fm.status == KnowledgeStatus.DRAFT
    assert fm.generated.by.value == "process"


def test_search_isolation_of_draft_cards(test_env):
    """2. Черновики изолированы от дефолтного поиска, но видны с include_drafts=True и task_id."""
    _, service, _ = test_env

    req = CardRecordRequest(
        title="Ловушка буферизации fastcgi",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.LESSON,
        symptom="Буферизация Nginx блокирует chunked transfer encoding для fastcgi бэкенда",
        root_cause="Директива proxy_buffering включена по умолчанию и накапливает чанки в буферах",
        solution="Установка proxy_buffering off и proxy_cache off в конфигурации локейшна",
        rationale="Обеспечивает немедленную потоковую передачу данных клиенту без задержек",
        slug="fastcgi-buffering-trap",
        task_id="task-debug-stream-1",
    )
    service.record(req)

    # Дефолтный поиск: черновик НЕ виден
    normal_res = service.search(query="fastcgi")
    assert len(normal_res) == 0

    faceted_normal = service.search_faceted(query="fastcgi")
    assert len(faceted_normal.query_results) == 0

    # Поиск с include_drafts=True: черновик найден с бейджем draft
    draft_res = service.search(query="fastcgi", include_drafts=True)
    assert len(draft_res) == 1
    assert draft_res[0].id == "backend-fastcgi-buffering-trap"
    assert draft_res[0].badge == "draft"

    faceted_draft = service.search_faceted(query="fastcgi", include_drafts=True)
    assert len(faceted_draft.query_results) == 1
    assert faceted_draft.query_results[0].badge == "draft"

    # Поиск с task_id: черновик доступен в контексте своей задачи
    task_res = service.search(query="fastcgi", task_id="task-debug-stream-1")
    assert len(task_res) == 1
    assert task_res[0].id == "backend-fastcgi-buffering-trap"


def test_review_flags_draft_candidate(test_env):
    """3. review_knowledge помечает черновик флагом 'draft_candidate'."""
    _, service, _ = test_env

    req = CardRecordRequest(
        title="Ловушка буферизации fastcgi",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.LESSON,
        symptom="Буферизация Nginx блокирует chunked transfer encoding для fastcgi бэкенда",
        root_cause="Директива proxy_buffering включена по умолчанию и накапливает чанки в буферах",
        solution="Установка proxy_buffering off и proxy_cache off в конфигурации локейшна",
        rationale="Обеспечивает немедленную потоковую передачу данных клиенту без задержек",
        slug="fastcgi-buffering-trap",
        task_id="task-debug-stream-1",
    )
    service.record(req)

    review_res = service.review(task_id="task-debug-stream-1")
    assert review_res["candidates_count"] == 1
    candidate = review_res["candidates"][0]
    assert candidate["id"] == "backend-fastcgi-buffering-trap"
    assert "draft_candidate" in candidate["flags"]
    assert "promote" in review_res["verdict_actions"]


def test_agent_cannot_self_promote_draft(test_env):
    """4. Автономный агент не может перевести карточку из draft в active без человека."""
    _, service, _ = test_env

    req = CardRecordRequest(
        title="Ловушка буферизации fastcgi",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.LESSON,
        symptom="Буферизация Nginx блокирует chunked transfer encoding для fastcgi бэкенда",
        root_cause="Директива proxy_buffering включена по умолчанию и накапливает чанки в буферах",
        solution="Установка proxy_buffering off и proxy_cache off в конфигурации локейшна",
        rationale="Обеспечивает немедленную потоковую передачу данных клиенту без задержек",
        slug="fastcgi-buffering-trap",
    )
    service.record(req)

    update_req = CardStatusUpdateRequest(
        id="backend-fastcgi-buffering-trap",
        action="promote",
        note="Агент решил повысить статус сам",
        by="agent",
        source=KnowledgeSource.AUTO_INFERRED,
    )

    with pytest.raises(ForbiddenUserConfirmedActionError) as exc_info:
        service.update_status(update_req)

    assert "требует подтверждения человека" in str(exc_info.value)


def test_human_promotion_workflow(test_env):
    """5. Человек успешно переводит карточку в active, она появляется в поиске."""
    _, service, _ = test_env

    req = CardRecordRequest(
        title="Ловушка буферизации fastcgi",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.LESSON,
        symptom="Буферизация Nginx блокирует chunked transfer encoding для fastcgi бэкенда",
        root_cause="Директива proxy_buffering включена по умолчанию и накапливает чанки в буферах",
        solution="Установка proxy_buffering off и proxy_cache off в конфигурации локейшна",
        rationale="Обеспечивает немедленную потоковую передачу данных клиенту без задержек",
        slug="fastcgi-buffering-trap",
    )
    resp = service.record(req)
    card_id = resp["id"]
    file_path = Path(resp["path"])

    # Продвижение человеком
    update_req = CardStatusUpdateRequest(
        id=card_id,
        action="promote",
        note="Утверждено разработчиком после код-ревью",
        by="human",
        source=KnowledgeSource.USER_CONFIRMED,
    )
    promote_res = service.update_status(update_req)
    assert promote_res["status"] == "updated"
    assert promote_res["new_status"] == "active"
    assert promote_res["id"] == card_id

    # Проверка на диске: статус active, происхождение сохранено, by human, лог PROMOTED
    fm, sections = parse_card_markdown(file_path.read_text(encoding="utf-8"))
    assert fm.status == KnowledgeStatus.ACTIVE
    # Происхождение карточки не подменяется: автор остаётся 'process',
    # согласование фиксируется в verified-записи, confidence не назначается извне.
    assert fm.generated.by.value == "process"
    assert fm.verified and fm.verified[-1].by == "human"
    assert fm.confidence != 1.0

    log_content = "\n".join(sections.values())
    assert "PROMOTED: Утверждено разработчиком после код-ревью" in log_content

    # Проверка в поиске: карточка мгновенно видна в обычном поиске!
    results = service.search(query="fastcgi")
    assert len(results) == 1
    assert results[0].id == card_id
    assert results[0].badge == "ok"


def test_user_confirmed_card_bypasses_draft_quarantine(test_env):
    """6. Карточка, согласованная с человеком (source='user_confirmed'), сразу пишется как active."""
    _, service, _ = test_env

    req = CardRecordRequest(
        title="Стандарт именования сервисов",
        category=KnowledgeCategory.PLATFORM,
        source=KnowledgeSource.USER_CONFIRMED,
        type=KnowledgeType.DECISION,
        symptom="Разнобой префиксов в именовании микросервисов платформы",
        root_cause="Договорённость с тимлидом: единый kebab-case префикс svc- для всех сервисов",
        solution="Обязательный префикс svc- во всех Dockerfile, манифестах K8s и именах репозиториев",
        rationale="Обеспечивает консистентный сервис-дискавери и мониторинг в Prometheus",
        slug="service-naming-standard",
    )
    resp = service.record(req)

    assert resp["status"] == "created"
    assert resp["card_status"] == "active"
    assert "draft" not in resp.get("hint", "")

    file_path = Path(resp["path"])
    fm, _ = parse_card_markdown(file_path.read_text(encoding="utf-8"))
    assert fm.status == KnowledgeStatus.ACTIVE
    assert fm.generated.by.value == "human"

    # Сразу видна в дефолтном боевом поиске
    results = service.search(query="именования")
    assert len(results) == 1
    assert results[0].id == "platform-service-naming-standard"
    assert results[0].badge == "ok"


def test_auto_inferred_cannot_bypass_quarantine_with_explicit_status_active(test_env):
    """7. Автономный агент не может обойти карантин черновиков передачей status='active'."""
    _, service, _ = test_env

    req = CardRecordRequest(
        title="Попытка обхода карантина черновиков",
        category=KnowledgeCategory.BACKEND,
        source=KnowledgeSource.AUTO_INFERRED,
        status=KnowledgeStatus.ACTIVE,  # Попытка обойти карантин!
        type=KnowledgeType.LESSON,
        symptom="Агент пытается установить status=active при автономной записи",
        root_cause="Правило изоляции Draft Quarantine запрещает активный статус без участия человека",
        solution="Принудительный перевод в draft для всех карточек с source=auto_inferred в RecordKnowledgeUseCase",
        rationale="Исключает проникновение непроверенных автономных карточек в боевой поисковый индекс",
        slug="quarantine-bypass-attempt",
    )
    resp = service.record(req)

    # Должен быть принудительно переведён в draft
    assert resp["status"] == "created"
    assert resp["card_status"] == "draft"

    file_path = Path(resp["path"])
    fm, _ = parse_card_markdown(file_path.read_text(encoding="utf-8"))
    assert fm.status == KnowledgeStatus.DRAFT

    # Не должна быть видна в боевом поиске без флага include_drafts
    results = service.search(query="обхода карантина")
    assert len(results) == 0


def test_promote_rejects_non_draft_card(test_env):
    """8. Действие promote применимо только к карточкам со статусом draft."""
    _, service, _ = test_env

    # 1. Создаем карточку от человека (сразу active)
    req = CardRecordRequest(
        title="Активная карточка соглашения",
        category=KnowledgeCategory.BACKEND,
        source=KnowledgeSource.USER_CONFIRMED,
        type=KnowledgeType.DECISION,
        symptom="Существующее архитектурное решение команды по сервисам",
        root_cause="Утвержденный стандарт разработки архитектуры сервисов для всей команды проекта",
        solution="Применение утвержденного стандарта во всех модулях системы без исключений",
        rationale="Обеспечивает консистентность кодовой базы",
        slug="active-card-rule",
    )
    resp = service.record(req)
    card_id = resp["id"]

    # 2. Попытка промоушена уже активной карточки должна отклониться
    with pytest.raises(ValueError, match="применимо только к карточкам в статусе 'draft'"):
        service.update_status(
            CardStatusUpdateRequest(
                id=card_id,
                action="promote",
                by="human",
                source=KnowledgeSource.USER_CONFIRMED,
                note="Повторный промоушен",
            )
        )
