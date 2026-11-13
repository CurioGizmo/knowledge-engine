"""Модуль безопасности и валидации контента скиллов (Anti-Spam & Secret Gate)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["SpamGate", "SpamGateResult", "is_kebab_name", "is_safe_name"]

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("OpenAI / AI API Key", re.compile(r"\b(sk-[a-zA-Z0-9_\-]{20,})\b")),
    ("Google API Key", re.compile(r"\b(AIza[0-9A-Za-z\-_]{35})\b")),
    (
        "GitHub Token",
        re.compile(r"\b(gh[pousr][_\-][a-zA-Z0-9]{20,}|github_pat_[a-zA-Z0-9_]{82})\b"),
    ),
    (
        "Slack Token / Webhook",
        re.compile(
            r"\b(xox[baprs]-[0-9a-zA-Z]{10,48})\b|https://hooks\.slack\.com/services/[a-zA-Z0-9_/]+"
        ),
    ),
    ("Stripe API Key", re.compile(r"\b(sk_live_[0-9a-zA-Z]{24,})\b")),
    (
        "JWT Token",
        re.compile(r"\b(eyJ[A-Za-z0-9\-_=]+\.eyJ[A-Za-z0-9\-_=]+\.[A-Za-z0-9\-_.+/=]*)\b"),
    ),
    ("AWS Access Key", re.compile(r"\b((?:AKIA|ASIA)[0-9A-Z]{16})\b")),
    ("Generic Private Key", re.compile(r"-----BEGIN\s+([A-Z\s]+)?PRIVATE\s+KEY-----")),
    (
        "Discord Webhook",
        re.compile(r"https://discord\.com/api/webhooks/[a-zA-Z0-9_/]+"),
    ),
    ("Bearer Token", re.compile(r"(?i)bearer\s+[a-zA-Z0-9_\-\.]{30,}")),
    (
        "Generic Secret Assignment",
        re.compile(
            r"""(?i)(?:password|secret|secret_key|api_key|access_key|auth_token)\s*[:=]\s*['"][^'"]{8,}['"]"""
        ),
    ),
)

_PLACEHOLDER_RE = re.compile(
    r"""(?i)^['"]?\s*(?:replace_me|your_[a-z0-9_]+_here|your-[a-z0-9-]+-here|<[^>]+>|\.{3,}|xxx+|dummy|test|placeholder|example|changeme|change_me|todo|fixme)\s*['"]?$"""
)


_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


def is_safe_name(name: str) -> bool:
    """Проверяет, является ли имя скилла/агента безопасным идентификатором (без path traversal)."""
    if not name or ".." in name or "/" in name or "\\" in name or "\x00" in name:
        return False
    return bool(_SAFE_NAME_RE.match(name))


def is_kebab_name(name: str) -> bool:
    """Проверяет имя на канон kebab-case (Ф2 плана core-dedup v4).

    Поглощает 4 копии предиката ``is_safe_name(x) and x == x.lower()`` из
    ``creator._validate_inputs`` / ``server._validate_stack_items`` /
    ``server.create_skill_tool`` / ``models``: безопасный идентификатор
    (без path traversal) строго в нижнем регистре — CamelCase и кириллица
    отклоняются.
    """
    return is_safe_name(name) and name == name.lower()


@dataclass
class SpamGateResult:
    is_valid: bool
    issues: list[str] = field(default_factory=list)


class SpamGate:
    """Проверяет контент и пути обновлений скиллов на секреты, инъекции и спам."""

    def validate_content(self, skill_name: str, content: str) -> SpamGateResult:
        """Сканирует имя и текст скилла на утечки ключей, паролей и вредоносных конструкций."""
        issues: list[str] = []

        if not is_safe_name(skill_name):
            issues.append(f"Недопустимое или небезопасное имя скилла '{skill_name}'")

        if not content.strip():
            issues.append("Контент скилла пуст")
            return SpamGateResult(is_valid=False, issues=issues)

        # 1. Поиск секретов с фильтрацией плейсхолдеров
        for label, pattern in _SECRET_PATTERNS:
            for match in pattern.finditer(content):
                matched_text = match.group(0)
                if label == "Generic Secret Assignment":
                    # Проверяем значение после : или =
                    val_part = re.split(r"[:=]", matched_text, maxsplit=1)[-1].strip()
                    if _PLACEHOLDER_RE.match(val_part):
                        continue
                issues.append(f"Обнаружен потенциальный секрет или токен доступа ({label})")
                break

        # 2. Проверка чрезмерного размера (> 100 КБ)
        if len(content.encode("utf-8")) > 100 * 1024:
            issues.append("Размер контента превышает лимит безопасности 100 КБ")

        return SpamGateResult(is_valid=len(issues) == 0, issues=issues)

    def is_safe_path(self, relative_path: str | Path) -> bool:
        """Проверяет относительный путь на отсутствие Path Traversal."""
        path_str = str(relative_path).replace("\\", "/")
        if "\x00" in path_str:
            return False
        parts = Path(path_str).parts
        if ".." in parts or any(".." in part for part in parts):
            return False
        return not Path(path_str).is_absolute()
