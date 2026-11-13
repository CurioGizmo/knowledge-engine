"""Тесты для парсера, сериализатора и генератора слагов Markdown-карточек."""

from pathlib import Path
import pytest
from pydantic import ValidationError

from mcp_knowledge.models import (
    CardRecordRequest,
    GeneratedBy,
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


def test_parse_valid_card_frontmatter_and_sections():
    """
    Вход: Валидный Markdown с YAML frontmatter и секциями.
    Выход: Распарсенный frontmatter и словарь секций.
    Проверка: Все поля DTO совпадают с исходными данными, секции извлечены корректно.
    """
    raw_md = """---
id: audio-ffmpeg-sigttin
title: Зависание FFmpeg из-за SIGTTIN
category: audio
type: lesson
status: active
source: user_confirmed
created_at: '2026-09-20'
expires_at: null
supersedes: null
superseded_by: null
related_to:
- desk-create-no-window
derived_from: null
aliases:
- зависание ffmpeg
- sigttin
tags:
- ffmpeg
- audio
---

### Симптом / Контекст
FFmpeg зависает на 0% при запуске в фоне.

### Первопричина (Root Cause)
Ядро Linux посылает сигнал SIGTTIN фоновому процессу, читающему stdin.

### Решение / Выводы
Обязательный флаг -nostdin и stdin=subprocess.DEVNULL при запуске.

### Обоснование / Почему именно так (Rationale)
Исключает блокировку фонового воркера сигналом терминала.
"""

    frontmatter, sections = parse_card_markdown(raw_md)

    assert frontmatter.id == "audio-ffmpeg-sigttin"
    assert frontmatter.title == "Зависание FFmpeg из-за SIGTTIN"
    assert frontmatter.category == KnowledgeCategory.AUDIO
    assert frontmatter.type == KnowledgeType.LESSON
    assert frontmatter.status == KnowledgeStatus.ACTIVE
    assert frontmatter.source == KnowledgeSource.USER_CONFIRMED
    assert frontmatter.created_at == "2026-09-20"
    assert "desk-create-no-window" in frontmatter.related_to
    assert "sigttin" in frontmatter.aliases
    assert "ffmpeg" in frontmatter.tags

    assert "Симптом / Контекст" in sections
    assert "FFmpeg зависает" in sections["Симптом / Контекст"]
    assert "Первопричина (Root Cause)" in sections
    assert "SIGTTIN" in sections["Первопричина (Root Cause)"]
    assert "Решение / Выводы" in sections
    assert "-nostdin" in sections["Решение / Выводы"]
    assert "Обоснование / Почему именно так (Rationale)" in sections


def test_generate_card_markdown_from_dto():
    """
    Вход: Валидный DTO CardRecordRequest.
    Выход: Канонический Markdown-текст с YAML frontmatter.
    Проверка: Round-trip парсинг восстанавливает идентичные поля и секции.
    """
    req = CardRecordRequest(
        title="Тестовая карточка по аудио",
        category=KnowledgeCategory.AUDIO,
        type=KnowledgeType.LESSON,
        source=KnowledgeSource.AUTO_INFERRED,
        symptom="FFmpeg зависает на 0% при вызове из фонового воркера",
        root_cause="Ядро Linux посылает сигнал SIGTTIN фоновому процессу, читающему stdin",
        solution="Обязательный флаг -nostdin и stdin=subprocess.DEVNULL при запуске процесса",
        rationale="Исключает блокировку фонового воркера сигналом терминала операционной системы",
        conditions="Linux, systemd service или nohup",
        nuances="Не применимо для Windows без модификации создания процесса",
        aliases=["ffmpeg hang", "sigttin freeze"],
        tags=["ffmpeg", "linux", "subprocess"],
    )

    card_id = "audio-ffmpeg-test"
    serialized_md = serialize_card_markdown(card_id=card_id, req=req, created_at="2026-09-20")

    assert serialized_md.startswith("---\n")
    assert "id: audio-ffmpeg-test" in serialized_md
    assert "### Симптом / Контекст" in serialized_md
    assert "### Первопричина (Root Cause)" in serialized_md
    assert "### Решение / Выводы" in serialized_md
    assert "### Обоснование / Почему именно так (Rationale)" in serialized_md
    assert "### Условия и окружение (Conditions)" in serialized_md
    assert "### Нюансы и ограничения (Nuances)" in serialized_md
    assert "### Журнал сбоев и наблюдений (Log)" in serialized_md

    # Round-trip проверка
    fm, sections = parse_card_markdown(serialized_md)
    assert fm.id == card_id
    assert fm.category == KnowledgeCategory.AUDIO
    assert sections["Симптом / Контекст"] == req.symptom
    assert sections["Первопричина (Root Cause)"] == req.root_cause
    assert sections["Решение / Выводы"] == req.solution
    assert sections["Обоснование / Почему именно так (Rationale)"] == req.rationale


def test_anti_spam_validation_rejects_empty_or_short_root_cause():
    """
    Вход: CardRecordRequest с пустым или коротким root_cause (< 40 символов).
    Выход: Исключение pydantic.ValidationError.
    Проверка: Universal Anti-Spam Gate блокирует сохранение неинформативных отписок.
    """
    with pytest.raises(ValidationError) as exc_info:
        CardRecordRequest(
            title="Тестовая карточка со спамом",
            category=KnowledgeCategory.AUDIO,
            type=KnowledgeType.LESSON,
            symptom="FFmpeg упал",
            root_cause="была ошибка в коде",  # 18 символов < 40
            solution="Обязательный флаг -nostdin и stdin=subprocess.DEVNULL при запуске процесса",
            rationale="Исключает блокировку фонового воркера сигналом терминала",
        )

    errors = exc_info.value.errors()
    assert any(e["loc"] == ("root_cause",) for e in errors)


def test_anti_spam_validation_rejects_short_rationale():
    """
    Вход: CardRecordRequest с коротким rationale (< 30 символов).
    Выход: Исключение pydantic.ValidationError.
    Проверка: Universal Anti-Spam Gate требует фундаментального обоснования решения.
    """
    with pytest.raises(ValidationError) as exc_info:
        CardRecordRequest(
            title="Тестовая карточка без обоснования",
            category=KnowledgeCategory.AUDIO,
            type=KnowledgeType.LESSON,
            symptom="FFmpeg упал с ошибкой",
            root_cause="Ядро Linux посылает сигнал SIGTTIN фоновому процессу, читающему stdin",
            solution="Обязательный флаг -nostdin и stdin=subprocess.DEVNULL при запуске процесса",
            rationale="так надо",  # 8 символов < 30
        )

    errors = exc_info.value.errors()
    assert any(e["loc"] == ("rationale",) for e in errors)


def test_slug_generation_and_hash_collision():
    """
    Вход: Заголовок и существующий набор ID.
    Выход: Бесконфликтный слаг вида <category>-<slug> или <category>-<slug>-<hash4>.
    Проверка: При коллизии добавляется детерминированный хэш без последовательных номеров.
    """
    category = KnowledgeCategory.AUDIO

    # 1. Базовая генерация слага из латинского названия
    slug1 = generate_card_slug(category, "FFmpeg SIGTTIN freeze in background")
    assert slug1 == "audio-ffmpeg-sigttin-freeze-in"

    # 2. Кастомный слаг
    slug_custom = generate_card_slug(category, "Любой заголовок", custom_slug="ffmpeg-sigttin")
    assert slug_custom == "audio-ffmpeg-sigttin"

    # 3. Коллизия: если такой ID уже существует
    existing = {"audio-ffmpeg-sigttin"}
    slug_collided = generate_card_slug(
        category,
        "FFmpeg SIGTTIN freeze in background",
        custom_slug="ffmpeg-sigttin",
        existing_ids=existing,
    )
    assert slug_collided.startswith("audio-ffmpeg-sigttin-")
    assert slug_collided != "audio-ffmpeg-sigttin"
    assert len(slug_collided) == len("audio-ffmpeg-sigttin") + 1 + 4  # дефис + 4 символа хэша


def test_parse_okf_v02_card():
    """Вход: Текст Markdown с валидным OKF v0.2 фронтматтером.
    Выход: Кортеж (CardFrontmatter, sections).
    Проверка: frontmatter.generated.by == GeneratedBy.PROCESS, verified == [].
    """
    raw_md = """---
id: platform-vulkan-leak
title: Vulkan Sidecar Memory Leak
category: platform
type: lesson
status: active
generated:
  by: process
  agent: antigravity
  created_at: '2026-09-20'
verified: []
stale_after: '2027-01-01'
supersedes: null
superseded_by: null
related_to: []
derived_from: null
aliases: []
tags:
- vulkan
---

### Симптом / Контекст
Утечка памяти при непрерывной транскрипции.

### Первопричина (Root Cause)
Аллокация буферов Vulkan без сброса контекста.

### Решение / Выводы
Перезапуск sidecar процесса каждые 50 задач.

### Обоснование / Почему именно так (Rationale)
Изоляция утечек на уровне отдельного процесса.
"""
    fm, sections = parse_card_markdown(raw_md)
    assert fm.id == "platform-vulkan-leak"
    assert fm.generated.by == GeneratedBy.PROCESS
    assert fm.generated.agent == "antigravity"
    assert fm.generated.created_at == "2026-09-20"
    assert fm.verified == []
    assert fm.stale_after == "2027-01-01"
    assert "Симптом / Контекст" in sections


def test_parse_legacy_card_and_dump_converts_to_okf():
    """Вход: Текст карточки старого формата (source: user_confirmed, expires_at: '2026-12-31').
    Выход: Строка сериализованного Markdown после вызова serialize_card_from_parts.
    Проверка: В YAML присутствует generated.by == human, stale_after: '2026-12-31', отсутствует source:.
    """
    legacy_md = """---
id: ml-ctranslate2-int8
title: CTranslate2 int8 quantization
category: ml
type: decision
status: active
source: user_confirmed
created_at: '2026-09-20'
expires_at: '2026-12-31'
supersedes: null
superseded_by: null
related_to: []
derived_from: null
aliases: []
tags:
- ctranslate2
---

### Симптом / Контекст
Высокое потребление памяти моделью float32.

### Первопричина (Root Cause)
Отсутствие квантования весов модели при инференсе на CPU.

### Решение / Выводы
Использование int8_float16 квантования при экспорте весов.

### Обоснование / Почему именно так (Rationale)
Снижает объем оперативной памяти в 2.5 раза без потери качества.
"""
    fm, sections = parse_card_markdown(legacy_md)
    dumped = serialize_card_from_parts(fm, sections)

    assert "generated:" in dumped
    assert "by: human" in dumped
    assert "stale_after: '2026-12-31'" in dumped or "stale_after: 2026-12-31" in dumped
    assert "source:" not in dumped
    assert "expires_at:" not in dumped


def test_parse_card_with_code_block_containing_markdown_headers():
    """Проверка, что строки '### ' внутри блоков кода не создают новые секции."""
    raw_md = """---
id: audio-code-fence
title: Тест блоков кода
category: audio
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
Ошибка запуска ffmpeg скрипта.

### Решение / Выводы
```bash
### Это комментарий внутри bash скрипта
ffmpeg -i input.wav -nostdin output.mp3
### Конец скрипта
```

### Первопричина (Root Cause)
Комментарии внутри скрипта не должны ломать парсер.

### Обоснование / Почему именно так (Rationale)
Корректный парсер должен распознавать code fences.
"""
    fm, sections = parse_card_markdown(raw_md)
    assert "Решение / Выводы" in sections
    assert "### Это комментарий" in sections["Решение / Выводы"]
    assert "### Конец скрипта" in sections["Решение / Выводы"]
    assert "### Это комментарий" not in sections
    assert "### Конец скрипта" not in sections

    serialized = serialize_card_from_parts(fm, sections)
    assert "### Это комментарий внутри bash скрипта" in serialized


def test_parse_and_serialize_preamble():
    """Проверка сохранения вводного текста (преамбулы) до первого заголовка ###."""
    raw_md = """---
id: audio-preamble-test
title: Тест преамбулы
category: audio
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

> Внимание: это важная заметка перед началом документа!

### Симптом / Контекст
Симптом с преамбулой.

### Решение / Выводы
Решение.

### Первопричина (Root Cause)
Первопричина.

### Обоснование / Почему именно так (Rationale)
Обоснование.
"""
    fm, sections = parse_card_markdown(raw_md)
    assert "_preamble" in sections
    assert "Внимание: это важная заметка" in sections["_preamble"]

    serialized = serialize_card_from_parts(fm, sections)
    assert "> Внимание: это важная заметка перед началом документа!" in serialized
