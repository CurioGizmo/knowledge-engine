"""Парсер, сериализатор и генератор слагов для Markdown-карточек MCP Knowledge Engine.

Полностью изолированный модуль (только PyYAML, Pydantic, hashlib, re).
"""

import hashlib
import re
from collections.abc import Mapping, MutableMapping
from datetime import date
from typing import Any

from mcp_core.card_codec import dump_frontmatter, parse_frontmatter
from mcp_core.markdown import SectionCollection
from mcp_core.markdown import find_section as core_find_section
from mcp_core.markdown import find_section_key as core_find_section_key
from mcp_knowledge.models import (
    CardFrontmatter,
    CardRecordRequest,
    HypothesisRecordRequest,
    KnowledgeCategory,
    KnowledgeSource,
    KnowledgeStatus,
)

FRONTMATTER_PATTERN = re.compile(
    r"^---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)(.*)$",
    re.DOTALL,
)


def _dump_yaml(data: dict[str, Any]) -> str:
    """Дамп frontmatter без лишних тегов Python (общий для всех сериализаторов).

    Делегирует core dump_frontmatter (Ф3-финал): канонические параметры
    + CRLF→LF-нормализация значений (кейс 21) на записи.
    """
    return dump_frontmatter(data)


def parse_card_markdown(raw_md: str) -> tuple[CardFrontmatter, SectionCollection]:
    """Парсит Markdown-карточку с YAML frontmatter и секциями вида '### Заголовок'.

    Возвращает:
        tuple[CardFrontmatter, dict[str, str]]: Объект frontmatter и словарь секций.
    """
    raw_md = raw_md.strip()
    match = FRONTMATTER_PATTERN.match(raw_md)
    if not match:
        if not raw_md.startswith("---"):
            raise ValueError("Карточка должна начинаться с блока YAML frontmatter ('---')")
        raise ValueError("Некорректный блок frontmatter: отсутствует закрывающий разделитель '---'")

    # Разбор меты — единый core-кодек (Ф3): CRLF+EOF-толерантный, non-dict
    # и невалидный YAML → FrontmatterError (подкласс ValueError — сообщения
    # contract-ячеек сохранены байт-в-байт; тип подменён на доменный род).
    fm_dict = parse_frontmatter(raw_md)
    if not isinstance(fm_dict, dict):  # pragma: no cover — кодек гарантирует
        raise ValueError("Frontmatter не является валидным YAML-словарём")
    body_content = match.group(2)

    # Преобразование строковых дат в строки при необходимости
    if "created_at" in fm_dict and isinstance(fm_dict["created_at"], date):
        fm_dict["created_at"] = fm_dict["created_at"].isoformat()
    if "expires_at" in fm_dict and isinstance(fm_dict["expires_at"], date):
        fm_dict["expires_at"] = fm_dict["expires_at"].isoformat()
    if "stale_after" in fm_dict and isinstance(fm_dict["stale_after"], date):
        fm_dict["stale_after"] = fm_dict["stale_after"].isoformat()

    frontmatter = CardFrontmatter(**fm_dict)

    # Парсинг секций по '### <Название>' или '## <Название>' с учетом блоков кода
    # (движок вынесен в mcp_core.markdown; параметры — семантика knowledge-семейства).
    # K9: SectionCollection — Mapping-контракт словаря + сохранение уровней
    # заголовков (H2 остаётся H2 при round-trip мутаций).
    sections = SectionCollection.from_body(body_content)

    return frontmatter, sections


def serialize_card_markdown(
    card_id: str,
    req: CardRecordRequest,
    created_at: str | None = None,
) -> str:
    """Сериализует CardRecordRequest в канонический текст Markdown с YAML frontmatter OKF v0.2."""
    if created_at is None:
        created_at = date.today().isoformat()

    by_val = "human" if req.source == KnowledgeSource.USER_CONFIRMED else "process"
    stale_after = req.stale_after
    cat_val = (
        req.category.value if isinstance(req.category, KnowledgeCategory) else str(req.category)
    )

    # K1 (defensive-ветка сериализатора): после enum-only валидации на модели
    # req.status — KnowledgeStatus | None; str-ветка оставлена как защита
    # от прямых вызовов сериализатора в обход модели — значение обязано
    # быть допустимым статусом, иначе карточка станет «зомби» (нечитаемой).
    if req.status is not None:
        if isinstance(req.status, KnowledgeStatus):
            status_val = req.status.value
        else:
            normalized = str(req.status).strip().lower()
            if normalized not in {s.value for s in KnowledgeStatus}:
                raise ValueError(
                    f"Недопустимый статус карточки: {normalized!r}. "
                    f"Допустимо: {[s.value for s in KnowledgeStatus]}."
                )
            status_val = normalized
    elif req.source == KnowledgeSource.USER_CONFIRMED:
        status_val = KnowledgeStatus.ACTIVE.value
    else:
        status_val = KnowledgeStatus.DRAFT.value

    confidence_val = (
        1.0
        if req.source == KnowledgeSource.USER_CONFIRMED
        else (0.50 if status_val == KnowledgeStatus.DRAFT.value else 0.75)
    )

    fm_dict: dict[str, Any] = {
        "id": card_id,
        "title": req.title,
        "category": cat_val,
        "type": req.type.value,
        "status": status_val,
        "confidence": confidence_val,
        "task_id": req.task_id,
        "refuted_by": None,
        "refutation_reason": None,
        "generated": {
            "by": by_val,
            "agent": req.agent,
            "created_at": created_at,
        },
        "verified": [],
        "stale_after": stale_after,
        "supersedes": req.supersedes,
        "superseded_by": None,
        "related_to": req.related_to,
        "derived_from": None,
        "aliases": req.aliases,
        "tags": req.tags,
    }

    # Дамп frontmatter без лишних тегов Python
    yaml_text = _dump_yaml(fm_dict)

    lines = [
        "---",
        yaml_text.strip(),
        "---",
        "",
        "### Симптом / Контекст",
        req.symptom.strip(),
        "",
        "### Первопричина (Root Cause)",
        req.root_cause.strip(),
        "",
        "### Решение / Выводы",
        req.solution.strip(),
        "",
        "### Обоснование / Почему именно так (Rationale)",
        req.rationale.strip(),
    ]

    if req.conditions:
        lines.extend(["", "### Условия и окружение (Conditions)", req.conditions.strip()])

    if req.nuances:
        lines.extend(["", "### Нюансы и ограничения (Nuances)", req.nuances.strip()])

    lines.extend(["", "### Журнал сбоев и наблюдений (Log)", "Записей пока нет.", ""])

    return "\n".join(lines)


def serialize_hypothesis_markdown(
    card_id: str,
    req: HypothesisRecordRequest,
    created_at: str | None = None,
) -> str:
    """Сериализует HypothesisRecordRequest в Markdown с YAML frontmatter."""
    if created_at is None:
        created_at = date.today().isoformat()

    cat_val = (
        req.category.value if isinstance(req.category, KnowledgeCategory) else str(req.category)
    )

    fm_dict: dict[str, Any] = {
        "id": card_id,
        "title": req.title,
        "category": cat_val,
        "type": "research",
        "status": "hypothesis",
        "confidence": req.confidence,
        "task_id": req.task_id,
        "refuted_by": None,
        "refutation_reason": None,
        "generated": {
            "by": "process",
            "agent": req.agent,
            "created_at": created_at,
        },
        "verified": [],
        "stale_after": None,
        "supersedes": None,
        "superseded_by": None,
        "related_to": [],
        "derived_from": None,
        "aliases": req.aliases,
        "tags": req.tags,
    }

    yaml_text = _dump_yaml(fm_dict)

    lines = [
        "---",
        yaml_text.strip(),
        "---",
        "",
        "### Симптом / Контекст",
        req.symptom.strip(),
        "",
        "### Рабочая гипотеза (Hypothesis)",
        req.hypothesis.strip(),
    ]

    if req.test_plan:
        lines.extend(["", "### План проверки (Test Plan)", req.test_plan.strip()])

    lines.extend(["", "### Журнал сбоев и наблюдений (Log)", "Записей пока нет.", ""])

    return "\n".join(lines)


def serialize_card_from_parts(
    frontmatter: CardFrontmatter,
    sections: MutableMapping[str, str] | SectionCollection,
) -> str:
    """Сериализует frontmatter и секции обратно в канонический текст Markdown.

    K9: уровень заголовков берётся из узлов SectionCollection — исходные H2
    остаются H2. Для плоского dict-аргумента (легаси-вызовы) уровень H3,
    как прежде.
    """
    fm_dict = frontmatter.model_dump(mode="json")
    fm_dict["confidence"] = frontmatter.confidence
    for v_item in fm_dict.get("verified", []):
        if isinstance(v_item, dict) and v_item.get("action") is None:
            v_item.pop("action", None)
    yaml_text = _dump_yaml(fm_dict)

    lines = [
        "---",
        yaml_text.strip(),
        "---",
        "",
    ]

    if isinstance(sections, SectionCollection):
        body = sections.serialize()
        return "\n".join(lines).rstrip() + "\n" + body

    preamble = sections.get("_preamble")
    if preamble:
        lines.append(preamble.strip())
        lines.append("")

    for header, text in sections.items():
        if header == "_preamble":
            continue
        lines.append(f"### {header}")
        lines.append(text.strip())
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def generate_card_slug(
    category: str | KnowledgeCategory,
    title: str,
    custom_slug: str | None = None,
    existing_ids: set[str] | None = None,
) -> str:
    """Генерирует семантический бесконфликтный ID карточки вида <category>-<slug>.

    При коллизии с existing_ids добавляет 4-символьный хэш: <category>-<slug>-<hash4>.
    """
    if existing_ids is None:
        existing_ids = set()

    cat_val = (
        category.value if isinstance(category, KnowledgeCategory) else str(category).strip().lower()
    )

    base_slug = ""
    if custom_slug and custom_slug.strip():
        base_slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", custom_slug.strip().lower()).strip("-")

    if not base_slug:
        # Извлекаем безопасные латинские слова из заголовка
        latin_words = re.findall(r"[a-zA-Z0-9]+", title.lower())
        if latin_words:
            base_slug = "-".join(latin_words[:4])
        else:
            # Если в заголовке только кириллица — используем детерминированный хэш
            h = hashlib.sha256(title.encode("utf-8")).hexdigest()[:8]
            base_slug = f"card-{h}"

    if base_slug.startswith(f"{cat_val}-"):
        base_slug = base_slug[len(cat_val) + 1 :]

    base_id = f"{cat_val}-{base_slug}"

    if base_id not in existing_ids:
        return base_id

    # При коллизии добавляем короткий 4-символьный хэш
    hash4 = hashlib.sha256(title.encode("utf-8")).hexdigest()[:4]
    collision_id = f"{base_id}-{hash4}"

    # Если даже с hash4 коллизия (крайне маловероятно), инкрементируем счетчик
    counter = 1
    final_id = collision_id
    while final_id in existing_ids:
        final_id = f"{collision_id}-{counter}"
        counter += 1

    return final_id


def find_section_content(sections: Mapping[str, str], *keywords: str) -> str:
    """Ищет содержимое секции по ключевым словам в названии (нечувствительно к регистру)."""
    return core_find_section(sections, *keywords) or ""


def find_section_key(
    sections: Mapping[str, str], *keywords: str, default: str | None = None
) -> str | None:
    """Ищет название секции по ключевым словам (нечувствительно к регистру)."""
    return core_find_section_key(sections, *keywords, default=default)


def append_log_entry(sections: MutableMapping[str, str], entry: str) -> None:
    """Добавляет запись в секцию журнала сбоев и наблюдений."""
    log_key = find_section_key(sections, "журнал", "log") or "Журнал сбоев и наблюдений (Log)"
    current = sections.get(log_key, "").strip()
    if not current or current == "Записей пока нет.":
        sections[log_key] = entry
    else:
        sections[log_key] = f"{current}\n{entry}"
