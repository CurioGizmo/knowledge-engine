"""Управление ресурсами каталога скиллов (справочники, шаблоны, примеры).

Step 12 плана: доставка выбранных справочников и шаблонов через load_skill(name, resource_id=..., cursor=...).
Безопасное чтение Markdown-ресурсов каталога с защитой от traversal/symlink escape,
курсорной пагинацией под TokenGuard и разрешением связей с владельцами (owner_skill).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger("mcp_skills.resources")

ResourceType = Literal["reference", "template", "example", "skill", "flow"]

DEFAULT_RESOURCE_CHUNK_SIZE: int = 8000


def determine_resource_type(resource_id: str) -> ResourceType:
    """Определяет тип ресурса по его пути."""
    clean = resource_id.split("#", 1)[0].lower()
    if clean.endswith("skill.md") or "/skills/" in clean:
        return "skill"
    if clean.startswith("flows/") or clean.endswith(".flow.md"):
        return "flow"
    if "template" in clean or clean.endswith(".template.md"):
        return "template"
    if "example" in clean or clean.endswith(".example.md"):
        return "example"
    return "reference"


def validate_resource_id(resource_id: str) -> str:
    """Валидирует resource_id на отсутствие path traversal и абсолютных путей.

    Возвращает очищенный resource_id без ведущих слэшей.
    """
    if not resource_id or not isinstance(resource_id, str):
        msg = "resource_id должен быть непустой строкой"
        raise ValueError(msg)

    # Отрезаем якорь для проверки пути
    path_part = resource_id.split("#", 1)[0].strip()
    if not path_part:
        msg = "resource_id содержит пустой путь"
        raise ValueError(msg)

    if "\0" in resource_id or "\\" in resource_id:
        msg = f"Недопустимые символы в resource_id: {resource_id!r}"
        raise ValueError(msg)

    if path_part.startswith("/") or Path(path_part).is_absolute():
        msg = f"resource_id должен быть относительным путем: {resource_id!r}"
        raise ValueError(msg)

    p = Path(path_part)
    if any(part in ("..", ".") for part in p.parts):
        msg = f"Directory traversal в resource_id запрещен: {resource_id!r}"
        raise ValueError(msg)

    return resource_id.strip()


def resolve_resource_file(
    resource_id: str, catalog_root: Path, project_root: Path | None = None
) -> tuple[Path, str | None]:
    """Безопасно разрешает путь к файлу ресурса внутри catalog_root.

    Возвращает (resolved_file_path, anchor).
    Проверяет существование, отсутствие выхода за пределы catalog_root через symlink.
    """
    valid_id = validate_resource_id(resource_id)
    anchor: str | None = None
    if "#" in valid_id:
        path_part, anchor = valid_id.split("#", 1)
        anchor = anchor.strip() or None
    else:
        path_part = valid_id

    resource_root = catalog_root
    if path_part.startswith("project-skills/"):
        if project_root is None:
            raise ValueError("Для project-skills/ требуется корень проекта")
        resource_root = project_root / ".agents" / "skills"
        path_part = path_part.removeprefix("project-skills/")
    cat_resolved = resource_root.resolve()
    target = (resource_root / path_part).resolve()

    # Проверка symlink escape
    if not target.is_relative_to(cat_resolved):
        msg = f"Доступ к ресурсу за пределами каталога запрещен: {resource_id!r}"
        raise PermissionError(msg)

    if not target.is_file():
        msg = f"Ресурс '{path_part}' не найден в каталоге"
        raise FileNotFoundError(msg)

    return target, anchor


def resolve_link_to_resource_id(
    target_link: str,
    origin_file: Path,
    catalog_root: Path,
    project_root: Path | None = None,
) -> tuple[str | None, str | None]:
    """Разрешает относительную ссылку из origin_file в resource_id (относительно catalog_root).

    Возвращает (resource_id, anchor) или (None, None), если ссылка внешняя или выходит за корень.
    """
    if "://" in target_link or target_link.startswith("mailto:") or not target_link.strip():
        return None, None

    anchor: str | None = None
    path_part = target_link.strip()
    if "#" in path_part:
        path_part, anchor = path_part.split("#", 1)
        anchor = anchor.strip() or None

    target_path = (origin_file.parent / path_part).resolve() if path_part else origin_file.resolve()
    roots = [(catalog_root.resolve(), "")]
    if project_root is not None:
        roots.append(((project_root / ".agents" / "skills").resolve(), "project-skills/"))
    for allowed_root, prefix in roots:
        if target_path.is_relative_to(allowed_root):
            return prefix + target_path.relative_to(allowed_root).as_posix(), anchor
    return None, None


def extract_resources_from_markdown(
    content: str,
    origin_file: Path,
    catalog_root: Path,
    owner_skill: str,
    project_root: Path | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Извлекает ресурсы из Markdown-ссылок и переписывает относительные ссылки в resource_id."""
    resources: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def _replace_link(match: re.Match[str]) -> str:
        link_text = match.group(1)
        link_target = match.group(2)

        res_id, anchor = resolve_link_to_resource_id(
            link_target, origin_file, catalog_root, project_root
        )
        if res_id is not None:
            anchor_suffix = f"#{anchor}" if anchor else ""
            rewritten_target = f"{res_id}{anchor_suffix}"

            if rewritten_target not in seen_ids:
                seen_ids.add(rewritten_target)
                res_type = determine_resource_type(res_id)
                resources.append(
                    {
                        "resource_id": res_id,
                        "title": link_text.strip("`"),
                        "type": res_type,
                        "owner_skill": owner_skill,
                        "description": link_text.strip("`"),
                        "anchor": anchor,
                    }
                )
            return f"[{link_text}]({rewritten_target})"

        return match.group(0)

    rewritten_content = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _replace_link, content)
    return rewritten_content, resources


def extract_anchor_section(content: str, anchor: str) -> str:
    """Извлекает секцию Markdown по якорю (заголовку или его номеру/слагу)."""
    norm_anchor = anchor.lower().replace("-", " ").strip()

    lines = content.splitlines()
    start_idx: int | None = None
    start_level: int = 1

    for idx, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            heading_text = m.group(2).lower().replace("-", " ").strip()
            # Проверяем совпадение: точное, вхождение слага или номера (например "2.4")
            if norm_anchor in heading_text or heading_text.startswith(norm_anchor):
                start_idx = idx
                start_level = level
                break

    if start_idx is None:
        logger.warning("Якорь '%s' не найден в ресурсе; возвращается полное содержимое", anchor)
        return content

    # Собираем до следующего заголовка такого же или более высокого уровня
    end_idx = len(lines)
    for idx in range(start_idx + 1, len(lines)):
        m = re.match(r"^(#{1,6})\s+", lines[idx])
        if m and len(m.group(1)) <= start_level:
            end_idx = idx
            break

    return "\n".join(lines[start_idx:end_idx]).strip()


def read_resource_chunk(
    resource_id: str,
    catalog_root: Path,
    cursor: str | None = None,
    chunk_size: int = DEFAULT_RESOURCE_CHUNK_SIZE,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Читает контент ресурса с поддержкой курсорной пагинации и якорей.

    Ответ:
      resource_id: стабильный путь
      content: фрагмент Markdown
      revision: хэш содержимого файла
      next_cursor: курсор для продолжения (None, если всё прочитано)
      total_chars: длина полного текста
      offset: начальное смещение чанка
      was_truncated: true, если доступно продолжение
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size должен быть положительным")
    target_file, anchor = resolve_resource_file(resource_id, catalog_root, project_root)

    try:
        raw_bytes = target_file.read_bytes()
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = f"Файл ресурса '{resource_id}' содержит недопустимые байты (ожидался UTF-8): {exc}"
        raise ValueError(msg) from exc

    revision = hashlib.sha256(raw_bytes).hexdigest()[:16]

    if anchor:
        text = extract_anchor_section(text, anchor)

    identity = validate_resource_id(resource_id)
    offset = _decode_cursor(cursor, identity, revision, len(text)) if cursor is not None else 0
    chunk = text[offset : offset + chunk_size]
    next_offset = offset + len(chunk)
    next_cursor = None
    if next_offset < len(text):
        payload = {"v": 1, "resource": identity, "revision": revision, "offset": next_offset}
        next_cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    total_len = len(text)
    was_truncated = next_cursor is not None

    clean_id = resource_id.split("#", 1)[0]
    return {
        "resource_id": clean_id,
        "anchor": anchor,
        "content": chunk,
        "revision": revision,
        "next_cursor": next_cursor,
        "total_chars": total_len,
        "offset": offset,
        "was_truncated": was_truncated,
    }


def _decode_cursor(cursor: str, identity: str, revision: str, total: int) -> int:
    try:
        data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if not isinstance(data, dict) or data.get("v") != 1:
            raise ValueError("version")
        offset = data.get("offset")
        if type(offset) is not int or not 0 <= offset <= total:
            raise ValueError("offset")
    except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        raise ValueError("Некорректный формат курсора") from exc
    if data.get("resource") != identity:
        raise ValueError("Курсор принадлежит другому ресурсу или якорю")
    if data.get("revision") != revision:
        raise ValueError("Ресурс изменился с момента последнего чтения; начните с cursor=None")
    return offset


def read_resource_text(
    resource_id: str, catalog_root: Path, project_root: Path | None = None
) -> str:
    """Читает все страницы выбранного ресурса, проверяя ревизию на каждом шаге."""
    parts: list[str] = []
    cursor = None
    while True:
        chunk = read_resource_chunk(
            resource_id, catalog_root, cursor=cursor, project_root=project_root
        )
        parts.append(chunk["content"])
        cursor = chunk["next_cursor"]
        if cursor is None:
            return "".join(parts)


def _skill_resource_sources(
    skill_name: str, catalog_root: Path, skill_base_dir: Path | None, storage: Any
) -> list[Path]:
    """Источники конкретного владельца и его настоящих предков, без поиска по basename."""
    sources: list[Path] = []
    visited: set[str] = set()
    current: str | None = skill_name
    while current and current not in visited:
        visited.add(current)
        entry = storage.get_skill(current) if storage is not None else None
        card = entry[0] if entry else None
        base = card.base_dir if card is not None else skill_base_dir
        if base is not None and base.is_dir():
            sources.extend(base.rglob("*.md"))
        for slot in (card.available_slots or []) if card is not None else []:
            if re.fullmatch(r"[a-zA-Z0-9_-]+", slot):
                sources.extend((catalog_root / "blocks" / slot).glob("*.md"))
        current = card.extends if card is not None else None
    return sources


def is_resource_associated_with_skill(
    skill_name: str,
    resource_id: str,
    catalog_root: Path,
    skill_base_dir: Path | None = None,
    storage: Any = None,
    project_root: Path | None = None,
) -> bool:
    """Разрешает только файлы владельца/предков и их точные Markdown-ссылки."""
    target, _ = resolve_resource_file(resource_id, catalog_root, project_root)
    sources = _skill_resource_sources(skill_name, catalog_root, skill_base_dir, storage)
    visited: set[Path] = set()
    # Включая вложенные ссылки справочников. Каждый путь проверяется общим resolver.
    while sources:
        origin = sources.pop().resolve()
        if origin in visited:
            continue
        visited.add(origin)
        if origin == target:
            return True
        try:
            content = origin.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for link in re.findall(r"\[[^\]]+\]\(([^)]+)\)", content):
            res_id, _ = resolve_link_to_resource_id(link, origin, catalog_root, project_root)
            if res_id is None:
                continue
            try:
                linked, _ = resolve_resource_file(res_id, catalog_root, project_root)
            except (OSError, ValueError):
                continue
            if linked == target:
                return True
            if (
                linked.suffix.lower() == ".md"
                and linked.name != "SKILL.md"
                and linked not in visited
            ):
                sources.append(linked)
    return False
