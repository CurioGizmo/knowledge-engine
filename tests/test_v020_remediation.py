"""Тесты для верификации решений плана ремедиации v0.2.0."""

import json
import logging
import shutil
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from mcp.types import CallToolResult, TextContent

from mcp_knowledge.installer import _update_json_config, run_installer
from mcp_knowledge.models import (
    CardFrontmatter,
    CardRecordRequest,
    CardStatusUpdateRequest,
    GeneratedMetadata,
    HypothesesResolveRequest,
    HypothesisRecordRequest,
    HypothesisResolution,
    KnowledgeSource,
    KnowledgeStatus,
    KnowledgeType,
    StorageUnavailableError,
)
from mcp_knowledge.server import create_server
from mcp_knowledge.service import (
    CardNotFoundError,
    ForbiddenUserConfirmedActionError,
    KnowledgeService,
)
from mcp_knowledge.storage import BM25_WEIGHTS, KnowledgeStorage


def extract_data(res: Any) -> Any:
    if hasattr(res, "structured_content") and res.structured_content:
        return res.structured_content.get("result", res.structured_content)
    if hasattr(res, "content") and res.content:
        first_item = res.content[0]
        if isinstance(first_item, TextContent):
            return json.loads(first_item.text)
    return {}


def test_bm25_weights_and_immediate_search(tmp_path: Path):
    """Проверка Stage 2.1: 20 весов BM25 (включая created_at) и поиск сразу после инициализации базы без падений."""
    assert len(BM25_WEIGHTS) == 20
    # Позиционное выравнивание: сдвиг весов при изменении схемы должен ломать тест.
    assert BM25_WEIGHTS[1] == 3.5  # title
    assert BM25_WEIGHTS[7] == 0.0  # created_at (UNINDEXED)
    assert BM25_WEIGHTS[8] == 0.0  # stale_after
    assert BM25_WEIGHTS[12] == 2.5  # aliases
    assert BM25_WEIGHTS[13] == 2.0  # symptom
    assert BM25_WEIGHTS[14] == 1.5  # root_cause
    assert BM25_WEIGHTS[15] == 1.0  # solution
    assert BM25_WEIGHTS[16] == 1.0  # rationale
    assert BM25_WEIGHTS[17] == 0.0  # refutation_reason (UNINDEXED)
    assert BM25_WEIGHTS[18] == 2.0  # tags
    assert BM25_WEIGHTS[19] == 0.15  # body
    db_path = tmp_path / "fts_weights.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    # Поиск на пустой свежеинициализированной базе должен успешно отработать
    res = storage.search("test query", limit=5)
    assert res == []


def test_sqlite_operational_error_in_search_and_dup_checks(tmp_path: Path):
    """Проверка Stage 1.1: Сбои SQLite."""
    db_path = tmp_path / "error_test.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    # 1. Поиск при сбое SQLite рейзит StorageUnavailableError, сервис/сервер возвращает error-dict
    with patch.object(
        storage, "get_connection", side_effect=sqlite3.OperationalError("disk I/O error")
    ):
        with pytest.raises(StorageUnavailableError):
            storage.search("query")

    # 2. Сбой при проверке дубликата НЕ блокирует запись (превращается в warning)
    req = CardRecordRequest(
        title="Тест устойчивости к сбою dup-детектора",
        category="platform",
        type=KnowledgeType.LESSON,
        symptom="Симптом для проверки записи при сбое базы данных",
        root_cause="Первопричина для проверки записи при сбое базы данных более 40 символов",
        solution="Решение проблемы для проверки записи при сбое базы данных более 50 символов",
        rationale="Обоснование правильности решения более 30 символов",
        slug="dup-check-failover",
    )

    with patch.object(
        storage, "get_connection", side_effect=sqlite3.OperationalError("disk locked")
    ):
        rec_res = service.record(req)
        assert rec_res["status"] == "created"
        assert rec_res.get("warning") is not None
        assert "проверка дубликатов пропущена" in rec_res["warning"].lower()

    # 3. Сбой при проверке дубликата гипотезы НЕ блокирует фиксацию гипотезы
    hypo_req = HypothesisRecordRequest(
        title="Тестовая гипотеза сбой dup",
        category="platform",
        symptom="Симптом гипотезы при сбое базы данных",
        hypothesis="Суть рабочей гипотезы при сбое базы данных",
        slug="hypo-dup-failover",
    )
    with patch.object(
        storage, "get_connection", side_effect=sqlite3.OperationalError("disk locked")
    ):
        hypo_res = service.propose_hypothesis(hypo_req)
        assert hypo_res["status"] == "proposed"
        assert hypo_res.get("warning") is not None


def test_two_phase_force_confirmation_flow(tmp_path: Path):
    """Проверка Stage 2.9 (UX-1c): Двухфазный force=True."""
    db_path = tmp_path / "two_phase.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    # 1. Записываем первую карточку
    req1 = CardRecordRequest(
        title="Первая карточка в категории",
        category="backend",
        type=KnowledgeType.LESSON,
        symptom="Специфический сбой таймаута соединения с PostgreSQL",
        root_cause="Превышение пула соединений в asyncpg при высокой нагрузке",
        solution="Увеличение max_size пула и настройка таймаута acquire=10.0",
        rationale="Предотвращает каскадный отказ сервиса при всплесках трафика",
        slug="pg-pool-timeout",
    )
    service.record(req1)

    # 2. Пытаемся записать карточку с похожим симптомом и force=True, но confirmed_force=False
    req2 = CardRecordRequest(
        title="Вторая карточка с похожим симптомом",
        category="backend",
        type=KnowledgeType.LESSON,
        symptom="Специфический сбой таймаута соединения с PostgreSQL",
        root_cause="Другая первопричина сбоя таймаута соединений в базе данных",
        solution="Другое законченное техническое решение для соединений базы",
        rationale="Обоснование правильности другого решения проблемы",
        slug="pg-pool-timeout-2",
        force=True,
        confirmed_force=False,
    )
    res_phase1 = service.record(req2)
    assert res_phase1["status"] == "confirmation_required"
    assert "pg-pool-timeout" in res_phase1["existing_id"]
    assert "supersedes" in res_phase1["message"]
    assert "existing_snippet" in res_phase1
    assert "Специфический сбой таймаута" in res_phase1["existing_snippet"]

    # Карточка еще не создана
    assert not (k_dir / "backend" / "backend-pg-pool-timeout-2.md").exists()

    # 3. Подтверждаем с confirmed_force=True
    req2.confirmed_force = True
    res_phase2 = service.record(req2)
    assert res_phase2["status"] == "created"
    assert (k_dir / "backend" / "backend-pg-pool-timeout-2.md").exists()


def test_two_pass_validation_in_resolve_hypotheses(tmp_path: Path):
    """Проверка Stage 2.3: Двухпроходная валидация resolve_hypotheses (атомарность)."""
    db_path = tmp_path / "two_pass.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    # Создаем 1 валидную гипотезу
    h_req = HypothesisRecordRequest(
        title="Валидная гипотеза",
        category="backend",
        symptom="Симптом первой гипотезы для двухпроходной валидации",
        hypothesis="Суть первой гипотезы для двухпроходной валидации",
        slug="valid-hypo",
    )
    service.propose_hypothesis(h_req)

    # Запрашиваем батч из валидной и заведомо несуществующей гипотезы
    with pytest.raises(CardNotFoundError):
        service.resolve_hypotheses(
            HypothesesResolveRequest(
                resolutions=[
                    HypothesisResolution(
                        id="backend-valid-hypo",
                        action="confirm",
                        solution="Законченное валидное решение проблемы",
                    ),
                    HypothesisResolution(
                        id="backend-non-existent-hypo",
                        action="drop",
                    ),
                ]
            )
        )

    # Первая гипотеза НЕ должна была подтвердиться из-за сбоя на первом проходе!
    card = service.get(["backend-valid-hypo"]).cards[0]
    assert card.status.value == "hypothesis"


def test_resolve_hypotheses_duplicate_ids_rejected(tmp_path: Path):
    """Проверка отклонения дублирующихся ID гипотез в одном батче."""
    db_path = tmp_path / "dup_res.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    h_req = HypothesisRecordRequest(
        title="Гипотеза для проверки дублей батча",
        category="backend",
        symptom="Симптом гипотезы для проверки дублей в батче резолюций",
        hypothesis="Суть гипотезы для проверки дублей в батче резолюций",
        slug="batch-dup-hypo",
    )
    service.propose_hypothesis(h_req)

    with pytest.raises(ValueError, match="Дублирующийся ID"):
        service.resolve_hypotheses(
            HypothesesResolveRequest(
                resolutions=[
                    HypothesisResolution(
                        id="backend-batch-dup-hypo",
                        action="confirm",
                        solution="Первое подтверждение гипотезы",
                    ),
                    HypothesisResolution(
                        id="backend-batch-dup-hypo",
                        action="drop",
                    ),
                ]
            )
        )


def test_resolve_hypotheses_pass2_partial_failure_syncs_and_returns_details(tmp_path: Path):
    """Проверка Step 5 / AUDIT-FIX-P0: при сбое на k-й карточке в Pass 2 выполняется storage.sync() и возвращается partial_failure."""
    db_path = tmp_path / "partial_fail.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    # 1. Создаем две разные гипотезы
    h1 = HypothesisRecordRequest(
        title="Утечка памяти в кэше LRU",
        category="backend",
        symptom="Потребление RAM растет при частых обновлениях ключей кэша",
        hypothesis="Кэш не освобождает циклические ссылки при вытеснении",
        slug="cache-leak-hypo",
    )
    service.propose_hypothesis(h1)

    h2 = HypothesisRecordRequest(
        title="Таймаут сокетов TCP при реконнекте",
        category="backend",
        symptom="Соединение клиента зависает на 60 секунд при сбросе сети",
        hypothesis="Отсутствует SO_KEEPALIVE и TCP_USER_TIMEOUT на сокете",
        slug="socket-timeout-hypo",
    )
    service.propose_hypothesis(h2)

    assert len(service.get(["backend-cache-leak-hypo"]).cards) == 1
    assert len(service.get(["backend-socket-timeout-hypo"]).cards) == 1

    orig_find = service._resolve_use_case.repo.find_card_file
    pass1_done = False

    def hooked_find(card_id: str):
        if pass1_done and card_id == "backend-socket-timeout-hypo":
            return None
        return orig_find(card_id)

    sync_called = False
    orig_sync = storage.sync

    def hooked_sync():
        nonlocal sync_called
        sync_called = True
        return orig_sync()

    orig_validate = service._resolve_use_case._validate_resolution_item

    def hooked_validate(res):
        ret = orig_validate(res)
        if res.id == "backend-socket-timeout-hypo":
            nonlocal pass1_done
            pass1_done = True
        return ret

    with (
        patch.object(service._resolve_use_case.repo, "find_card_file", side_effect=hooked_find),
        patch.object(storage, "sync", side_effect=hooked_sync),
        patch.object(
            service._resolve_use_case, "_validate_resolution_item", side_effect=hooked_validate
        ),
    ):
        resp = service.resolve_hypotheses(
            HypothesesResolveRequest(
                task_id="TASK-PARTIAL",
                resolutions=[
                    HypothesisResolution(
                        id="backend-cache-leak-hypo",
                        action="drop",
                    ),
                    HypothesisResolution(
                        id="backend-socket-timeout-hypo",
                        action="drop",
                    ),
                ],
            )
        )

    assert resp["status"] == "partial_failure"
    assert resp["task_id"] == "TASK-PARTIAL"
    assert resp["resolved_count"] == 1
    assert len(resp["details"]) == 1
    assert resp["details"][0]["id"] == "backend-cache-leak-hypo"
    assert "удалена между проходами" in resp["error"]
    assert sync_called is True

    # Первая карточка удалена
    assert len(service.get(["backend-cache-leak-hypo"]).cards) == 0
    # Вторая осталась нетронутой
    assert len(service.get(["backend-socket-timeout-hypo"]).cards) == 1


def test_resolve_hypotheses_pass2_first_card_failure_raises_and_syncs(tmp_path: Path):
    """Проверка Step 5 / AUDIT-FIX-P0: при сбое на первой карточке в Pass 2 исключение пробрасывается, но sync() вызывается."""
    db_path = tmp_path / "first_fail.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    h1 = HypothesisRecordRequest(
        title="Зависание пула соединений",
        category="backend",
        symptom="Пул соединений блокирует воркеры при исчерпании лимита",
        hypothesis="Счетчик активных соединений не декрементируется при ошибке",
        slug="pool-hang-hypo",
    )
    service.propose_hypothesis(h1)

    sync_called = False
    orig_sync = storage.sync

    def hooked_sync():
        nonlocal sync_called
        sync_called = True
        return orig_sync()

    orig_find = service._resolve_use_case.repo.find_card_file
    pass1_done = False

    def hooked_find(card_id: str):
        if pass1_done:
            raise RuntimeError("Disk read error in Pass 2")
        return orig_find(card_id)

    orig_validate = service._resolve_use_case._validate_resolution_item

    def hooked_validate(res):
        ret = orig_validate(res)
        nonlocal pass1_done
        pass1_done = True
        return ret

    with (
        patch.object(service._resolve_use_case.repo, "find_card_file", side_effect=hooked_find),
        patch.object(
            service._resolve_use_case, "_validate_resolution_item", side_effect=hooked_validate
        ),
        patch.object(storage, "sync", side_effect=hooked_sync),
    ):
        with pytest.raises(RuntimeError, match="Disk read error in Pass 2"):
            service.resolve_hypotheses(
                HypothesesResolveRequest(
                    task_id="TASK-FAIL-1",
                    resolutions=[
                        HypothesisResolution(
                            id="backend-pool-hang-hypo",
                            action="drop",
                        ),
                    ],
                )
            )

    assert sync_called is True


def test_refute_merge_into_user_confirmed_guardrail(tmp_path: Path):
    """Проверка защиты карточек user_confirmed от несанкционированного merge_into при refute."""
    db_path = tmp_path / "user_conf.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    # Создаем карточку от пользователя
    user_card = CardRecordRequest(
        title="Подтвержденное решение пользователя",
        category="backend",
        type=KnowledgeType.DECISION,
        source=KnowledgeSource.USER_CONFIRMED,
        symptom="Критический симптом подтвержденный человеком",
        root_cause="Первопричина подтвержденная человеком более 40 символов",
        solution="Законченное техническое решение подтвержденное человеком более 50 символов",
        rationale="Обоснование правильности решения более 30 символов",
        slug="user-decision-card",
    )
    service.record(user_card)

    # Создаем гипотезу
    h_req = HypothesisRecordRequest(
        title="Опровергаемая гипотеза",
        category="backend",
        symptom="Симптом опровергаемой гипотезы для проверки слияния",
        hypothesis="Суть опровергаемой гипотезы для проверки слияния",
        slug="refuted-hypo",
    )
    service.propose_hypothesis(h_req)

    # Пытаемся слить опровержение в user_confirmed карточку -> должно быть заблокировано
    with pytest.raises(ForbiddenUserConfirmedActionError):
        service.resolve_hypotheses(
            HypothesesResolveRequest(
                resolutions=[
                    HypothesisResolution(
                        id="backend-refuted-hypo",
                        action="refute",
                        refutation_reason="Причина опровержения длиннее 15 символов",
                        merge_into="backend-user-decision-card",
                    )
                ]
            )
        )


def test_apply_supersede_failure_does_not_rollback(tmp_path: Path):
    """Проверка Решения B: Сбой обновления старой версии не откатывает создание новой."""
    db_path = tmp_path / "supersede_fail.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    # 1. Создаем старую карточку
    old_card = CardRecordRequest(
        title="Старая версия решения",
        category="backend",
        type=KnowledgeType.LESSON,
        symptom="Симптом старой версии карточки",
        root_cause="Первопричина старой версии карточки более 40 символов",
        solution="Законченное техническое решение старой версии карточки более 50 символов",
        rationale="Обоснование старой версии более 30 символов",
        slug="old-version",
    )
    service.record(old_card)

    # 2. Мокаем _apply_supersede, имитируя сбой записи на диск для старого файла
    with patch.object(
        service, "_apply_supersede", return_value="Сбой записи на диск старой версии"
    ):
        new_card = CardRecordRequest(
            title="Новая версия решения",
            category="backend",
            type=KnowledgeType.LESSON,
            symptom="Симптом старой версии карточки",
            root_cause="Новая первопричина карточки более 40 символов",
            solution="Законченное техническое решение новой версии карточки более 50 символов",
            rationale="Новое обоснование более 30 символов",
            slug="new-version",
            supersedes="backend-old-version",
        )
        res = service.record(new_card)
        assert res["status"] == "created"
        assert res["id"] == "backend-new-version"
        assert "warning" in res
        assert "Сбой записи на диск старой версии" in res["warning"]
        assert (k_dir / "backend" / "backend-new-version.md").exists()


def test_check_active_card_consistency(tmp_path: Path):
    """Проверка консистенси-чеккера активных карточек."""
    db_path = tmp_path / "consistency.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    req1 = CardRecordRequest(
        title="Консистентность тест 1",
        category="backend",
        source=KnowledgeSource.USER_CONFIRMED,
        type=KnowledgeType.LESSON,
        symptom="Тестовый симптом консистентности для проверки базы",
        root_cause="Первопричина консистентности номер один более 40 символов",
        solution="Решение проблемы консистентности номер один более 50 символов",
        rationale="Обоснование консистентности номер один более 30 символов",
        slug="consist-1",
    )
    service.record(req1)

    req2 = CardRecordRequest(
        title="Консистентность тест 2",
        category="backend",
        source=KnowledgeSource.USER_CONFIRMED,
        type=KnowledgeType.LESSON,
        symptom="Тестовый симптом консистентности для проверки базы",
        root_cause="Первопричина консистентности номер два более 40 символов",
        solution="Решение проблемы консистентности номер два более 50 символов",
        rationale="Обоснование консистентности номер два более 30 символов",
        slug="consist-2",
        force=True,
        confirmed_force=True,
    )
    service.record(req2)

    # Проверяем консистентность
    conflicts = storage.check_active_card_consistency(category="backend")
    assert len(conflicts) >= 1
    pair_ids = {conflicts[0][0], conflicts[0][1]}
    assert "backend-consist-1" in pair_ids
    assert "backend-consist-2" in pair_ids


def test_tool_calls_telemetry(tmp_path: Path):
    """Проверка записи Adoption Telemetry в таблицу tool_calls."""
    db_path = tmp_path / "telemetry.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    storage.record_tool_call(
        tool_name="search_knowledge",
        args={"query": "test"},
        latency_ms=12.5,
    )

    with storage.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT tool_name, args, latency_ms FROM tool_calls;")
        rows = cursor.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "search_knowledge"
        assert "test" in rows[0][1]
        assert rows[0][2] == 12.5


def test_badge_and_superseded_info_in_get_knowledge(tmp_path: Path):
    """Проверка бейджей и superseded_info в ответе get_knowledge."""
    db_path = tmp_path / "badges.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    old_card = CardRecordRequest(
        title="Старая карточка",
        category="backend",
        source=KnowledgeSource.USER_CONFIRMED,
        type=KnowledgeType.LESSON,
        symptom="Симптом старой карточки для проверки бейджей",
        root_cause="Первопричина старой карточки для проверки бейджей более 40 символов",
        solution="Решение старой карточки для проверки бейджей более 50 символов",
        rationale="Обоснование старой карточки для проверки бейджей более 30 символов",
        slug="badge-old",
    )
    service.record(old_card)

    new_card = CardRecordRequest(
        title="Новая карточка",
        category="backend",
        source=KnowledgeSource.USER_CONFIRMED,
        type=KnowledgeType.LESSON,
        symptom="Симптом новой карточки для проверки бейджей",
        root_cause="Первопричина новой карточки для проверки бейджей более 40 символов",
        solution="Решение новой карточки для проверки бейджей более 50 символов",
        rationale="Обоснование новой карточки для проверки бейджей более 30 символов",
        slug="badge-new",
        supersedes="backend-badge-old",
    )
    service.record(new_card)

    resp = service.get(["backend-badge-new", "backend-badge-old"])
    assert len(resp.cards) == 2

    new_detail = next(c for c in resp.cards if c.id == "backend-badge-new")
    old_detail = next(c for c in resp.cards if c.id == "backend-badge-old")

    assert new_detail.badge == "ok"
    assert new_detail.supersedes == "backend-badge-old"
    assert "Заменила карточку: backend-badge-old" in (new_detail.superseded_info or "")
    assert new_detail.id not in new_detail.see_also

    assert old_detail.badge == "superseded"
    assert old_detail.superseded_by == "backend-badge-new"
    assert "Вытеснена карточкой" in (old_detail.superseded_info or "")

    # Проверка R-1: числовой confidence полностью исключен из сериализованного ответа get_knowledge
    get_dump = resp.model_dump(mode="json")
    for card in get_dump["cards"]:
        assert "confidence" not in card["frontmatter"], (
            "confidence не должен присутствовать в frontmatter"
        )
        assert "badge" in card

    # Проверка R-1: числовой confidence полностью исключен из сериализованного ответа search_knowledge
    search_res = storage.search_faceted(categories=["backend"])
    search_dump = search_res.model_dump(mode="json")
    for section in ("query_results", "results", "antipatterns", "hypotheses"):
        for snippet in search_dump.get(section, []):
            assert "confidence" not in snippet, f"confidence не должен присутствовать в {section}"
            assert "badge" in snippet
    for _cat, snippets in search_dump.get("category_results", {}).items():
        for snippet in snippets:
            assert "confidence" not in snippet, (
                "confidence не должен присутствовать в category_results"
            )
            assert "badge" in snippet


def test_ux1b_suspect_superseded_hint(tmp_path: Path):
    """Проверка UX-1b: Авто-подсказка вытесненной цепочки при suspect."""
    db_path = tmp_path / "ux1b.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    old_card = CardRecordRequest(
        title="Первоначальная карточка",
        category="backend",
        type=KnowledgeType.LESSON,
        symptom="Симптом первоначальной карточки для проверки hint",
        root_cause="Первопричина первоначальной карточки для проверки hint более 40 символов",
        solution="Решение первоначальной карточки для проверки hint более 50 символов",
        rationale="Обоснование первоначальной карточки для проверки hint более 30 символов",
        slug="ux1b-old",
    )
    service.record(old_card)

    new_card = CardRecordRequest(
        title="Замещающая карточка",
        category="backend",
        type=KnowledgeType.LESSON,
        symptom="Симптом замещающей карточки для проверки hint",
        root_cause="Первопричина замещающей карточки для проверки hint более 40 символов",
        solution="Решение замещающей карточки для проверки hint более 50 символов",
        rationale="Обоснование замещающей карточки для проверки hint более 30 символов",
        slug="ux1b-new",
        supersedes="backend-ux1b-old",
        # K2: карточка auto_inferred создаётся в draft — suspect недоступен
        # до promote; тестируем hint на active-карточке
        source=KnowledgeSource.USER_CONFIRMED,
    )
    service.record(new_card)

    update_res = service.update_status(
        CardStatusUpdateRequest(
            id="backend-ux1b-new",
            action="suspect",
            note="Решение перестало работать на новой версии",
        )
    )
    assert update_res["status"] == "updated"
    assert "hint" in update_res
    assert "backend-ux1b-old" in update_res["hint"]
    assert "include_archived=True" in update_res["hint"]


def test_installer_backups_and_no_antigravity_flag(tmp_path: Path):
    """Проверка R-2: Без --force пользовательский файл защищен от перезаписи; с --force создается .bak."""
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir(parents=True)

    # Создаем существующий пользовательский скилл
    skill_file = proj_dir / ".agents" / "skills" / "knowledge-memory" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text("Custom user skill content", encoding="utf-8")

    # 1. Запуск инсталлера без --force: файл НЕ должен быть перезаписан, .bak не создается
    run_installer(["--skill", "--no-antigravity", "--project-root", str(proj_dir)])
    assert skill_file.read_text(encoding="utf-8") == "Custom user skill content"
    bak_file = proj_dir / ".agents" / "skills" / "knowledge-memory" / "SKILL.md.bak"
    assert not bak_file.exists()

    # 2. Запуск инсталлера с --force: создается .bak со старым контентом, файл обновляется шаблоном
    run_installer(["--skill", "--force", "--no-antigravity", "--project-root", str(proj_dir)])
    assert bak_file.exists()
    assert bak_file.read_text(encoding="utf-8") == "Custom user skill content"
    assert "Knowledge Engine Lifecycle" in skill_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_search_faceted_operational_error_raises_storage_unavailable(tmp_path: Path):
    """Проверка R-3: Сбои SQLite в фасетных коллекторах вызывают StorageUnavailableError и единый error envelope."""
    db_path = tmp_path / "r3_facet_fail.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)
    server = create_server(service=service, knowledge_dir=k_dir, cache_db=db_path)

    # 1. Сбой в _collect_category_results не маскируется пустой базой
    with patch.object(
        storage,
        "_collect_category_results",
        side_effect=StorageUnavailableError("DB failure in category facets"),
    ):
        res = await server.call_tool("search_knowledge", {"categories": ["backend"]})
        data = extract_data(res)
        assert data.get("error") is True
        assert data.get("status") == "rejected"
        assert data.get("reason") == "STORAGE_UNAVAILABLE"
        assert "StorageUnavailableError" in data.get("error_type", "")

    # 1b. Сбой в _collect_antipatterns строго рейзит StorageUnavailableError
    with patch.object(
        storage,
        "_collect_antipatterns",
        side_effect=StorageUnavailableError("DB failure in antipatterns"),
    ):
        res = await server.call_tool(
            "search_knowledge", {"categories": ["backend"], "include_antipatterns": True}
        )
        data = extract_data(res)
        assert data.get("error") is True
        assert data.get("status") == "rejected"
        assert data.get("reason") == "STORAGE_UNAVAILABLE"

    # 1c. Сбой в _collect_hypotheses строго рейзит StorageUnavailableError
    with patch.object(
        storage,
        "_collect_hypotheses",
        side_effect=StorageUnavailableError("DB failure in hypotheses"),
    ):
        res = await server.call_tool(
            "search_knowledge", {"categories": ["backend"], "include_hypotheses": True}
        )
        data = extract_data(res)
        assert data.get("error") is True
        assert data.get("status") == "rejected"
        assert data.get("reason") == "STORAGE_UNAVAILABLE"

    # 2. get_knowledge с ошибкой хранилища возвращает унифицированный error envelope
    with patch.object(service, "get", side_effect=StorageUnavailableError("DB failure in get")):
        res = await server.call_tool("get_knowledge", {"ids": ["backend-card"]})
        data = extract_data(res)
        assert data.get("error") is True
        assert data.get("status") == "rejected"
        assert data.get("reason") == "STORAGE_UNAVAILABLE"

    # 3. get_help с ошибкой не падает и возвращает структурированный is_error=True
    #    envelope (план harness-stability-fixes, шаг 1: вместо текстового fallback
    #    со статусом успеха клиент получает единый доменный error-envelope)
    with patch("mcp_knowledge.server.get_help_text", side_effect=RuntimeError("help render error")):
        res = await server.call_tool("get_help", {"tool_name": "search"})
        assert isinstance(res, CallToolResult)
        assert res.is_error is True
        data = extract_data(res)
        assert data.get("error") is True
        assert data.get("status") == "rejected"
        assert data.get("error_type") == "RuntimeError"
        assert "help render error" in data.get("message", "")


@pytest.mark.asyncio
async def test_r4_r5_phase_rules_removed_and_results_deprecated(tmp_path: Path):
    """R-4 & R-5: phase_rules удалено из схемы и ответов; results помечен deprecated."""
    db_path = tmp_path / "r4_r5.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)
    server = create_server(service=service, knowledge_dir=k_dir, cache_db=db_path)

    # Записываем тестовую карточку
    service.record(
        CardRecordRequest(
            title="Тестовая карточка FTS",
            category="backend",
            type=KnowledgeType.LESSON,
            symptom="Симптом ошибки FTS при тестировании",
            root_cause="Причина ошибки FTS для проверки выдачи результатов",
            solution="Корректное воспроизводимое решение для проверки FTS поиска инженерных знаний",
            rationale="Обоснование корректности решения для FTS поиска",
            slug="backend-test-fts",
        )
    )

    res = await server.call_tool("search_knowledge", {"query": "FTS"})
    data = extract_data(res)
    assert isinstance(data, dict)

    # R-4: phase_rules не должно присутствовать в ответе
    assert "phase_rules" not in data, "phase_rules должно быть полностью удалено"

    # R-5: results присутствует и равен query_results
    assert "results" in data
    assert "query_results" in data
    assert data["results"] == data["query_results"]

    # Проверяем описание поля в модели
    from mcp_knowledge.models import FacetedSearchResponse

    assert "DEPRECATED" in (FacetedSearchResponse.model_fields["results"].description or "")


@pytest.mark.asyncio
async def test_r6_include_overview_strict_boolean_and_auto(tmp_path: Path):
    """R-6: include_overview имеет строгий тип bool и auto-семантику."""
    db_path = tmp_path / "r6_overview.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)
    server = create_server(service=service, knowledge_dir=k_dir, cache_db=db_path)

    # 1. Zero-query: overview автоматически включается
    res_zero = await server.call_tool("search_knowledge", {})
    data_zero = extract_data(res_zero)
    assert data_zero.get("overview") is not None

    # 2. Query без include_overview: overview не возвращается
    res_q = await server.call_tool("search_knowledge", {"query": "something"})
    data_q = extract_data(res_q)
    assert data_q.get("overview") is None

    # 3. Query с include_overview=True: overview возвращается
    res_qr = await server.call_tool(
        "search_knowledge", {"query": "something", "include_overview": True}
    )
    data_qr = extract_data(res_qr)
    assert data_qr.get("overview") is not None


def test_r7_hypothesis_resolution_solution_required():
    """R-7: solution обязательно при confirm (>= 10 символов) и задокументировано в описании."""
    from pydantic import ValidationError
    from mcp_knowledge.models import HypothesisResolution

    # Без solution падает с ошибкой валидации
    with pytest.raises(ValidationError):
        HypothesisResolution(id="hyp-1", action="confirm")

    # С коротким solution падает
    with pytest.raises(ValidationError):
        HypothesisResolution(id="hyp-1", action="confirm", solution="short")

    # С валидным solution создается корректно
    valid = HypothesisResolution(
        id="hyp-1", action="confirm", solution="Достаточно длинное решение для подтверждения"
    )
    assert valid.solution is not None

    # Проверяем docstring/description
    assert "обязательно при confirm" in HypothesisResolution.model_fields["solution"].description


def test_r8_telemetry_stats_and_retention(tmp_path: Path):
    """R-8: Retention и чтение статистики телеметрии (get_tool_call_stats)."""
    db_path = tmp_path / "r8_stats.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    # Записываем вызовы
    storage.record_tool_call("search_knowledge", {"query": "test"}, latency_ms=12.5)
    storage.record_tool_call("search_knowledge", {"query": "test2"}, latency_ms=18.0)
    storage.record_tool_call("get_knowledge", {"ids": ["id-1"]}, latency_ms=5.0, error="NOT_FOUND")

    stats = storage.get_tool_call_stats()
    assert len(stats) >= 2

    search_stat = next(s for s in stats if s["tool_name"] == "search_knowledge")
    assert search_stat["total_calls"] == 2
    assert search_stat["error_calls"] == 0
    assert search_stat["avg_latency_ms"] == 15.25

    get_stat = next(s for s in stats if s["tool_name"] == "get_knowledge")
    assert get_stat["total_calls"] == 1
    assert get_stat["error_calls"] == 1

    # Проверяем печать таблицы через модуль stats
    from mcp_knowledge.stats import print_stats_table

    print_stats_table(stats)

    # Проверяем sync с retention
    storage.sync()


def test_r9_storage_unavailable_error_sanitization(tmp_path: Path):
    """R-9: Санитизация текста исключений StorageUnavailableError."""
    db_path = tmp_path / "r9_sanitization.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    with patch.object(
        storage,
        "get_connection",
        side_effect=sqlite3.OperationalError("disk I/O error on /secret/path/db.sqlite"),
    ):
        with pytest.raises(StorageUnavailableError) as exc_info:
            storage.search("test")
        err_msg = str(exc_info.value)
        assert "OperationalError" in err_msg
        assert "/secret/path" not in err_msg


def test_r10_card_record_request_no_expires_at():
    """R-10: expires_at удален из CardRecordRequest и CardFrontmatter."""
    from pydantic import ValidationError

    # CardRecordRequest не принимает expires_at как валидное поле без stale_after
    assert "expires_at" not in CardRecordRequest.model_fields
    assert "stale_after" in CardRecordRequest.model_fields


def test_instructions_backup_not_overwritten(tmp_path: Path):
    """R-2: instructions.md.bak не перезаписывается при повторном запуске инсталлера."""
    from mcp_knowledge.installer import install_antigravity

    mock_home = tmp_path / "user_home"
    inst_dir = mock_home / ".gemini" / "antigravity" / "mcp" / "knowledge-engine"
    inst_dir.mkdir(parents=True, exist_ok=True)
    inst_file = inst_dir / "instructions.md"
    inst_bak = inst_dir / "instructions.md.bak"

    # Создаем исходный .bak с пользовательским содержимым
    inst_bak.write_text("Original pristine backup content", encoding="utf-8")
    inst_file.write_text("Current instruction content", encoding="utf-8")

    install_antigravity(home_dir=mock_home)

    # Проверяем, что исходный .bak не был затерт
    assert inst_bak.read_text(encoding="utf-8") == "Original pristine backup content"


def test_r11_disputed_badge():
    """R-11: Карточка в статусе DISPUTED возвращает бейдж 'disputed', а не 'ok'."""
    from mcp_knowledge.models import compute_card_badge

    assert compute_card_badge(KnowledgeStatus.DISPUTED) == "disputed"
    assert compute_card_badge("disputed") == "disputed"
    assert compute_card_badge("DISPUTED") == "disputed"


@pytest.mark.asyncio
async def test_r12_results_deprecation_in_docstring(tmp_path: Path):
    """R-12: Пометка DEPRECATED для results включена в docstring search_knowledge (MCP-схему)."""
    db_path = tmp_path / "r12_docstring.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)
    server = create_server(service=service, knowledge_dir=k_dir, cache_db=db_path)

    # Ищем зарегистрированный инструмент search_knowledge
    tools = await server.list_tools()
    search_tool = next(t for t in tools if t.name == "search_knowledge")
    assert search_tool.description is not None
    assert "DEPRECATED" in search_tool.description
    assert "query_results" in search_tool.description


def test_r8_telemetry_pruning_in_storage(tmp_path: Path):
    """R-8: Pruning телеметрии работает корректно и сокращает таблицу до max_records."""
    db_path = tmp_path / "r8_pruning.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    # Добавляем 20 записей
    for i in range(20):
        storage.record_tool_call("test_tool", {"i": i}, latency_ms=1.0)

    stats = storage.get_tool_call_stats()
    assert stats[0]["total_calls"] == 20

    # Pruning до 5 записей
    deleted = storage.prune_tool_calls(max_records=5)
    assert deleted == 15

    stats_after = storage.get_tool_call_stats()
    assert stats_after[0]["total_calls"] == 5


def test_r8_get_tool_call_stats_raises_on_failure(tmp_path: Path):
    """R-8: Сбой чтения телеметрии выбрасывает StorageUnavailableError, а не возвращает [] (отличие сбоя от пусто)."""
    db_path = tmp_path / "r8_fail.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    with patch.object(storage, "get_connection", side_effect=sqlite3.OperationalError("db locked")):
        with pytest.raises(StorageUnavailableError):
            storage.get_tool_call_stats()


def test_r8_stats_db_resolution(tmp_path: Path, monkeypatch):
    """R-8: stats.py находит базу данных проекта по умолчанию (.agents/.cache/knowledge_fts.db)."""
    from mcp_knowledge.stats import resolve_db_path

    # Создаем фиктивный проект с базой
    proj_dir = tmp_path / "my_project"
    cache_dir = proj_dir / ".agents" / ".cache"
    cache_dir.mkdir(parents=True)
    db_file = cache_dir / "knowledge_fts.db"
    db_file.touch()

    # Мокаем _find_default_project_root
    with patch("mcp_knowledge.stats._find_default_project_root", return_value=proj_dir):
        resolved = resolve_db_path(None)
        assert resolved == db_file


def test_hypothesis_sentinel_legacy_confidence_migration(tmp_path: Path):
    """Сентинел 0.75 -> 0.20: Легаси-гипотеза с confidence: 0.75 при индексации получает вес 0.20."""
    db_path = tmp_path / "sentinel.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    fm = CardFrontmatter(
        id="platform-legacy-hypo",
        title="Legacy Hypothesis",
        category="platform",
        type=KnowledgeType.RESEARCH,
        status=KnowledgeStatus.HYPOTHESIS,
        confidence=0.75,
        generated=GeneratedMetadata(created_at="2026-09-01"),
    )
    conf = storage._calculate_card_confidence(fm)
    assert conf == 0.20


def test_storage_indexes_id_mismatch_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """4.3: При frontmatter.id != md_file.stem в лог выдается warning."""
    db_path = tmp_path / "mismatch.db"
    k_dir = tmp_path / "knowledge" / "platform"
    k_dir.mkdir(parents=True, exist_ok=True)
    card_file = k_dir / "actual-stem.md"
    card_file.write_text(
        """---
id: wrong-id
title: Mismatched ID Card
category: platform
type: lesson
status: active
generated:
  created_at: '2026-09-29'
---

## Symptom
Test symptom for ID mismatch

## Root Cause
Test root cause for ID mismatch

## Solution
Test solution for ID mismatch

## Rationale
Test rationale for ID mismatch
""",
        encoding="utf-8",
    )

    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=tmp_path / "knowledge")
    with caplog.at_level(logging.WARNING):
        storage.sync()

    assert any(
        "frontmatter.id='wrong-id' != filename='actual-stem'" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_server_tool_telemetry_failure_does_not_break_tool(tmp_path: Path):
    """Сбой записи телеметрии в server.py не ломает вызов инструмента."""
    db_path = tmp_path / "telemetry_fail.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)
    server = create_server(service=service, knowledge_dir=k_dir, cache_db=db_path)

    with patch.object(
        storage,
        "record_tool_call",
        side_effect=RuntimeError("telemetry failure"),
    ):
        # Вызов инструмента должен успешно завершиться несмотря на сбой телеметрии
        help_res = await server.call_tool("get_help", {"tool_name": "overview"})
        data = extract_data(help_res)
        assert "MCP Knowledge Engine" in str(data)


def test_update_json_config_invalid_json_creates_backup_and_resets(tmp_path: Path):
    """Невалидный JSON конфига: создается .bak и конфиг безопасно сбрасывается для записи нового сервера."""
    cfg_file = tmp_path / "mcp_config.json"
    corrupted_content = "{ invalid: json content [[["
    cfg_file.write_text(corrupted_content, encoding="utf-8")

    res = _update_json_config(cfg_file, "new_server", {"command": "echo"})
    assert res is True

    # Проверяем бэкап
    bak_file = tmp_path / "mcp_config.json.bak"
    assert bak_file.exists()
    assert bak_file.read_text(encoding="utf-8") == corrupted_content

    # Проверяем, что исходный файл валиден и содержит новый сервер
    data = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert "mcpServers" in data
    assert "new_server" in data["mcpServers"]
    assert data["mcpServers"]["new_server"]["command"] == "echo"


def test_r13_stats_reader_is_readonly(tmp_path: Path):
    """R-13: read-only reader не создаёт каталоги и читает телеметрию без мутации БД."""
    from mcp_knowledge.storage import read_tool_call_stats

    db_path = tmp_path / "r13_live.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    storage.record_tool_call("search_knowledge", {"query": "x"}, latency_ms=7.5)
    storage.record_tool_call("get_knowledge", {"ids": ["a"]}, latency_ms=2.0, error="NOT_FOUND")

    # Удаляем каталог knowledge, чтобы доказать отсутствие side-effect mkdir у ридера
    shutil.rmtree(k_dir)

    stats = read_tool_call_stats(db_path)
    search_stat = next(s for s in stats if s["tool_name"] == "search_knowledge")
    assert search_stat["total_calls"] == 1
    assert search_stat["avg_latency_ms"] == 7.5

    # Reader не пересоздаёт каталог knowledge (в отличие от конструктора KnowledgeStorage)
    assert not k_dir.exists()


def test_r13_readonly_reader_does_not_drop_foreign_schema(tmp_path: Path):
    """R-13: ридер на БД чужой схемы не уничтожает таблицы (нет DROP TABLE) и падает безопасно."""
    from mcp_knowledge.storage import read_tool_call_stats

    db_path = tmp_path / "foreign.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE precious (id INTEGER PRIMARY KEY, val TEXT)")
    conn.execute("INSERT INTO precious (val) VALUES ('keep me')")
    conn.commit()
    conn.close()

    with pytest.raises(StorageUnavailableError):
        read_tool_call_stats(db_path)

    # Чужая таблица пережила чтение; каталог knowledge не создан
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT val FROM precious").fetchall()
    conn.close()
    assert rows == [("keep me",)]
    assert not (tmp_path / "knowledge").exists()


def test_r8_stats_cli_exit_codes(tmp_path: Path, monkeypatch):
    """R-8: CLI-резолвер различает сбой (exit 1) от валидного пути, а не молчит."""
    from mcp_knowledge import stats as stats_mod

    # Явный --db на несуществующий путь -> exit 1
    with pytest.raises(SystemExit) as exc:
        stats_mod.resolve_db_path(tmp_path / "missing.db")
    assert exc.value.code == 1

    # Автопоиск без единой существующей базы -> exit 1
    empty = tmp_path / "empty_root"
    empty.mkdir()
    monkeypatch.setattr(stats_mod, "_find_default_project_root", lambda: empty)
    monkeypatch.setattr(stats_mod.Path, "cwd", classmethod(lambda cls: empty))
    monkeypatch.setattr(stats_mod.Path, "home", classmethod(lambda cls: empty))

    with pytest.raises(SystemExit) as exc2:
        stats_mod.resolve_db_path(None)
    assert exc2.value.code == 1


def test_r11_disputed_badge_integration(tmp_path: Path):
    """R-11 (интеграция): disputed-карточка в search_faceted(include_archived=True) несёт badge 'disputed'."""
    db_path = tmp_path / "r11_integration.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    service.record(
        CardRecordRequest(
            title="Спорная карточка zzdisputed",
            category="backend",
            type=KnowledgeType.LESSON,
            symptom="Симптом спорной карточки zzdisputed для интеграционной проверки бейджа",
            root_cause="Первопричина спорной карточки zzdisputed длиной более 40 символов",
            solution="Решение спорной карточки zzdisputed длиной более 50 символов для проверки",
            rationale="Обоснование спорной карточки zzdisputed длиной более 30 символов",
            slug="disputed-card",
            # K2: карточка auto_inferred создаётся в draft — suspect/dispute
            # недоступны до promote; бейдж тестируем на active-карточке
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )
    service.update_status(
        CardStatusUpdateRequest(
            id="backend-disputed-card", action="suspect", note="решение не сработало"
        )
    )
    service.update_status(
        CardStatusUpdateRequest(
            id="backend-disputed-card",
            action="dispute",
            note="опровергнуто тестом",
            # user_confirmed-карточка: спор инициирует человек
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )

    res = storage.search_faceted(query="zzdisputed", include_archived=True)
    dump = res.model_dump(mode="json")
    snippets = dump.get("query_results", []) + dump.get("results", [])
    snippet = next(s for s in snippets if s["id"] == "backend-disputed-card")
    assert snippet["status"] == "disputed"
    assert snippet["badge"] == "disputed"


def test_r8_auto_prune_in_record_path(tmp_path: Path):
    """R-8: оппортунистический pruning срабатывает в record_tool_call на каждом 500-м id (read-heavy путь)."""
    db_path = tmp_path / "r8_autoprune.db"
    k_dir = tmp_path / "knowledge"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)

    # Предзаполняем 10499 записей напрямую (одно соединение — быстро)
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO tool_calls (tool_name, args, called_at, latency_ms, error) "
        "VALUES ('seed_tool', NULL, '2026-01-01T00:00:00+00:00', 1.0, NULL)",
        [() for _ in range(10499)],
    )
    conn.commit()
    conn.close()

    # 10500-й id (%) == 0 -> внутри record_tool_call срабатывает pruning до 10000
    storage.record_tool_call("search_knowledge", {"q": "x"}, latency_ms=1.0)

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
    max_id = conn.execute("SELECT MAX(id) FROM tool_calls").fetchone()[0]
    conn.close()
    assert max_id == 10500
    assert count == 10000
