"""Тесты для сервисного слоя MCP Knowledge Engine (Service Layer)."""

from pathlib import Path
import pytest

from mcp_knowledge.models import (
    CardRecordRequest,
    CardStatusUpdateRequest,
    KnowledgeCategory,
    KnowledgeSource,
    KnowledgeStatus,
    KnowledgeType,
)
from mcp_knowledge.parser import parse_card_markdown
from mcp_knowledge.service import (
    ForbiddenUserConfirmedActionError,
    KnowledgeService,
    UserConfirmedConflictError,
)
from mcp_knowledge.storage import KnowledgeStorage


@pytest.fixture
def service(tmp_path: Path) -> KnowledgeService:
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    cache_db = tmp_path / ".cache" / "knowledge_fts.db"
    cache_db.parent.mkdir(parents=True, exist_ok=True)

    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)
    return KnowledgeService(storage=storage, knowledge_dir=knowledge_dir)


def test_record_card_creates_file_and_indexes(service: KnowledgeService):
    """
    Вход: Валидный CardRecordRequest.
    Выход: Созданный файл на диске и найденный сниппет через service.search().
    Проверка: Файл физически существует, поиск находит его за 1 шаг.
    """
    req = CardRecordRequest(
        title="Зависание FFmpeg из-за SIGTTIN",
        category=KnowledgeCategory.AUDIO,
        source=KnowledgeSource.USER_CONFIRMED,
        type=KnowledgeType.LESSON,
        symptom="FFmpeg зависает на 0% в фоне",
        root_cause="Ядро Linux посылает сигнал SIGTTIN фоновому процессу, читающему stdin",
        solution="Обязательный флаг -nostdin и stdin=subprocess.DEVNULL при запуске",
        rationale="Исключает блокировку фонового воркера сигналом терминала",
    )

    resp = service.record(req)
    assert resp["status"] == "created"
    card_id = resp["id"]
    file_path = Path(resp["path"])

    assert file_path.exists()
    content = file_path.read_text(encoding="utf-8")
    fm, sections = parse_card_markdown(content)
    assert fm.id == card_id
    assert fm.title == req.title
    assert fm.status == KnowledgeStatus.ACTIVE

    # Проверяем, что поиск находит карточку
    search_res = service.search(query="sigttin")
    assert len(search_res) == 1
    assert search_res[0].id == card_id
    assert search_res[0].warning is None


def test_record_card_supersedes_old_card_and_updates_status(service: KnowledgeService):
    """
    Вход: Создание новой карточки с supersedes="old-id".
    Выход: Старая карточка получает status: superseded, superseded_by: "new-id".
    Проверка: Старая карточка исчезает из дефолтного search(), но доступна при include_archived=True.
    """
    # 1. Создаем старую карточку
    old_req = CardRecordRequest(
        title="Старый метод конвертации аудио через pydub",
        category=KnowledgeCategory.AUDIO,
        type=KnowledgeType.LESSON,
        symptom="Медленная конвертация аудио через pydub в память",
        root_cause="Pydub декодирует аудио полностью в RAM без стриминга",
        solution="Использование прямого пайплайна ffmpeg через subprocess",
        rationale="Снижение потребления RAM и кратное ускорение обработки",
        slug="audio-pydub-old",
    )
    old_resp = service.record(old_req)
    old_id = old_resp["id"]

    # 2. Создаем новую карточку, вытесняющую старую
    new_req = CardRecordRequest(
        title="Новый метод стриминга аудио через C-библиотеку libav",
        category=KnowledgeCategory.AUDIO,
        type=KnowledgeType.LESSON,
        symptom="Высокий оверхед на запуск процессов ffmpeg",
        root_cause="Частый форк процессов создает избыточную нагрузку на ОС",
        solution="Прямой биндинг libav через ctypes или cython для непрерывного стриминга данных",
        rationale="Исключение оверхеда на форк процессов и IPC",
        supersedes=old_id,
        slug="audio-libav-new",
    )
    new_resp = service.record(new_req)
    new_id = new_resp["id"]

    # Проверяем статус старой карточки на диске
    old_file = Path(old_resp["path"])
    fm, _ = parse_card_markdown(old_file.read_text(encoding="utf-8"))
    assert fm.status == KnowledgeStatus.SUPERSEDED
    assert fm.superseded_by == new_id

    # Проверяем видимость в поиске: старая скрыта, новая видна
    results_default = service.search(query="pydub")
    assert len(results_default) == 0

    results_archived = service.search(query="pydub", include_archived=True)
    assert len(results_archived) == 1
    assert results_archived[0].id == old_id
    assert results_archived[0].status == KnowledgeStatus.SUPERSEDED


def test_record_card_blocks_auto_inferred_superseding_user_confirmed(
    service: KnowledgeService,
):
    """
    Вход: Попытка вытеснить карточку source: user_confirmed запросом с source: auto_inferred.
    Выход: Исключение UserConfirmedConflictError.
    Проверка: Статус старой карточки остаётся active.
    """
    # Создаем карточку, подтвержденную пользователем
    req_user = CardRecordRequest(
        title="Архитектурное решение: SQLite WAL и Single Writer",
        category=KnowledgeCategory.DATABASE,
        type=KnowledgeType.DECISION,
        source=KnowledgeSource.USER_CONFIRMED,
        symptom="Параллельные транзакции вызывают sqlite3.OperationalError database locked",
        root_cause="SQLite не поддерживает одновременную запись из нескольких потоков без блокировки",
        solution="Паттерн Single-Writer Queue и транзакции через контекстный менеджер",
        rationale="Гарантирует консистентность и предотвращает дедлоки на уровне приложения",
        slug="db-wal-single-writer",
    )
    resp_user = service.record(req_user)
    user_id = resp_user["id"]

    # Попытка вытеснить ее от имени auto_inferred
    req_inferred = CardRecordRequest(
        title="Переход на PostgreSQL вместо SQLite",
        category=KnowledgeCategory.DATABASE,
        type=KnowledgeType.DECISION,
        source=KnowledgeSource.AUTO_INFERRED,
        symptom="Блокировки базы данных при масштабировании на несколько процессов",
        root_cause="SQLite является встроенной БД и не масштабируется на распределенные узлы",
        solution="Полный переход на PostgreSQL с пулом соединений asyncpg",
        rationale="Обеспечивает горизонтальное масштабирование и высокую пропускную способность",
        supersedes=user_id,
        slug="db-postgres-switch",
    )

    with pytest.raises(UserConfirmedConflictError) as exc_info:
        service.record(req_inferred)

    assert user_id in str(exc_info.value)

    # Проверяем, что исходная карточка осталась активной
    card_file = Path(resp_user["path"])
    fm, _ = parse_card_markdown(card_file.read_text(encoding="utf-8"))
    assert fm.status == KnowledgeStatus.ACTIVE
    assert fm.superseded_by is None


def test_update_status_blocks_auto_inferred_dispute_of_user_confirmed(
    service: KnowledgeService,
):
    """
    Вход: update_status(action="dispute", source="auto_inferred") на user_confirmed карточку.
    Выход: Исключение ForbiddenUserConfirmedActionError.
    Проверка: Статус не изменился.
    """
    req_user = CardRecordRequest(
        title="Изоляция оконных процессов Windows через CREATE_NO_WINDOW",
        category=KnowledgeCategory.DESKTOP,
        type=KnowledgeType.LESSON,
        source=KnowledgeSource.USER_CONFIRMED,
        symptom="Мерцание черных консольных окон при вызове ffmpeg и whisper",
        root_cause="Windows по умолчанию создает консольное окно для любого дочернего процесса",
        solution="Флаг creationflags=0x08000000 (CREATE_NO_WINDOW) в subprocess",
        rationale="Полное устранение мерцания консольных окон в десктопном приложении",
        slug="desk-create-no-window",
    )
    resp = service.record(req_user)
    card_id = resp["id"]

    update_req = CardStatusUpdateRequest(
        id=card_id,
        action="dispute",
        note="В Linux этот флаг не нужен и вызывает ошибку",
        source=KnowledgeSource.AUTO_INFERRED,
    )

    with pytest.raises(ForbiddenUserConfirmedActionError) as exc_info:
        service.update_status(update_req)

    assert card_id in str(exc_info.value)

    # Проверяем, что статус остался ACTIVE
    card_file = Path(resp["path"])
    fm, _ = parse_card_markdown(card_file.read_text(encoding="utf-8"))
    assert fm.status == KnowledgeStatus.ACTIVE


def test_two_stage_verification_suspect_then_dispute(service: KnowledgeService):
    """
    Вход: Перевод в suspect с логом ошибки, затем перевод в disputed.
    Выход: Карточка сначала выдаётся с warning, затем исключается из выдачи.
    Проверка: Лог ошибки записан в тело Markdown, статус обновлен.
    """
    req = CardRecordRequest(
        title="Использование флага --threads 8 для Whisper",
        category=KnowledgeCategory.ML,
        type=KnowledgeType.HINT,
        symptom="Медленная транскрибация аудио на CPU",
        root_cause="Whisper по умолчанию использует только одно ядро процессора для вычислений",
        solution="Явное указание количества потоков --threads 8 при вызове модели",
        rationale="Максимальная утилизация всех ядер процессора для ускорения инференса",
        slug="ml-whisper-threads",
    )
    resp = service.record(req)
    card_id = resp["id"]

    # K2: draft → promote, затем suspect
    service.update_status(
        CardStatusUpdateRequest(
            id=card_id,
            action="promote",
            note="Продвинута для двухэтапной проверки",
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )

    # Этап 1: Перевод в suspect
    suspect_update = CardStatusUpdateRequest(
        id=card_id,
        action="suspect",
        note="При 8 потоках на 4-ядерном CPU процесс начинает троттлить и замедляется",
        source=KnowledgeSource.AUTO_INFERRED,
    )
    s_resp = service.update_status(suspect_update)
    assert s_resp["status"] == "updated"
    assert s_resp["new_status"] == KnowledgeStatus.SUSPECT.value

    # Проверяем поиск: карточка находится, но содержит warning
    search_res = service.search(query="whisper")
    assert len(search_res) == 1
    assert search_res[0].id == card_id
    assert search_res[0].warning is not None

    # Этап 2: Перевод в disputed (опровергнуто человеком)
    dispute_update = CardStatusUpdateRequest(
        id=card_id,
        action="dispute",
        note="Окончательно опровергнуто: оптимальное число потоков равно числу физических ядер",
        by="human",
    )
    d_resp = service.update_status(dispute_update)
    assert d_resp["new_status"] == KnowledgeStatus.DISPUTED.value

    # Проверяем поиск: карточка исключена из дефолтной выдачи
    assert len(service.search(query="whisper")) == 0

    # Проверяем лог в файле
    card_file = Path(resp["path"])
    content = card_file.read_text(encoding="utf-8")
    assert "SUSPECT: При 8 потоках" in content
    assert "DISPUTED: Окончательно опровергнуто" in content


def test_two_stage_verification_suspect_then_verify_with_caveats(
    service: KnowledgeService,
):
    """
    Вход: Возврат из suspect в verified с текстом ограничений.
    Выход: Карточка получает status: verified, в текст добавлена секция нюансов.
    Проверка: Warning сброшен, статус verified.
    """
    req = CardRecordRequest(
        title="Оптимизация ONNX сессий через CoreML Execution Provider",
        category=KnowledgeCategory.ML,
        type=KnowledgeType.HINT,
        symptom="Высокая задержка при вычислении эмбеддингов на macOS",
        root_cause="По умолчанию ONNX Runtime использует CPU провайдер без аппаратного ускорения",
        solution="Подключение CoreMLExecutionProvider в options сессии",
        rationale="Использование Neural Engine для многократного ускорения вычислений",
        slug="ml-onnx-coreml",
        # K2: карточка auto_inferred создаётся в draft — suspect недоступен до promote
        source=KnowledgeSource.USER_CONFIRMED,
    )
    resp = service.record(req)
    card_id = resp["id"]

    # 1. Переводим в suspect
    service.update_status(
        CardStatusUpdateRequest(
            id=card_id,
            action="suspect",
            note="Сбой на старых версиях macOS < 12.0",
            source=KnowledgeSource.AUTO_INFERRED,
        )
    )

    # 2. Возвращаем в verified с нюансами (от имени человека)
    v_resp = service.update_status(
        CardStatusUpdateRequest(
            id=card_id,
            action="verify",
            note="Требуется проверка версии macOS: использовать только на macOS 12+",
            by="human",
        )
    )
    assert v_resp["new_status"] == KnowledgeStatus.VERIFIED.value

    # Проверяем поиск: карточка находится, warning сброшен
    search_res = service.search(query="coreml")
    assert len(search_res) == 1
    assert search_res[0].id == card_id
    assert search_res[0].warning is None
    assert search_res[0].status == KnowledgeStatus.VERIFIED

    # Проверяем Markdown
    card_file = Path(resp["path"])
    content = card_file.read_text(encoding="utf-8")
    assert "### Нюансы и ограничения (Nuances)" in content
    assert "Требуется проверка версии macOS" in content


def test_bidirectional_link_sync(service: KnowledgeService):
    """
    Вход: Создание карточки B со связью related_to=["card-a"].
    Выход: Карточка A автоматически получает related_to=["card-b"].
    Проверка: Взаимные ссылки присутствуют в frontmatter обоих файлов.
    """
    req_a = CardRecordRequest(
        title="Изоляция оконных процессов Windows",
        category=KnowledgeCategory.DESKTOP,
        type=KnowledgeType.LESSON,
        symptom="Мерцание черных окон консоли",
        root_cause="Windows по умолчанию создает консольное окно для фоновых процессов",
        solution="Флаг CREATE_NO_WINDOW в параметрах запуска subprocess",
        rationale="Устраняет визуальные артефакты при вызове утилит",
        slug="desk-create-no-window",
    )
    resp_a = service.record(req_a)
    id_a = resp_a["id"]

    req_b = CardRecordRequest(
        title="Зависание FFmpeg из-за SIGTTIN",
        category=KnowledgeCategory.AUDIO,
        type=KnowledgeType.LESSON,
        symptom="FFmpeg зависает на 0%",
        root_cause="Ядро Linux посылает сигнал SIGTTIN фоновому процессу, читающему stdin",
        solution="Обязательный флаг -nostdin и stdin=subprocess.DEVNULL при запуске",
        rationale="Исключает блокировку фонового воркера сигналом терминала",
        related_to=[id_a],
        slug="audio-sigttin",
    )
    resp_b = service.record(req_b)
    id_b = resp_b["id"]

    # Проверяем frontmatter карточки B
    file_b = Path(resp_b["path"])
    fm_b, _ = parse_card_markdown(file_b.read_text(encoding="utf-8"))
    assert id_a in fm_b.related_to

    # Проверяем frontmatter карточки A: должна появиться обратная ссылка на B!
    file_a = Path(resp_a["path"])
    fm_a, _ = parse_card_markdown(file_a.read_text(encoding="utf-8"))
    assert id_b in fm_a.related_to


def test_get_card_returns_details_and_not_found(service: KnowledgeService):
    """
    Вход: Вызов get() с существующим и несуществующим ID.
    Выход: CardGetResponse со списком карточек и списком not_found.
    Проверка: Структура ответа полностью валидна.
    """
    req = CardRecordRequest(
        title="Тестовая карточка для проверки чтения",
        category=KnowledgeCategory.PLATFORM,
        type=KnowledgeType.HINT,
        symptom="Тестовый симптом для проверки чтения карточек",
        root_cause="Тестовая первопричина для проверки чтения карточек длиной более 40 символов",
        solution="Тестовое решение для проверки чтения карточек длиной более 50 символов в системе",
        rationale="Тестовое обоснование решения для проверки чтения карточек",
        slug="plat-test-read",
    )
    resp = service.record(req)
    card_id = resp["id"]

    get_resp = service.get(ids=[card_id, "non-existent-card-id"])
    assert len(get_resp.cards) == 1
    assert get_resp.cards[0].id == card_id
    assert get_resp.cards[0].title == req.title
    assert "non-existent-card-id" in get_resp.not_found


def test_service_guardrail_protects_human_generated_card(service: KnowledgeService):
    """
    Вход: Попытка вызвать service.record() с параметром supersedes карточки, у которой generated.by == GeneratedBy.HUMAN, при вызове от имени process.
    Выход: Исключение UserConfirmedConflictError.
    Проверка: Вытеснение отклонено, статус существующей карточки остался неизменным.
    """
    card_path = service.knowledge_dir / "platform" / "plat-human.md"
    card_path.parent.mkdir(parents=True, exist_ok=True)
    raw_md = """---
id: plat-human
title: Human Architecture Standard
category: platform
type: decision
status: active
generated:
  by: human
  created_at: '2026-09-20'
verified: []
stale_after: null
supersedes: null
superseded_by: null
related_to: []
derived_from: null
aliases: []
tags:
- standard
---

### Симптом / Контекст
Архитектурный стандарт платформы Akousa.

### Первопричина (Root Cause)
Утвержденный человеком инвариант изоляции процессов.

### Решение / Выводы
Все дочерние процессы запускаются через единый менеджер процессов.

### Обоснование / Почему именно так (Rationale)
Предотвращает зомби-процессы и утечки дескрипторов.
"""
    card_path.write_text(raw_md, encoding="utf-8")
    service.storage.sync()

    req_agent = CardRecordRequest(
        title="Попытка агента вытеснить стандарт",
        category=KnowledgeCategory.PLATFORM,
        type=KnowledgeType.DECISION,
        source=KnowledgeSource.AUTO_INFERRED,
        symptom="Архитектурный стандарт платформы Akousa устарел",
        root_cause="Агент сгенерировал гипотезу о замене менеджера процессов",
        solution="Удаление единого менеджера процессов и запуск напрямую",
        rationale="Упрощает кодовую базу и снижает количество абстракций",
        supersedes="plat-human",
        slug="plat-agent-overwrite",
    )

    with pytest.raises(UserConfirmedConflictError):
        service.record(req_agent)

    # Проверяем, что исходная карточка осталась активной
    fm, _ = parse_card_markdown(card_path.read_text(encoding="utf-8"))
    assert fm.status == KnowledgeStatus.ACTIVE
    assert fm.superseded_by is None


def test_service_verify_action_creates_audit_entry(service: KnowledgeService):
    """
    Вход: Вызов service.update_status(id, action="verify", note="Проверено на стенде").
    Выход: Обновленный объект CardFrontmatter.
    Проверка: status == KnowledgeStatus.VERIFIED, список verified содержит запись с by="human", текущей датой и переданным комментарием.
    """
    req = CardRecordRequest(
        title="Тест верификации",
        category=KnowledgeCategory.AUDIO,
        type=KnowledgeType.LESSON,
        symptom="Проверка формирования аудиторского журнала верификации",
        root_cause="Действие verify должно добавлять структурированную запись в список verified",
        solution="Добавление VerificationRecord в CardFrontmatter.verified при update_status",
        rationale="Соответствие спецификации Open Knowledge Format OKF v0.2",
        slug="audio-verify-test",
        # K2: карточка auto_inferred создаётся в draft — verify недоступен до promote
        source=KnowledgeSource.USER_CONFIRMED,
    )
    resp = service.record(req)
    card_id = resp["id"]

    service.update_status(
        CardStatusUpdateRequest(
            id=card_id,
            action="verify",
            note="Проверено на стенде",
            by="human",
        )
    )

    card_file = Path(resp["path"])
    fm, _ = parse_card_markdown(card_file.read_text(encoding="utf-8"))
    assert fm.status == KnowledgeStatus.VERIFIED
    assert len(fm.verified) == 1
    assert fm.verified[0].by == "human"
    assert fm.verified[0].note == "Проверено на стенде"


def test_service_search_attaches_stale_warning(service: KnowledgeService):
    """
    Вход: Карточка с stale_after = "2026-01-01" (в прошлом) и поисковый запрос service.search().
    Выход: Список KnowledgeSnippet.
    Проверка: Поле warning сниппета содержит строку с упоминанием устаревания stale_after: 2026-01-01.
    """
    req = CardRecordRequest(
        title="Временный воркэраунд",
        category=KnowledgeCategory.AUDIO,
        source=KnowledgeSource.USER_CONFIRMED,
        type=KnowledgeType.HINT,
        symptom="Временное решение для старого драйвера звуковой карты",
        root_cause="Драйвер версии 1.0 содержал ошибку буферизации аудиопотока",
        solution="Принудительное включение флага fallback_audio_driver в конфиге",
        rationale="Позволяет работать со старым оборудованием до обновления ОС",
        stale_after="2026-01-01",
        slug="audio-stale-test",
    )
    service.record(req)

    results = service.search(query="воркэраунд")
    assert len(results) == 1
    assert results[0].warning is not None
    assert "stale_after" in results[0].warning
    assert "2026-01-01" in results[0].warning


def test_find_card_file_and_get_existing_ids_recursive(service: KnowledgeService):
    """Проверка, что _find_card_file и _get_existing_ids находят карточки в глубоких подпапках."""
    deep_dir = service.knowledge_dir / "platform" / "vulkan" / "sidecars"
    deep_dir.mkdir(parents=True, exist_ok=True)
    card_path = deep_dir / "platform-deep-vulkan.md"

    raw_md = """---
id: platform-deep-vulkan
title: Deep Nested Vulkan Sidecar
category: platform
type: lesson
status: active
generated:
  by: process
  created_at: '2026-09-20'
verified: []
stale_after: null
supersedes: null
superseded_by: null
related_to: []
derived_from: null
aliases: []
tags: []
---

### Симптом / Контекст
Симптом в глубоко вложенной директории.

### Решение / Выводы
Решение в глубоко вложенной директории.

### Первопричина (Root Cause)
Первопричина в глубоко вложенной директории.

### Обоснование / Почему именно так (Rationale)
Обоснование в глубоко вложенной директории.
"""
    card_path.write_text(raw_md, encoding="utf-8")

    # Проверяем нахождение файла
    found = service._find_card_file("platform-deep-vulkan")
    assert found is not None
    assert found.resolve() == card_path.resolve()

    # Проверяем получение ID
    existing_ids = service._get_existing_ids()
    assert "platform-deep-vulkan" in existing_ids


def test_service_verify_action_custom_by(service: KnowledgeService):
    """Проверка передачи явного автора верификации (например, ci-bot)."""
    req = CardRecordRequest(
        title="Тест кастомного автора верификации",
        category=KnowledgeCategory.AUDIO,
        type=KnowledgeType.LESSON,
        symptom="Проверка записи кастомного имени автора верификации",
        root_cause="Действие verify должно поддерживать передачу явного поля by",
        solution="Передача параметра by в CardStatusUpdateRequest и VerificationRecord",
        rationale="Позволяет аудировать автоматическую верификацию ботами",
        slug="audio-custom-by",
        # K2: карточка auto_inferred создаётся в draft — verify недоступен до promote
        source=KnowledgeSource.USER_CONFIRMED,
    )
    resp = service.record(req)
    card_id = resp["id"]

    service.update_status(
        CardStatusUpdateRequest(
            id=card_id,
            action="verify",
            note="Верифицировано CI раннером",
            by="ci-runner-v2",
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )

    card_file = Path(resp["path"])
    fm, _ = parse_card_markdown(card_file.read_text(encoding="utf-8"))
    assert fm.status == KnowledgeStatus.VERIFIED
    assert len(fm.verified) == 1
    assert fm.verified[0].by == "ci-runner-v2"
    assert fm.verified[0].note == "Верифицировано CI раннером"


def test_service_get_includes_preamble(service: KnowledgeService):
    """Проверка, что service.get() включает преамбулу в content карточки."""
    deep_dir = service.knowledge_dir / "platform"
    deep_dir.mkdir(parents=True, exist_ok=True)
    card_path = deep_dir / "platform-preamble.md"

    raw_md = """---
id: platform-preamble
title: Preamble Card
category: platform
type: lesson
status: active
generated:
  by: process
  created_at: '2026-09-20'
verified: []
stale_after: null
supersedes: null
superseded_by: null
related_to: []
derived_from: null
aliases: []
tags: []
---

> Вводный дисклеймер платформы

### Симптом / Контекст
Симптом с преамбулой.

### Решение / Выводы
Решение с преамбулой.

### Первопричина (Root Cause)
Первопричина с преамбулой.

### Обоснование / Почему именно так (Rationale)
Обоснование с преамбулой.
"""
    card_path.write_text(raw_md, encoding="utf-8")
    service.storage.sync()

    resp = service.get(["platform-preamble"])
    assert len(resp.cards) == 1
    assert "> Вводный дисклеймер платформы" in resp.cards[0].content


def test_related_to_omits_self_reference(service: KnowledgeService):
    """Проверка, что карточка не может ссылаться сама на себя в related_to."""
    req = CardRecordRequest(
        title="Тест самоссылки в связях",
        category=KnowledgeCategory.AUDIO,
        type=KnowledgeType.LESSON,
        symptom="Попытка добавления собственного ID в список related_to карточки",
        root_cause="Двусторонняя синхронизация связей не должна допускать циклических самоссылок",
        solution="Фильтрация card_id из req.related_to перед сохранением карточки на диск",
        rationale="Предотвращает замусоривание блока see_also ссылками на саму себя",
        slug="audio-self-ref-test",
        related_to=["audio-self-ref-test", "another-card"],
    )
    resp = service.record(req)
    card_path = Path(resp["path"])

    content = card_path.read_text(encoding="utf-8")
    assert "audio-self-ref-test" not in content.split("related_to:")[1].split("derived_from:")[0]

    get_resp = service.get(["audio-self-ref-test"])
    assert "audio-self-ref-test" not in get_resp.cards[0].see_also


def test_supersedes_appends_superseded_log_entry(service: KnowledgeService):
    """Проверка, что при вытеснении старой карточки в ее Log добавляется запись SUPERSEDED."""
    # 1. Создаем старую карточку
    old_req = CardRecordRequest(
        title="Старое временное решение",
        category=KnowledgeCategory.PLATFORM,
        type=KnowledgeType.HINT,
        symptom="Временное решение до релиза новой архитектуры",
        root_cause="Предыдущая версия платформы требовала ручного перезапуска демона",
        solution="Скрипт перезапуска демона каждые 30 минут по расписанию cron",
        rationale="Предотвращает утечки ресурсов на старой версии платформы",
        slug="plat-old-workaround",
    )
    old_resp = service.record(old_req)
    old_file = Path(old_resp["path"])

    # 2. Новая карточка, вытесняющая старую
    new_req = CardRecordRequest(
        title="Новая постоянная архитектура",
        category=KnowledgeCategory.PLATFORM,
        type=KnowledgeType.DECISION,
        symptom="Отказ от периодического перезапуска демона cron",
        root_cause="Новая версия демона с исправленной моделью управления памятью",
        solution="Внедрение автоматического сборщика мусора и пула соединений",
        rationale="Устраняет необходимость в костылях с периодическим перезапуском процесса",
        supersedes=old_resp["id"],
        slug="plat-new-arch",
    )
    new_resp = service.record(new_req)

    # Проверяем лог старой карточки
    old_content = old_file.read_text(encoding="utf-8")
    assert f"SUPERSEDED: Replaced by {new_resp['id']}" in old_content
    assert "status: superseded" in old_content
    assert f"superseded_by: {new_resp['id']}" in old_content


def test_get_knowledge_clamps_to_10_cards(service: KnowledgeService):
    """Проверка, что service.get() клампит количество запрашиваемых карточек до 10."""
    # Создаем 12 карточек
    card_ids = []
    for i in range(12):
        c_req = CardRecordRequest(
            title=f"Тестовая карточка для проверки клампинга {i}",
            category=KnowledgeCategory.PLATFORM,
            type=KnowledgeType.HINT,
            symptom=f"Симптом для клампинга get номер {i} длиной более 10 символов",
            root_cause=f"Первопричина для клампинга get номер {i} длиной более 40 символов",
            solution=f"Решение проблемы для клампинга get номер {i} длиной более 50 символов",
            rationale=f"Обоснование правильности решения {i} более 30 символов в системе",
            slug=f"plat-clamp-{i}",
            force=True,
            confirmed_force=True,
        )
        resp = service.record(c_req)
        card_ids.append(resp["id"])

    # Запрашиваем все 12 штук
    get_resp = service.get(card_ids)

    # Должно вернуться не более 10 штук, и должно присутствовать предупреждение
    assert len(get_resp.cards) == 10
    assert get_resp.warning is not None
    assert "ограничено первыми 10" in get_resp.warning


def test_update_status_blocks_mutating_superseded_card(service: KnowledgeService):
    """Проверка, что вытесненная карточка со статусом superseded не может быть изменена через update_status."""
    # 1. Создаем старую карточку
    old_req = CardRecordRequest(
        title="Исходное временное решение",
        category=KnowledgeCategory.PLATFORM,
        type=KnowledgeType.HINT,
        symptom="Утечка дескрипторов файлов при массовом парсинге",
        root_cause="Не закрываются файловые дескрипторы в цикле",
        solution="Ручной периодический перезапуск рабочего процесса раз в час через cron расписание",
        rationale="Временное решение до рефакторинга",
        slug="plat-leak-old",
    )
    old_resp = service.record(old_req)

    # 2. Вытесняем ее новой карточкой
    new_req = CardRecordRequest(
        title="Постоянное исправление утечки",
        category=KnowledgeCategory.PLATFORM,
        type=KnowledgeType.DECISION,
        symptom="Отказ от перезапуска процесса раз в час",
        root_cause="Найдены незакрытые дескрипторы в контекстных менеджерах",
        solution="Полноценное использование with open(...) во всех обработчиках файлов проекта",
        rationale="Полное устранение утечки без костылей с cron",
        supersedes=old_resp["id"],
        slug="plat-leak-fix",
    )
    new_resp = service.record(new_req)

    # 3. Пытаемся подтвердить старую карточку
    upd_req = CardStatusUpdateRequest(
        id=old_resp["id"],
        action="verify",
        note="Пытаемся подтвердить старое решение",
        by="human",
    )
    with pytest.raises(ValueError, match="вытеснена карточкой"):
        service.update_status(upd_req)


def test_find_card_file_compound_category(service: KnowledgeService, tmp_path: Path):
    """Проверка O(1) поиска файла карточки для составных категорий (game-dev, net-code)."""
    req = CardRecordRequest(
        title="Vulkan Pipeline State Caching",
        category="game-dev",
        type=KnowledgeType.DECISION,
        symptom="Фризы при первой компиляции шейдеров во время геймплея",
        root_cause="Синхронная компиляция Pipeline State Objects в главном потоке",
        solution="Прогрев и кэширование PSO на этапе загрузки уровня",
        rationale="Устраняет задержки рендера и обеспечивает ровный фреймрейт",
        slug="shader-cache",
    )
    resp = service.record(req)
    card_id = resp["id"]

    found_path = service._find_card_file(card_id)
    assert found_path is not None
    assert found_path.exists()
    assert "game-dev" in str(found_path.parent)
