"""Модуль структурной валидации формата (Content Gate) для MCP Knowledge Engine.

Защищает базу знаний от сохранения структурных чеклистов задач,
дампов трассировок стека и локальных отчётов с номерами строк.
Качественная фильтрация и валидация ценности карточек агентов обеспечивается
карантином черновиков (status='draft') и ручным аппрувом (Human Promotion).
"""

from __future__ import annotations

import re
from typing import Any

from mcp_knowledge.models import KnowledgeSource

CHECKBOX_PATTERN = re.compile(r"[-*+]\s+\[[ xX]\]")
FILE_LINE_PATTERN = re.compile(
    r"\b[a-zA-Z0-9_\-\./]+\.(?:py|ts|tsx|jsx|js|go|rs|md|json|yaml|yml):\d+\b"
)
PYTHON_TRACEBACK_PATTERN = re.compile(r'File\s+"[^"]+",\s+line\s+\d+')

REPORT_REJECTION_MESSAGE = (
    "Попытка сохранить чеклист задач, трассировку стека или локальный вывод тестов вместо инженерного знания. "
    "Карточки базы знаний предназначены для архитектурных решений, договорённостей кодовой базы "
    "и платформенных gotchas. Чеклисты и протоколы ведутся в рабочей системе проекта (файлы планов, баг-трекер или логи)."
)


def detect_report_like_content(req: Any) -> str | None:
    """Проверяет запрос карточки или гипотезы на структурные аномалии (чеклисты, дампы стека, спам строками).

    Возвращает причину обнаружения невалидного формата или None, если контент валиден.
    Карточки с source=USER_CONFIRMED обладают абсолютным иммунитетом.
    """
    if getattr(req, "source", None) == KnowledgeSource.USER_CONFIRMED:
        return None

    if isinstance(req, str):
        full_text = req
    else:
        # Проверяем все текстовые поля запроса
        text_blocks = [
            getattr(req, "title", "") or "",
            getattr(req, "symptom", "") or "",
            getattr(req, "root_cause", "") or "",
            getattr(req, "solution", "") or "",
            getattr(req, "rationale", "") or "",
            getattr(req, "hypothesis", "") or "",
            getattr(req, "test_plan", "") or "",
            getattr(req, "context", "") or "",
            getattr(req, "nuances", "") or "",
            getattr(req, "note", "") or "",
        ]
        full_text = "\n".join(b for b in text_blocks if b)
    if not full_text.strip():
        return None

    # 1. Проверка чеклистов (2+ чекбокса - это план/спринт, а не знание)
    checkbox_matches = CHECKBOX_PATTERN.findall(full_text)
    if len(checkbox_matches) >= 2:
        return f"Обнаружен чеклист задач ({len(checkbox_matches)} чекбоксов)"

    # 2. Проверка спама номерами строк файлов (4+ file.py:123 - это локальный баг-репорт для pytest)
    file_line_matches = FILE_LINE_PATTERN.findall(full_text)
    if len(file_line_matches) >= 4:
        return f"Обнаружен локальный баг-репорт с номерами строк файлов ({len(file_line_matches)} ссылок)"

    # 3. Проверка дампов трассировок (2+ File "...", line ...)
    traceback_matches = PYTHON_TRACEBACK_PATTERN.findall(full_text)
    if len(traceback_matches) >= 2:
        return f"Обнаружен дамп трассировки стека ({len(traceback_matches)} фреймов)"

    return None
