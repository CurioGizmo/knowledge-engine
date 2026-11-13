"""Модуль бандлинга субагентов и компиляции их скиллов для целевых рантаймов."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from mcp_core.paths import (
    _find_default_project_root,
    get_agents_cache_dir,
    get_agents_dir,
)
from mcp_skills.adapters import SUPPORTED_TARGETS, get_adapter
from mcp_skills.errors import SkillNotFoundError
from mcp_skills.models import (
    AgentOverride,
    AgentPermissions,
    AgentProfile,
    SkillOptionItem,
)
from mcp_skills.project_context import get_project_context, supplement_mode_axis
from mcp_skills.resources import (
    is_resource_associated_with_skill,
    read_resource_text,
)
from mcp_skills.slot_compiler import SlotCompiler
from mcp_skills.spam_gate import is_safe_name
from mcp_skills.stack_detector import detect_canonical_project_name
from mcp_skills.storage import SkillsStorage
from mcp_skills.token_guard import TokenGuard

__all__ = [
    "AgentBundler",
    "_validate_agent_key",
    "create_default_override",
    "merge_profile_with_override",
    "merge_skill_options",
    "validate_agent_key",
]


def validate_agent_key(name: str, target: str, project: str | None = None) -> None:
    """Проверяет безопасность компонентов ключа агента."""
    if not is_safe_name(name):
        msg = f"Недопустимое имя агента '{name}'"
        raise ValueError(msg)
    if target not in SUPPORTED_TARGETS:
        msg = f"Неподдерживаемый целевой рантайм '{target}'. Допустимые: {list(SUPPORTED_TARGETS)}"
        raise ValueError(msg)
    if project is not None and not is_safe_name(project):
        msg = f"Недопустимое имя проекта '{project}'"
        raise ValueError(msg)


_validate_agent_key = validate_agent_key


def merge_skill_options(
    base: Mapping[str, SkillOptionItem] | None,
    higher: Mapping[str, SkillOptionItem | dict[str, Any]] | None,
) -> dict[str, SkillOptionItem]:
    """Смерживает skill_options: higher переопределяет base на уровне полей.

    Списки touches/resources заменяются целиком (не конкатенируются).
    Отсутствующее поле (None) наследуется из base.
    [] означает явный пустой выбор.
    """
    merged: dict[str, SkillOptionItem] = {}
    if base:
        for k, v in base.items():
            item = v if isinstance(v, SkillOptionItem) else SkillOptionItem.model_validate(v)
            merged[k] = item.model_copy()

    if higher:
        for k, higher_val in higher.items():
            higher_item = (
                higher_val
                if isinstance(higher_val, SkillOptionItem)
                else SkillOptionItem.model_validate(higher_val)
            )
            if k in merged:
                base_item = merged[k]
                eff_touches = (
                    higher_item.touches if higher_item.touches is not None else base_item.touches
                )
                eff_resources = (
                    higher_item.resources
                    if higher_item.resources is not None
                    else base_item.resources
                )
                merged[k] = SkillOptionItem(touches=eff_touches, resources=eff_resources)
            else:
                merged[k] = higher_item.model_copy()

    return merged


def merge_profile_with_override(
    profile: AgentProfile,
    override: AgentOverride,
) -> AgentProfile:
    """Смерживает базовый профиль агента с оверрайдом по спецификации."""
    # 1. skills
    if override.skills is not None:
        merged_skills = list(dict.fromkeys(override.skills))
    else:
        merged_skills = list(dict.fromkeys(profile.skills + override.extra_skills))

    # 2. permissions: field-level merge
    base_perms = profile.permissions.model_dump()
    if override.permissions is not None:
        for k, v in override.permissions.model_dump(exclude_unset=True).items():
            base_perms[k] = v
    merged_permissions = AgentPermissions.model_validate(base_perms)

    # 3. model
    model = override.model or profile.model

    # 4. instructions
    instructions = profile.instructions
    if override.instructions_suffix:
        instructions = f"{instructions}\n\n{override.instructions_suffix}".strip()

    # 5. mode
    mode = override.mode or profile.mode

    # 6. skill_options: field-level merge
    merged_skill_options = merge_skill_options(profile.skill_options, override.skill_options)

    return AgentProfile(
        name=profile.name,
        role=profile.role,
        description=profile.description,
        model=model,
        skills=merged_skills,
        permissions=merged_permissions,
        mode=mode,
        scope=profile.scope,
        instructions=instructions,
        tools=override.tools if override.tools is not None else profile.tools,
        model_name=override.model_name or profile.model_name,
        skill_options=merged_skill_options,
        # R2 (ревизия 4): подтверждение read-only переносится в merged-профиль
        # (оверрайд не может ни снять, ни установить его — это решение владельца
        # базового профиля, а не точечная настройка проекта).
        readonly_acknowledged=profile.readonly_acknowledged,
    )


def create_default_override(
    name: str,
    target: str,
    project: str | None = None,
) -> AgentOverride:
    """Формирует in-memory драфт оверрайда без записи на диск."""
    _validate_agent_key(name, target, project)
    return AgentOverride(
        base_agent=name,
        project=project,
        target=target,
        model=None,
        model_name=None,
        tools=None,
        permissions=None,
        extra_skills=[],
        skills=None,
        instructions_suffix="",
        mode=None,
    )


def _collect_available_slots(store: SkillsStorage, name: str) -> list[str]:
    """Собирает слоты скилла: предки (extends) + собственные через компилятор."""
    return SlotCompiler(store).collect_available_slots(name)


class AgentBundler:
    """Загружает профили агентов, компилирует их скиллы и экспортирует в целевые форматы."""

    def __init__(
        self,
        storage: SkillsStorage | None = None,
        compiler: SlotCompiler | None = None,
        skills_dir: Path | None = None,
    ) -> None:
        self.storage = storage
        self.compiler = compiler or (SlotCompiler(storage) if storage else None)
        # Явный каталог репозитория скиллов (напр. --skills-dir сервера):
        # карточки агентов ищутся в <skills_dir>/agents в приоритете, иначе
        # load_profile резолвит их через env/дефолтный кэш и --skills-dir
        # молча не действует (E2E: 'plan-reviewer не найден' при живой карточке).
        self.skills_dir = skills_dir

    def _get_storage(self) -> SkillsStorage:
        if self.storage is None:
            msg = "AgentBundler требует storage — передайте его в конструктор"
            raise ValueError(msg)
        return self.storage

    def _get_compiler(self) -> SlotCompiler:
        if self.compiler is None:
            self.compiler = SlotCompiler(self._get_storage())
        return self.compiler

    def find_override(
        self,
        name: str,
        target: str,
        project: str | None = None,
        project_root: Path | None = None,
    ) -> AgentOverride | None:
        """Ищет оверрайд по приоритету: локальный проектный > кэш проектный > локальный общий > кэш общий."""
        _validate_agent_key(name, target, project)
        clean_name = Path(name).name
        root = project_root or _find_default_project_root()
        agents_dir = get_agents_dir(root)
        agents_cache_dir = get_agents_cache_dir()

        candidates: list[Path] = []
        clean_project: str | None = None
        if project:
            clean_project = Path(project).name
            candidates.extend(
                [
                    agents_dir / f"{clean_name}_{clean_project}_{target}.override.yaml",
                    agents_dir / f"{clean_name}_{clean_project}_{target}.override.yml",
                    agents_cache_dir
                    / clean_name
                    / "overrides"
                    / f"{clean_name}_{clean_project}_{target}.override.yaml",
                    agents_cache_dir
                    / clean_name
                    / "overrides"
                    / f"{clean_name}_{clean_project}_{target}.override.yml",
                    agents_dir / f"{clean_name}_{clean_project}.override.yaml",
                    agents_dir / f"{clean_name}_{clean_project}.override.yml",
                    agents_cache_dir
                    / clean_name
                    / "overrides"
                    / f"{clean_name}_{clean_project}.override.yaml",
                    agents_cache_dir
                    / clean_name
                    / "overrides"
                    / f"{clean_name}_{clean_project}.override.yml",
                ]
            )
        candidates.extend(
            [
                agents_dir / f"{clean_name}_{target}.override.yaml",
                agents_dir / f"{clean_name}_{target}.override.yml",
                agents_cache_dir
                / clean_name
                / "overrides"
                / f"{clean_name}_{target}.override.yaml",
                agents_cache_dir / clean_name / "overrides" / f"{clean_name}_{target}.override.yml",
                agents_dir / f"{clean_name}.override.yaml",
                agents_dir / f"{clean_name}.override.yml",
                agents_cache_dir / clean_name / "overrides" / f"{clean_name}.override.yaml",
                agents_cache_dir / clean_name / "overrides" / f"{clean_name}.override.yml",
            ]
        )

        for p in candidates:
            if p.is_file():
                try:
                    data = yaml.safe_load(p.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        msg = f"Оверрайд в {p} должен быть YAML-словарём"
                        raise ValueError(msg)
                    override = AgentOverride.model_validate(data)
                except (yaml.YAMLError, ValidationError, ValueError) as e:
                    # Fail-closed: если специфичный оверрайд найден, но повреждён или невалиден,
                    # запрещено молча переходить к менее специфичному или драфту.
                    msg = f"Ошибка загрузки оверрайда из {p}: {e}"
                    raise ValueError(msg) from e

                if override.base_agent and override.base_agent != clean_name:
                    continue
                if override.target and override.target != target:
                    continue
                if override.project and clean_project and override.project != clean_project:
                    continue
                return override
        return None

    def load_profile(self, name: str, project_root: Path | None = None) -> AgentProfile:
        """Ищет и загружает профиль агента из файловой системы или создает дефолтный из скилла."""
        if not is_safe_name(name):
            msg = f"Недопустимое имя агента '{name}'"
            raise ValueError(msg)

        clean_name = Path(name).name
        root = project_root or _find_default_project_root()
        agents_dir = get_agents_dir(root)
        # Приоритет 0: явный skills_dir бандлера (напр. --skills-dir сервера).
        # Без него резолв падает в env/дефолтный кэш, и карточки репозитория
        # скиллов не находятся, хотя сервер запущен с правильным каталогом.
        agents_cache_dir = self.skills_dir / "agents" if self.skills_dir else get_agents_cache_dir()

        candidate_dirs = [agents_dir, agents_cache_dir]
        if self.skills_dir and agents_cache_dir != get_agents_cache_dir():
            candidate_dirs.append(get_agents_cache_dir())

        candidates: list[Path] = []
        for d in candidate_dirs:
            candidates.extend(
                [
                    d / clean_name / "AGENT.yaml",
                    d / clean_name / "AGENT.yml",
                    d / f"{clean_name}.yaml",
                    d / f"{clean_name}.yml",
                    d / f"{clean_name}.json",
                ]
            )

        for p in candidates:
            if p.is_file():
                raw_text = p.read_text(encoding="utf-8")
                data = json.loads(raw_text) if p.suffix == ".json" else yaml.safe_load(raw_text)
                if isinstance(data, dict):
                    return AgentProfile.model_validate(data)

        # Fallback: создание профиля на базе одноименного скилла
        store = self._get_storage()
        skill_res = store.get_skill(clean_name)
        if skill_res:
            card, _ = skill_res
            return AgentProfile(
                name=card.name,
                role=f"{card.name.replace('-', ' ').title()} Specialist",
                description=card.description or card.title,
                skills=[card.name],
            )

        searched = "; ".join(str(d) for d in candidate_dirs)
        msg = (
            f"Профиль агента '{clean_name}' не найден. Проверенные каталоги: {searched}. "
            "Добавьте карточку <dir>/<name>/AGENT.yaml или скилл с этим именем."
        )
        raise ValueError(msg)

    def _deliver_resources(
        self,
        skill_name: str,
        selected: list[str] | None,
        discovered: list[dict[str, Any]],
        cat_root: Path,
        base_dir: Path | None,
        root: Path,
        *,
        offline: bool,
    ) -> list[str]:
        for resource_id in selected or []:
            if not is_resource_associated_with_skill(
                skill_name,
                resource_id,
                cat_root,
                base_dir,
                storage=self._get_storage(),
                project_root=root,
            ):
                raise ValueError(
                    f"Ресурс '{resource_id}' не связан со скиллом-владельцем '{skill_name}'"
                )
        if not offline:
            return []
        resource_ids = (
            selected
            if selected is not None
            else [
                r["resource_id"] + ("#" + r["anchor"] if r.get("anchor") else "")
                for r in discovered
            ]
        )
        return [
            f"### Вложенный ресурс: {rid}\n\n{read_resource_text(rid, cat_root, project_root=root)}"
            for rid in dict.fromkeys(resource_ids)
        ]

    def bundle_agent(
        self,
        name: str | None = None,
        profile: AgentProfile | None = None,
        target: str = "antigravity",
        project_root: Path | None = None,
        project: str | None = None,
        override: AgentOverride | None = None,
        skill_options: Mapping[str, SkillOptionItem | dict[str, Any]] | None = None,
    ) -> str:
        """Собирает субагента со всеми его скиллами под целевой рантайм."""
        root = project_root or _find_default_project_root()

        target_profile = profile
        if target_profile is None:
            if not name:
                msg = "Необходимо указать 'name' или передать готовый 'profile'"
                raise ValueError(msg)
            target_profile = self.load_profile(name, root)

            canonical_proj = project or detect_canonical_project_name(root)
            eff_override = override
            if eff_override is None:
                eff_override = self.find_override(name, target, canonical_proj, root)
            if eff_override is not None:
                target_profile = merge_profile_with_override(target_profile, eff_override)

        # Смерживаем переданные настройки с настройками профиля
        eff_skill_options = merge_skill_options(target_profile.skill_options, skill_options)

        # Валидация неизвестных скиллов в явно переданных skill_options
        if skill_options:
            for k in skill_options:
                if k not in target_profile.skills:
                    msg = (
                        f"Неизвестный скилл '{k}' в skill_options агента '{target_profile.name}'. "
                        f"Допустимые скиллы: {target_profile.skills}"
                    )
                    raise ValueError(msg)

        comp = self._get_compiler()
        # D6: единый ProjectContext — тот же источник стека/режима, что в
        # search_skills и load_skill (режим определяется ОДИН раз, а не
        # тремя разными способами). Ось режима ортогональна технологиям
        # (stack_detector.detect_mode): 'mcp'/'offline' выбирает
        # blocks/<slot>/{mcp,offline}.md, если слот дуальный. Без неё бандл
        # mcp-проекта получал бы default-блок.
        ctx = get_project_context(root)
        stack, _ = supplement_mode_axis(ctx.stack, ctx.mode)

        compiled_skills: list[str] = []
        embedded_resources_content: list[str] = []
        for skill_name in target_profile.skills:
            try:
                opt_item = eff_skill_options.get(skill_name)
                req_touches = opt_item.touches if opt_item is not None else None
                req_resources = opt_item.resources if opt_item is not None else None

                _, content, skill_resources = comp.assemble_skill_with_resources(
                    name=skill_name,
                    touches=req_touches,
                    project_stack=stack,
                    project_root=root,
                )
                compiled_skills.append(content)

                res_card = self._get_storage().get_skill(skill_name)
                base_dir = res_card[0].base_dir if res_card else None
                cat_root = comp._detect_catalog_root(base_dir) or root
                embedded_resources_content.extend(
                    self._deliver_resources(
                        skill_name,
                        req_resources,
                        skill_resources,
                        cat_root,
                        base_dir,
                        root,
                        offline=not target_profile.permissions.mcp,
                    )
                )
            except SkillNotFoundError:
                # Если скилл отсутствует, добавляем предупреждение
                compiled_skills.append(f"> ⚠️ [Скилл '{skill_name}' не найден в реестре]")

        compiled_skills_content = "\n\n---\n\n".join(compiled_skills)
        if embedded_resources_content:
            compiled_skills_content += (
                "\n\n---\n\n## Вложенные справочные ресурсы\n\n"
                + "\n\n---\n\n".join(embedded_resources_content)
            )

        adapter = get_adapter(target)
        exported = adapter.export_agent(target_profile, compiled_skills_content)
        return TokenGuard().enforce_budget(exported, raise_on_core_overflow=True)[0]

    def install_workflow(
        self,
        name: str,
        target: str = "antigravity",
        project_root: Path | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Экспортирует и сохраняет артефакты агента в рабочем пространстве проекта."""
        if not is_safe_name(name):
            msg = f"Недопустимое имя агента '{name}'"
            raise ValueError(msg)

        clean_name = Path(name).name
        root = project_root or _find_default_project_root()
        canonical_proj = project or detect_canonical_project_name(root)
        content = self.bundle_agent(
            name=clean_name,
            target=target,
            project_root=root,
            project=canonical_proj,
        )

        if target == "opencode":
            dest_dir = root / ".opencode" / "agents"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_file = dest_dir / f"{clean_name}.md"
        elif target == "cursor":
            dest_dir = root / ".cursor" / "rules"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_file = dest_dir / f"{clean_name}.mdc"
        else:
            dest_dir = root / ".agents" / "workflows"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_file = dest_dir / f"{clean_name}.json"

        # defense-in-depth: имя уже проверено is_safe_name (clean_name без
        # разделителей путей), поэтому ветка недостижима при текущих инвариантах;
        # проверка сохранена на случай будущих изменений в формировании dest_dir.
        if not dest_file.resolve().is_relative_to(dest_dir.resolve()):
            msg = f"Небезопасный путь к файлу воркфлоу '{dest_file}'"
            raise ValueError(msg)

        dest_file.write_text(content, encoding="utf-8")
        return {
            "success": True,
            "agent": clean_name,
            "target": target,
            "installed_path": str(dest_file),
            "path": str(dest_file),
        }
