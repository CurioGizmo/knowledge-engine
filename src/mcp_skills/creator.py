"""Модуль создания и добавления новых скиллов в репозиторий (create_skill)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from mcp_core.markdown import FRONTMATTER_SPLIT_REGEX
from mcp_core.paths import resolve_skills_repo_dir
from mcp_core.sanitizer import normalize_category_slug
from mcp_core.sync import _atomic_write_text
from mcp_skills.categories import is_valid_category
from mcp_skills.errors import SkillAlreadyExistsError, SpamGateRejectedError
from mcp_skills.models import SkillCard
from mcp_skills.parser import parse_skill_markdown
from mcp_skills.spam_gate import SpamGate, is_kebab_name, is_safe_name
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer

__all__ = ["create_skill"]


def _validate_inputs(
    name: str,
    available_slots: list[str] | None,
    stack: list[str] | None,
    category: str,
    storage: SkillsStorage | None,
    skills_repo_dir: Path | None = None,
) -> tuple[list[str], list[str], str]:
    """Валидирует имя скилла, слоты, стек и категорию."""
    if not is_kebab_name(name):
        msg = f"Недопустимое имя скилла '{name}': используйте kebab-case без спецсимволов и path traversal."
        raise ValueError(msg)

    clean_slots: list[str] = []
    if available_slots:
        for slot in available_slots:
            if not is_kebab_name(slot):
                msg = f"Недопустимое имя слота '{slot}': используйте kebab-case без спецсимволов."
                raise ValueError(msg)
            clean_slots.append(slot)

    clean_stack: list[str] = []
    if stack:
        for item in stack:
            if not isinstance(item, str) or not is_kebab_name(item):
                msg = f"Недопустимое имя технологии в стеке '{item}': используйте kebab-case без спецсимволов."
                raise ValueError(msg)
            clean_stack.append(item)

    clean_category = normalize_category_slug(category) if category else "general"
    if clean_category != "general" and not is_valid_category(
        clean_category, storage, skills_repo_dir
    ):
        msg = f"Недопустимая категория скилла '{clean_category}'."
        raise ValueError(msg)

    return clean_slots, clean_stack, clean_category


_ALIAS_RE = re.compile(r"^[\w\- ]+$", re.UNICODE)


def _validate_aliases(aliases: list[str] | str | None) -> list[str]:
    """Валидирует и нормализует список алиасов скилла."""
    if not aliases:
        return []
    raw = [aliases] if isinstance(aliases, str) else aliases
    clean: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            msg = f"Недопустимый алиас {item!r}: каждый алиас должен быть строкой."
            raise ValueError(msg)
        a = re.sub(r"\s+", " ", item.strip())
        if not a:
            continue
        if len(a) > 100 or not _ALIAS_RE.match(a):
            msg = (
                f"Недопустимый алиас '{a}': разрешены буквы (латиница/кириллица), "
                f"цифры, дефисы, подчёркивания и пробелы; длина 1–100 символов."
            )
            raise ValueError(msg)
        key = a.casefold()
        if key not in seen:
            seen.add(key)
            clean.append(a)
    if len(clean) > 30:
        msg = f"Слишком много алиасов ({len(clean)}): максимум 30."
        raise ValueError(msg)
    return clean


def _resolve_target_paths(
    name: str,
    skills_repo_dir: Path | None,
    existing_base_dir: Path | None = None,
) -> tuple[Path, Path]:
    """Разрешает целевые пути и проверяет отсутствие симлинков и выхода за границы.

    При ``existing_base_dir`` (фактическое место существующего скилла из индекса,
    A3) запись направляется туда, а не в канонический ``skills/<name>`` — иначе
    overwrite создаёт дубликат, не обновляя оригинал. Каталог вне границ
    репозитория игнорируется (fallback на канонический путь).
    """
    # Guard: без явного skills_repo_dir пишем в дефолт ~/.cache/mcp-skills/repo —
    # глобальный шаренный кэш. Тесты/E2E без якоря проекта не должны мусорить туда:
    # требуем явно переданный каталог или уже существующий (заякоренный) репозиторий.
    if skills_repo_dir is None:
        default_repo = resolve_skills_repo_dir(None)
        if (
            not (default_repo / "skills").exists()
            and not (default_repo / "docs").exists()
            and not any((default_repo / "skills").glob("*"))
        ):
            msg = (
                "Не передан skills_repo_dir, а репозиторий скиллов по умолчанию "
                f"({default_repo}) не существует. create_skill требует явный "
                "skills_repo_dir либо инициализированный репозиторий скиллов "
                "(иначе запись попадёт в глобальный кэш)."
            )
            raise ValueError(msg)
    repo_dir = resolve_skills_repo_dir(skills_repo_dir)
    skills_base = repo_dir if repo_dir.name == "skills" else repo_dir / "skills"

    target_skill_dir = skills_base / name
    if existing_base_dir is not None:
        candidate = Path(existing_base_dir)
        try:
            candidate.resolve().relative_to(skills_base.resolve())
        except (ValueError, OSError):
            candidate = skills_base / name
        target_skill_dir = candidate
    target_file = target_skill_dir / "SKILL.md"

    if target_skill_dir.is_symlink():
        msg = f"Недопустимый путь скилла '{name}': обнаружена символическая ссылка."
        raise ValueError(msg)
    try:
        target_skill_dir.resolve().relative_to(skills_base.resolve())
    except ValueError:
        msg = f"Недопустимый путь скилла '{name}': выход за пределы репозитория скиллов."
        raise ValueError(msg) from None

    return target_skill_dir, target_file


def _build_frontmatter(card: SkillCard) -> dict[str, Any]:
    """Собирает словарь frontmatter для сериализации SkillCard.

    Базовые поля (name/title/description/category) пишутся всегда; остальные —
    только непустые/не-дефолтные, чтобы не раздувать frontmatter и не менять
    поведение скиллов без этих полей (A4: наследование метаданных при overwrite
    должно сохраняться в файл).
    """
    frontmatter_dict: dict[str, Any] = {
        "name": card.name,
        "title": card.title,
        "description": card.description,
        "category": card.category,
    }
    if card.stack:
        frontmatter_dict["stack"] = card.stack
    if card.aliases:
        frontmatter_dict["aliases"] = card.aliases
    if card.available_slots:
        frontmatter_dict["available_slots"] = card.available_slots
    if card.optional_slots:
        frontmatter_dict["optional_slots"] = card.optional_slots
    if card.kind != "skill":
        frontmatter_dict["kind"] = card.kind
    if card.extends:
        frontmatter_dict["extends"] = card.extends
    if card.merge_strategy != "append":
        frontmatter_dict["merge_strategy"] = card.merge_strategy
    if card.subagents:
        frontmatter_dict["subagents"] = card.subagents
    if card.scope != "global":
        frontmatter_dict["scope"] = card.scope
    if card.activation:
        frontmatter_dict["activation"] = card.activation
    if card.requires_stack:
        frontmatter_dict["requires_stack"] = card.requires_stack
    return frontmatter_dict


def create_skill(
    name: str,
    title: str,
    description: str,
    content: str,
    category: str = "general",
    stack: list[str] | None = None,
    aliases: list[str] | str | None = None,
    available_slots: list[str] | None = None,
    stack_descriptions: dict[str, str] | None = None,
    blocks: dict[str, dict[str, str]] | None = None,
    overwrite: bool = False,
    skills_repo_dir: Path | None = None,
    storage: SkillsStorage | None = None,
    synchronizer: SkillsSynchronizer | None = None,
    optional_slots: list[str] | None = None,
) -> dict[str, Any]:
    """Создаёт новый канонический скилл в репозитории скиллов с валидацией и индексацией.

    Parameters
    ----------
    name : str
        Уникальное безопасное имя скилла (kebab-case).
    title : str
        Читаемый заголовок скилла.
    description : str
        Описание назначения скилла (условие активации агентом).
    content : str
        Основной текст инструкций Markdown.
    category : str | None
        Категория скилла ('general', одна из категорий или зарегистрированная в БД).
    stack : list[str] | None
        Технологический стек (например, ['python', 'fastapi']).
    aliases : list[str] | str | None
        Поисковые синонимы и формулировки интентов.
    available_slots : list[str] | None
        Доступные слоты для подстановки стековых блоков.
    overwrite : bool
        Разрешить перезапись существующего скилла (по умолчанию False).
    skills_repo_dir : Path | None
        Каталог репозитория скиллов (override).
    storage : SkillsStorage | None
        Экземпляр хранилища SQLite.
    synchronizer : SkillsSynchronizer | None
        Экземпляр синхронизатора для немедленной переиндексации.

    Returns
    -------
    dict[str, Any]
        Словарь с метаданными созданного скилла и напоминанием.
    """
    clean_slots, clean_stack, clean_category = _validate_inputs(
        name, available_slots, stack, category, storage, skills_repo_dir
    )
    clean_aliases = _validate_aliases(aliases)

    # A3: фактическое место существующего скилла берём из индекса — при overwrite
    # пишем туда, а не всегда в канонический skills/<name> (иначе — дубликат).
    existing_card: SkillCard | None = None
    if storage is not None and (found := storage.get_skill(name)):
        existing_card, _ = found
    target_skill_dir, target_file = _resolve_target_paths(
        name, skills_repo_dir, existing_base_dir=existing_card.base_dir if existing_card else None
    )

    # Проверка на существование файла/скилла
    is_existing = (
        target_file.is_file()
        or (target_skill_dir / "SKELETON.md").is_file()
        or existing_card is not None
    )
    if is_existing and not overwrite:
        existing_card_data: dict[str, Any] = (
            existing_card.model_dump(mode="json") if existing_card else {}
        )
        raise SkillAlreadyExistsError(
            f"Скилл '{name}' уже существует. Передайте overwrite=True для перезаписи.",
            skill_name=name,
            existing_card=existing_card_data,
        )

    # 6. Нормализация контента: отрезаем дублирующийся frontmatter, если передан
    clean_body = content.strip()
    if not clean_body:
        raise SpamGateRejectedError(
            "Контент скилла отклонён фильтром безопасности: Контент скилла пуст",
            issues=["Контент скилла пуст"],
        )
    if match := FRONTMATTER_SPLIT_REGEX.match(clean_body):
        clean_body = match.group(2).strip()

    # 7. Сборка канонического YAML frontmatter.
    #    A4: поля, не управляемые сигнатурой вызова (requires_stack, activation,
    #    kind, extends, merge_strategy, scope, subagents), при overwrite
    #    наследуются из существующего frontmatter — источник истины файл на диске
    #    (индекс может отставать), fallback — карточка из индекса.
    preserved: SkillCard | None = None
    if is_existing:
        if target_file.is_file():
            try:
                preserved, _ = parse_skill_markdown(target_file.read_text(encoding="utf-8"))
            except Exception:
                preserved = existing_card
        else:
            preserved = existing_card

    clean_title = title.strip() if title else name
    clean_description = description.strip() if description else ""

    card = SkillCard(
        name=name,
        title=clean_title,
        description=clean_description,
        category=clean_category,
        stack=clean_stack,
        aliases=clean_aliases,
        available_slots=clean_slots,
        optional_slots=(
            optional_slots
            if optional_slots is not None
            else preserved.optional_slots
            if preserved
            else []
        ),
        kind=preserved.kind if preserved else "skill",
        extends=preserved.extends if preserved else None,
        merge_strategy=preserved.merge_strategy if preserved else "append",
        subagents=preserved.subagents if preserved else [],
        scope=preserved.scope if preserved else "global",
        activation=preserved.activation if preserved else None,
        requires_stack=preserved.requires_stack if preserved else [],
        base_dir=target_skill_dir,
    )

    frontmatter_dict = _build_frontmatter(card)

    yaml_header = yaml.dump(frontmatter_dict, allow_unicode=True, sort_keys=False).strip()
    full_markdown = f"---\n{yaml_header}\n---\n\n{clean_body}\n"

    # 8. Валидация полного документа через SpamGate (секреты, токены, лимит 100 КБ)
    gate = SpamGate()
    gate_res = gate.validate_content(name, full_markdown)
    if not gate_res.is_valid:
        raise SpamGateRejectedError(
            f"Контент скилла отклонён фильтром безопасности: {'; '.join(gate_res.issues)}",
            issues=gate_res.issues,
        )

    # 8.1. A4: валидация блоков ДО записи SKILL.md — невалидный блок не оставляет
    #      частично записанного скилла на диске.
    block_plan = _validate_blocks(blocks, target_skill_dir, name)

    # 9. Атомарная запись через _atomic_write_text с защитой от гонок
    try:
        _atomic_write_text(target_file, full_markdown, overwrite=overwrite)
    except FileExistsError:
        existing_card_data = existing_card.model_dump(mode="json") if existing_card else {}
        raise SkillAlreadyExistsError(
            f"Скилл '{name}' уже существует. Передайте overwrite=True для перезаписи.",
            skill_name=name,
            existing_card=existing_card_data,
        ) from None

    # 10. Авто-дополнение конвенций стека: новые значения попадают в словарь осей.
    #     Семантика берётся из stack_descriptions (если LLM передал) — иначе null,
    #     и валидатор _load_stack_conventions пропустит запись до ручного описания.
    _append_stack_conventions(card.stack, skills_repo_dir, stack_descriptions)
    _append_categories(card.category, skills_repo_dir)

    # 10.1. Запись модульных блоков blocks/<slot>/<value>.md (план уже провалидирован)
    written_blocks = _write_block_plan(block_plan, overwrite)

    # 11. Мгновенная синхронизация индекса
    if synchronizer is not None:
        synchronizer.sync()

    status_str = "updated" if is_existing else "created"
    return {
        "status": status_str,
        "name": card.name,
        "path": str(target_file),
        "blocks_written": written_blocks,
        "skill": card.model_dump(mode="json"),
        "reminder": (
            "Скилл успешно сохранён в репозитории скиллов (untracked). "
            "Проверьте файл, закоммитьте и отправьте в upstream вручную."
        ),
    }


def _append_stack_conventions(
    stack: list[str] | None,
    skills_repo_dir: Path | None,
    descriptions: dict[str, str] | None = None,
) -> list[str]:
    """Дополняет docs/stack-conventions.yaml новыми значениями стека (idempotent).

    Новое значение пишется с описанием из ``descriptions`` (если передано) или
    с null — валидатор пропустит его с warning до ручного описания. Существующая
    непустая семантика не перетирается: update скилла не может молча сменить
    смысл оси. Возвращает список добавленных/заполненных значений.
    """
    if not stack:
        return []
    repo_dir = resolve_skills_repo_dir(skills_repo_dir)
    conv_path = repo_dir / "docs" / "stack-conventions.yaml"
    try:
        existing = yaml.safe_load(conv_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        existing = {}
    except Exception:
        return []
    if not isinstance(existing, dict):
        return []
    desc_map = {
        str(k).strip().lower(): str(v).strip()
        for k, v in (descriptions or {}).items()
        if str(v).strip()
    }
    added: list[str] = []
    changed = False
    for tech in stack:
        key = tech.strip().lower()
        current = existing.get(tech)
        current_desc = current.get("description") if isinstance(current, dict) else current
        if tech not in existing:
            existing[tech] = {"description": desc_map.get(key) or None}
            added.append(tech)
            changed = True
        elif not str(current_desc or "").strip() and desc_map.get(key):
            # Заполняем пустую (null) запись — существующая семантика не трогается
            if isinstance(current, dict):
                current["description"] = desc_map[key]
            else:
                existing[tech] = {"description": desc_map[key]}
            added.append(tech)
            changed = True
    if changed:
        try:
            conv_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(conv_path, yaml.dump(existing, allow_unicode=True, sort_keys=False))
        except Exception:
            return []
    return added


def _append_categories(
    category: str | None,
    skills_repo_dir: Path | None,
    title: str = "",
    description: str = "",
) -> bool:
    """Дополняет docs/categories.yaml новой категорией (idempotent, F4)."""
    if not category:
        return False
    cat_key = category.strip().lower()
    if not is_safe_name(cat_key) or cat_key == "general":
        return False
    repo_dir = resolve_skills_repo_dir(skills_repo_dir)
    cat_path = repo_dir / "docs" / "categories.yaml"
    try:
        raw = yaml.safe_load(cat_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raw = {}
    except Exception:
        return False
    if not isinstance(raw, dict):
        raw = {}
    categories_map = raw.setdefault("categories", {})
    if not isinstance(categories_map, dict):
        categories_map = {}
        raw["categories"] = categories_map
    if cat_key in categories_map:
        return False
    categories_map[cat_key] = {
        "title": title or cat_key.title(),
        "description": description or f"Категория {cat_key}",
        "keywords": [cat_key],
    }
    try:
        cat_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(cat_path, yaml.dump(raw, allow_unicode=True, sort_keys=False))
        return True
    except Exception:
        return False


def _validate_blocks(
    blocks: dict[str, dict[str, str]] | None,
    target_skill_dir: Path,
    skill_name: str,
) -> list[tuple[Path, str]]:
    """Валидирует модульные блоки и возвращает план записи ``[(файл, контент)]``.

    Формат входа: ``{slot: {value: content}}``, где ``default`` — допустимое
    значение (fallback-блок). Имена slot/value через ``is_safe_name``; контент
    прогоняется через SpamGate (секреты, лимит). Файлы НЕ записываются —
    план отдаётся ``_write_block_plan``. Невалидный элемент вызывает ошибку
    до любой записи (вызывается до записи SKILL.md).
    """
    if not blocks:
        return []
    gate = SpamGate()
    plan: list[tuple[Path, str]] = []
    for slot, variants in blocks.items():
        slot_clean = str(slot).strip().lower()
        if not is_safe_name(slot_clean) or slot_clean != slot:
            raise ValueError(f"Недопустимое имя слота в blocks: {slot!r}")
        if not isinstance(variants, dict):
            raise ValueError(f"blocks[{slot!r}]: ожидается словарь {{value: content}}")
        for value, content in variants.items():
            value_clean = str(value).strip().lower()
            if not is_safe_name(value_clean) or value_clean != value:
                raise ValueError(f"Недопустимое значение блока: {slot}/{value!r}")
            if not isinstance(content, str) or not content.strip():
                raise ValueError(f"blocks[{slot!r}][{value!r}]: контент обязателен")
            block_file = target_skill_dir / "blocks" / slot_clean / f"{value_clean}.md"
            # path traversal невозможен (is_safe_name), но перепроверяем границы
            try:
                block_file.resolve().relative_to((target_skill_dir / "blocks").resolve())
            except ValueError:
                raise ValueError(f"Недопустимый путь блока: {slot}/{value}") from None
            gate_res = gate.validate_content(f"{skill_name}-{slot}-{value_clean}", content)
            if not gate_res.is_valid:
                raise SpamGateRejectedError(
                    f"Контент блока {slot}/{value} отклонён: {'; '.join(gate_res.issues)}",
                    issues=gate_res.issues,
                )
            plan.append((block_file, content.strip() + "\n"))
    return plan


def _write_block_plan(plan: list[tuple[Path, str]], overwrite: bool) -> list[str]:
    """Записывает провалидированные блоки из плана ``_validate_blocks``.

    Возвращает список записанных относительных путей.
    """
    written: list[str] = []
    for block_file, body in plan:
        _atomic_write_text(block_file, body, overwrite=overwrite)
        written.append(f"blocks/{block_file.parent.name}/{block_file.stem}.md")
    return written
