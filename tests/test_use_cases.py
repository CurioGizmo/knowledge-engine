"""Unit-тесты для Use Cases MCP Knowledge Engine."""

from pathlib import Path

import pytest

from mcp_knowledge.errors import (
    DuplicateCardError,
    UserConfirmedConflictError,
)
from mcp_knowledge.models import (
    CardRecordRequest,
    CardStatusUpdateRequest,
    HypothesesResolveRequest,
    HypothesisRecordRequest,
    HypothesisResolution,
    KnowledgeCategory,
    KnowledgeSource,
    KnowledgeStatus,
    KnowledgeType,
)
from mcp_knowledge.parser import parse_card_markdown
from mcp_knowledge.repository import CardRepository
from mcp_knowledge.storage import KnowledgeStorage
from mcp_knowledge.synchronizer import KnowledgeSynchronizer
from mcp_knowledge.use_cases import (
    ProposeHypothesisUseCase,
    QueryKnowledgeUseCase,
    RecordKnowledgeUseCase,
    ResolveHypothesesUseCase,
    UpdateStatusUseCase,
)


@pytest.fixture
def test_env(tmp_path: Path):
    k_dir = tmp_path / "knowledge"
    db_path = tmp_path / "index.db"
    storage = KnowledgeStorage(db_path=db_path, knowledge_dir=k_dir)
    repo = CardRepository(knowledge_dir=k_dir)
    synchronizer = KnowledgeSynchronizer(storage=storage, repo=repo)
    storage.set_synchronizer(synchronizer)
    return {
        "k_dir": k_dir,
        "db_path": db_path,
        "storage": storage,
        "repo": repo,
        "synchronizer": synchronizer,
    }


def test_record_use_case_lifecycle(test_env):
    repo = test_env["repo"]
    storage = test_env["storage"]
    synchronizer = test_env["synchronizer"]
    use_case = RecordKnowledgeUseCase(repo=repo, storage=storage, synchronizer=synchronizer)

    # 1. Запись первой карточки
    req = CardRecordRequest(
        title="Тестовое решение",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.LESSON,
        symptom="Симптом проблемы при работе с БД",
        root_cause="Неправильная настройка connection pool при высокой нагрузке на кластер",
        solution="Увеличить размер пула и настроить тайм-аут ожидания соединений в конфиге",
        rationale="Предотвращает исчерпание соединений в часы пик",
        slug="db-pool-fix",
    )
    res = use_case.execute(req)
    assert res["status"] == "created"
    assert res["id"] == "backend-db-pool-fix"
    assert Path(res["path"]).exists()

    # 2. Попытка создания дубликата без force -> DuplicateCardError
    with pytest.raises(DuplicateCardError):
        use_case.execute(req)

    # 3. Запись с force=True без confirmed_force -> confirmation_required
    dup_req = req.model_copy(
        update={"force": True, "confirmed_force": False, "slug": "db-pool-fix-2"}
    )
    conf_res = use_case.execute(dup_req)
    assert conf_res["status"] == "confirmation_required"
    assert conf_res["existing_id"] == "backend-db-pool-fix"

    # 4. Вытеснение (supersedes)
    super_req = CardRecordRequest(
        title="Новое решение БД",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.LESSON,
        symptom="Симптом проблемы при работе с БД",
        root_cause="Новая первопричина: утечка транзакций в долгоживущих HTTP-сессиях",
        solution="Использовать контекстный менеджер для закрытия сессий и возврата в пул",
        rationale="Гарантирует своевременное освобождение ресурсов пула соединений",
        slug="db-pool-v2",
        supersedes="backend-db-pool-fix",
    )
    super_res = use_case.execute(super_req)
    assert super_res["status"] == "created"
    assert super_res["id"] == "backend-db-pool-v2"

    # Проверяем, что старая карточка помечена как superseded
    old_file = repo.find_card_file("backend-db-pool-fix")
    assert old_file is not None
    old_fm, _ = parse_card_markdown(old_file.read_text(encoding="utf-8"))
    assert old_fm.status == KnowledgeStatus.SUPERSEDED
    assert old_fm.superseded_by == "backend-db-pool-v2"


def test_record_use_case_user_confirmed_guardrail(test_env):
    repo = test_env["repo"]
    storage = test_env["storage"]
    synchronizer = test_env["synchronizer"]
    use_case = RecordKnowledgeUseCase(repo=repo, storage=storage, synchronizer=synchronizer)

    # Записываем карточку с user_confirmed
    req = CardRecordRequest(
        title="Архитектурное решение пользователя",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.DECISION,
        symptom="Выбор протокола коммуникации микросервисов в кластере",
        root_cause="Необходима строгая типизация контрактов и минимальный сетевой оверхед",
        solution="Использовать gRPC с HTTP/2 multiplexing для всего межсервисного обмена",
        rationale="gRPC обеспечивает строгий контракт через protobuf схемы",
        slug="grpc-choice",
        source=KnowledgeSource.USER_CONFIRMED,
    )
    res = use_case.execute(req)
    assert res["status"] == "created"

    # Попытка вытеснить через auto_inferred -> UserConfirmedConflictError
    auto_req = CardRecordRequest(
        title="Попытка авто-вытеснения",
        category=KnowledgeCategory.BACKEND,
        type=KnowledgeType.DECISION,
        symptom="Выбор протокола коммуникации микросервисов в кластере",
        root_cause="Обновление данных и упрощение интеграции для внешних клиентов",
        solution="Использовать REST JSON API вместо gRPC на всех сервисах компании",
        rationale="Проще интегрировать с внешними веб-клиентами",
        slug="rest-choice",
        supersedes="backend-grpc-choice",
        source=KnowledgeSource.AUTO_INFERRED,
    )
    with pytest.raises(UserConfirmedConflictError):
        use_case.execute(auto_req)


def test_propose_hypothesis_use_case(test_env):
    repo = test_env["repo"]
    storage = test_env["storage"]
    synchronizer = test_env["synchronizer"]
    use_case = ProposeHypothesisUseCase(repo=repo, storage=storage, synchronizer=synchronizer)

    req = HypothesisRecordRequest(
        title="Гипотеза о сбое кэширования",
        category=KnowledgeCategory.BACKEND,
        symptom="Периодические 502 ошибки на бэкенде",
        hypothesis="Redis падает из-за превышения maxmemory при пиках трафика",
        task_id="task-redis-debug",
        slug="redis-oom",
    )
    res = use_case.execute(req)
    assert res["status"] == "proposed"
    assert res["id"] == "backend-redis-oom"
    assert Path(res["path"]).exists()

    # Повторная попытка создания той же гипотезы -> DuplicateCardError
    with pytest.raises(DuplicateCardError):
        use_case.execute(req)


def test_resolve_hypotheses_use_case(test_env):
    repo = test_env["repo"]
    storage = test_env["storage"]
    synchronizer = test_env["synchronizer"]
    propose_uc = ProposeHypothesisUseCase(repo=repo, storage=storage, synchronizer=synchronizer)
    resolve_uc = ResolveHypothesesUseCase(repo=repo, storage=storage)

    # 1. Создаем две гипотезы
    h1 = propose_uc.execute(
        HypothesisRecordRequest(
            title="Гипотеза 1",
            category=KnowledgeCategory.FRONTEND,
            symptom="Белый экран при загрузке приложения",
            hypothesis="Ошибка гидратации в React 19 из-за SSR разметки",
            slug="react-hydration",
        )
    )
    h2 = propose_uc.execute(
        HypothesisRecordRequest(
            title="Гипотеза 2",
            category=KnowledgeCategory.FRONTEND,
            symptom="Утечка памяти в браузере",
            hypothesis="Неотписанный EventListener в кастомном хуке",
            slug="hook-leak",
        )
    )

    # 2. Валидация: дубликат в батче
    with pytest.raises(ValueError, match="Дублирующийся ID"):
        resolve_uc.execute(
            HypothesesResolveRequest(
                resolutions=[
                    HypothesisResolution(
                        id=h1["id"],
                        action="confirm",
                        solution="Исправить несоответствие SSR разметки и клиента",
                    ),
                    HypothesisResolution(
                        id=h1["id"],
                        action="confirm",
                        solution="Повторный вызов того же ID",
                    ),
                ]
            )
        )

    # 3. Валидация: confirm без solution (слишком короткий)
    with pytest.raises(ValueError, match="не менее 10 символов"):
        resolve_uc.execute(
            HypothesesResolveRequest(
                resolutions=[
                    HypothesisResolution(
                        id=h1["id"],
                        action="confirm",
                        solution="short",
                    )
                ]
            )
        )

    # 4. Успешная резолюция: h1 -> confirm, h2 -> refute (антипаттерн)
    res = resolve_uc.execute(
        HypothesesResolveRequest(
            resolutions=[
                HypothesisResolution(
                    id=h1["id"],
                    action="confirm",
                    solution="Добавлен suppressHydrationWarning и выровнен рендеринг дат",
                    rationale="Устраняет расхождения между сервером и клиентом",
                ),
                HypothesisResolution(
                    id=h2["id"],
                    action="refute",
                    refutation_reason="Хук корректно отписывается через useEffect cleanup функцию",
                ),
            ]
        )
    )
    assert res["status"] == "resolved"
    assert res["resolved_count"] == 2

    # Проверяем состояние файлов
    f1 = repo.find_card_file(h1["id"])
    assert f1 is not None
    fm1, _ = parse_card_markdown(f1.read_text(encoding="utf-8"))
    # Auto-inferred hypothesis stays in draft quarantine after confirm (plan B);
    # promotion to active requires human confirmation via update_status promote.
    assert fm1.status == KnowledgeStatus.DRAFT
    assert fm1.type == KnowledgeType.LESSON

    f2 = repo.find_card_file(h2["id"])
    assert f2 is not None
    fm2, _ = parse_card_markdown(f2.read_text(encoding="utf-8"))
    assert fm2.status == KnowledgeStatus.REFUTED
    assert fm2.type == KnowledgeType.ANTIPATTERN


def test_update_status_use_case(test_env):
    repo = test_env["repo"]
    storage = test_env["storage"]
    synchronizer = test_env["synchronizer"]
    record_uc = RecordKnowledgeUseCase(repo=repo, storage=storage, synchronizer=synchronizer)
    status_uc = UpdateStatusUseCase(repo=repo, synchronizer=synchronizer)

    record_res = record_uc.execute(
        CardRecordRequest(
            title="Тест статусов",
            category=KnowledgeCategory.PLATFORM,
            type=KnowledgeType.LESSON,
            symptom="Сбой файловой системы при пиковой нагрузке",
            root_cause="Переполнение дескрипторов файлов из-за утечки сокетов в процессе",
            solution="Увеличить ulimit -n в конфигурации systemd сервиса до 65536 и закрывать сокеты",
            rationale="Предотвращает ошибку too many open files в ядре Linux",
            slug="fs-descriptors",
            # K2: карточка auto_inferred создаётся в draft — статусные действия
            # недоступны до promote
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )
    card_id = record_res["id"]

    # 1. suspect
    res1 = status_uc.execute(
        CardStatusUpdateRequest(id=card_id, action="suspect", note="Сбой повторился в проде")
    )
    assert res1["new_status"] == "suspect"

    # 2. verify
    res2 = status_uc.execute(
        CardStatusUpdateRequest(
            id=card_id,
            action="verify",
            by="developer",
            note="Проверено на нагрузочном стенде с 50к соединений",
        )
    )
    assert res2["new_status"] == "verified"

    # 3. append_note
    res3 = status_uc.execute(
        CardStatusUpdateRequest(
            id=card_id, action="append_note", note="Также актуально для Ubuntu 24.04"
        )
    )
    assert res3["new_status"] == "verified"


def test_query_knowledge_use_case(test_env):
    repo = test_env["repo"]
    storage = test_env["storage"]
    synchronizer = test_env["synchronizer"]
    record_uc = RecordKnowledgeUseCase(repo=repo, storage=storage, synchronizer=synchronizer)
    query_uc = QueryKnowledgeUseCase(repo=repo, storage=storage)

    record_uc.execute(
        CardRecordRequest(
            title="Поисковая карточка",
            category=KnowledgeCategory.BACKEND,
            source=KnowledgeSource.USER_CONFIRMED,
            type=KnowledgeType.HINT,
            symptom="Оптимизация SQLite FTS5 поисковых запросов",
            root_cause="Медленный полнотекстовый поиск по большим текстовым полям карточек",
            solution="Использовать BM25 веса и триграммные токены для быстрого сканирования",
            rationale="Ускоряет поиск на порядок по бенчмаркам",
            slug="sqlite-fts-opt",
        )
    )

    # 1. get()
    get_res = query_uc.get(["backend-sqlite-fts-opt", "non-existent-card"])
    assert len(get_res.cards) == 1
    assert get_res.cards[0].id == "backend-sqlite-fts-opt"
    assert "non-existent-card" in get_res.not_found

    # 2. get() > 10 IDs truncation
    many_ids = [f"id-{i}" for i in range(15)]
    trunc_res = query_uc.get(many_ids)
    assert trunc_res.warning is not None
    assert "Количество ограничено первыми 10" in trunc_res.warning

    # 3. search()
    snippets = query_uc.search(query="SQLite FTS5")
    assert len(snippets) >= 1
    assert snippets[0].id == "backend-sqlite-fts-opt"

    # 4. search_faceted()
    faceted = query_uc.search_faceted(query="SQLite")
    assert len(faceted.query_results) >= 1
    assert faceted.query_results[0].id == "backend-sqlite-fts-opt"


def test_update_status_storage_failure_raises_storage_unavailable(test_env, monkeypatch):
    """D5: OSError в write-пути update_status маппится в StorageUnavailableError."""
    import mcp_knowledge.repository as repo_module
    from mcp_knowledge.errors import StorageUnavailableError

    repo = test_env["repo"]
    storage = test_env["storage"]
    synchronizer = test_env["synchronizer"]
    record_uc = RecordKnowledgeUseCase(repo=repo, storage=storage, synchronizer=synchronizer)
    status_uc = UpdateStatusUseCase(repo=repo, synchronizer=synchronizer)

    record_res = record_uc.execute(
        CardRecordRequest(
            title="Сбой записи статуса",
            category=KnowledgeCategory.PLATFORM,
            type=KnowledgeType.LESSON,
            symptom="Отказ диска при обновлении статуса карточки",
            root_cause="Аппаратный сбой накопителя в момент записи Markdown файла",
            solution="Перейти на реплику и восстановить карточку из резервной копии",
            rationale="Сохраняет целостность базы знаний при сбоях оборудования",
            slug="status-write-fail",
            # K2: карточка auto_inferred создаётся в draft — suspect недоступен
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )
    card_id = record_res["id"]

    def _fail_write(path: Path, content: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(repo_module, "_atomic_write_text", _fail_write)

    with pytest.raises(StorageUnavailableError):
        status_uc.execute(CardStatusUpdateRequest(id=card_id, action="suspect", note="Сбой записи"))


def test_resolve_confirm_storage_failure_raises_storage_unavailable(test_env, monkeypatch):
    """D5: OSError в write-пути resolve(confirm) маппится в StorageUnavailableError."""
    import mcp_knowledge.repository as repo_module
    from mcp_knowledge.errors import StorageUnavailableError

    repo = test_env["repo"]
    storage = test_env["storage"]
    synchronizer = test_env["synchronizer"]
    propose_uc = ProposeHypothesisUseCase(repo=repo, storage=storage, synchronizer=synchronizer)
    resolve_uc = ResolveHypothesesUseCase(repo=repo, storage=storage)

    hypo = propose_uc.execute(
        HypothesisRecordRequest(
            title="Гипотеза со сбоем записи",
            category=KnowledgeCategory.BACKEND,
            symptom="Ошибка подтверждения гипотезы при сбое диска",
            hypothesis="Падение записи при резолюции из-за переполнения раздела",
            slug="confirm-write-fail",
        )
    )
    hypo_id = hypo["id"]

    def _fail_write(path: Path, content: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(repo_module, "_atomic_write_text", _fail_write)

    with pytest.raises(StorageUnavailableError):
        resolve_uc.execute(
            HypothesesResolveRequest(
                resolutions=[
                    HypothesisResolution(
                        id=hypo_id,
                        action="confirm",
                        solution="Решение, которое не удастся записать на диск",
                    )
                ]
            )
        )


def test_record_supersede_storage_failure_returns_warning(test_env, monkeypatch):
    """W2: сбой записи вытесненной карточки не ломает создание новой — warning + карточка создана."""
    import mcp_knowledge.repository as repo_module

    repo = test_env["repo"]
    storage = test_env["storage"]
    synchronizer = test_env["synchronizer"]
    record_uc = RecordKnowledgeUseCase(repo=repo, storage=storage, synchronizer=synchronizer)

    first = record_uc.execute(
        CardRecordRequest(
            title="Первая версия решения",
            category=KnowledgeCategory.BACKEND,
            type=KnowledgeType.LESSON,
            symptom="Симптом для проверки сбоев вытеснения",
            root_cause="Первая первопричина достаточно длинная для валидатора модели",
            solution="Первое решение достаточно длинное для валидатора модели",
            rationale="Первое обоснование достаточно длинное для валидатора",
            slug="supersede-fail-v1",
        )
    )

    calls = {"count": 0}
    real_write = repo_module._atomic_write_text

    def _fail_on_second_write(path: Path, content: str) -> None:
        # Первая запись (новая карточка) проходит, вторая (supersede) падает
        calls["count"] += 1
        if calls["count"] > 1:
            raise OSError("disk full")
        real_write(path, content)

    monkeypatch.setattr(repo_module, "_atomic_write_text", _fail_on_second_write)

    res = record_uc.execute(
        CardRecordRequest(
            title="Вторая версия решения",
            category=KnowledgeCategory.BACKEND,
            type=KnowledgeType.LESSON,
            symptom="Симптом для проверки сбоев вытеснения",
            root_cause="Вторая первопричина достаточно длинная для валидатора модели",
            solution="Второе решение достаточно длинное для валидатора модели",
            rationale="Второе обоснование достаточно длинное для валидатора",
            slug="supersede-fail-v2",
            supersedes=first["id"],
        )
    )
    assert res["status"] == "created"
    assert "не помечена вытесненной" in res.get("warning", "")
    assert Path(res["path"]).exists()
