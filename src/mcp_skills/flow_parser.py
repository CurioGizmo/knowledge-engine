"""Парсер сценариев флоу, валидатор метаданных и движок слияния оверрайдов."""

from __future__ import annotations

import contextlib
import importlib.resources
import logging
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from mcp_core.card_codec import FrontmatterError, parse_frontmatter
from mcp_core.markdown import FRONTMATTER_SPLIT_REGEX, Fence, check_fence_line, split_sections
from mcp_core.paths import (
    _find_default_project_root,
    get_flows_cache_dir,
    get_flows_dir,
    safe_read_text,
)
from mcp_skills.catalog_version import (
    check_engine_version_compatibility,
    get_catalog_min_engine_version,
)
from mcp_skills.errors import FlowNotFoundError, InvalidFlowFormatError
from mcp_skills.models import FlowDefinition, FlowStage, FlowStageMeta
from mcp_skills.spam_gate import is_safe_name
from mcp_skills.stack_detector import detect_canonical_project_name

__all__ = [
    "list_available_flows",
    "load_flow_definition",
    "merge_flow_override",
    "parse_flow_markdown",
]

logger = logging.getLogger("mcp_skills.flow_parser")

_HEADING_PATTERN = re.compile(r"^(.+?)(?:\s+\[id:\s*([a-zA-Z0-9_-]+)\])?$")
_META_PATTERN = re.compile(r"<!--\s*meta:\s*(\{.*?\})\s*-->", re.DOTALL)
_H1_PATTERN = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def _ensure_catalog_compatible(skills_dir: Path | None) -> None:
    """Reject a catalog requiring a newer engine before parsing or discovery."""
    from mcp_skills import __version__

    catalog_root = get_flows_cache_dir(skills_dir).parent
    minimum = get_catalog_min_engine_version(catalog_root)
    error = check_engine_version_compatibility(minimum, __version__)
    if error:
        raise InvalidFlowFormatError(error)


def _slugify(text: str) -> str:
    """Генерирует безопасный идентификатор из заголовка."""
    clean = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "_", clean)


def _parse_stage_section(
    raw_heading: str,
    body: str,
    name: str,
    order_idx: int,
) -> FlowStage:
    """Парсит отдельную секцию H2 этапа, валидирует заголовок и метаданные."""
    clean_heading = raw_heading.strip()
    h_match = _HEADING_PATTERN.match(clean_heading)
    if not h_match:
        msg = f"Некорректный заголовок этапа '{raw_heading}' в флоу '{name}'"
        raise InvalidFlowFormatError(msg)

    stage_title = h_match.group(1).strip()
    stage_id = h_match.group(2)
    if not stage_id:
        stage_id = _slugify(stage_title)

    if not is_safe_name(stage_id):
        msg = f"Недопустимый ID этапа '{stage_id}' в флоу '{name}'"
        raise InvalidFlowFormatError(msg)

    meta_obj = FlowStageMeta()
    meta_match = _META_PATTERN.search(body)
    if meta_match:
        raw_meta = meta_match.group(1).strip()
        try:
            parsed_meta = yaml.safe_load(raw_meta)
            if not isinstance(parsed_meta, dict):
                msg = f"Метаданные этапа '{stage_id}' должны быть YAML-словарем"
                raise InvalidFlowFormatError(msg)
            meta_obj = FlowStageMeta.model_validate(parsed_meta)
        except (yaml.YAMLError, ValidationError, ValueError) as e:
            msg = f"Некорректные метаданные этапа '{stage_id}' в флоу '{name}': {e}"
            raise InvalidFlowFormatError(msg) from e

        instruction = _META_PATTERN.sub("", body).strip()
    else:
        instruction = body.strip()

    if not instruction:
        msg = f"Инструкция этапа '{stage_id}' в флоу '{name}' не может быть пустой"
        raise InvalidFlowFormatError(msg)

    return FlowStage(
        id=stage_id,
        title=stage_title,
        instruction=instruction,
        meta=meta_obj,
        order=order_idx,
    )


def _validate_fence_balance(body_content: str, name: str) -> None:
    """Проверяет баланс code fences по правилам CommonMark.

    Отступ 0–3 пробела, маркеры ````` ``/``~~~``, совпадение символа
    и длины закрывающего маркера. Незакрытый блок — ошибка формата флоу.
    """
    active_fence: Fence | None = None
    for line in body_content.splitlines():
        _, active_fence = check_fence_line(line, active_fence)
    if active_fence is not None:
        msg = f"Несбалансированные кодовые блоки (code fences) в флоу '{name}'"
        raise InvalidFlowFormatError(msg)


def _extract_flow_frontmatter(content: str, name: str) -> tuple[dict[str, Any], str]:
    """Извлекает опциональный frontmatter флоу (Ф3, core-кодек).

    Возвращает (meta, body). FrontmatterError (non-dict / невалидный YAML /
    пустая мета) и сырые yaml-ошибки мапятся в InvalidFlowFormatError —
    raw-ParserError не утекает. Guard-условия входа в error-режим (v2):

    - сплиттер сматчился → валидируем содержимое (кейсы 08/13/20);
    - кейс 11 (BOM '\\ufeff---...'): сплиттер не матчится, но за BOM —
      явный frontmatter-блок → domain-error (v1 молчал);
    - кейс 12 ('--- \\n' открывающий с хвостовым пробелом): сплиттер не
      матчится и валидного блока нет → эквивалентно отсутствию fm
      (contract: flow прагматичен, «нет блока» = парсим как чистый Markdown).
    """
    stripped = content.strip()
    no_bom = stripped.lstrip("\ufeff")
    has_bom_fm = (
        no_bom.startswith("---")
        and not no_bom.startswith(("--- \n", "---\t"))
        and no_bom != stripped
    )
    if not FRONTMATTER_SPLIT_REGEX.match(stripped) and not has_bom_fm:
        # Нет блока (или сломанный маркер — кейс 12): как v1, без меты.
        return {}, content

    try:
        parsed = parse_frontmatter(stripped)
    except FrontmatterError as e:
        msg = f"Frontmatter флоу '{name}' невалиден: {e}"
        raise InvalidFlowFormatError(msg) from e

    if parsed is None:
        # Гипотетически недостижимо (вход в ветку — только сматченный блок),
        # но сохраняем защитный контракт опционального fm.
        return {}, content
    m = FRONTMATTER_SPLIT_REGEX.match(stripped)
    if m is None:  # pragma: no cover — guard выше обеспечил сматченный блок
        return parsed, content
    return parsed, m.groups()[1]


def parse_flow_markdown(
    content: str,
    name: str,
    source_file: Path | None = None,
    *,
    validate_transition_targets: bool = True,
) -> FlowDefinition:
    """Разбивает Markdown-файл сценария на секции этапов и метаданные.

    Использует ``split_sections`` с ``fence_aware=True`` для игнорирования ``##``
    внутри блоков кода. Поддерживает YAML frontmatter для title, description и aliases.
    """
    if not content or not content.strip():
        msg = f"Файл сценария флоу '{name}' пуст"
        raise InvalidFlowFormatError(msg)

    # 1. Извлечение YAML frontmatter (если присутствует) — core-кодек (Ф3).
    fm, body_content = _extract_flow_frontmatter(content, name)

    _validate_fence_balance(body_content, name)

    sections = split_sections(
        body_content,
        header_match="prefix",
        header_levels=(2,),
        fence_aware=True,
        preamble_on_whitespace=False,
        store_empty_sections=True,
    )

    # 2. Извлечение названия, описания и алиасов
    title = str(fm.get("title") or "").strip() or name
    description = str(fm.get("description") or "").strip()
    aliases = fm.get("aliases") or []
    intent_group = fm.get("intent_group")
    when_to_use = fm.get("when_to_use") or []
    # F1: мостики «куда идти дальше» (имена флоу) — в completed-ответ step_flow
    next_flows = fm.get("next_flows") or []
    exclude_aliases = fm.get("exclude_aliases") or []

    preamble = sections.get("_preamble", "").strip()
    if preamble:
        if m1 := _H1_PATTERN.search(preamble):
            if not fm.get("title"):
                title = m1.group(1).strip()
            if not description:
                desc_lines = [
                    line.strip()
                    for line in preamble.splitlines()
                    if not line.strip().startswith("#") and line.strip()
                ]
                description = " ".join(desc_lines)
        elif not description:
            description = preamble

    # 3. Парсинг этапов из H2-секций
    stages: dict[str, FlowStage] = {}
    stage_order: list[str] = []
    order_idx = 0

    for raw_heading, body in sections.items():
        if raw_heading == "_preamble":
            continue

        stage = _parse_stage_section(raw_heading, body, name, order_idx)
        if stage.id in stages:
            msg = f"Дубликат ID этапа '{stage.id}' в флоу '{name}'"
            raise InvalidFlowFormatError(msg)

        stage.source_file = source_file
        stages[stage.id] = stage
        stage_order.append(stage.id)
        order_idx += 1

    if not stage_order:
        msg = f"Флоу '{name}' не содержит ни одного этапа (H2-заголовка)"
        raise InvalidFlowFormatError(msg)

    flow_data: dict[str, Any] = {
        "name": name,
        "title": title,
        "description": description,
        "aliases": aliases,
        "intent_group": intent_group,
        "when_to_use": when_to_use,
        "next_flows": next_flows,
        "exclude_aliases": exclude_aliases,
        "stages": stages,
        "stage_order": stage_order,
    }
    # Preserve whether optional frontmatter fields were actually declared.
    # merge_flow_override relies on model_fields_set so an override that omits
    # visibility/defaults cannot reset an internal base flow to model defaults.
    for key in (
        "visibility",
        "entrypoint",
        "deprecated",
        "superseded_by",
        "option_schema",
        "defaults",
        "option_rules",
        "dispatch_policy",
        "exclude_aliases",
    ):
        if key in fm:
            flow_data[key] = fm[key]

    try:
        flow = FlowDefinition.model_validate(
            flow_data, context={"partial_flow": not validate_transition_targets}
        )
    except (ValidationError, ValueError) as e:
        msg = f"Некорректные метаданные флоу '{name}': {e}"
        raise InvalidFlowFormatError(msg) from e

    if validate_transition_targets:
        _validate_transition_targets(flow)
    return flow


def _validate_transition_targets(flow: FlowDefinition) -> None:
    for stage in flow.stages.values():
        for label, transition in stage.meta.transitions.items():
            if transition.target not in flow.stages:
                msg = (
                    f"Переход '{label}' этапа '{stage.id}' в флоу '{flow.name}' "
                    f"ссылается на отсутствующий этап '{transition.target}'"
                )
                raise InvalidFlowFormatError(msg)


def merge_flow_override(base: FlowDefinition, override: FlowDefinition) -> FlowDefinition:
    """Смерживает базовый сценарий флоу с секционным оверрайдом."""
    merged_stages = dict(base.stages)
    merged_order = list(base.stage_order)

    for stage_id in override.stage_order:
        ov_stage = override.stages[stage_id]
        if stage_id in merged_stages:
            merged_stages[stage_id] = FlowStage(
                id=stage_id,
                title=ov_stage.title,
                instruction=ov_stage.instruction,
                source_file=ov_stage.source_file,
                meta=ov_stage.meta,
                order=merged_stages[stage_id].order,
            )
        else:
            ov_stage.order = len(merged_order)
            merged_stages[stage_id] = ov_stage
            merged_order.append(stage_id)

    title = override.title if override.title and override.title != override.name else base.title
    description = override.description or base.description
    intent_group = override.intent_group or base.intent_group

    # Слияние алиасов (дедупликация с сохранением порядка)
    merged_aliases = list(base.aliases)
    for a in override.aliases:
        if a not in merged_aliases:
            merged_aliases.append(a)

    # Слияние when_to_use (дедупликация с сохранением порядка)
    merged_when_to_use = list(base.when_to_use)
    for w in override.when_to_use:
        if w not in merged_when_to_use:
            merged_when_to_use.append(w)

    # F1: слияние мостики «куда дальше» (дедупликация с сохранением порядка)
    merged_next_flows = list(base.next_flows)
    for nf in override.next_flows:
        if nf not in merged_next_flows:
            merged_next_flows.append(nf)

    merged_exclude_aliases = list(base.exclude_aliases)
    for ea in override.exclude_aliases:
        if ea not in merged_exclude_aliases:
            merged_exclude_aliases.append(ea)

    def _metadata(name: str) -> Any:
        return getattr(override, name) if name in override.model_fields_set else getattr(base, name)

    merged = FlowDefinition(
        name=base.name,
        title=title,
        description=description,
        aliases=merged_aliases,
        intent_group=intent_group,
        when_to_use=merged_when_to_use,
        next_flows=merged_next_flows,
        exclude_aliases=merged_exclude_aliases,
        stages=merged_stages,
        stage_order=merged_order,
        visibility=_metadata("visibility"),
        entrypoint=_metadata("entrypoint"),
        deprecated=_metadata("deprecated"),
        superseded_by=_metadata("superseded_by"),
        option_schema=_metadata("option_schema"),
        defaults=_metadata("defaults"),
        option_rules=_metadata("option_rules"),
        dispatch_policy=_metadata("dispatch_policy"),
    )
    _validate_transition_targets(merged)
    return merged


def load_flow_definition(
    name: str,
    project_root: Path | None = None,
    skills_dir: Path | None = None,
    canonical_name: str | None = None,
) -> FlowDefinition:
    """Загружает определение сценария флоу с учетом иерархии оверрайдов."""
    if not is_safe_name(name):
        msg = f"Недопустимое имя сценария флоу '{name}'"
        raise ValueError(msg)

    _ensure_catalog_compatible(skills_dir)

    root = project_root or _find_default_project_root()
    proj_flows = get_flows_dir(root)
    cache_flows = get_flows_cache_dir(skills_dir)
    canonical_proj = (
        canonical_name if canonical_name is not None else detect_canonical_project_name(root)
    )

    # 1. Локальный полный файл проекта ($PROJECT_ROOT/.agents/flows/{name}.md)
    local_full = proj_flows / f"{name}.md"
    if local_full.is_file():
        content = safe_read_text(root, local_full)
        if content is None:
            msg = f"Не удалось прочитать локальный файл флоу '{local_full}'"
            raise InvalidFlowFormatError(msg)
        return parse_flow_markdown(content, name, source_file=local_full)

    # 2. Поиск базового определения
    base_content: str | None = None
    base_source = cache_flows / f"{name}.md"

    # Попытка 2a: кэш (~/.cache/mcp-skills/repo/flows/{name}.md или skills_dir/flows/{name}.md)
    cache_base = cache_flows / f"{name}.md"
    if cache_base.is_file():
        with contextlib.suppress(OSError):
            base_content = cache_base.read_text(encoding="utf-8")

    # Попытка 2b: встроенный шаблон пакета
    if base_content is None:
        try:
            ref = importlib.resources.files("mcp_skills.templates.flows").joinpath(f"{name}.md")
            if ref.is_file():
                base_content = ref.read_text(encoding="utf-8")
                base_source = Path(__file__).parent / "templates" / "flows" / f"{name}.md"
        except Exception:
            pkg_file = Path(__file__).parent / "templates" / "flows" / f"{name}.md"
            if pkg_file.is_file():
                with contextlib.suppress(OSError):
                    base_content = pkg_file.read_text(encoding="utf-8")
                    base_source = pkg_file

    if base_content is None:
        avail = [f["name"] for f in list_available_flows(root, skills_dir=skills_dir)]
        avail_str = f". Доступные сценарии: {avail}" if avail else ""
        msg = f"Сценарий флоу '{name}' не найден в проекте, кэше и встроенных шаблонах{avail_str}"
        raise FlowNotFoundError(msg)

    base_def = parse_flow_markdown(base_content, name, source_file=base_source)

    # 3. Поиск секционного оверрайда
    override_candidates: list[Path] = [
        proj_flows / f"{name}.override.md",
    ]
    if canonical_proj:
        override_candidates.append(
            cache_flows / "overrides" / f"{name}_{canonical_proj}.override.md"
        )
    override_candidates.append(cache_flows / "overrides" / f"{name}.override.md")

    for cand in override_candidates:
        if cand.is_file():
            ov_content = cand.read_text(encoding="utf-8")
            ov_def = parse_flow_markdown(
                ov_content,
                f"{name}_override",
                source_file=cand,
                validate_transition_targets=False,
            )
            return merge_flow_override(base_def, ov_def)

    return base_def


def _flow_summary(
    df: FlowDefinition, source: str, *, include_stage_text: bool = False
) -> dict[str, Any]:
    """Строит сводку сценария для выдачи ``list_available_flows``.

    ``include_stage_text=True`` добавляет текст этапов (заголовки + инструкции) —
    он нужен FTS-индексу сценариев, где доменная лексика живёт именно в этапах.
    По умолчанию текст не включается: сводка уходит в ответ MCP-инструмента, и
    ~44 КБ тела на 13 сценариях — это расход токенов клиента без пользы.
    """
    summary: dict[str, Any] = {
        "name": df.name,
        "title": df.title,
        "description": df.description,
        "aliases": df.aliases,
        "exclude_aliases": df.exclude_aliases,
        "intent_group": df.intent_group,
        "when_to_use": df.when_to_use,
        "visibility": df.visibility.value,
        "entrypoint": df.entrypoint,
        "deprecated": df.deprecated,
        "superseded_by": df.superseded_by,
        "option_schema": {
            key: spec.model_dump(mode="json") for key, spec in df.option_schema.items()
        },
        "defaults": df.defaults,
        "option_rules": [rule.model_dump(mode="json") for rule in df.option_rules],
        "dispatch_policy": (
            df.dispatch_policy.model_dump(mode="json") if df.dispatch_policy is not None else None
        ),
        "stages_count": len(df.stage_order),
        "source": source,
    }
    if include_stage_text:
        parts: list[str] = []
        for stage_id in df.stage_order:
            stage = df.stages.get(stage_id)
            if stage is None:
                continue
            parts.append(stage.title)
            parts.append(stage.instruction)
        summary["stage_text"] = "\n".join(parts)
    return summary


def _append_flow_summary(
    results: list[dict[str, Any]],
    flow: FlowDefinition,
    source: str,
    *,
    include_stage_text: bool,
    discoverable_only: bool,
) -> None:
    if discoverable_only and (
        flow.visibility.value != "public" or not flow.entrypoint or flow.deprecated
    ):
        return
    results.append(_flow_summary(flow, source, include_stage_text=include_stage_text))


def list_available_flows(
    project_root: Path | None = None,
    skills_dir: Path | None = None,
    *,
    include_stage_text: bool = False,
    discoverable_only: bool = True,
) -> list[dict[str, Any]]:
    """Возвращает сценарии; по умолчанию только публичные точки входа."""
    _ensure_catalog_compatible(skills_dir)
    root = project_root or _find_default_project_root()
    canonical = detect_canonical_project_name(root)
    seen_names: set[str] = set()
    results: list[dict[str, Any]] = []

    # 1. Локальные сценарии проекта
    proj_flows = get_flows_dir(root)
    if proj_flows.is_dir():
        for p in sorted(proj_flows.glob("*.md")):
            if p.name.endswith(".override.md"):
                continue
            flow_name = p.stem
            if is_safe_name(flow_name) and flow_name not in seen_names:
                seen_names.add(flow_name)
                try:
                    df = load_flow_definition(
                        flow_name, root, skills_dir=skills_dir, canonical_name=canonical
                    )
                    _append_flow_summary(
                        results,
                        df,
                        "project",
                        include_stage_text=include_stage_text,
                        discoverable_only=discoverable_only,
                    )
                except Exception as err:
                    logger.warning("Пропуск поврежденного файла флоу %s: %s", p, err)
                    continue

    # 2. Кэш репозитория / переданный skills_dir
    cache_flows = get_flows_cache_dir(skills_dir)
    if cache_flows.is_dir():
        for p in sorted(cache_flows.glob("*.md")):
            if p.name.endswith(".override.md"):
                continue
            flow_name = p.stem
            if is_safe_name(flow_name) and flow_name not in seen_names:
                seen_names.add(flow_name)
                try:
                    df = load_flow_definition(
                        flow_name, root, skills_dir=skills_dir, canonical_name=canonical
                    )
                    _append_flow_summary(
                        results,
                        df,
                        "cache",
                        include_stage_text=include_stage_text,
                        discoverable_only=discoverable_only,
                    )
                except Exception as err:
                    logger.warning("Пропуск поврежденного файла флоу %s: %s", p, err)
                    continue

    # 3. Встроенные шаблоны пакета
    tpl_dir = Path(__file__).parent / "templates" / "flows"
    if tpl_dir.is_dir():
        for p in sorted(tpl_dir.glob("*.md")):
            flow_name = p.stem
            if is_safe_name(flow_name) and flow_name not in seen_names:
                seen_names.add(flow_name)
                try:
                    df = load_flow_definition(flow_name, root, canonical_name=canonical)
                    _append_flow_summary(
                        results,
                        df,
                        "builtin",
                        include_stage_text=include_stage_text,
                        discoverable_only=discoverable_only,
                    )
                except Exception as err:
                    logger.warning("Пропуск поврежденного файла флоу %s: %s", p, err)
                    continue

    return results
