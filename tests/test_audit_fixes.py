"""Тесты для проверки исправлений по результатам аудита кодовой базы."""

from pathlib import Path
import pytest
from mcp.types import CallToolResult
from pydantic import ValidationError

from mcp_knowledge.models import (
    CardRecordRequest,
    CardStatusUpdateRequest,
    KnowledgeCategory,
    KnowledgeSource,
    KnowledgeType,
)
from mcp_knowledge.parser import parse_card_markdown, serialize_card_markdown
from mcp_knowledge.sanitizer import sanitize_fts_query
from mcp_knowledge.server import create_server
from mcp_knowledge.service import KnowledgeService
from mcp_knowledge.storage import KnowledgeStorage


def test_path_traversal_blocked_in_find_card_file(tmp_path: Path):
    """1. Проверка блокировки Path Traversal при передаче .. или спецсимволов в card_id."""
    k_dir = tmp_path / "knowledge"
    k_dir.mkdir(parents=True)
    c_db = tmp_path / "fts.db"
    storage = KnowledgeStorage(db_path=c_db, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    # Создаем внешний файл за пределами knowledge_dir
    outside_file = tmp_path / "secret.md"
    outside_file.write_text("secret content", encoding="utf-8")

    # Попытки Path Traversal должны возвращать None
    assert service._find_card_file("..-secret") is None
    assert service._find_card_file("../secret") is None
    assert service._find_card_file("../../secret") is None
    assert service._find_card_file("/etc/passwd") is None
    assert service._find_card_file("invalid/slug") is None


def test_anti_spam_gate_rejects_whitespace_only():
    """2. Проверка, что строки из одних пробелов отклоняются Anti-Spam Gate."""
    with pytest.raises(ValidationError):
        CardRecordRequest(
            title="   ",
            category=KnowledgeCategory.AUDIO,
            type=KnowledgeType.LESSON,
            symptom="Тестовый симптом длиной более 10",
            root_cause="Первопричина длиной более 40 символов для валидации поля",
            solution="Решение проблемы длиной более 50 символов для Anti-Spam Gate",
            rationale="Обоснование правильности решения более 30 символов",
        )

    with pytest.raises(ValidationError):
        CardRecordRequest(
            title="Валидный заголовок",
            category=KnowledgeCategory.AUDIO,
            type=KnowledgeType.LESSON,
            symptom="   " * 5,
            root_cause="Первопричина длиной более 40 символов для валидации поля",
            solution="Решение проблемы длиной более 50 символов для Anti-Spam Gate",
            rationale="Обоснование правильности решения более 30 символов",
        )

    with pytest.raises(ValidationError):
        CardRecordRequest(
            title="Валидный заголовок",
            category=KnowledgeCategory.AUDIO,
            type=KnowledgeType.LESSON,
            symptom="Тестовый симптом длиной более 10",
            root_cause="   " * 15,
            solution="Решение проблемы длиной более 50 символов для Anti-Spam Gate",
            rationale="Обоснование правильности решения более 30 символов",
        )


def test_markdown_parser_inline_code_block_does_not_swallow_headers():
    """3. Проверка, что инлайн-код ```foo``` на одной строке не залипает как открытый блок."""
    raw_md = """---
id: audio-inline-code
title: Тест инлайн кода
category: audio
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

### Симптом / Контекст
```code inline on single line```

### Первопричина (Root Cause)
Текст первопричины с описанием сбоя более сорока символов.

### Решение / Выводы
Текст законченного решения проблемы более пятидесяти символов.

### Обоснование / Почему именно так (Rationale)
Обоснование правильности решения более тридцати символов.
"""
    fm, sections = parse_card_markdown(raw_md)
    assert "Симптом / Контекст" in sections
    assert "Первопричина (Root Cause)" in sections
    assert "Решение / Выводы" in sections
    assert "Обоснование / Почему именно так (Rationale)" in sections
    assert "более сорока символов" in sections["Первопричина (Root Cause)"]


def test_markdown_parser_supports_h2_headers():
    """4. Проверка распознавания заголовков H2 (##) наряду с H3 (###)."""
    raw_md = """---
id: audio-h2-test
title: Тест H2 заголовков
category: audio
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

## Симптом / Контекст
Симптом в секции с H2 заголовком.

## Первопричина (Root Cause)
Первопричина в секции с H2 заголовком более 40 символов.

## Решение / Выводы
Решение проблемы в секции с H2 заголовком более 50 символов.

## Обоснование / Почему именно так (Rationale)
Обоснование решения в секции с H2 заголовком более 30 символов.
"""
    fm, sections = parse_card_markdown(raw_md)
    assert "Симптом / Контекст" in sections
    assert "Первопричина (Root Cause)" in sections
    assert "Решение / Выводы" in sections
    assert "Обоснование / Почему именно так (Rationale)" in sections


def test_sanitizer_dot_expressions_and_new_acronyms():
    """5. Проверка санитизации запросов с точками и двухбуквенных акронимов."""
    # Точки не должны склеиваться
    query_dot = "sqlite3.OperationalError"
    sanitized_dot = sanitize_fts_query(query_dot)
    assert "sqlite3OperationalError" not in sanitized_dot
    assert "sqlite3*" in sanitized_dot
    assert "OperationalError*" in sanitized_dot

    # Акронимы GO (верхний регистр), js, ts, бд, дб (консонантные без словарей) не должны отбрасываться
    query_acronyms = "GO channel js event ts types бд дб sqlite"
    sanitized_acronyms = sanitize_fts_query(query_acronyms)
    tokens = [t.strip('* "').lower() for t in sanitized_acronyms.split(" OR ")]
    assert "go" in tokens
    assert "js" in tokens
    assert "ts" in tokens
    assert "бд" in tokens
    assert "дб" in tokens


def test_fuzzy_duplicate_detection_threshold(tmp_path: Path):
    """6. Проверка, что нечеткие дубликаты корректно ловятся без завязки на жесткий score."""
    k_dir = tmp_path / "k"
    c_db = tmp_path / "c.db"
    (k_dir / "audio").mkdir(parents=True)
    storage = KnowledgeStorage(db_path=c_db, knowledge_dir=k_dir)

    # 1. Создаем существующую карточку
    req1 = CardRecordRequest(
        title="Зависание FFmpeg в фоне",
        category=KnowledgeCategory.AUDIO,
        type=KnowledgeType.LESSON,
        symptom="FFmpeg зависает на нуле процентов при транскрибации аудиофайлов",
        root_cause="Ядро Linux посылает сигнал SIGTTIN фоновому процессу, читающему stdin",
        solution="Обязательный флаг -nostdin и stdin=subprocess.DEVNULL при запуске процесса",
        rationale="Исключает блокировку фонового воркера сигналом терминала",
        slug="audio-sigttin-fuzzy",
    )
    (k_dir / "audio" / "audio-sigttin-fuzzy.md").write_text(
        serialize_card_markdown("audio-sigttin-fuzzy", req1), encoding="utf-8"
    )
    storage.sync()

    # 2. Новая карточка с 80% совпадающих слов в симптоме (но не идентичная)
    req2 = CardRecordRequest(
        title="FFmpeg зависает при транскрибации",
        category=KnowledgeCategory.AUDIO,
        type=KnowledgeType.LESSON,
        symptom="FFmpeg зависает на нуле процентов при обработке аудиофайлов",
        root_cause="Фоновый процесс ожидает ввода из терминального дескриптора stdin",
        solution="Перенаправление потока ввода subprocess.DEVNULL при старте",
        rationale="Предотвращает зависание процесса подпроцесса",
    )

    dup_id = storage.check_duplicate(req2)
    assert dup_id == "audio-sigttin-fuzzy"


@pytest.mark.asyncio
async def test_case_insensitive_enums_and_agent_field_in_server(tmp_path: Path):
    """7. Проверка нормализации регистра Enum (Audio, Lesson) и сохранения поля agent."""
    k_dir = tmp_path / "k"
    c_db = tmp_path / "c.db"
    storage = KnowledgeStorage(db_path=c_db, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)
    server = create_server(service=service)

    # Регистрация с заглавным регистром category и type, плюс указание agent
    rec_res = await server.call_tool(
        "record_knowledge",
        {
            "title": "Тест регистра и агента",
            "category": "Audio",
            "type": "Lesson",
            "source": "auto_inferred",
            "agent": "antigravity-code",
            "symptom": "Проверка нечувствительности к регистру и сохранения агента",
            "root_cause": "Агенты часто присылают имена типов с заглавной буквы",
            "solution": "Автоматическая нормализация входных параметров к нижнему регистру",
            "rationale": "Исключает ошибки валидации ValidationError при вызовах от LLM",
            "slug": "audio-case-test",
        },
    )
    assert isinstance(rec_res, CallToolResult)
    assert not rec_res.is_error

    card_path = k_dir / "audio" / "audio-case-test.md"
    fm, _ = parse_card_markdown(card_path.read_text(encoding="utf-8"))
    assert fm.category == KnowledgeCategory.AUDIO
    assert fm.type == KnowledgeType.LESSON
    assert fm.generated.agent == "antigravity-code"

    # Поиск с регистром "AUDIO"
    search_res = await server.call_tool(
        "search_knowledge", {"query": "регистра", "categories": ["AUDIO"]}
    )
    assert isinstance(search_res, CallToolResult)
    assert not search_res.is_error


def test_get_knowledge_deduplicates_ids(tmp_path: Path):
    """8. Проверка дедупликации переданных идентификаторов в get()."""
    k_dir = tmp_path / "k"
    c_db = tmp_path / "c.db"
    storage = KnowledgeStorage(db_path=c_db, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    req = CardRecordRequest(
        title="Тест дедупликации",
        category=KnowledgeCategory.AUDIO,
        type=KnowledgeType.HINT,
        symptom="Проверка дедупликации идентификаторов при вызове get",
        root_cause="Повторяющиеся ID в запросе не должны дублировать карточки в ответе",
        solution="Применяем list(dict.fromkeys(ids)) непосредственно перед обходом файлов карточек",
        rationale="Исключает лишние аллокации памяти и дублирование ответов",
        slug="audio-dedup-test",
    )
    service.record(req)

    resp = service.get(["audio-dedup-test", "audio-dedup-test", "audio-dedup-test"])
    assert len(resp.cards) == 1
    assert resp.cards[0].id == "audio-dedup-test"


def test_related_to_broken_file_resilience(tmp_path: Path):
    """9. Проверка устойчивости к поврежденным связанным карточкам."""
    k_dir = tmp_path / "k"
    c_db = tmp_path / "c.db"
    (k_dir / "audio").mkdir(parents=True)
    storage = KnowledgeStorage(db_path=c_db, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    # Создаем поврежденную карточку
    broken_file = k_dir / "audio" / "audio-corrupted.md"
    broken_file.write_text("corrupted content without frontmatter", encoding="utf-8")

    # Создание новой карточки со ссылкой на поврежденную должно пройти успешно
    req = CardRecordRequest(
        title="Устойчивость связывания",
        category=KnowledgeCategory.AUDIO,
        type=KnowledgeType.LESSON,
        symptom="Проверка изоляции ошибок при связывании карточек знаний",
        root_cause="Сбой одной ссылки не должен срывать создание новой карточки знаний",
        solution="Оборачивание цикла обновления related_to в блок try/except",
        rationale="Гарантирует надежность сохранения знаний агента в любых условиях",
        related_to=["audio-corrupted"],
        slug="audio-resilient",
    )
    resp = service.record(req)
    assert resp["status"] == "created"
    assert (k_dir / "audio" / "audio-resilient.md").exists()


def test_verify_action_omits_trivial_notes_from_nuances_section(tmp_path: Path):
    """10. Проверка, что тривиальные заметки верификации (OK, verified) не засоряют секцию нюансов."""
    k_dir = tmp_path / "k"
    c_db = tmp_path / "c.db"
    (k_dir / "audio").mkdir(parents=True)
    storage = KnowledgeStorage(db_path=c_db, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)

    req = CardRecordRequest(
        title="Тест фильтрации тривиальных заметок",
        category=KnowledgeCategory.AUDIO,
        type=KnowledgeType.LESSON,
        symptom="Проверка формирования секции нюансов при действии verify",
        root_cause="Тривиальное подтверждение OK не является ограничением или нюансом",
        solution="Фильтрация тривиальных заметок при добавлении в секцию нюансов",
        rationale="Исключает замусоривание структуры документа",
        slug="audio-trivial-note",
        # K2: карточка auto_inferred создаётся в draft — verify недоступен до
        # promote; тестируем поведение verify на active-карточке
        source=KnowledgeSource.USER_CONFIRMED,
    )
    rec = service.record(req)
    card_path = Path(rec["path"])

    # Выполняем verify с тривиальной заметкой "OK" от имени человека
    service.update_status(
        CardStatusUpdateRequest(
            id="audio-trivial-note",
            action="verify",
            note="OK",
            by="human",
        )
    )

    content = card_path.read_text(encoding="utf-8")
    fm, sections = parse_card_markdown(content)
    # Запись верификации создана
    assert len(fm.verified) == 1
    assert fm.verified[0].note == "OK"
    # Лог обновлен
    assert "VERIFIED: OK" in content
    # Секция нюансов НЕ создана с текстом "OK"
    assert "Нюансы и ограничения (Nuances)" not in sections
