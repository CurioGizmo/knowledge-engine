"""Тесты для Content Gate (защита от отчётов о работе) и двухстадийной дедупликации."""

from pathlib import Path
import pytest
from pydantic import ValidationError

from mcp_knowledge.content_gate import detect_report_like_content
from mcp_knowledge.errors import DuplicateCardError, ReportLikeContentError
from mcp_knowledge.models import (
    CardRecordRequest,
    HypothesesResolveRequest,
    HypothesisRecordRequest,
    HypothesisResolution,
    KnowledgeSource,
    KnowledgeStatus,
    KnowledgeType,
)
from mcp_knowledge.parser import parse_card_markdown, serialize_card_markdown
from mcp_knowledge.service import KnowledgeService
from mcp_knowledge.storage import KnowledgeStorage


def test_content_gate_allows_narrative_engineering_cards():
    """Проверка, что естественный инженерный язык не блокируется гейтом (нет ложных срабатываний)."""
    # 1. Описание задачи и плана рефакторинга
    req_plan = CardRecordRequest(
        title="Коэрцит Pydantic типов",
        category="backend",
        type=KnowledgeType.LESSON,
        symptom="Реализация плана search_discoverability: выполнено слияние колонок",
        root_cause="В ходе задачи возникла необходимость проверить коэрцит Pydantic v2 типов",
        solution="Обязательный field_validator mode='before' для строковых полей в модели",
        rationale="Предотвращает выпадение карточек из индексации при парсинге YAML",
    )
    assert detect_report_like_content(req_plan) is None

    # 2. Требования пользователя
    req_user = CardRecordRequest(
        title="Настройка прокси fastcgi",
        category="backend",
        type=KnowledgeType.LESSON,
        symptom="Пользователь попросил добавить поддержку стриминга в fastcgi",
        root_cause="Буферизация Nginx блокирует chunked transfer encoding для бэкенда",
        solution="Установка proxy_buffering off и proxy_cache off в конфигурации виртуального хоста",
        rationale="Обеспечивает передачу чанков без накопления в промежуточных буферах",
    )
    assert detect_report_like_content(req_user) is None

    # 3. Поведение субагентов
    req_sub = CardRecordRequest(
        title="Идентификация моделей",
        category="ai-agents",
        type=KnowledgeType.LESSON,
        symptom="Субагенты ответили синхронным отказом на вопрос об идентичности",
        root_cause="Системный промпт маскирует исходную тренировочную идентичность LLM",
        solution="Использование прямого API-запроса без системного промпта для зондирования",
        rationale="Исключает искажение ответов директивами верхнего уровня оркестратора",
    )
    assert detect_report_like_content(req_sub) is None

    # 4. Прохождение тестов
    req_passed = CardRecordRequest(
        title="Стабильность слоя репозитория",
        category="backend",
        type=KnowledgeType.LESSON,
        symptom="Все тесты зеленые после рефакторинга слоя репозитория",
        root_cause="Регрессионные ошибки отсутствуют благодаря полному покрытию юнит-тестами",
        solution="Регулярный запуск тестового набора через pytest перед каждым коммитом в репозиторий",
        rationale="Гарантирует стабильность кодовой базы и отсутствие скрытых регрессий",
    )
    assert detect_report_like_content(req_passed) is None

    # 5. English phrasing
    req_en = CardRecordRequest(
        title="Refactor sync engine",
        category="backend",
        type=KnowledgeType.LESSON,
        symptom="During the session implementation of the plan was completed successfully",
        root_cause="Legacy code contained redundant lock primitives causing livelocks",
        solution="Adopt single-writer lock with atomic state transitions in sqlite wal mode",
        rationale="Guarantees zero livelocks and consistent serialization order",
    )
    assert detect_report_like_content(req_en) is None


def test_content_gate_detects_structural_anomalies():
    """Проверка блокировки структурного мусора: чеклистов, спама строками файлов и трассировок стека."""
    # 1. Чекбоксы (чеклист спринта / плана)
    req_checklist = CardRecordRequest(
        title="Чеклист синхронизации",
        category="backend",
        type=KnowledgeType.LESSON,
        symptom="Список задач спринта:\n- [ ] Обновить парсер\n- [ ] Прогнать тесты",
        root_cause="Накопившийся долг требует поэтапного закрытия задач разработчиками",
        solution="Поэтапное выполнение шагов согласно утверждённому чек-листу спринта",
        rationale="Обеспечивает контроль прогресса выполнения запланированных работ",
    )
    reason = detect_report_like_content(req_checklist)
    assert reason is not None
    assert "чеклист" in reason

    # 2. Спам номерами строк файлов (локальный баг-репорт pytest)
    req_lines = CardRecordRequest(
        title="Локальные ошибки",
        category="backend",
        type=KnowledgeType.LESSON,
        symptom="Ошибки в parser.py:120, storage.py:450, server.py:89, models.py:230",
        root_cause="Рассинхрон сигнатур между модулями при вызове внутренних методов",
        solution="Синхронизация аргументов функций во всех затронутых модулях системы",
        rationale="Устраняет TypeErrors при межмодульных взаимодействиях компонентов",
    )
    reason = detect_report_like_content(req_lines)
    assert reason is not None
    assert "номерами строк" in reason

    # 3. Дамп трассировки стека Python
    req_traceback = CardRecordRequest(
        title="Сбой парсера",
        category="backend",
        type=KnowledgeType.LESSON,
        symptom='File "parser.py", line 42, in parse\nFile "storage.py", line 100, in save',
        root_cause="Необработанное исключение при парсинге поврежденного YAML frontmatter",
        solution="Добавление блока try/except для перехвата yaml.YAMLError",
        rationale="Предотвращает аварийное завершение процесса при поврежденных файлах",
    )
    reason = detect_report_like_content(req_traceback)
    assert reason is not None
    assert "дамп трассировки" in reason


def test_content_gate_user_confirmed_immunity():
    """Человеческие договорённости (user_confirmed) имеют абсолютный иммунитет даже при наличии чеклистов."""
    req_user_confirmed = CardRecordRequest(
        title="Регламент чеклиста релиза",
        category="platform",
        type=KnowledgeType.DECISION,
        source=KnowledgeSource.USER_CONFIRMED,
        symptom="Пользователь утвердил чеклист:\n- [ ] Проверить сборку\n- [ ] Обновить версию",
        root_cause="Договорённость с пользователем: обязательный чеклист перед выпуском релиза",
        solution="Следовать утверждённому регламенту релиза из документации и выполнять все обязательные проверки",
        rationale="Обеспечивает стабильность поставок релизов пользователям",
    )
    # Иммунитет: гейт пропускает карточку
    assert detect_report_like_content(req_user_confirmed) is None


def test_record_use_case_blocks_report_content_and_supports_override(tmp_path: Path):
    """Проверка блокировки через use case и двухфазного подтверждения force."""
    k_dir = tmp_path / "k"
    c_db = tmp_path / "c.db"
    storage = KnowledgeStorage(db_path=c_db, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    req = CardRecordRequest(
        title="План рефакторинга слоя хранения",
        category="backend",
        type=KnowledgeType.LESSON,
        symptom="План работ спринта:\n- [ ] Выделить интерфейсы\n- [ ] Написать адаптеры",
        root_cause="Старый код содержал архитектурные изъяны и дублирование логики",
        solution="Перенос бизнес-логики в отдельные use case классы чистой архитектуры",
        rationale="Улучшает тестируемость и модульность кодовой базы проекта",
    )

    # 1. Без force: рейзит ReportLikeContentError
    with pytest.raises(ReportLikeContentError):
        service.record(req)

    # 2. С force=True без confirmed_force: возвращает confirmation_required
    req.force = True
    resp_conf = service.record(req)
    assert resp_conf["status"] == "confirmation_required"
    assert resp_conf["reason"] == "REPORT_CONFIRMATION_REQUIRED"

    # 3. С force=True и confirmed_force=True: создаёт карточку
    req.confirmed_force = True
    resp_ok = service.record(req)
    assert resp_ok["status"] == "created"


def test_check_duplicate_detects_title_and_alias_duplicates(tmp_path: Path):
    """Проверка обнаружения дубликатов по совпадению заголовка или специфичных aliases."""
    k_dir = tmp_path / "k"
    c_db = tmp_path / "c.db"
    storage = KnowledgeStorage(db_path=c_db, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    # Создаем базовую карточку
    base_req = CardRecordRequest(
        title="Синхронизация SQLite WAL пула соединений",
        category="database",
        type=KnowledgeType.DECISION,
        symptom="Ошибки database is locked при параллельных транзакциях воркеров",
        root_cause="Отсутствие PRAGMA busy_timeout и synchronous=NORMAL в конфигурации SQLite",
        solution="Установка busy_timeout=5000 и journal_mode=WAL для всех соединений",
        rationale="Исключает дедлоки при одновременной записи и чтении несколькими процессами",
        aliases=["sqlite-busy-timeout", "sqlite-wal-concurrency"],
        slug="sqlite-wal-sync",
    )
    service.record(base_req)

    # 1. Дубликат с похожим заголовком, но совершенно другим текстом симптома
    dup_title_req = CardRecordRequest(
        title="Синхронизация SQLite WAL соединений",  # > 0.6 overlap
        category="database",
        type=KnowledgeType.LESSON,
        symptom="Разные процессы падают с исключением таймаута при записи в базу данных",
        root_cause="Конфликт одновременных блокировок таблиц разными процессами операционной системы",
        solution="Добавление busy_timeout=5000 при инициализации подключения к базе данных",
        rationale="Позволяет процессам ожидать освобождения блокировки вместо немедленного сбоя",
    )
    dup_id = storage.check_duplicate(dup_title_req)
    assert dup_id == "database-sqlite-wal-sync"

    # 2. Дубликат с теми же специфичными aliases
    dup_alias_req = CardRecordRequest(
        title="Блокировки базы данных при записи",
        category="database",
        type=KnowledgeType.LESSON,
        symptom="Не удается выполнить запись в файл базы при интенсивной нагрузке",
        root_cause="Превышение лимита времени ожидания снятия блокировки файла базы",
        solution="Конфигурация параметров тайм-аута в строке подключения к базе данных",
        rationale="Предотвращает ошибки конкурентного доступа к общему файлу данных",
        aliases=["sqlite-busy-timeout", "sqlite-wal-concurrency"],
    )
    dup_id2 = storage.check_duplicate(dup_alias_req)
    assert dup_id2 == "database-sqlite-wal-sync"


def test_resolve_hypotheses_confirm_blocks_duplicate_active_cards(tmp_path: Path):
    """Проверка, что resolve_hypotheses(confirm) не создает дубликат существующей активной карточки."""
    k_dir = tmp_path / "k"
    c_db = tmp_path / "c.db"
    storage = KnowledgeStorage(db_path=c_db, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    # 1. Создаем существующую активную карточку
    active_req = CardRecordRequest(
        title="Трансляция аудио через FFmpeg",
        category="audio",
        type=KnowledgeType.LESSON,
        symptom="FFmpeg зависает на 0% в фоне из-за сигнала SIGTTIN",
        root_cause="Ядро Linux посылает сигнал SIGTTIN фоновому процессу, читающему stdin",
        solution="Обязательный флаг -nostdin и stdin=subprocess.DEVNULL при запуске процесса",
        rationale="Исключает блокировку фонового воркера сигналом терминала операционной системы",
        slug="audio-ffmpeg-sigttin",
    )
    service.record(active_req)

    # 2. Создаем гипотезу с тем же симптомом и той же причиной
    hypo_req = HypothesisRecordRequest(
        title="Зависание аудио воркера FFmpeg",
        category="audio",
        symptom="FFmpeg зависает на 0% в фоне из-за сигнала SIGTTIN",
        hypothesis="Ядро Linux посылает сигнал SIGTTIN фоновому процессу, читающему stdin",
        slug="audio-worker-hang",
    )
    service.propose_hypothesis(hypo_req)

    # 3. Попытка подтвердить гипотезу (confirm) должна заблокироваться из-за дубликата
    with pytest.raises(DuplicateCardError) as exc_info:
        service.resolve_hypotheses(
            HypothesesResolveRequest(
                resolutions=[
                    HypothesisResolution(
                        id="audio-worker-hang",
                        action="confirm",
                        solution="Флаг -nostdin и stdin=DEVNULL при создании подпроцесса",
                    )
                ]
            )
        )
    assert "audio-ffmpeg-sigttin" in str(exc_info.value)


def test_category_frontmatter_serialization_and_parsing(tmp_path: Path):
    """Проверка, что карточки сериализуются с category: (без domain:) и парсятся с обоими полями."""
    req = CardRecordRequest(
        title="Тест категории в сериализаторе",
        category="backend",
        type=KnowledgeType.DECISION,
        symptom="Проверка сериализации поля category вместо domain в YAML frontmatter",
        root_cause="Требование чистоты формата данных и унификации терминологии проекта",
        solution="Использование ключа category в frontmatter и автоматическая поддержка обратной совместимости",
        rationale="Исключает путаницу между domain и category для агентов и разработчиков",
    )
    serialized = serialize_card_markdown("backend-cat-test", req)

    # В YAML frontmatter должно быть 'category: backend', а 'domain:' быть не должно
    assert "category: backend" in serialized
    assert "domain:" not in serialized

    # Round-trip парсинг: category доступно на объекте frontmatter
    fm, _ = parse_card_markdown(serialized)
    assert fm.category == "backend"
    assert fm.category == "backend"
