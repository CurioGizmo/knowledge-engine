"""Тесты для проверки исправлений (11 замечаний peer review)."""

from datetime import date
import json
from pathlib import Path
import sqlite3
from mcp.types import CallToolResult, TextContent
import pytest

from mcp_knowledge.models import (
    CardFrontmatter,
    CardRecordRequest,
    GeneratedBy,
    GeneratedMetadata,
    KnowledgeCategory,
    KnowledgeSource,
    KnowledgeStatus,
    KnowledgeType,
    VerificationRecord,
)
from mcp_knowledge.parser import (
    generate_card_slug,
    parse_card_markdown,
    serialize_card_from_parts,
    serialize_card_markdown,
)
from mcp_knowledge.sanitizer import sanitize_fts_query
from mcp_knowledge.server import create_server
from mcp_knowledge.service import KnowledgeService, _atomic_write_text
from mcp_knowledge.storage import DuplicateCardError, KnowledgeStorage


def test_sanitizer_reserved_fts5_keywords():
    """1. Проверка безопасной санитизации зарезервированных операторов FTS5 (AND, OR, NOT, NEAR)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE fts USING fts5(content, tokenize = 'unicode61 remove_diacritics 2');"
    )
    conn.execute("INSERT INTO fts VALUES ('whisper and ffmpeg background worker crash');")

    queries = [
        "AND",
        "NOT",
        "OR",
        "NEAR",
        "whisper AND ffmpeg",
        "NOT crash",
        "worker OR failure",
        "audio NEAR stream",
    ]

    for q in queries:
        sanitized = sanitize_fts_query(q)
        # Убеждаемся, что нет незакавыченных операторов с префиксной звёздочкой
        assert "AND*" not in sanitized
        assert "NOT*" not in sanitized
        assert "OR*" not in sanitized
        assert "NEAR*" not in sanitized

        # Запрос в FTS5 не должен выбрасывать sqlite3.OperationalError
        cur = conn.execute(f"SELECT * FROM fts WHERE fts MATCH '{sanitized}';")
        rows = cur.fetchall()
        assert rows is not None

    conn.close()


def test_unquoted_yaml_dates_coercion():
    """2. Проверка автоматического приведения неэкранированных YAML дат (datetime.date) к str."""
    raw_md = """---
id: platform-unquoted-date
title: Test Unquoted Date
category: platform
type: lesson
status: active
generated:
  by: human
  created_at: 2026-09-20
verified:
  - by: human
    at: 2026-09-20
    note: OK
stale_after: 2027-01-01
---

### Симптом / Контекст
Симптом с неэкранированными датами в YAML.

### Первопричина (Root Cause)
Парсер YAML преобразует даты в datetime.date объекты.

### Решение / Выводы
Валидатор Pydantic преобразует date в строку ISO 8601.

### Обоснование / Почему именно так (Rationale)
Строгая типизация без падений при ручном редактировании.
"""
    fm, sections = parse_card_markdown(raw_md)
    assert isinstance(fm.generated.created_at, str)
    assert fm.generated.created_at == "2026-09-20"
    assert len(fm.verified) == 1
    assert isinstance(fm.verified[0].at, str)
    assert fm.verified[0].at == "2026-09-20"
    assert isinstance(fm.stale_after, str)
    assert fm.stale_after == "2027-01-01"

    # Прямая передача date в модели
    d = date(2026, 9, 20)
    meta = GeneratedMetadata(by=GeneratedBy.HUMAN, created_at=d)
    assert isinstance(meta.created_at, str)
    assert meta.created_at == "2026-09-20"

    vrec = VerificationRecord(by="human", at=d)
    assert isinstance(vrec.at, str)
    assert vrec.at == "2026-09-20"


def test_frontmatter_split_with_dashes():
    """3. Проверка парсинга frontmatter при наличии '---' в заголовке, комментариях и теле."""
    raw_md = """---
id: audio-dashes-test
title: "DirectML --- Fallback Pipeline"
category: audio
type: lesson
status: active
generated:
  by: process
  created_at: '2026-09-20'
verified: []
stale_after: null
# --- Comment with three dashes ---
aliases: []
tags: []
---

### Симптом / Контекст
Симптом с дефисами.

---

### Первопричина (Root Cause)
Текст первопричины длиной более 40 символов для валидации.

### Решение / Выводы
Текст законченного технического решения длиной более 50 символов.

### Обоснование / Почему именно так (Rationale)
Обоснование правильности решения более 30 символов.
"""
    fm, sections = parse_card_markdown(raw_md)
    assert fm.id == "audio-dashes-test"
    assert fm.title == "DirectML --- Fallback Pipeline"
    assert "Симптом / Контекст" in sections
    assert "---" in sections["Симптом / Контекст"]


def test_check_duplicate_on_cold_start(tmp_path: Path):
    """4. Проверка детекции дубликатов на холодном старте (до первого поиска)."""
    knowledge_dir = tmp_path / "knowledge"
    audio_dir = knowledge_dir / "audio"
    audio_dir.mkdir(parents=True)
    cache_db = tmp_path / ".cache" / "fts.db"
    cache_db.parent.mkdir(parents=True)

    # 1. Создаем существующую карточку на диске
    req1 = CardRecordRequest(
        title="Зависание FFmpeg в фоне",
        category=KnowledgeCategory.AUDIO,
        type=KnowledgeType.LESSON,
        symptom="FFmpeg зависает на 0% при старте транскрибации",
        root_cause="Ядро Linux посылает сигнал SIGTTIN фоновому процессу, читающему stdin",
        solution="Обязательный флаг -nostdin и stdin=subprocess.DEVNULL при запуске процесса",
        rationale="Исключает блокировку фонового воркера сигналом терминала",
        slug="audio-sigttin-cold",
    )
    (audio_dir / "audio-sigttin-cold.md").write_text(
        serialize_card_markdown("audio-sigttin-cold", req1), encoding="utf-8"
    )

    # 2. Инициализируем хранилище и сервис БЕЗ предварительного вызова sync()
    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=knowledge_dir)

    # 3. Попытка создать дубликат должна обнаружить существующую карточку
    req2 = CardRecordRequest(
        title="Новая карточка с тем же симптомом",
        category=KnowledgeCategory.AUDIO,
        type=KnowledgeType.LESSON,
        symptom="FFmpeg зависает на 0% при старте транскрибации",
        root_cause="Фоновый процесс пытается читать дескриптор терминала без перенаправления",
        solution="Обязательное добавление флага -nostdin при вызове подпроцесса в фоне",
        rationale="Предотвращает сигнал остановки процесса со стороны операционной системы",
        force=False,
    )
    with pytest.raises(DuplicateCardError):
        service.record(req2)


@pytest.mark.asyncio
async def test_server_update_status_default_agent_by(tmp_path: Path):
    """5. Проверка назначения by='agent' при MCP update_status от auto_inferred."""
    k_dir = tmp_path / "k"
    c_db = tmp_path / "c.db"
    storage = KnowledgeStorage(db_path=c_db, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)
    server = create_server(service=service)

    # Создаем активную карточку процесса напрямую на диске
    (k_dir / "audio").mkdir(parents=True, exist_ok=True)
    card_id = "audio-by-test"
    raw_md = """---
id: audio-by-test
title: Тест авторства верификации
category: audio
type: lesson
status: active
generated:
  by: process
  created_at: '2026-10-30'
---
# Тест авторства верификации

## Симптом
Проверка авторства верификации через MCP

## Первопричина
Агент должен маркироваться как agent

## Решение
Автоматическое определение actual_by

## Обоснование
Сохранение целостности
"""
    (k_dir / "audio" / f"{card_id}.md").write_text(raw_md, encoding="utf-8")
    storage.sync()

    # Вызов verify от auto_inferred без передачи by
    upd_res = await server.call_tool(
        "update_knowledge_status",
        {
            "id": card_id,
            "action": "verify",
            "note": "Верифицировано автоматическим процессом",
            "source": "auto_inferred",
        },
    )
    assert isinstance(upd_res, CallToolResult)
    assert not upd_res.is_error

    card_path = k_dir / "audio" / f"{card_id}.md"
    fm, _ = parse_card_markdown(card_path.read_text(encoding="utf-8"))
    assert len(fm.verified) == 1
    assert fm.verified[0].by == "agent"


@pytest.mark.asyncio
async def test_server_case_insensitive_action_and_scalar_coercion(tmp_path: Path):
    """6. Проверка устойчивости MCP сервера к регистру action и передаче скалярных строк."""
    k_dir = tmp_path / "k"
    c_db = tmp_path / "c.db"
    storage = KnowledgeStorage(db_path=c_db, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)
    server = create_server(service=service)

    # 1. record_knowledge с aliases и tags в виде строки через запятую
    rec_res = await server.call_tool(
        "record_knowledge",
        {
            "title": "Тест устойчивости сервера к типам",
            "category": "ml",
            "type": "hint",
            "symptom": "Проверка обработки скалярных строк вместо списков",
            "root_cause": "LLM часто передает строку с запятыми вместо массива строк",
            "solution": "Вспомогательная функция _normalize_str_list в server.py",
            "rationale": "Исключает ошибки валидации ValidationError при вызовах от LLM",
            "aliases": "sigttin, freeze, hang",
            "tags": "ffmpeg, audio",
            "slug": "ml-scalar-test",
            # K2: карточка auto_inferred создаётся в draft — VERIFY недоступен
            # до promote; для теста не-регистрочувствительности создадим active
            "source": "user_confirmed",
        },
    )
    assert isinstance(rec_res, CallToolResult)
    assert not rec_res.is_error

    card_path = k_dir / "ml" / "ml-scalar-test.md"
    fm, _ = parse_card_markdown(card_path.read_text(encoding="utf-8"))
    assert "sigttin" in fm.aliases
    assert "freeze" in fm.aliases
    assert "ffmpeg" in fm.tags

    # 2. get_knowledge с единичной строкой вместо списка
    get_res = await server.call_tool("get_knowledge", {"ids": "ml-scalar-test"})
    assert isinstance(get_res, CallToolResult)
    assert not get_res.is_error
    if get_res.structured_content:
        data = get_res.structured_content
    else:
        first_content = get_res.content[0]
        assert isinstance(first_content, TextContent)
        data = json.loads(first_content.text)
    assert len(data["cards"]) == 1
    assert data["cards"][0]["id"] == "ml-scalar-test"

    # 3. update_knowledge_status с регистром ' VERIFY ' от человека
    upd_res = await server.call_tool(
        "update_knowledge_status",
        {
            "id": "ml-scalar-test",
            "action": " VERIFY ",
            "by": "human",
            "note": "Успешно верифицировано с заглавным регистром",
        },
    )
    assert isinstance(upd_res, CallToolResult)
    assert not upd_res.is_error


def test_storage_indexes_english_sections(tmp_path: Path):
    """7. Проверка индексации карточек с англоязычными заголовками секций."""
    knowledge_dir = tmp_path / "knowledge"
    cicd_dir = knowledge_dir / "cicd"
    cicd_dir.mkdir(parents=True)
    cache_db = tmp_path / "fts.db"

    raw_md = """---
id: cicd-docker-cache
title: Docker Layer Caching in CI
category: cicd
type: lesson
status: active
generated:
  by: process
  created_at: '2026-09-20'
verified: []
stale_after: null
aliases: []
tags: []
---

### Symptom / Context
GitHub Actions builds take over 30 minutes without caching.

### Root Cause
Docker layers are rebuilt from scratch on every runner invocation.

### Solution
Use docker/build-push-action with type=gha cache exporter and importer.

### Rationale
Dramatically reduces build times by reusing unmodified container layers.
"""
    (cicd_dir / "cicd-docker-cache.md").write_text(raw_md, encoding="utf-8")

    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)
    storage.sync()

    results = storage.search(query="caching")
    assert len(results) == 1
    assert results[0].id == "cicd-docker-cache"
    # Сниппет должен содержать текст англоязычного симптома
    assert "GitHub Actions builds" in results[0].snippet


def test_custom_frontmatter_fields_preserved():
    """8. Проверка сохранения кастомных полей YAML frontmatter при round-trip."""
    raw_md = """---
id: platform-custom-meta
title: Custom Metadata Card
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
tags: []
custom_owner: security-team
priority: critical
---

### Симптом / Контекст
Симптом с кастомными метаданными.

### Первопричина (Root Cause)
Необходимость сохранения произвольных полей во frontmatter.

### Решение / Выводы
ConfigDict(extra="allow") в модели CardFrontmatter.

### Обоснование / Почему именно так (Rationale)
Предотвращает потерю сторонних метаданных при перезаписи карточек.
"""
    fm, sections = parse_card_markdown(raw_md)
    assert getattr(fm, "custom_owner", None) == "security-team"
    assert getattr(fm, "priority", None) == "critical"

    serialized = serialize_card_from_parts(fm, sections)
    assert "custom_owner: security-team" in serialized
    assert "priority: critical" in serialized


def test_extended_categories_lifecycle(tmp_path: Path):
    """9. Проверка работы новых категорий (backend, frontend, security, general)."""
    k_dir = tmp_path / "k"
    c_db = tmp_path / "c.db"
    storage = KnowledgeStorage(db_path=c_db, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    categories = [
        KnowledgeCategory.BACKEND,
        KnowledgeCategory.FRONTEND,
        KnowledgeCategory.SECURITY,
        KnowledgeCategory.GENERAL,
    ]

    for cat in categories:
        req = CardRecordRequest(
            title=f"Тест категории {cat.value}",
            category=cat,
            source=KnowledgeSource.USER_CONFIRMED,
            type=KnowledgeType.DECISION,
            symptom=f"Симптом для категории {cat.value} длиной более 10 символов",
            root_cause=f"Первопричина для категории {cat.value} длиной более 40 символов для валидации",
            solution=f"Решение проблемы для категории {cat.value} длиной более 50 символов для Anti-Spam Gate",
            rationale=f"Обоснование решения для категории {cat.value} более 30 символов",
            slug=f"{cat.value}-test-card",
        )
        resp = service.record(req)
        assert resp["status"] == "created"
        assert Path(resp["path"]).exists()
        assert resp["path"].endswith(f"{cat.value}/{cat.value}-test-card.md")

    # Поиск по новой категории
    sec_results = service.search(query="security", category=KnowledgeCategory.SECURITY)
    assert len(sec_results) == 1
    assert sec_results[0].category == KnowledgeCategory.SECURITY


def test_atomic_write_preserves_content(tmp_path: Path):
    """10. Проверка атомарной записи _atomic_write_text без оставления мусорных файлов."""
    target_file = tmp_path / "nested" / "test.md"
    _atomic_write_text(target_file, "Hello atomic world!")

    assert target_file.exists()
    assert target_file.read_text(encoding="utf-8") == "Hello atomic world!"

    # Проверяем отсутствие временных файлов
    tmp_files = list(tmp_path.rglob("*.tmp"))
    assert len(tmp_files) == 0


def test_find_card_file_sqlite_index(tmp_path: Path):
    """11. Проверка трехуровневого поиска карточек через SQLite file_sync индекс."""
    k_dir = tmp_path / "k"
    c_db = tmp_path / "c.db"
    storage = KnowledgeStorage(db_path=c_db, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    # 1. Записываем карточку в глубокую подпапку
    deep_path = k_dir / "platform" / "deep" / "sub" / "platform-deep-card.md"
    req = CardRecordRequest(
        title="Deep Card",
        category=KnowledgeCategory.PLATFORM,
        type=KnowledgeType.LESSON,
        symptom="Тестовый симптом глубокой карточки",
        root_cause="Первопричина глубокой карточки длиной более 40 символов",
        solution="Решение проблемы глубокой карточки длиной более 50 символов",
        rationale="Обоснование решения глубокой карточки более 30 символов",
        slug="platform-deep-card",
    )
    _atomic_write_text(deep_path, serialize_card_markdown("platform-deep-card", req))
    storage.sync()

    # Поиск должен найти через file_sync индекс без rglob
    found = service._find_card_file("platform-deep-card")
    assert found is not None
    assert found.resolve() == deep_path.resolve()


def test_slug_fallback_on_stripped_custom_slug():
    """12. Проверка, что при custom_slug из одних спецсимволов генератор слага откатывается на title."""
    slug = generate_card_slug(
        category=KnowledgeCategory.AUDIO,
        title="FFmpeg Output Processing",
        custom_slug="---$$$---",
    )
    assert slug == "audio-ffmpeg-output-processing"
