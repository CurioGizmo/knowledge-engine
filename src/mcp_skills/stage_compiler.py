"""Компилятор этапов, инструкций и ресурсов сценариев (Flow Stage Compiler)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from mcp_core.paths import get_cache_dir, resolve_skills_repo_dir
from mcp_skills.bundler import (
    AgentBundler,
    TokenGuard,
    detect_canonical_project_name,
    merge_profile_with_override,
)
from mcp_skills.errors import AssemblyBudgetExceededError, SkillNotFoundError
from mcp_skills.models import (
    AgentProfile,
    FlowDefinition,
    FlowSession,
    FlowStage,
    FlowStatus,
)
from mcp_skills.orchestration_state import summarize_orchestration_state
from mcp_skills.resources import (
    determine_resource_type,
    is_resource_associated_with_skill,
    resolve_link_to_resource_id,
)
from mcp_skills.slot_compiler import SlotCompiler
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer

logger = logging.getLogger("mcp_skills.stage_compiler")

__all__ = [
    "_append_flow_payload_metadata",
    "_build_stage_payload",
    "_bundle_stage_agent",
    "_catalog_root",
    "_collect_available_slots_for_skill",
    "_compile_stage_skills",
    "_completed_response",
    "_compute_escalate_next_call",
    "_compute_stage_next_call",
    "_compute_stage_required_inputs",
    "_extract_stage_resources",
    "_instruction_resource_candidates",
    "_resource_descriptor",
    "_resource_owner",
    "_same_option_value",
    "_skills_sync_dirs",
    "_stage_agent_context",
    "_stage_gate_hint",
    "_stage_instruction",
    "_validate_stage_skill_options",
]


def _same_option_value(left: Any, right: Any) -> bool:
    """JSON scalar equality without treating ``True`` as ``1``."""
    return type(left) is type(right) and left == right


def _compute_stage_required_inputs(stage: FlowStage) -> list[str]:
    """Вычисляет список обязательных входных данных для перехода с текущего этапа."""
    inputs: list[str] = []
    if stage.meta.deliverable_must_exist or stage.meta.target_file:
        inputs.append("deliverable")
    if stage.meta.require_verdict or stage.meta.agent:
        inputs.append("verdict")
    if stage.meta.require_human_confirm:
        inputs.append("human_confirmed")
    if stage.meta.transition_required:
        inputs.append("transition")
    return inputs


def _compute_stage_next_call(
    session: FlowSession,
    stage: FlowStage,
    root: Path | str,
    target: str = "antigravity",
) -> dict[str, Any]:
    """Формирует шаблон следующего вызова step_flow для активного этапа."""
    args: dict[str, Any] = {
        "action": "advance",
        "project_path": str(root),
        "session_id": session.id,
        "expected_stage_id": stage.id,
        "target": target,
    }
    return {
        "tool": "step_flow",
        "arguments": args,
    }


def _compute_escalate_next_call(
    session: FlowSession,
    root: Path | str,
    target: str = "antigravity",
) -> dict[str, Any]:
    """Формирует вызов разрешения эскалации для заблокированной Circuit Breaker сессии."""
    args: dict[str, Any] = {
        "action": "escalation_resolve",
        "project_path": str(root),
        "session_id": session.id,
        "target": target,
    }
    return {
        "tool": "step_flow",
        "arguments": args,
    }


def _bundle_stage_agent(
    stage: FlowStage,
    root: Path,
    bundler: AgentBundler | None = None,
    skills_storage: SkillsStorage | None = None,
    profile: AgentProfile | None = None,
    target: str = "antigravity",
) -> dict[str, Any] | None:
    """Генерирует бандл субагента этапа с контролем лимита токенов TokenGuard."""
    if not stage.meta.agent:
        return None

    try:
        b = bundler
        if b is None:
            db_path = get_cache_dir(root) / "skills_fts.db"
            store = skills_storage or (SkillsStorage(db_path) if db_path.exists() else None)
            if store is None:
                store = SkillsStorage(get_cache_dir(root) / "skills_fts.db")
                skills_dirs = _skills_sync_dirs(root)
                if skills_dirs:
                    SkillsSynchronizer(store, skills_dirs).sync()
            b = AgentBundler(storage=store)

        eff_profile = profile
        if eff_profile is None:
            eff_profile = b.load_profile(stage.meta.agent, root)
            canonical_proj = detect_canonical_project_name(root)
            eff_override = b.find_override(stage.meta.agent, target, canonical_proj, root)
            if eff_override is not None:
                eff_profile = merge_profile_with_override(eff_profile, eff_override)

        # Передаем в бандлер только те настройки, которые относятся к скиллам агента
        agent_skill_options = {
            k: v for k, v in stage.meta.skill_options.items() if k in eff_profile.skills
        }

        bundled_text = b.bundle_agent(
            stage.meta.agent,
            profile=eff_profile,
            target=target,
            project_root=root,
            skill_options=agent_skill_options,
        )
        guard = TokenGuard()
        enforced_text, truncated = guard.enforce_budget(bundled_text, raise_on_core_overflow=True)
        return {
            "name": stage.meta.agent,
            "bundle": enforced_text,
            "truncated": truncated,
        }
    except AssemblyBudgetExceededError:
        raise
    except Exception as err:
        return {
            "name": stage.meta.agent,
            "error": str(err),
            "bundle": None,
            "truncated": False,
        }


def _compile_stage_skills(
    stage: FlowStage,
    root: Path,
    skills_storage: SkillsStorage | None = None,
    catalog_root: Path | None = None,
) -> tuple[str, list[str], bool]:
    """Компилирует скиллы этапа (stage.meta.skills) в дополнение инструкции."""
    if not stage.meta.skills:
        return "", [], False

    from mcp_skills.project_context import get_project_context, supplement_mode_axis

    store = skills_storage
    if store is None:
        db_path = get_cache_dir(root) / "skills_fts.db"
        if not db_path.exists():
            return "", [], False
        store = SkillsStorage(db_path)

    compiled_parts: list[str] = []
    compiled_names: list[str] = []
    truncated = False

    ctx = get_project_context(root)
    stack, _ = supplement_mode_axis(ctx.stack, ctx.mode)
    compiler = SlotCompiler(store, catalog_root=catalog_root)

    for skill_name in stage.meta.skills:
        try:
            opt_item = stage.meta.skill_options.get(skill_name)
            touches = opt_item.touches if opt_item is not None else None
            _, content = compiler.assemble_skill(
                name=skill_name,
                touches=touches,
                project_stack=stack,
                project_root=root,
            )
            compiled_parts.append(content)
            compiled_names.append(skill_name)
        except SkillNotFoundError:
            compiled_parts.append(f"> ⚠️ [Скилл '{skill_name}' не найден в реестре]")

    supplement = "\n\n---\n\n".join(compiled_parts) if compiled_parts else ""
    if supplement:
        guard = TokenGuard()
        if not guard.is_within_budget(supplement):
            supplement, truncated = guard.enforce_budget(supplement, raise_on_core_overflow=True)
    return supplement, compiled_names, truncated


def _collect_available_slots_for_skill(store: SkillsStorage, name: str) -> list[str]:
    """Слоты скилла с extends-предками через компилятор."""
    return SlotCompiler(store).collect_available_slots(name)


def _skills_sync_dirs(root: Path) -> list[Path]:
    """Каталоги скиллов для синхронизации кэша: репозиторий скиллов + проект."""
    dirs: list[Path] = []
    repo = resolve_skills_repo_dir()
    skills_base = repo if repo.name == "skills" else repo / "skills"
    if skills_base.is_dir():
        dirs.append(skills_base)
    proj_skills = root / ".agents" / "skills"
    if proj_skills.is_dir():
        dirs.append(proj_skills)
    return dirs


def _catalog_root(skills_dir: Path | None) -> Path:
    repo = skills_dir if skills_dir is not None else resolve_skills_repo_dir()
    return repo.parent if repo.name == "skills" else repo


def _instruction_resource_candidates(
    stage: FlowStage, cat_root: Path, root: Path
) -> list[tuple[str, str | None, str]]:
    candidates: list[tuple[str, str | None, str]] = []
    pattern = r'load_skill\(\s*name=["\'](?P<skill>[a-zA-Z0-9_-]+)["\']\s*,\s*resource_id=["\'](?P<res>[^"\']+)["\']\s*\)'
    for match in re.finditer(pattern, stage.instruction):
        candidates.append((match.group("res"), match.group("skill"), Path(match.group("res")).name))
    origin = stage.source_file or cat_root / "__stage__.md"
    for title, link in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", stage.instruction):
        res_id, anchor = resolve_link_to_resource_id(link, origin, cat_root, root)
        if res_id is not None:
            full_id = f"{res_id}#{anchor}" if anchor else res_id
            candidates.append((full_id, None, title.strip("`")))
    for template in re.findall(r"(?:templates/[a-zA-Z0-9_.-]+\.template\.md)", stage.instruction):
        candidates.append((template, None, Path(template).name))
    return candidates


def _resource_descriptor(resource_id: str, owner: str, title: str) -> dict[str, Any]:
    return {
        "resource_id": resource_id,
        "owner_skill": owner,
        "title": title,
        "type": determine_resource_type(resource_id),
    }


def _resource_owner(
    resource_id: str, owner: str | None, store: SkillsStorage | None, cat_root: Path, root: Path
) -> str:
    cards = (
        ([entry[0]] if (entry := store.get_skill(owner)) else [])
        if owner and store
        else (store.list_all_skills() if store else [])
    )
    for card in cards:
        try:
            associated = is_resource_associated_with_skill(
                card.name, resource_id, cat_root, card.base_dir, storage=store, project_root=root
            )
        except FileNotFoundError:
            break
        if associated:
            return card.name
    if owner is not None:
        raise ValueError(
            f"Ресурс '{resource_id}' не связан со скиллом-владельцем '{owner}' или недоступен"
        )
    raise ValueError(
        f"Ресурс '{resource_id}' не имеет объявленного скилла-владельца или недоступен"
    )


def _extract_stage_resources(
    stage: FlowStage,
    root: Path,
    skills_storage: SkillsStorage | None = None,
    catalog_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Общий resolver для origin-relative ссылок, выбранных ресурсов и блоков этапа."""
    cat_root = catalog_root or _catalog_root(None)
    store = skills_storage
    if store is None:
        db_path = get_cache_dir(root) / "skills_fts.db"
        store = SkillsStorage(db_path) if db_path.exists() else None
    found: dict[tuple[str, str], dict[str, Any]] = {}
    candidates = _instruction_resource_candidates(stage, cat_root, root)
    for skill, options in stage.meta.skill_options.items():
        candidates.extend((rid, skill, Path(rid).name) for rid in options.resources or [])
    for rid, owner, title in candidates:
        owner = _resource_owner(rid, owner, store, cat_root, root)
        found[(owner, rid)] = _resource_descriptor(rid, owner, title)
    if store is not None:
        from mcp_skills.project_context import get_project_context, supplement_mode_axis

        ctx = get_project_context(root)
        stack, _ = supplement_mode_axis(ctx.stack, ctx.mode)
        compiler = SlotCompiler(store, catalog_root=cat_root)
        for name in stage.meta.skills:
            if store.get_skill(name) is None:
                continue
            selected_options = stage.meta.skill_options.get(name)
            _, _, resources = compiler.assemble_skill_with_resources(
                name,
                touches=selected_options.touches if selected_options else None,
                project_stack=stack,
                project_root=root,
            )
            for resource in resources:
                rid = resource["resource_id"]
                if resource.get("anchor"):
                    rid += "#" + resource["anchor"]
                resource = {**resource, "resource_id": rid}
                found.setdefault((name, rid), resource)
    return list(found.values())


def _stage_agent_context(
    stage: FlowStage,
    root: Path,
    bundler: AgentBundler | None,
    store: SkillsStorage | None,
    cat_root: Path,
    target: str = "antigravity",
) -> tuple[AgentBundler | None, AgentProfile | None]:
    if not stage.meta.agent:
        return bundler, None
    if bundler is None:
        store = store or SkillsStorage(get_cache_dir(root) / "skills_fts.db")
        skills_dirs = [cat_root / "skills", root / ".agents" / "skills"]
        SkillsSynchronizer(store, [path for path in skills_dirs if path.is_dir()]).sync()
        bundler = AgentBundler(
            storage=store, compiler=SlotCompiler(store, catalog_root=cat_root), skills_dir=cat_root
        )
    try:
        profile = bundler.load_profile(stage.meta.agent, root)
        override = bundler.find_override(
            stage.meta.agent, target, detect_canonical_project_name(root), root
        )
        if override is not None:
            profile = merge_profile_with_override(profile, override)
        return bundler, profile
    except (ValueError, OSError) as err:
        logger.warning("Не удалось загрузить профиль агента этапа %s: %s", stage.id, err)
        return bundler, None


def _validate_stage_skill_options(
    stage: FlowStage,
    profile: AgentProfile | None,
    root: Path,
    store: SkillsStorage | None,
    cat_root: Path,
) -> None:
    allowed = set(stage.meta.skills) | (set(profile.skills) if profile else set())
    if store is None:
        db_path = get_cache_dir(root) / "skills_fts.db"
        store = SkillsStorage(db_path) if db_path.exists() else None
    for name, options in stage.meta.skill_options.items():
        if name not in allowed:
            raise ValueError(
                f"Неизвестный скилл '{name}' в skill_options этапа '{stage.id}'. Допустимые скиллы: {sorted(allowed)}"
            )
        if store is None:
            continue
        available = SlotCompiler(store, catalog_root=cat_root).collect_available_slots(name)
        unknown = [slot for slot in options.touches or [] if slot not in available]
        if unknown:
            raise ValueError(
                f"Неизвестные секции для скилла '{name}' в skill_options этапа '{stage.id}': {unknown}. Доступные слоты: {available}"
            )
        for resource_id in options.resources or []:
            _resource_owner(resource_id, name, store, cat_root, root)


def _stage_instruction(
    stage: FlowStage, root: Path, store: SkillsStorage | None, cat_root: Path
) -> dict[str, Any]:
    instruction = stage.instruction
    details: dict[str, Any] = {}
    if stage.meta.skills:
        supplement, names, trimmed = _compile_stage_skills(stage, root, store, cat_root)
        if supplement:
            instruction += f"\n\n---\n\n# Скиллы этапа\n\n{supplement}"
        details.update(skills_compiled=names, skills_truncated=trimmed)
    instruction, trimmed = TokenGuard().enforce_budget(instruction, raise_on_core_overflow=True)
    details["instruction"] = instruction
    if trimmed:
        details["skills_truncated"] = True
    return details


def _stage_gate_hint(stage: FlowStage, root: Path) -> str | None:
    meta = stage.meta
    if not (
        meta.validator
        or meta.require_verdict
        or meta.require_human_confirm
        or meta.deliverable_must_exist
    ):
        return None
    note = (
        f"При следующем advance валидатор '{meta.validator}' проверит deliverable"
        if meta.validator
        else "При следующем advance действуют требования качества"
    )
    if meta.target_file:
        note += f": файл '{meta.target_file}' в корне проекта ({root}) — создайте его там заранее"
    if meta.require_verdict:
        note += "; этап требует явный verdict (approved/needs_work)"
    if meta.require_human_confirm:
        note += "; этап требует подтверждения человека — передайте human_confirmed=true после явного согласования с пользователем"
    if meta.deliverable_must_exist:
        note += "; deliverable должен быть существующим файлом"
    return note


def _append_flow_payload_metadata(
    payload: dict[str, Any], stage: FlowStage, session: FlowSession
) -> None:
    if "flow_options" in session.context_data:
        payload["flow_options"] = session.context_data["flow_options"]
        payload["requested_flow"] = session.context_data.get("requested_flow", session.flow_name)
        payload["canonical_flow"] = session.context_data.get("canonical_flow", session.flow_name)
    if stage.meta.transitions:
        effective_options = session.context_data.get("flow_options", {})
        transition_counts = session.context_data.get("transition_counts", {})
        payload["available_transitions"] = [
            label
            for label, spec in stage.meta.transitions.items()
            if all(
                key in effective_options and _same_option_value(effective_options[key], value)
                for key, value in spec.when_options.items()
            )
            and transition_counts.get(f"{stage.id}:{label}", 0) < spec.max_uses
        ]


def _build_stage_payload(
    stage: FlowStage,
    total_stages: int,
    session: FlowSession,
    root: Path,
    bundler: AgentBundler | None = None,
    skills_storage: SkillsStorage | None = None,
    skills_dir: Path | None = None,
    target: str = "antigravity",
) -> dict[str, Any]:
    """Формирует структурированный ответ этапа для передачи модели."""
    payload: dict[str, Any] = {
        "status": session.status.value,
        "session_id": session.id,
        "flow_name": session.flow_name,
        "stage": {
            "id": stage.id,
            "title": stage.title,
            "order": stage.order,
            "total_stages": total_stages,
            "instruction": stage.instruction,
            "meta": stage.meta.model_dump(),
        },
    }

    cat_root = _catalog_root(skills_dir)

    bundler, effective_agent_profile = _stage_agent_context(
        stage, root, bundler, skills_storage, cat_root, target=target
    )
    _validate_stage_skill_options(stage, effective_agent_profile, root, skills_storage, cat_root)

    stage_resources = _extract_stage_resources(stage, root, skills_storage, cat_root)
    if stage_resources:
        payload["stage"]["resources"] = stage_resources

    payload["stage"].update(_stage_instruction(stage, root, skills_storage, cat_root))
    if stage.meta.required_tools:
        payload["required_tools"] = stage.meta.required_tools

    if stage.meta.agent:
        agent_bundle = _bundle_stage_agent(
            stage, root, bundler, skills_storage, effective_agent_profile, target=target
        )
        if agent_bundle:
            payload["subagent"] = agent_bundle
        if effective_agent_profile is not None:
            req_caps: list[str] = []
            if effective_agent_profile.permissions.write:
                req_caps.append("write")
            if effective_agent_profile.permissions.subagents:
                req_caps.append("subagents")
            if effective_agent_profile.permissions.mcp:
                req_caps.append("mcp")
            payload["agent_spec"] = {
                "name": stage.meta.agent,
                "role": effective_agent_profile.role,
                "description": effective_agent_profile.description,
                "instructions": effective_agent_profile.instructions,
                "skills": effective_agent_profile.skills,
                "permissions": effective_agent_profile.permissions.model_dump(),
                "tools": effective_agent_profile.tools,
                "required_capabilities": req_caps,
            }

    if stage.meta.dispatch_agents:
        payload["dispatch_agents"] = True
        policy = stage.meta.dispatch_policy
        if policy is not None:
            payload["dispatch_policy"] = policy.model_dump()
        elif session.context_data.get("dispatch_policy") is not None:
            payload["dispatch_policy"] = session.context_data["dispatch_policy"]
        payload["hint"] = (
            "Запустите субагентов нативным механизмом хоста (например, invoke_subagent в "
            "Antigravity или task/subagent в OpenCode), дождитесь завершения, затем передайте результаты: "
            "step_flow(action='advance', deliverable=...). Движок субагентов сам "
            "не запускает — он библиотека."
        )

    gate_hint = _stage_gate_hint(stage, root)
    if gate_hint:
        payload["gate_hint"] = gate_hint
    if stage.meta.loop_max is not None and stage.meta.loop_max < 5:
        payload["loop_hint"] = (
            f"Максимум итераций этапа: {stage.meta.loop_max}. При verdict='needs_work' "
            "счётчик растёт; превышение — Circuit Breaker (эскалация к пользователю)."
        )

    payload["required_inputs"] = _compute_stage_required_inputs(stage)
    payload["next_call"] = _compute_stage_next_call(session, stage, root, target=target)
    _append_flow_payload_metadata(payload, stage, session)
    orch_summary = summarize_orchestration_state(session.context_data.get("orchestration"))
    if orch_summary is not None:
        payload["orchestration_state"] = orch_summary

    return payload


def _completed_response(session: FlowSession, flow_def: FlowDefinition) -> dict[str, Any]:
    """Completed-ответ сессии с мостики «куда идти дальше» (F1)."""
    res: dict[str, Any] = {
        "status": FlowStatus.COMPLETED.value,
        "session_id": session.id,
        "flow_name": session.flow_name,
        "message": f"Флоу '{session.flow_name}' успешно завершен!",
        "required_inputs": [],
        "next_call": None,
    }
    orch_summary = summarize_orchestration_state(session.context_data.get("orchestration"))
    if orch_summary is not None:
        res["orchestration_state"] = orch_summary
    if flow_def.next_flows:
        res["next_flows"] = list(flow_def.next_flows)
        res["next_flow_hint"] = "Следующие сценарии после завершения: " + ", ".join(
            f"'{nf}' (step_flow(flow='{nf}'))" for nf in flow_def.next_flows
        )
    return res
