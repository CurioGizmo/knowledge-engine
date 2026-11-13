"""Парсер Markdown-файлов скиллов и слотов."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from mcp_core.card_codec import FrontmatterError, parse_frontmatter
from mcp_core.markdown import FRONTMATTER_SPLIT_REGEX
from mcp_skills.errors import InvalidSkillFormatError
from mcp_skills.models import SkillCard

__all__ = ["find_slots_in_skeleton", "find_slots_with_modes", "parse_skill_markdown"]

SLOT_MARKER_REGEX = re.compile(r"<!--\s*SLOT(\+)?:\s*([a-zA-Z0-9_-]+)\s*-->")


def find_slots_in_skeleton(text: str) -> list[str]:
    """Извлекает имена всех слотов из маркеров <!-- SLOT: <slot_name> --> или <!-- SLOT+: <slot_name> -->."""
    matches = SLOT_MARKER_REGEX.findall(text)
    # Сохраняем порядок, убирая дубликаты
    seen: set[str] = set()
    result: list[str] = []
    for _plus, name in matches:
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


def find_slots_with_modes(text: str) -> dict[str, bool]:
    """Извлекает словарь {slot_name: is_multi} из маркеров тела скилла."""
    matches = SLOT_MARKER_REGEX.findall(text)
    result: dict[str, bool] = {}
    for plus, name in matches:
        is_multi = bool(plus)
        if name not in result or is_multi:
            result[name] = is_multi
    return result


def parse_skill_markdown(text: str, base_dir: Path | None = None) -> tuple[SkillCard, str]:
    content = text.strip()
    match = FRONTMATTER_SPLIT_REGEX.match(content)
    if not match and content.startswith("---") and content.endswith("---"):
        match = FRONTMATTER_SPLIT_REGEX.match(content + "\n")
    if not match:
        msg = "Скилл должен содержать корректный YAML frontmatter между маркерами '---'"
        raise InvalidSkillFormatError(msg)

    _raw_yaml, body = match.groups()
    # Разбор меты — единый core-кодек (Ф3): FrontmatterError (non-dict/
    # невалидный YAML/пустая мета) мапится в доменную ошибку пакета.
    try:
        data: dict[str, Any] | None = parse_frontmatter(content)
    except FrontmatterError as e:
        msg = f"Ошибка парсинга YAML frontmatter скилла: {e}"
        raise InvalidSkillFormatError(msg) from e
    if data is None:  # недостижимо: сплиттер сматчился → блок есть — pragma ниже
        data = {}

    name = data.get("name")
    if not name:
        msg = "Поле 'name' обязательно в frontmatter скилла"
        raise InvalidSkillFormatError(msg)

    slots_in_body = find_slots_in_skeleton(body)
    declared_slots = data.get("available_slots", [])
    all_slots = list(dict.fromkeys([*slots_in_body, *declared_slots]))

    # R7 (ревизия 4): единственная форма requires_servers — список имён.
    # Dict (slot → [имена]) и CSV-строка отклоняются (MINOR-4): «нужны все
    # перечисленные» без слотовой адресации; проверка применяется к
    # mcp-кандидатам любого слота. Строгий list-only без неявных
    # преобразований: незаявленная форма — ошибка формата.
    rs_raw = data.get("requires_servers", [])
    if not isinstance(rs_raw, list) or (rs_raw and not all(isinstance(s, str) for s in rs_raw)):
        msg = (
            "Поле 'requires_servers' должно быть списком имён серверов "
            "(нужны все перечисленные): ['audit-engine']. Формы "
            "'slot: [имена]' и CSV-строка не поддерживаются."
        )
        raise InvalidSkillFormatError(msg)
    requires_servers = [str(s).strip().lower() for s in rs_raw if str(s).strip()]

    title = str(data.get("title") or name)
    description = str(data.get("description") or "")
    category_val = data.get("category") or data.get("domain") or "general"
    categories_raw = data.get("categories") or data.get("domains")
    categories_val = categories_raw if isinstance(categories_raw, list) else [category_val]
    card = SkillCard(
        name=name,
        title=title,
        description=description,
        categories=categories_val,
        category=category_val,
        stack=data.get("stack", []),
        kind=data.get("kind", "skill"),
        extends=data.get("extends"),
        merge_strategy=data.get("merge_strategy", "append"),
        available_slots=all_slots,
        optional_slots=data.get("optional_slots", []),
        subagents=data.get("subagents", []),
        scope=data.get("scope", "global"),
        activation=data.get("activation"),
        requires_stack=data.get("requires_stack", []),
        requires_servers=requires_servers,
        base_dir=base_dir,
        aliases=data.get("aliases") or [],
    )

    return card, body.strip()
