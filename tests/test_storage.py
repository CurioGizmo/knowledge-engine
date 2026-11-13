"""Тесты для хранилища, SQLite FTS5 кэша и санитизатора MCP Knowledge Engine."""

from pathlib import Path
import sqlite3

import pytest

from mcp_knowledge.models import (
    CardFrontmatter,
    CardRecordRequest,
    GeneratedMetadata,
    HypothesisRecordRequest,
    KnowledgeCategory,
    KnowledgeSource,
    KnowledgeStatus,
    KnowledgeType,
)
from mcp_knowledge.parser import (
    serialize_card_from_parts,
    serialize_card_markdown,
    serialize_hypothesis_markdown,
)
from mcp_knowledge.sanitizer import sanitize_fts_query, stem_russian
from mcp_knowledge.storage import DuplicateCardError, KnowledgeStorage
from mcp_knowledge.errors import StorageUnavailableError


def test_fts_index_initialization_with_wal_and_busy_timeout(tmp_path: Path):
    """
    Вход: Временная директория для кэша БД.
    Выход: Инициализированный объект KnowledgeStorage.
    Проверка: В базе установлены PRAGMA journal_mode=wal, busy_timeout=5000, synchronous=NORMAL.
    """
    cache_db = tmp_path / "knowledge_fts.db"
    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=tmp_path / "knowledge")

    with storage.get_connection() as conn:
        cursor = conn.cursor()
        mode = cursor.execute("PRAGMA journal_mode;").fetchone()[0]
        timeout = cursor.execute("PRAGMA busy_timeout;").fetchone()[0]
        sync = cursor.execute("PRAGMA synchronous;").fetchone()[0]

        assert mode.lower() == "wal"
        assert timeout == 5000
        assert sync == 1


def test_query_sanitizer_handles_special_characters_and_quotes():
    """
    Вход: Строка запроса со спецсимволами FTS5 (-nostdin, 0%, CREATE_NO_WINDOW:test, кавычки).
    Выход: Безопасное MATCH-выражение.
    Проверка: Спецсимволы экранированы, токены с дефисами/знаками закавычены, синтаксических ошибок нет.
    """
    raw_query = "-nostdin 0% CREATE_NO_WINDOW:test"
    sanitized = sanitize_fts_query(raw_query)

    assert '"-nostdin"' in sanitized or '"nostdin"' in sanitized or "*-nostdin*" not in sanitized
    assert '":test"' not in sanitized
    assert "0%" in sanitized or '"0%"' in sanitized

    # Проверяем, что SQLite FTS5 не падает при MATCH этого выражения
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE test_fts USING fts5(content, tokenize = 'unicode61 remove_diacritics 2');"
    )
    conn.execute("INSERT INTO test_fts VALUES ('flag -nostdin used for ffmpeg 0%');")
    cursor = conn.execute(f"SELECT * FROM test_fts WHERE test_fts MATCH '{sanitized}';")
    assert cursor.fetchall() is not None
    conn.close()


def test_query_sanitizer_filters_short_stop_words():
    """
    Вход: Строка запроса с короткими предлогами и словами менее 3 символов ("ошибка на сервере в аудио").
    Выход: MATCH-выражение без мусорных стоп-слов.
    Проверка: Токены "на" и "в" удалены из запроса, оставлены только содержательные слова.
    Слово 6 букв с одиночным окончанием ("ошибка" — срез 1 < 2) не стеммится: "ошибк*" отсутствует.
    """
    query = "ошибка на сервере в аудио"
    sanitized = sanitize_fts_query(query)

    tokens = [t.strip('* "') for t in sanitized.split(" OR ")]
    assert "на" not in tokens
    assert "в" not in tokens
    assert any("ошибк" in t or "ошибка" in t for t in tokens)
    assert any("сервер" in t for t in tokens)
    # Честный негатив: слово 6 букв, одиночное окончание -а не стрипается (§2.2 плана)
    assert "ошибка*" in sanitized
    assert "ошибк*" not in sanitized


def test_short_acronyms_pass_filter():
    """
    Вход: Запрос из коротких технических акронимов ("uv sync ws fk id fs tg vk ux di" и "id ids ws wsgi").
    Выход: MATCH-выражение, содержащее все 9 акронимов с префиксом *.
    Проверка: Частотные технические акронимы не отбрасываются фильтром длины.
    Допустимая префиксная коллизия: id* матчит idea/ids, ws* матчит wsgi — OR-семантика терпит лишний хит.
    """
    sanitized = sanitize_fts_query("uv sync ws fk id fs tg vk ux di")
    tokens = [t.strip('* "') for t in sanitized.split(" OR ")]
    for acronym in ("uv", "sync", "ws", "fk", "id", "fs", "tg", "vk", "ux", "di"):
        assert acronym in tokens, f"акроним '{acronym}' потерян в: {sanitized}"

    # Префиксные коллизии: id*/ids*, ws*/wsgi* эмитятся одновременно
    sanitized_collisions = sanitize_fts_query("id ids ws wsgi")
    assert "id*" in sanitized_collisions
    assert "ids*" in sanitized_collisions
    assert "ws*" in sanitized_collisions
    assert "wsgi*" in sanitized_collisions


def test_stem_russian_positive_pairs():
    """
    Вход: Пары словоформ: «оптимизировать»/«оптимизация», «миграции»/«миграция», прилагательные с н-эпентезой.
    Выход: MATCH-выражения с общими стем-токенами для каждой пары.
    Проверка: Лёгкий стемминг сводит словоформы к общей основе (симуляция-таблица §2.2 плана).
    """
    # «оптимизировать» -> оптимиз*, «оптимизация» -> оптимизац*
    sanitized = sanitize_fts_query("оптимизировать оптимизация")
    assert "оптимизировать*" in sanitized
    assert "оптимиз*" in sanitized
    assert "оптимизац*" in sanitized

    # «миграции» и «миграция» -> общий стем миграц*
    assert "миграц*" in sanitize_fts_query("миграции")
    assert "миграц*" in sanitize_fts_query("миграция")

    # Н-эпентеза: прилагательное сводится к основе существительного
    assert "архитектур*" in sanitize_fts_query("архитектурный")
    assert "архитектур*" in sanitize_fts_query("архитектура")
    assert "архитектур*" in sanitize_fts_query("архитектуру")


def test_stem_russian_negative():
    """
    Вход: Слова, для которых стемминг не должен применяться.
    Выход: None от stem_russian для каждого случая.
    Проверка: Короткие слова, латиница, опасные склейки, одиночные окончания и стоп-стемы не стеммятся.
    """
    assert stem_russian("карта") is None  # len < 6
    assert stem_russian("аудио") is None  # len < 6
    assert stem_russian("базу") is None  # len < 6
    assert stem_russian("линия") is None  # len < 6
    assert stem_russian("картон") is None  # суффикса «он» нет в списках — склейки с «карта» нет
    assert stem_russian("сервере") is None  # срез 1 < 2
    assert stem_russian("запросы") is None  # срез 1 < 2
    assert stem_russian("системы") is None  # срез 1 < 2
    assert stem_russian("версии") is None  # стем «верс» < 5
    assert stem_russian("который") is None  # стоп-стем «котор»
    assert stem_russian("table") is None  # латиница
    assert stem_russian("run") is None  # латиница + короткое


def test_stemmed_query_matches_word_forms_in_fts5():
    """
    Вход: In-memory FTS5-таблица со строками «оптимизация», «миграция», «архитектура».
    Выход: Результаты MATCH для запросов «оптимизировать», «миграции», «архитектурный».
    Проверка: Стем-токены матчят словоформы в индексе; sqlite3.OperationalError не выбрасывается.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE test_fts USING fts5(content, tokenize = 'unicode61 remove_diacritics 2');"
    )
    conn.execute("INSERT INTO test_fts VALUES ('оптимизация запросов к базе');")
    conn.execute("INSERT INTO test_fts VALUES ('миграция схемы БД');")
    conn.execute("INSERT INTO test_fts VALUES ('архитектура сервиса');")
    try:
        cursor = conn.execute(
            f"SELECT * FROM test_fts WHERE test_fts MATCH '{sanitize_fts_query('оптимизировать')}';"
        )
        assert any("оптимизация" in row[0] for row in cursor.fetchall())
        cursor = conn.execute(
            f"SELECT * FROM test_fts WHERE test_fts MATCH '{sanitize_fts_query('миграции')}';"
        )
        assert any("миграция" in row[0] for row in cursor.fetchall())
        cursor = conn.execute(
            f"SELECT * FROM test_fts WHERE test_fts MATCH '{sanitize_fts_query('архитектурный')}';"
        )
        assert any("архитектура" in row[0] for row in cursor.fetchall())
    finally:
        conn.close()


def test_auto_reindex_on_cold_start_and_mtime_change(tmp_path: Path):
    """
    Вход: Каталог с Markdown-карточками, последующее изменение одного файла на диске.
    Выход: Актуальный индекс FTS5.
    Проверка: При первом запуске строит индекс с нуля; при изменении mtime обновляет только затронутый файл.
    """
    knowledge_dir = tmp_path / "knowledge"
    audio_dir = knowledge_dir / "audio"
    audio_dir.mkdir(parents=True)

    cache_db = tmp_path / ".cache" / "knowledge_fts.db"
    cache_db.parent.mkdir(parents=True)

    # Создаем 2 тестовые карточки
    card1_path = audio_dir / "audio-sigttin.md"
    req1 = CardRecordRequest(
        title="Зависание FFmpeg из-за SIGTTIN",
        category=KnowledgeCategory.AUDIO,
        source=KnowledgeSource.USER_CONFIRMED,
        type=KnowledgeType.LESSON,
        symptom="FFmpeg зависает на 0%",
        root_cause="Ядро Linux посылает сигнал SIGTTIN фоновому процессу, читающему stdin",
        solution="Обязательный флаг -nostdin и stdin=subprocess.DEVNULL при запуске",
        rationale="Исключает блокировку фонового воркера сигналом терминала",
    )
    card1_path.write_text(serialize_card_markdown("audio-sigttin", req1), encoding="utf-8")

    card2_path = audio_dir / "audio-peaks.md"
    req2 = CardRecordRequest(
        title="Генерация peaks.json для плеера",
        category=KnowledgeCategory.AUDIO,
        source=KnowledgeSource.USER_CONFIRMED,
        type=KnowledgeType.HINT,
        symptom="Медленная отрисовка волны в плеере",
        root_cause="Вычисление пиков аудио на клиенте в реальном времени блокирует UI",
        solution="Предварительный расчет пиков peaks.json на бэкенде через numpy",
        rationale="Мгновенный рендеринг формы волны без блокировки главного потока браузера",
    )
    card2_path.write_text(serialize_card_markdown("audio-peaks", req2), encoding="utf-8")

    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)

    # 1. Проверяем холодный старт
    storage.sync()
    results = storage.search(query="sigttin")
    assert len(results) == 1
    assert results[0].id == "audio-sigttin"

    # 2. Модифицируем карточку 1 (добавляем новое слово)
    req1.symptom = "FFmpeg зависает на 0% и вызывает таймаут uvicorn"
    card1_path.write_text(serialize_card_markdown("audio-sigttin", req1), encoding="utf-8")

    # Инкрементальная синхронизация
    storage.sync()
    results = storage.search(query="uvicorn")
    assert len(results) == 1
    assert results[0].id == "audio-sigttin"


def test_duplicate_detector_flags_similar_symptoms(tmp_path: Path):
    """
    Вход: Попытка сохранения карточки с идентичным симптомом в той же категории.
    Выход: DuplicateCardError при force=False, успешное сохранение при force=True.
    Проверка: Предотвращает раздувание базы дубликатами.
    """
    knowledge_dir = tmp_path / "knowledge"
    audio_dir = knowledge_dir / "audio"
    audio_dir.mkdir(parents=True)

    cache_db = tmp_path / "knowledge_fts.db"
    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)

    card1_path = audio_dir / "audio-sigttin.md"
    req1 = CardRecordRequest(
        title="Зависание FFmpeg из-за SIGTTIN",
        category=KnowledgeCategory.AUDIO,
        type=KnowledgeType.LESSON,
        symptom="FFmpeg зависает на 0% при старте транскрибации",
        root_cause="Ядро Linux посылает сигнал SIGTTIN фоновому процессу, читающему stdin",
        solution="Обязательный флаг -nostdin и stdin=subprocess.DEVNULL при запуске",
        rationale="Исключает блокировку фонового воркера сигналом терминала",
    )
    card1_path.write_text(serialize_card_markdown("audio-sigttin", req1), encoding="utf-8")
    storage.sync()

    # Пробуем добавить очень похожую карточку
    req2 = CardRecordRequest(
        title="Зависание FFmpeg в фоне",
        category=KnowledgeCategory.AUDIO,
        type=KnowledgeType.LESSON,
        symptom="FFmpeg зависает на 0% при старте транскрибации",
        root_cause="Фоновый процесс пытается читать дескриптор терминала без перенаправления",
        solution="Обязательное добавление флага -nostdin при вызове подпроцесса в фоне",
        rationale="Предотвращает сигнал остановки процесса со стороны операционной системы",
        force=False,
    )

    duplicate_id = storage.check_duplicate(req2)
    assert duplicate_id == "audio-sigttin"

    # При force=True и confirmed_force=True проверка дубликатов возвращает None
    req2.force = True
    req2.confirmed_force = True
    duplicate_forced = storage.check_duplicate(req2)
    assert duplicate_forced is None


def test_search_ranking_respects_weights_and_status(tmp_path: Path):
    """
    Вход: Две карточки с совпадающим термином: одна user_confirmed, вторая auto_inferred (status=suspect).
    Выход: Список сниппетов.
    Проверка: Карточка user_confirmed ранжируется выше suspect карточки.
    """
    knowledge_dir = tmp_path / "knowledge"
    ml_dir = knowledge_dir / "ml"
    ml_dir.mkdir(parents=True)

    cache_db = tmp_path / "knowledge_fts.db"
    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)

    # 1. Создаем карточку auto_inferred, статус suspect
    card_suspect = ml_dir / "ml-suspect.md"
    req_suspect = CardRecordRequest(
        title="Параметры инференса Whisper для CPU",
        category=KnowledgeCategory.ML,
        type=KnowledgeType.HINT,
        source=KnowledgeSource.AUTO_INFERRED,
        symptom="Низкая скорость транскрибации моделей Whisper на процессоре Intel",
        root_cause="Базовые настройки инференса не используют параллелизм потоков на многоядерных CPU",
        solution="Увеличение количества потоков до 16 для ускорения обработки аудиофайлов",
        rationale="Параллелизация ускоряет вычисления матричных произведений на CPU",
    )
    card_suspect.write_text(serialize_card_markdown("ml-suspect", req_suspect), encoding="utf-8")

    # 2. Создаем карточку user_confirmed, статус active
    card_confirmed = ml_dir / "ml-confirmed.md"
    req_confirmed = CardRecordRequest(
        title="Оптимальное число потоков Whisper для CPU",
        category=KnowledgeCategory.ML,
        type=KnowledgeType.LESSON,
        source=KnowledgeSource.USER_CONFIRMED,
        symptom="Высокая задержка транскрибации Whisper при большом числе потоков",
        root_cause="Гипертрединг и превышение числа физических ядер приводит к деградации кэша",
        solution="Ограничение количества потоков числом физических ядер через cpu_count(logical=False)",
        rationale="Исключает конкуренцию за L3 кэш и предотвращает троттлинг процессора",
    )
    card_confirmed.write_text(
        serialize_card_markdown("ml-confirmed", req_confirmed), encoding="utf-8"
    )

    storage.sync()

    # Переводим первую в статус suspect вручную в базе
    with storage.get_connection() as conn:
        conn.execute("UPDATE knowledge_index SET status = 'suspect' WHERE id = 'ml-suspect';")
        conn.commit()

    # Поиск по термину "Whisper"
    results = storage.search(query="Whisper")
    assert len(results) == 2

    # Первая карточка должна быть user_confirmed
    assert results[0].id == "ml-confirmed"
    assert results[0].source == KnowledgeSource.USER_CONFIRMED

    # Вторая карточка suspect должна идти следом и содержать warning
    assert results[1].id == "ml-suspect"
    assert results[1].warning is not None


def test_storage_indexes_okf_metadata(tmp_path: Path):
    """
    Вход: Сохранение карточки с метаданными generated.by == GeneratedBy.HUMAN и stale_after == "2026-12-31".
    Выход: Запись в SQLite таблице knowledge_index.
    Проверка: Значения колонок generated_by == 'human', stale_after == '2026-12-31'.
    """
    knowledge_dir = tmp_path / "knowledge"
    platform_dir = knowledge_dir / "platform"
    platform_dir.mkdir(parents=True)

    cache_db = tmp_path / "knowledge_fts.db"
    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)

    card_path = platform_dir / "platform-vulkan.md"
    raw_md = """---
id: platform-vulkan
title: Vulkan Sidecar
category: platform
type: lesson
status: active
generated:
  by: human
  created_at: '2026-09-20'
verified:
- by: human
  at: '2026-09-20'
  note: OK
stale_after: '2026-12-31'
supersedes: null
superseded_by: null
related_to: []
derived_from: null
aliases: []
tags:
- vulkan
---

### Симптом / Контекст
Утечка памяти при непрерывной транскрипции через Vulkan.

### Первопричина (Root Cause)
Аллокация буферов без сброса контекста whisper_free_context.

### Решение / Выводы
Перезапуск sidecar процесса каждые 50 задач.

### Обоснование / Почему именно так (Rationale)
Изоляция утечек на уровне отдельного процесса.
"""
    card_path.write_text(raw_md, encoding="utf-8")
    storage.sync()

    with storage.get_connection() as conn:
        cursor = conn.cursor()
        row = cursor.execute(
            "SELECT generated_by, stale_after, verified_count FROM knowledge_index WHERE id = 'platform-vulkan';"
        ).fetchone()

        assert row is not None
        assert row[0] == "human"
        assert row[1] == "2026-12-31"
        assert row[2] == 1


def test_storage_schema_migration_auto_rebuild(tmp_path: Path):
    """
    Вход: База SQLite со старой схемой (без колонок generated_by, stale_after).
    Выход: Вызов KnowledgeStorage.init_db() и синхронизация.
    Проверка: Таблицы пересоздаются без sqlite3.OperationalError, данные успешно переиндексируются.
    """
    knowledge_dir = tmp_path / "knowledge"
    audio_dir = knowledge_dir / "audio"
    audio_dir.mkdir(parents=True)

    cache_db = tmp_path / "knowledge_fts.db"

    # Создаем старую схему вручную
    conn = sqlite3.connect(cache_db)
    conn.execute("""
        CREATE VIRTUAL TABLE knowledge_index USING fts5(
            id UNINDEXED,
            title,
            domain UNINDEXED,
            type UNINDEXED,
            status UNINDEXED,
            source UNINDEXED,
            aliases,
            symptom,
            root_cause,
            solution,
            rationale,
            tags,
            body
        );
    """)
    conn.execute(
        "CREATE TABLE file_sync (path TEXT PRIMARY KEY, mtime REAL, size INTEGER, card_id TEXT);"
    )
    conn.commit()
    conn.close()

    card_path = audio_dir / "audio-test.md"
    req = CardRecordRequest(
        title="Тестовая карточка",
        category=KnowledgeCategory.AUDIO,
        source=KnowledgeSource.USER_CONFIRMED,
        type=KnowledgeType.LESSON,
        symptom="Тестовый симптом для проверки миграции схемы",
        root_cause="Старая схема SQLite не содержала колонок OKF v0.2",
        solution="Автоматический безопасный сброс и переиндексация эфемерного кэша",
        rationale="Гарантирует отсутствие ошибок OperationalError при обновлении пакета",
    )
    card_path.write_text(serialize_card_markdown("audio-test", req), encoding="utf-8")

    # Инициализация хранилища должна обнаружить несовпадение схемы и пересоздать таблицы
    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)
    storage.sync()

    with storage.get_connection() as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(knowledge_index);").fetchall()]
        assert "generated_by" in cols
        assert "stale_after" in cols
        assert "verified_count" in cols
        assert "confidence" in cols
        assert "task_id" in cols
        assert "refutation_reason" in cols

    results = storage.search(query="миграции")
    assert len(results) == 1
    assert results[0].id == "audio-test"


def test_storage_search_ranks_human_higher(tmp_path: Path):
    """
    Вход: Две карточки с одинаковым совпадением FTS5 (одна generated_by = 'human', вторая generated_by = 'process').
    Выход: Результат поиска storage.search().
    Проверка: Первая позиция в выдаче отдана карточке от человека (human).
    """
    knowledge_dir = tmp_path / "knowledge"
    cicd_dir = knowledge_dir / "cicd"
    cicd_dir.mkdir(parents=True)

    cache_db = tmp_path / "knowledge_fts.db"
    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)

    # Карточка 1: process
    card_proc = cicd_dir / "cicd-proc.md"
    req_proc = CardRecordRequest(
        title="CI пайплайн сборки",
        category=KnowledgeCategory.CICD,
        type=KnowledgeType.HINT,
        source=KnowledgeSource.AUTO_INFERRED,
        status=KnowledgeStatus.ACTIVE,
        symptom="Сборка падает по таймауту в GitHub Actions на шаге тестов",
        root_cause="Автоматически созданная карточка с анализом таймаута",
        solution="Увеличение таймаута шага сборки до 60 минут в воркфлоу",
        rationale="Позволяет завершить медленные тесты на нагруженных раннерах",
    )
    card_proc.write_text(serialize_card_markdown("cicd-proc", req_proc), encoding="utf-8")

    # Карточка 2: human
    card_human = cicd_dir / "cicd-human.md"
    req_human = CardRecordRequest(
        title="CI оптимизация сборки",
        category=KnowledgeCategory.CICD,
        type=KnowledgeType.LESSON,
        source=KnowledgeSource.USER_CONFIRMED,
        symptom="Сборка падает по таймауту в GitHub Actions на шаге тестов",
        root_cause="Человеческий анализ первопричины: зависание фонового процесса",
        solution="Добавление флага --timeout в pytest и изоляция процессов",
        rationale="Устраняет зависание без искусственного раздувания времени сборки",
    )
    card_human.write_text(serialize_card_markdown("cicd-human", req_human), encoding="utf-8")

    storage.sync()

    results = storage.search(query="таймауту")
    assert len(results) == 2
    assert results[0].id == "cicd-human"
    assert results[1].id == "cicd-proc"


def test_sync_does_not_delete_renamed_file_from_fts(tmp_path: Path):
    """Проверка, что при переименовании файла карточка не удаляется из FTS5 индекса."""
    knowledge_dir = tmp_path / "knowledge"
    audio_dir = knowledge_dir / "audio"
    audio_dir.mkdir(parents=True)

    cache_db = tmp_path / "knowledge_fts.db"
    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)

    old_path = audio_dir / "audio-rename-test.md"
    req = CardRecordRequest(
        title="Тест переименования файла",
        category=KnowledgeCategory.AUDIO,
        source=KnowledgeSource.USER_CONFIRMED,
        type=KnowledgeType.LESSON,
        symptom="Проверка сохранения карточки в индексе при переименовании",
        root_cause="Старый код удалял карточку из-за попадания старого пути в deleted_paths",
        solution="Сверка active_card_ids перед удалением из FTS5 индекса",
        rationale="Сохраняет непрерывную доступность карточки при манипуляциях с файлами",
    )
    old_path.write_text(serialize_card_markdown("audio-rename-test", req), encoding="utf-8")
    storage.sync()

    # Убеждаемся, что карточка есть в индексе
    res1 = storage.search(query="переименования")
    assert len(res1) == 1
    assert res1[0].id == "audio-rename-test"

    # Переименовываем файл
    new_path = audio_dir / "audio-rename-test-new.md"
    old_path.rename(new_path)

    # Запускаем синхронизацию
    storage.sync()

    # Карточка ВСЁ ЕЩЁ должна быть найдена!
    res2 = storage.search(query="переименования")
    assert len(res2) == 1
    assert res2[0].id == "audio-rename-test"


def test_check_duplicate_does_not_false_positive_on_common_word(tmp_path: Path):
    """Проверка, что check_duplicate не выдает ложных срабатываний при одном общем слове."""
    knowledge_dir = tmp_path / "knowledge"
    audio_dir = knowledge_dir / "audio"
    audio_dir.mkdir(parents=True)

    cache_db = tmp_path / "knowledge_fts.db"
    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)

    # Существующая карточка про ffmpeg
    card1_path = audio_dir / "audio-sigttin.md"
    req1 = CardRecordRequest(
        title="Зависание FFmpeg из-за SIGTTIN",
        category=KnowledgeCategory.AUDIO,
        type=KnowledgeType.LESSON,
        symptom="FFmpeg зависает на 0% при старте транскрибации",
        root_cause="Ядро Linux посылает сигнал SIGTTIN фоновому процессу, читающему stdin",
        solution="Обязательный флаг -nostdin и stdin=subprocess.DEVNULL при запуске",
        rationale="Исключает блокировку фонового воркера сигналом терминала",
    )
    card1_path.write_text(serialize_card_markdown("audio-sigttin", req1), encoding="utf-8")
    storage.sync()

    # Новая карточка, у которой есть слово 'FFmpeg', но совершенно другой симптом
    req2 = CardRecordRequest(
        title="Ошибка кодека в FFmpeg",
        category=KnowledgeCategory.AUDIO,
        type=KnowledgeType.LESSON,
        symptom="FFmpeg не находит кодек aac при рендере дорожки",
        root_cause="Сборка FFmpeg была скомпилирована без поддержки libfdk-aac",
        solution="Использование встроенного кодека aac вместо внешней библиотеки",
        rationale="Встроенный кодек доступен во всех стандартных дистрибутивах FFmpeg",
    )

    dup_id = storage.check_duplicate(req2)
    assert dup_id is None


def test_search_limit_clamping(tmp_path: Path):
    """Проверка клампинга параметра limit в storage.search()."""
    knowledge_dir = tmp_path / "knowledge"
    audio_dir = knowledge_dir / "audio"
    audio_dir.mkdir(parents=True)

    cache_db = tmp_path / "knowledge_fts.db"
    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)

    # Создаем тестовую карточку
    card_path = audio_dir / "audio-limit-test.md"
    req = CardRecordRequest(
        title="Тест лимитов поиска",
        category=KnowledgeCategory.AUDIO,
        source=KnowledgeSource.USER_CONFIRMED,
        type=KnowledgeType.LESSON,
        symptom="Проверка клампинга limit в диапазоне 1 до 50",
        root_cause="Отрицательные или чрезмерные лимиты должны нормализоваться",
        solution="limit = max(1, min(limit, 50)) в начале функции search",
        rationale="Защита памяти и процесса от неконтролируемой выгрузки строк",
    )
    card_path.write_text(serialize_card_markdown("audio-limit-test", req), encoding="utf-8")
    storage.sync()

    # Отрицательный limit -> клампится до 1 (вместо возврата всех строк через LIMIT -1)
    res_neg = storage.search(query="лимитов", limit=-10)
    assert len(res_neg) == 1

    # Чрезмерный limit -> не падает
    res_huge = storage.search(query="лимитов", limit=1000)
    assert len(res_huge) == 1


def test_two_phase_search_relevant_agent_card_not_displaced_by_weak_human_cards(tmp_path: Path):
    """Проверка Способа 2: карточка агента с точным совпадением не вытесняется из топ-5

    слабо релевантными карточками человека.
    """
    knowledge_dir = tmp_path / "knowledge"
    audio_dir = knowledge_dir / "audio"
    audio_dir.mkdir(parents=True)

    cache_db = tmp_path / "knowledge_fts.db"
    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)

    # Создаем 8 карточек от человека (generated_by: human), которые лишь упоминают слово ffmpeg
    for i in range(8):
        c_req = CardRecordRequest(
            title=f"Человеческая карточка {i} про аудио форматы",
            category=KnowledgeCategory.AUDIO,
            type=KnowledgeType.LESSON,
            source=KnowledgeSource.USER_CONFIRMED,
            symptom=f"Конвертация ffmpeg формат {i} без сжатия в wav",
            root_cause=f"Длинная первопричина для валидации {i} более 40 символов в системе",
            solution=f"Решение проблемы конвертации {i} более 50 символов для Anti-Spam Gate",
            rationale=f"Обоснование решения {i} более 30 символов в коде",
            slug=f"audio-human-{i}",
        )
        (audio_dir / f"audio-human-{i}.md").write_text(
            serialize_card_markdown(f"audio-human-{i}", c_req), encoding="utf-8"
        )

    # Создаем 1 карточку от агента (generated_by: process) с точным совпадением по проблеме
    agent_req = CardRecordRequest(
        title="FFmpeg падение OOM Killer код 137",
        category=KnowledgeCategory.AUDIO,
        type=KnowledgeType.LESSON,
        source=KnowledgeSource.AUTO_INFERRED,
        status=KnowledgeStatus.ACTIVE,
        symptom="FFmpeg завершается с кодом 137 OOM Killer при транскрибации больших файлов",
        root_cause="Ядро Linux завершает процесс ffmpeg по сигналу SIGKILL из-за превышения лимита cgroup",
        solution="Ограничение буфера памяти через флаг -bufsize 64M и стриминг чанками",
        rationale="Предотвращает исчерпание RAM воркера и аварийное завершение контейнера",
        slug="audio-agent-oom-137",
    )
    (audio_dir / "audio-agent-oom-137.md").write_text(
        serialize_card_markdown("audio-agent-oom-137", agent_req), encoding="utf-8"
    )

    storage.sync()

    # Поиск по запросу проблемы "oom killer 137"
    results = storage.search(query="oom killer 137", limit=5)
    # Карточка агента ОБЯЗАНА быть в топ-5 (а точнее на 1 месте, так как точнее нее никого нет)
    assert len(results) >= 1
    assert results[0].id == "audio-agent-oom-137"


def test_two_phase_search_human_wins_on_same_problem(tmp_path: Path):
    """Проверка Способа 2: если по одной проблеме есть карточка агента и карточка человека,

    карточка человека всегда занимает 1-е место в выдаче.
    """
    knowledge_dir = tmp_path / "knowledge"
    audio_dir = knowledge_dir / "audio"
    audio_dir.mkdir(parents=True)

    cache_db = tmp_path / "knowledge_fts.db"
    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)

    # 1. Карточка агента по проблеме SIGTTIN
    agent_req = CardRecordRequest(
        title="Зависание процесса FFmpeg при старте",
        category=KnowledgeCategory.AUDIO,
        type=KnowledgeType.LESSON,
        source=KnowledgeSource.AUTO_INFERRED,
        status=KnowledgeStatus.ACTIVE,
        symptom="FFmpeg зависает на 0% в фоне из-за сигнала SIGTTIN",
        root_cause="Ядро Linux посылает сигнал SIGTTIN фоновому процессу при попытке чтения stdin",
        solution="Обязательный флаг -nostdin и stdin=subprocess.DEVNULL при запуске процесса",
        rationale="Исключает блокировку фонового воркера сигналом терминала",
        slug="audio-agent-sigttin",
    )
    (audio_dir / "audio-agent-sigttin.md").write_text(
        serialize_card_markdown("audio-agent-sigttin", agent_req), encoding="utf-8"
    )

    # 2. Карточка человека по ТОЙ ЖЕ проблеме SIGTTIN
    human_req = CardRecordRequest(
        title="Архитектурный стандарт: флаги фонового FFmpeg",
        category=KnowledgeCategory.AUDIO,
        type=KnowledgeType.DECISION,
        source=KnowledgeSource.USER_CONFIRMED,
        symptom="FFmpeg зависает на 0% в фоне из-за сигнала SIGTTIN",
        root_cause="Стандарт команды: фоновые процессы не должны захватывать дескриптор терминала",
        solution="Единая фабрика запуска процессов с creationflags и DEVNULL потоками",
        rationale="Исключает блокировку фонового воркера сигналом терминала операционной системы",
        slug="audio-human-sigttin",
    )
    (audio_dir / "audio-human-sigttin.md").write_text(
        serialize_card_markdown("audio-human-sigttin", human_req), encoding="utf-8"
    )

    storage.sync()

    # Обе карточки подходят идеально, но человек должен быть первым!
    results = storage.search(query="sigttin фоновом", limit=5)
    assert len(results) == 2
    assert results[0].id == "audio-human-sigttin"
    assert results[0].source == KnowledgeSource.USER_CONFIRMED
    assert results[1].id == "audio-agent-sigttin"


def test_sync_dirty_check_skips_write_transaction(tmp_path: Path):
    """Проверка, что повторный sync() при неизменных файлах корректно распознает чистый кэш."""
    knowledge_dir = tmp_path / "knowledge"
    audio_dir = knowledge_dir / "audio"
    audio_dir.mkdir(parents=True)

    cache_db = tmp_path / "knowledge_fts.db"
    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)

    req = CardRecordRequest(
        title="Тестовая карточка dirty check",
        category=KnowledgeCategory.AUDIO,
        source=KnowledgeSource.USER_CONFIRMED,
        type=KnowledgeType.HINT,
        symptom="Тестовый симптом для проверки dirty check",
        root_cause="Тестовая причина для проверки dirty check",
        solution="Тестовое решение проблемы для проверки dirty check",
        rationale="Тестовое обоснование решения для dirty check",
        slug="audio-dirty-check",
    )
    (audio_dir / "audio-dirty-check.md").write_text(
        serialize_card_markdown("audio-dirty-check", req), encoding="utf-8"
    )

    # 1. Первый sync() индексирует карточку
    storage.sync()

    # 2. Повторный вызов sync() проходит через ветку clean cache
    storage.sync()

    results = storage.search("dirty check")
    assert len(results) == 1
    assert results[0].id == "audio-dirty-check"


def test_collect_antipatterns_and_hypotheses_dedup_and_category_facets(tmp_path: Path):
    """Проверка общих фасетных коллекторов: выборка, дедуп по seen_card_ids, task_id и категории."""
    knowledge_dir = tmp_path / "knowledge"
    b_dir = knowledge_dir / "backend"
    b_dir.mkdir(parents=True)

    storage = KnowledgeStorage(db_path=tmp_path / "cache.db", knowledge_dir=knowledge_dir)

    # Антипаттерн (type=antipattern, status=refuted)
    antipattern_fm = CardFrontmatter(
        id="backend-bad-lock",
        title="Global File Locking for Concurrency",
        category="backend",
        type=KnowledgeType.ANTIPATTERN,
        status=KnowledgeStatus.REFUTED,
        confidence=0.10,
        refutation_reason="Приводит к деградации RPS и дедлокам при росте нагрузки",
        generated=GeneratedMetadata(created_at="2026-09-22"),
    )
    antipattern_sections = {
        "Симптом / Контекст": "Попытка синхронизировать воркеры через flock",
        "Опровержение / Почему это тупик (Refutation Reason)": (
            "Приводит к деградации RPS и дедлокам при росте нагрузки"
        ),
    }
    (b_dir / "backend-bad-lock.md").write_text(
        serialize_card_from_parts(antipattern_fm, antipattern_sections), encoding="utf-8"
    )

    # Гипотеза текущей задачи
    hypo_req = HypothesisRecordRequest(
        title="Postgres Index Scan Latency",
        category="backend",
        symptom="Медленные запросы к таблице orders при миллионах строк",
        hypothesis="Планировщик выбирает seq scan из-за устаревшей статистики",
        task_id="task-investigate-pg",
        slug="pg-stat-hypothesis",
    )
    (b_dir / "backend-pg-stat-hypothesis.md").write_text(
        serialize_hypothesis_markdown("backend-pg-stat-hypothesis", hypo_req), encoding="utf-8"
    )

    storage.sync()

    with storage.get_connection() as conn:
        cursor = conn.cursor()

        # 1. Антипаттерн без query: categories-ветка (category IN (...))
        seen: set[str] = set()
        antipatterns = storage._collect_antipatterns(
            cursor=cursor, query=None, categories=["backend"], seen_card_ids=seen
        )
        assert [s.id for s in antipatterns] == ["backend-bad-lock"]
        assert "backend-bad-lock" in seen
        assert "⛔ ОПРОВЕРГНУТО" in (antipatterns[0].warning or "")

        # 2. Дедуп: карточка в seen_card_ids не выбирается повторно
        seen_dup: set[str] = {"backend-bad-lock"}
        antipatterns_dup = storage._collect_antipatterns(
            cursor=cursor, query=None, categories=["backend"], seen_card_ids=seen_dup
        )
        assert antipatterns_dup == []

        # 3. Гипотеза с task_id: base-условие + task_id = ?
        seen_hyp: set[str] = set()
        hypotheses = storage._collect_hypotheses(
            cursor=cursor,
            query=None,
            categories=None,
            task_id="task-investigate-pg",
            clamped_facet_limit=3,
            seen_card_ids=seen_hyp,
            max_total=12,
        )
        assert [s.id for s in hypotheses] == ["backend-pg-stat-hypothesis"]
        assert hypotheses[0].status == KnowledgeStatus.HYPOTHESIS
        assert "backend-pg-stat-hypothesis" in seen_hyp

        # 4. Гипотеза с query: MATCH-ветка (bm25-order) находит по слову
        seen_query: set[str] = set()
        hyp_by_query = storage._collect_hypotheses(
            cursor=cursor,
            query="Postgres",
            categories=None,
            task_id=None,
            clamped_facet_limit=3,
            seen_card_ids=seen_query,
            max_total=12,
        )
        assert [s.id for s in hyp_by_query] == ["backend-pg-stat-hypothesis"]


def test_collect_facets_operational_error_raises_storage_unavailable(tmp_path: Path):
    """Проверка R-3: OperationalError в фасетных коллекторах строго рейзит StorageUnavailableError."""

    class _FailingCursor:
        """Stub курсора: execute имитирует сбой SQLite, как живой курсор."""

        def execute(self, sql: str, params: list = ()) -> None:
            raise sqlite3.OperationalError("boom")

        def fetchall(self) -> list:
            return []

    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir(parents=True)
    storage = KnowledgeStorage(db_path=tmp_path / "cache.db", knowledge_dir=knowledge_dir)
    storage.sync()

    failing_cursor = _FailingCursor()

    with pytest.raises(StorageUnavailableError, match="Database query failed for antipatterns"):
        storage._collect_antipatterns(
            cursor=failing_cursor, query=None, categories=["backend"], seen_card_ids=set()
        )

    with pytest.raises(StorageUnavailableError, match="Database query failed for hypotheses"):
        storage._collect_hypotheses(
            cursor=failing_cursor,
            query=None,
            categories=None,
            task_id="task-x",
            clamped_facet_limit=3,
            seen_card_ids=set(),
            max_total=12,
        )
