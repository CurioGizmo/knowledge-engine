"""Тесты моделей и парсера MCP Audit Engine (канонический YAML + legacy Markdown)."""

from pathlib import Path
from mcp_audit.models import (
    AuditAutonomy,
    AuditComplexity,
    AuditIssue,
    AuditPriority,
    AuditStatus,
    generate_audit_id,
)
from mcp_audit.parser import (
    _ROOT_CAUSE_KEYWORDS,
    _SOLUTION_KEYWORDS,
    _SYMPTOM_KEYWORDS,
    parse_audit_markdown,
    serialize_audit_markdown,
)


def test_generate_audit_id_format_and_entropy():
    id1 = generate_audit_id("backend")
    id2 = generate_audit_id("backend")
    assert id1.startswith("aud-backend-")
    assert id2.startswith("aud-backend-")
    assert len(id1) >= len("aud-backend-1234")
    assert id1 != id2  # Энтропия


def test_section_keyword_constants_are_stable():
    """Кортежи ключевых слов секций фиксированы контрактом парсинга (порядок значим)."""
    assert _SYMPTOM_KEYWORDS == ("симптом", "symptom", "проблема", "problem")
    assert _ROOT_CAUSE_KEYWORDS == ("первопричин", "root cause", "cause", "анализ")
    assert _SOLUTION_KEYWORDS == ("решение", "solution", "финализ")


def test_serialize_and_parse_canonical_markdown():
    issue = AuditIssue(
        id="aud-audio-a1b2",
        title="Переполнение буфера",
        category="audio",
        priority=AuditPriority.CRITICAL,
        autonomy=AuditAutonomy.HIGH,
        complexity=AuditComplexity.L,
        status=AuditStatus.PLANNED,
        blocked_by=["aud-audio-0001"],
        target_file="app/audio/stream.py",
        symbol="process_stream",
        symptom="Буфер переполняется при потоке > 48kHz",
        root_cause="Неверный размер чанка в буфере",
        solution="Увеличить размер чанка до 4096",
        aliases=["буфер", "аудио-буфер", "buffer-overflow"],
    )

    serialized = serialize_audit_markdown(issue)
    assert serialized.startswith("---")
    assert "id: aud-audio-a1b2" in serialized
    assert "target_file: app/audio/stream.py" in serialized
    assert "aliases:" in serialized

    parsed = parse_audit_markdown(serialized)
    assert parsed.id == "aud-audio-a1b2"
    assert parsed.aliases == ["буфер", "аудио-буфер", "buffer-overflow"]
    assert parsed.title == "Переполнение буфера"
    assert parsed.category == "audio"
    assert parsed.priority == AuditPriority.CRITICAL
    assert parsed.autonomy == AuditAutonomy.HIGH
    assert parsed.complexity == AuditComplexity.L
    assert parsed.status == AuditStatus.PLANNED
    assert parsed.blocked_by == ["aud-audio-0001"]
    assert parsed.target_file == "app/audio/stream.py"
    assert parsed.symbol == "process_stream"
    assert "Буфер переполняется" in parsed.symptom
    assert "Неверный размер" in (parsed.root_cause or "")
    assert "Увеличить размер" in (parsed.solution or "")


def test_parse_legacy_transcrib_markdown_card():
    legacy_content = """# ISS-21: Отсутствие изоляции stdin в subprocess вызовах hardware.py (риск SIGTTIN дедлока)

> **Статус:** [x] **Завершено** (добавлен `stdin=subprocess.DEVNULL` во все вызовы `sysctl` и `nvidia-smi`)

## ❌ Симптом
При запуске бэкенда в фоновом режиме опрос конфигурации оборудования может привести к SIGTTIN.

## 🔍 Техническая первопричина
В модуле hardware.py subprocess.check_output не передает stdin=DEVNULL.

## 🛠 Предлагаемое решение
Добавить stdin=subprocess.DEVNULL во все вызовы.

## 📊 Оценка задачи
- **Важность:** Important (стабильность фонового запуска)
- **Автономия:** Высокая (локализовано исключительно в hardware.py)
- **Сложность:** L (Low, до 20 минут)
"""
    file_path = Path("/project/.agents/audit/ISS-21-hardware-subprocess-stdin-isolation.md")
    parsed = parse_audit_markdown(legacy_content, file_path=file_path)

    assert parsed.id == "ISS-21"
    assert "Отсутствие изоляции stdin" in parsed.title
    assert parsed.status == AuditStatus.COMPLETED
    assert parsed.priority == AuditPriority.IMPORTANT
    assert parsed.autonomy == AuditAutonomy.HIGH
    assert parsed.complexity == AuditComplexity.L
    assert "SIGTTIN" in parsed.symptom
    assert "hardware.py" in (parsed.root_cause or "")
    assert "stdin=subprocess.DEVNULL" in (parsed.solution or "")


def test_parse_legacy_card_without_status_defaults_to_planned():
    legacy_content = """# ISS-15: Переполнение буфера pipe child.stderr в Tauri

## ❌ Симптом
Зависание процесса бэкенда при большом выводе stderr.

## 🔍 Техническая первопричина
Буфер pipe переполняется при отсутствии асинхронного чтения.

## 🛠 Предлагаемое решение
Читать stderr асинхронно.
"""
    file_path = Path("/project/.agents/audit/todo/desktop/ISS-15-tauri-stderr-pipe-deadlock.md")
    parsed = parse_audit_markdown(legacy_content, file_path=file_path)

    assert parsed.id == "ISS-15"
    assert parsed.status == AuditStatus.PLANNED
    assert parsed.category == "desktop"
