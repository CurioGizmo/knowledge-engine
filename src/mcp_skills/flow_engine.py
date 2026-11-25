"""Исполнительный движок пайплайна воркфлоу (Workflow State Machine) и реестр валидаторов."""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from mcp_core.paths import (
    _find_default_project_root,
    safe_read_text,
)
from mcp_skills.adapters import SUPPORTED_TARGETS
from mcp_skills.bundler import AgentBundler
from mcp_skills.errors import (
    AmbiguousSessionError,
    FlowError,
    GateValidationError,
    InvalidFlowFormatError,
    SessionConflictError,
    SessionNotFoundError,
    StageMismatchError,
)
from mcp_skills.flow_parser import list_available_flows, load_flow_definition
from mcp_skills.flow_storage import FlowStorage
from mcp_skills.git_manager import get_current_branch
from mcp_skills.models import (
    FlowDefinition,
    FlowSession,
    FlowStage,
    FlowStatus,
    FlowVerdict,
)
from mcp_skills.orchestration_state import (
    init_orchestration_state,
    is_orchestration_flow,
    summarize_orchestration_state,
    sync_orchestration_file,
    update_orchestration_state,
)
from mcp_skills.spam_gate import is_safe_name
from mcp_skills.stage_compiler import (
    _build_stage_payload,
    _bundle_stage_agent,
    _compile_stage_skills,
    _completed_response,
    _compute_escalate_next_call,
    _compute_stage_next_call,
    _compute_stage_required_inputs,
    _extract_stage_resources,
)
from mcp_skills.storage import SkillsStorage
from mcp_skills.validators import (
    VALIDATOR_REGISTRY,
    ValidationResult,
    _looks_like_path,
    validate_plan,
    validate_qa_report,
    validate_review,
)

logger = logging.getLogger("mcp_skills.flow_engine")

__all__ = [
    "VALIDATOR_REGISTRY",
    "FlowEngine",
    "ValidationResult",
    "_build_stage_payload",
    "_bundle_stage_agent",
    "_compile_stage_skills",
    "_extract_stage_resources",
    "abort_flow",
    "advance_flow",
    "get_flow_status",
    "list_flows",
    "resolve_flow_escalation",
    "start_flow",
    "step_flow",
    "validate_plan",
    "validate_qa_report",
    "validate_review",
]

MAX_DELIVERABLE_BYTES = 64 * 1024  # 64 KB
MAX_FLOW_REDIRECTS = 8


def _same_option_value(left: Any, right: Any) -> bool:
    """JSON scalar equality without treating ``True`` as ``1``."""
    return type(left) is type(right) and left == right


def _resolve_flow_definition(
    name: str, root: Path, skills_dir: Path | None
) -> tuple[FlowDefinition, list[FlowDefinition]]:
    """Follow deprecated wrappers to a canonical definition, guarding loops."""
    chain: list[FlowDefinition] = []
    seen: set[str] = set()
    current = name
    for _ in range(MAX_FLOW_REDIRECTS + 1):
        if current in seen:
            raise InvalidFlowFormatError(f"Цикл перенаправлений flow: '{current}'")
        seen.add(current)
        definition = load_flow_definition(current, root, skills_dir=skills_dir)
        chain.append(definition)
        if not definition.deprecated:
            return definition, chain
        if not definition.superseded_by:
            raise InvalidFlowFormatError(
                f"Устаревший flow '{definition.name}' не содержит superseded_by"
            )
        current = definition.superseded_by
    raise InvalidFlowFormatError(f"Слишком длинная цепочка перенаправлений flow '{name}'")


def _resolve_flow_options(
    definition: FlowDefinition,
    chain: list[FlowDefinition],
    explicit: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve schema, wrapper and explicit defaults with stable rule precedence."""
    if explicit is not None and not isinstance(explicit, dict):
        raise ValueError("flow_options должен быть объектом")
    schema = definition.option_schema

    def validate(values: dict[str, Any]) -> None:
        for key, value in values.items():
            spec = schema.get(key)
            if spec is None:
                raise ValueError(f"Неизвестный flow option '{key}'")
            if not any(_same_option_value(value, allowed) for allowed in spec.allowed):
                raise ValueError(f"Недопустимое значение flow option '{key}': {value!r}")

    effective = {key: spec.default for key, spec in schema.items()}
    source = dict.fromkeys(effective, "schema")
    validate(definition.defaults)
    effective.update(definition.defaults)
    source.update(dict.fromkeys(definition.defaults, "wrapper"))
    # A nearer wrapper wins over one further from the requested name.
    for wrapper in reversed(chain[:-1]):
        validate(wrapper.defaults)
        effective.update(wrapper.defaults)
        source.update(dict.fromkeys(wrapper.defaults, "wrapper"))
    validate(explicit or {})
    effective.update(explicit or {})
    source.update(dict.fromkeys(explicit or {}, "explicit"))
    provisional = effective.copy()
    written_by_rule: set[str] = set()
    for rule in definition.option_rules:
        if not all(
            key in provisional and _same_option_value(provisional[key], value)
            for key, value in rule.when.items()
        ):
            continue
        for key, value in rule.defaults.items():
            if source.get(key) == "schema" and key not in written_by_rule:
                effective[key] = value
                written_by_rule.add(key)
    validate(effective)
    return effective


def _flows_equivalent(name: str, active_name: str, root: Path, skills_dir: Path | None) -> bool:
    if name == active_name:
        return True
    requested, _ = _resolve_flow_definition(name, root, skills_dir)
    existing, _ = _resolve_flow_definition(active_name, root, skills_dir)
    return requested.name == existing.name


def _validate_start_only_arguments(
    action: str, flow_options: dict[str, Any] | None, transition: str | None
) -> None:
    if flow_options is not None and action != "advance":
        raise ValueError("flow_options допустимы только при старте новой сессии")
    if transition is not None and action != "advance":
        raise ValueError("transition допустим только для action='advance'")


def _normalize_verdict(verdict: FlowVerdict | str | None) -> FlowVerdict | None:
    """Нормализует вердикт (enum/строка/None) без падения: невалидная строка → None."""
    if isinstance(verdict, FlowVerdict):
        return verdict
    if isinstance(verdict, str):
        try:
            return FlowVerdict(verdict)
        except ValueError:
            return None
    return None


def _validate_target(target: str | None) -> str:
    """Проверяет допустимость целевого рантайма хоста."""
    t = (target or "antigravity").lower()
    if t not in SUPPORTED_TARGETS:
        valid = ", ".join(SUPPORTED_TARGETS)
        msg = f"Неизвестный целевой рантайм '{target}'. Допустимые: {valid}"
        raise ValueError(msg)
    return t


def _maybe_update_orchestration(
    session: FlowSession,
    flow_def: FlowDefinition | None,
    root: Path,
    *,
    stage_id: str,
    stage_validator: str | None = None,
    stage_iteration: int | None = None,
    deliverable: Any = None,
    verdict: str | None = None,
    notes: str = "",
    final_status_override: str | None = None,
) -> str | None:
    """Обновляет context_data['orchestration'] до _build_stage_payload и update_session."""
    has_orch = isinstance(session.context_data, dict) and isinstance(
        session.context_data.get("orchestration"), dict
    )
    if not has_orch and (flow_def is None or not is_orchestration_flow(flow_def)):
        return None
    effective_def = flow_def or FlowDefinition(name=session.flow_name, title=session.flow_name)
    _, renamed_from = update_orchestration_state(
        session,
        effective_def,
        root,
        stage_id=stage_id,
        stage_validator=stage_validator,
        stage_iteration=stage_iteration,
        deliverable=deliverable,
        verdict=verdict,
        notes=notes,
        final_status_override=final_status_override,
    )
    return renamed_from


def _aborted_response(session: FlowSession, message: str) -> dict[str, Any]:
    res: dict[str, Any] = {
        "status": FlowStatus.ABORTED.value,
        "session_id": session.id,
        "flow_name": session.flow_name,
        "message": message,
        "required_inputs": [],
        "next_call": None,
    }
    orch_summary = (
        summarize_orchestration_state(session.context_data.get("orchestration"))
        if isinstance(session.context_data, dict)
        else None
    )
    if orch_summary is not None:
        res["orchestration_state"] = orch_summary
    return res


def _sync_orchestration_after_commit(
    root: Path,
    session: FlowSession,
    renamed_from: str | None = None,
) -> None:
    """Материализует orchestration-state.md на диск строго после коммита в SQLite."""
    if not isinstance(session.context_data, dict):
        return
    orch = session.context_data.get("orchestration")
    if isinstance(orch, dict):
        sync_orchestration_file(root, orch, previous_path=renamed_from)


def _apply_needs_work(
    session: FlowSession,
    current_stage: FlowStage,
    notes: str,
    storage: FlowStorage,
    event: str,
    notes_default: str,
    root: Path | None = None,
    target: str = "antigravity",
    flow_def: FlowDefinition | None = None,
    deliverable: Any = None,
) -> dict[str, Any]:
    """Фиксирует вердикт needs_work: этап удерживается, итерация +1, Circuit Breaker.

    Общая логика для этапов с ревьюером и без него (D4): счётчик итераций
    растёт, при превышении loop_max сессия уходит в blocked_escalate,
    иначе — активна и ждёт доработки на том же этапе.
    """
    effective_root = root if root is not None else Path(session.project_root)
    evaluated_iteration = session.iteration_count
    session.iteration_count += 1
    if session.iteration_count > current_stage.meta.loop_max:
        session.status = FlowStatus.BLOCKED_ESCALATE
        renamed_from = _maybe_update_orchestration(
            session,
            flow_def,
            effective_root,
            stage_id=current_stage.id,
            stage_validator=current_stage.meta.validator,
            stage_iteration=evaluated_iteration,
            deliverable=deliverable,
            verdict=FlowVerdict.NEEDS_WORK.value,
            notes=notes,
            final_status_override="BLOCKED",
        )
        storage.update_session(
            session,
            history_events=[
                {
                    "stage_id": current_stage.id,
                    "iteration": session.iteration_count,
                    "event": "circuit_breaker_tripped",
                    "notes": f"Превышен лимит итераций ({current_stage.meta.loop_max})",
                }
            ],
        )
        _sync_orchestration_after_commit(effective_root, session, renamed_from)
        res = {
            "status": FlowStatus.BLOCKED_ESCALATE.value,
            "session_id": session.id,
            "stage_id": current_stage.id,
            "iteration_count": session.iteration_count,
            "loop_max": current_stage.meta.loop_max,
            "directive": (
                "Достигнут лимит итераций ревью. Сформулируй вопрос пользователю "
                "для разрешения разногласий и вызови step_flow(action='escalation_resolve', resolution=...) после получения ответа."
            ),
            "notes": notes,
            "required_inputs": ["resolution"],
            "next_call": _compute_escalate_next_call(session, effective_root, target=target),
        }
        orch_summary = summarize_orchestration_state(session.context_data.get("orchestration"))
        if orch_summary is not None:
            res["orchestration_state"] = orch_summary
        return res

    renamed_from = _maybe_update_orchestration(
        session,
        flow_def,
        effective_root,
        stage_id=current_stage.id,
        stage_validator=current_stage.meta.validator,
        stage_iteration=evaluated_iteration,
        deliverable=deliverable,
        verdict=FlowVerdict.NEEDS_WORK.value,
        notes=notes or notes_default,
    )
    storage.update_session(
        session,
        history_events=[
            {
                "stage_id": current_stage.id,
                "iteration": session.iteration_count,
                "event": event,
                "verdict": FlowVerdict.NEEDS_WORK.value,
                "notes": notes or notes_default,
            }
        ],
    )
    _sync_orchestration_after_commit(effective_root, session, renamed_from)
    res = {
        "status": FlowStatus.ACTIVE.value,
        "session_id": session.id,
        "stage_id": current_stage.id,
        "iteration_count": session.iteration_count,
        "loop_max": current_stage.meta.loop_max,
        "message": f"Требуется доработка (итерация {session.iteration_count} из {current_stage.meta.loop_max}).",
        "instruction": current_stage.instruction,
        "notes": notes or notes_default,
        "required_inputs": _compute_stage_required_inputs(current_stage),
        "next_call": _compute_stage_next_call(
            session, current_stage, effective_root, target=target
        ),
    }
    orch_summary = summarize_orchestration_state(session.context_data.get("orchestration"))
    if orch_summary is not None:
        res["orchestration_state"] = orch_summary
    return res


class FlowEngine:
    """Управляющий класс стейт-машины сценариев (Flow)."""

    def __init__(
        self,
        project_root: Path | None = None,
        storage: FlowStorage | None = None,
        skills_storage: SkillsStorage | None = None,
        bundler: AgentBundler | None = None,
        skills_dir: Path | None = None,
    ) -> None:
        self.project_root = project_root
        self.storage = storage
        self.skills_storage = skills_storage
        self.bundler = bundler
        self.skills_dir = skills_dir

    def _get_root(self, project_root: Path | None = None) -> Path:
        return project_root or self.project_root or _find_default_project_root()

    def _get_storage(self, root: Path) -> FlowStorage:
        if self.storage is not None:
            return self.storage
        return FlowStorage(project_root=root)

    def start_flow(
        self,
        name: str,
        task_id: str | None = None,
        project_root: Path | None = None,
        target: str = "antigravity",
        flow_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Инициализирует новую сессию флоу и возвращает инструкцию первого этапа."""
        if not is_safe_name(name):
            msg = f"Недопустимое имя сценария '{name}'"
            raise ValueError(msg)

        valid_target = _validate_target(target)
        root = self._get_root(project_root)
        storage = self._get_storage(root)
        flow_def, chain = _resolve_flow_definition(name, root, self.skills_dir)
        effective_options = _resolve_flow_options(flow_def, chain, flow_options)

        if not flow_def.stage_order:
            msg = f"Флоу '{name}' не содержит этапов"
            raise InvalidFlowFormatError(msg)

        first_stage_id = flow_def.stage_order[0]
        first_stage = flow_def.stages[first_stage_id]
        git_branch = get_current_branch(root)

        session_id = f"flow-{uuid.uuid4().hex[:8]}"
        now = time.time()
        session = FlowSession(
            id=session_id,
            task_id=task_id,
            project_root=str(root),
            git_branch=git_branch,
            flow_name=flow_def.name,
            current_stage_id=first_stage_id,
            iteration_count=1,
            status=FlowStatus.ACTIVE,
            version=1,
            context_data={
                "target": valid_target,
                "requested_flow": name,
                "canonical_flow": flow_def.name,
                "flow_options": effective_options,
                **(
                    {"dispatch_policy": flow_def.dispatch_policy.model_dump()}
                    if flow_def.dispatch_policy is not None
                    else {}
                ),
            },
            created_at=now,
            updated_at=now,
        )

        if is_orchestration_flow(flow_def):
            init_orchestration_state(session, flow_def, root)

        prepared_payload = _build_stage_payload(
            first_stage,
            len(flow_def.stage_order),
            session,
            root,
            self.bundler,
            self.skills_storage,
            self.skills_dir,
            target=valid_target,
        )
        storage.save_session(session)
        storage.record_history(
            session_id=session.id,
            stage_id=first_stage.id,
            iteration=1,
            event="start_flow",
            notes=f"Старт сценария '{name}'",
        )
        _sync_orchestration_after_commit(root, session)

        return prepared_payload

    def _check_deliverable_file(
        self, stage: FlowStage, deliverable: Any, root: Path, blockers: list[str]
    ) -> None:
        """Проверяет артефакт-файл с однозначной семантикой типов (F2).

        ``target_file`` и ``deliverable.file_path`` проверяются независимо:
        валидный файл из любого источника «спасает» ненайденные остальные
        (verdict_6 T4 — target_file больше не глушит корректный file_path).
        Строка-deliverable, похожая на путь, трактуется как путь
        (согласовано с validate_plan); текст без маркеров пути —
        не файл. Пустой артефакт фиксируется только когда ни один
        источник не дал содержимого.
        """
        local: list[str] = []
        found: bool = False

        def _read(rel: str, label: str, *, is_target: bool = False) -> None:
            nonlocal found
            try:
                text = safe_read_text(root, rel)
            except ValueError as err:
                raise GateValidationError(f"Ошибка безопасности пути: {err}") from err
            if text is None:
                if is_target:
                    local.append(f"Целевой файл '{rel}' не найден в проекте")
                else:
                    local.append(f"Файл '{rel}' не найден в проекте ({label})")
                return
            if not text.strip():
                if is_target:
                    local.append(f"Целевой файл '{rel}' пуст")
                else:
                    local.append(f"Файл '{rel}' пуст")
                return
            found = True

        if stage.meta.target_file:
            _read(stage.meta.target_file, "target_file", is_target=True)
        if not found and isinstance(deliverable, dict):
            file_path = deliverable.get("file_path")
            if isinstance(file_path, str):
                _read(file_path, "deliverable.file_path")
        if not found and isinstance(deliverable, str) and deliverable.strip():
            stripped = deliverable.strip()
            if _looks_like_path(stripped):
                _read(stripped, "deliverable")

        # Валидный файл-артефакт снимает ненайденные источники (T4)
        if found:
            return

        blockers.extend(local)
        if not local:
            blockers.append(
                "Артефакт этапа (deliverable) не предоставлен или не является существующим файлом"
            )

    def _check_quality_gate(
        self,
        session: FlowSession,
        stage: FlowStage,
        deliverable: Any,
        root: Path,
        storage: FlowStorage,
        verdict: FlowVerdict | str | None = None,
        notes: str = "",
        human_confirmed: bool = False,
        target: str = "antigravity",
    ) -> dict[str, Any] | None:
        """Проверяет валидатор качества перед обработкой вердикта.

        Включает декларативные проверки (require_verdict, require_human_confirm, deliverable_must_exist)
        и регистровый валидатор из VALIDATOR_REGISTRY (если задан stage.meta.validator).
        Проверка require_human_confirm идёт по явному флагу human_confirmed (D4):
        непустые notes не трактуются как прокси-подтверждение человека —
        подтверждение фиксирует вызывающая сторона после согласования.
        """
        has_declarative = (
            stage.meta.require_verdict
            or stage.meta.require_human_confirm
            or stage.meta.deliverable_must_exist
        )
        if not stage.meta.validator and not has_declarative:
            return None

        blockers: list[str] = []
        warnings: list[str] = []

        norm_verdict = _normalize_verdict(verdict)

        if stage.meta.require_verdict and norm_verdict is None:
            blockers.append("Этап требует явный вердикт (approved/needs_work)")

        if stage.meta.require_human_confirm and not human_confirmed:
            blockers.append(
                "Этап требует подтверждения человека: передайте human_confirmed=true "
                "после явного согласования с пользователем"
            )

        if stage.meta.deliverable_must_exist:
            self._check_deliverable_file(stage, deliverable, root, blockers)

        if stage.meta.validator:
            validator_func = VALIDATOR_REGISTRY.get(stage.meta.validator)
            if not validator_func:
                msg = f"Неизвестный шлюз качества '{stage.meta.validator}'"
                raise GateValidationError(msg)

            gate_res = validator_func(session, stage, deliverable, root, self.skills_storage)
            blockers.extend(gate_res.blockers)
            warnings.extend(gate_res.warnings)

        if not blockers:
            return None

        gate_name = stage.meta.validator or "declarative_checks"
        storage.record_history(
            session_id=session.id,
            stage_id=stage.id,
            iteration=session.iteration_count,
            event="gate_failed",
            verdict="gate_blocked",
            notes="; ".join(blockers),
        )
        return {
            "status": "gate_blocked",
            "session_id": session.id,
            "stage_id": stage.id,
            "blockers": blockers,
            "warnings": warnings,
            "message": (
                f"Шлюз качества '{gate_name}' не пройден. "
                "Устраните блокеры перед продолжением или ревью."
            ),
            **(
                {"expected_file": str(root / stage.meta.target_file)}
                if stage.meta.target_file
                else {}
            ),
            "required_inputs": _compute_stage_required_inputs(stage),
            "next_call": _compute_stage_next_call(session, stage, root, target=target),
        }

    def _advance_auto_stage(
        self,
        session: FlowSession,
        flow_def: FlowDefinition,
        current_stage: FlowStage,
        notes: str,
        root: Path,
        storage: FlowStorage,
        verdict: FlowVerdict | str | None = None,
        target: str = "antigravity",
        deliverable: Any = None,
    ) -> dict[str, Any]:
        """Продвигает этап без агента/ревьюера в автоматическом режиме.

        verdict='needs_work' удерживает этап: счётчик итераций растёт и
        Circuit Breaker срабатывает так же, как на ревью-этапах (D4).
        """
        if _normalize_verdict(verdict) == FlowVerdict.NEEDS_WORK:
            return _apply_needs_work(
                session,
                current_stage,
                notes,
                storage,
                event="auto_needs_work",
                notes_default="Требуется доработка до автоперехода",
                root=root,
                target=target,
                flow_def=flow_def,
                deliverable=deliverable,
            )
        idx = flow_def.stage_order.index(current_stage.id)
        if idx + 1 < len(flow_def.stage_order):
            previous_iteration = session.iteration_count
            next_stage_id = flow_def.stage_order[idx + 1]
            session.current_stage_id = next_stage_id
            session.iteration_count = 1
            renamed_from = _maybe_update_orchestration(
                session,
                flow_def,
                root,
                stage_id=current_stage.id,
                stage_validator=current_stage.meta.validator,
                stage_iteration=previous_iteration,
                deliverable=deliverable,
                verdict=verdict.value if isinstance(verdict, FlowVerdict) else verdict,
                notes=notes,
            )
            prepared_payload = _build_stage_payload(
                flow_def.stages[session.current_stage_id],
                len(flow_def.stage_order),
                session,
                root,
                self.bundler,
                self.skills_storage,
                self.skills_dir,
                target=target,
            )
            storage.update_session(
                session,
                history_events=[
                    {
                        "stage_id": current_stage.id,
                        "iteration": previous_iteration,
                        "event": "advance_auto",
                        "verdict": "auto",
                        "notes": notes or "Автопереход этапа без ревьюера",
                    }
                ],
            )
            _sync_orchestration_after_commit(root, session, renamed_from)

            return prepared_payload

        session.status = FlowStatus.COMPLETED
        renamed_from = _maybe_update_orchestration(
            session,
            flow_def,
            root,
            stage_id=current_stage.id,
            stage_validator=current_stage.meta.validator,
            stage_iteration=session.iteration_count,
            deliverable=deliverable,
            verdict=verdict.value if isinstance(verdict, FlowVerdict) else verdict,
            notes=notes,
            final_status_override="COMPLETED",
        )
        storage.update_session(
            session,
            history_events=[
                {
                    "stage_id": current_stage.id,
                    "iteration": session.iteration_count,
                    "event": "completed",
                    "notes": "Все этапы флоу успешно завершены",
                }
            ],
        )
        _sync_orchestration_after_commit(root, session, renamed_from)
        return _completed_response(session, flow_def)

    def _advance_named_transition(
        self,
        session: FlowSession,
        flow_def: FlowDefinition,
        current_stage: FlowStage,
        label: str,
        verdict: FlowVerdict | str | None,
        notes: str,
        root: Path,
        storage: FlowStorage,
        target: str,
        deliverable: Any = None,
    ) -> dict[str, Any]:
        """Apply a guarded named transition with its counter and history atomically."""
        spec = current_stage.meta.transitions.get(label)
        if spec is None:
            raise ValueError(f"Переход '{label}' не определён для этапа '{current_stage.id}'")
        normalized = _normalize_verdict(verdict)
        if normalized == FlowVerdict.NEEDS_WORK and not spec.allow_needs_work:
            raise ValueError("Переход нельзя совмещать с verdict='needs_work' без allow_needs_work")
        if (
            current_stage.meta.agent is not None
            and normalized != FlowVerdict.APPROVED
            and not (normalized == FlowVerdict.NEEDS_WORK and spec.allow_needs_work)
        ):
            raise ValueError(f"Этап '{current_stage.id}' требует verdict='approved' для перехода")
        effective_options = session.context_data.get("flow_options", {})
        if not all(
            key in effective_options and _same_option_value(effective_options[key], value)
            for key, value in spec.when_options.items()
        ):
            raise ValueError(f"Условия перехода '{label}' для flow_options не выполнены")
        counts = dict(session.context_data.get("transition_counts", {}))
        count_key = f"{current_stage.id}:{label}"
        count = counts.get(count_key, 0)
        if count >= spec.max_uses:
            raise SessionConflictError(
                f"Лимит перехода '{label}' этапа '{current_stage.id}' исчерпан"
            )
        if spec.target not in flow_def.stages:
            raise InvalidFlowFormatError(f"Целевой этап '{spec.target}' перехода не найден")
        previous_iteration = session.iteration_count
        counts[count_key] = count + 1
        session.context_data["transition_counts"] = counts
        session.current_stage_id = spec.target
        session.iteration_count = 1
        renamed_from = _maybe_update_orchestration(
            session,
            flow_def,
            root,
            stage_id=current_stage.id,
            stage_validator=current_stage.meta.validator,
            stage_iteration=previous_iteration,
            deliverable=deliverable,
            verdict=normalized.value if normalized else None,
            notes=notes,
        )
        prepared_payload = _build_stage_payload(
            flow_def.stages[spec.target],
            len(flow_def.stage_order),
            session,
            root,
            self.bundler,
            self.skills_storage,
            self.skills_dir,
            target=target,
        )
        storage.update_session(
            session,
            history_events=[
                {
                    "stage_id": current_stage.id,
                    "iteration": previous_iteration,
                    "event": "transition",
                    "verdict": normalized.value if normalized else "auto",
                    "notes": (
                        f"{current_stage.id} --{label}--> {spec.target} "
                        f"({count + 1}/{spec.max_uses})" + (f"; {notes}" if notes else "")
                    ),
                }
            ],
        )
        _sync_orchestration_after_commit(root, session, renamed_from)
        return prepared_payload

    def _advance_review_stage(
        self,
        session: FlowSession,
        flow_def: FlowDefinition,
        current_stage: FlowStage,
        verdict: FlowVerdict | str | None,
        notes: str,
        root: Path,
        storage: FlowStorage,
        target: str = "antigravity",
        deliverable: Any = None,
    ) -> dict[str, Any]:
        """Обрабатывает вердикт ревьюера с контролем Circuit Breaker."""
        if verdict is None:
            msg = (
                f"Этап '{current_stage.id}' требует вердикт ревьюера ('approved' или 'needs_work')"
            )
            raise ValueError(msg)

        if isinstance(verdict, str):
            try:
                verdict = FlowVerdict(verdict)
            except ValueError as err:
                msg = f"Недопустимый вердикт '{verdict}'. Допустимые: {[v.value for v in FlowVerdict]}"
                raise ValueError(msg) from err

        if verdict == FlowVerdict.NEEDS_WORK:
            return _apply_needs_work(
                session,
                current_stage,
                notes,
                storage,
                event="review_needs_work",
                notes_default="",
                root=root,
                target=target,
                flow_def=flow_def,
                deliverable=deliverable,
            )

        # verdict == FlowVerdict.APPROVED
        previous_iteration = session.iteration_count
        session.iteration_count = 1
        idx = flow_def.stage_order.index(current_stage.id)

        if idx + 1 < len(flow_def.stage_order):
            next_stage_id = flow_def.stage_order[idx + 1]
            session.current_stage_id = next_stage_id
            renamed_from = _maybe_update_orchestration(
                session,
                flow_def,
                root,
                stage_id=current_stage.id,
                stage_validator=current_stage.meta.validator,
                stage_iteration=previous_iteration,
                deliverable=deliverable,
                verdict=verdict.value,
                notes=notes,
            )
            prepared_payload = _build_stage_payload(
                flow_def.stages[session.current_stage_id],
                len(flow_def.stage_order),
                session,
                root,
                self.bundler,
                self.skills_storage,
                self.skills_dir,
                target=target,
            )
            storage.update_session(
                session,
                history_events=[
                    {
                        "stage_id": current_stage.id,
                        "iteration": previous_iteration,
                        "event": "review_approved",
                        "verdict": verdict.value,
                        "notes": notes,
                    }
                ],
            )
            _sync_orchestration_after_commit(root, session, renamed_from)

            return prepared_payload

        session.status = FlowStatus.COMPLETED
        renamed_from = _maybe_update_orchestration(
            session,
            flow_def,
            root,
            stage_id=current_stage.id,
            stage_validator=current_stage.meta.validator,
            stage_iteration=previous_iteration,
            deliverable=deliverable,
            verdict=verdict.value,
            notes=notes,
            final_status_override="COMPLETED",
        )
        storage.update_session(
            session,
            history_events=[
                {
                    "stage_id": current_stage.id,
                    "iteration": previous_iteration,
                    "event": "review_approved",
                    "verdict": verdict.value,
                    "notes": notes,
                },
                {
                    "stage_id": current_stage.id,
                    "iteration": previous_iteration,
                    "event": "completed",
                    "notes": "Все этапы флоу успешно завершены",
                },
            ],
        )
        _sync_orchestration_after_commit(root, session, renamed_from)
        return _completed_response(session, flow_def)

    def advance_flow(
        self,
        session_id: str,
        deliverable: Any = None,
        verdict: FlowVerdict | str | None = None,
        notes: str = "",
        expected_stage_id: str | None = None,
        project_root: Path | None = None,
        human_confirmed: bool = False,
        update_git_branch: str | None = None,
        target: str | None = None,
        transition: str | None = None,
    ) -> dict[str, Any]:
        """Продвигает сессию флоу по этапам с проверкой валидаторов качества и ревью.

        update_git_branch применяется вместе с переходом этапа в одной
        транзакции update_session (D5): используется при продолжении
        сессии после смены git-ветки. Для явного session_id не передаётся.
        """
        root = self._get_root(project_root)
        storage = self._get_storage(root)

        session = storage.get_session(session_id)
        if session is None:
            msg = f"Сессия '{session_id}' не найдена"
            raise SessionNotFoundError(msg)

        if target is not None:
            effective_target = _validate_target(target)
            session.context_data["target"] = effective_target
        else:
            effective_target = session.context_data.get("target", "antigravity")

        if update_git_branch is not None and session.git_branch != update_git_branch:
            # Пишется через update_session в той же транзакции, что и переход
            # этапа; при gate_blocked возвращаемся ДО записи — ветка не меняется.
            session.git_branch = update_git_branch

        if expected_stage_id is not None and session.current_stage_id != expected_stage_id:
            msg = (
                f"Рассинхронизация этапа: ожидался '{expected_stage_id}', "
                f"но текущий этап сессии — '{session.current_stage_id}'"
            )
            recovery: dict[str, Any]
            try:
                flow_def = load_flow_definition(session.flow_name, root, skills_dir=self.skills_dir)
                current_stage = flow_def.stages.get(session.current_stage_id)
                if current_stage is not None:
                    recovery = {
                        "session_id": session.id,
                        "flow_name": session.flow_name,
                        "current_stage": {
                            "id": current_stage.id,
                            "title": current_stage.title,
                            "order": current_stage.order,
                            "instruction": current_stage.instruction,
                        },
                        "instruction": (
                            f"Сессия уже находится на этапе '{current_stage.id}'. "
                            "Сначала выполните работу текущего этапа; не подставляйте новый expected_stage_id для немедленного перехода."
                        ),
                        "required_inputs": _compute_stage_required_inputs(current_stage),
                        "next_call": _compute_stage_next_call(
                            session, current_stage, root, target=effective_target
                        ),
                    }
                else:
                    recovery = {
                        "session_id": session.id,
                        "flow_name": session.flow_name,
                        "current_stage_id": session.current_stage_id,
                        "instruction": "Текущий этап сессии не найден в определении flow. Запросите статус сессии.",
                        "required_inputs": [],
                        "next_call": {
                            "tool": "step_flow",
                            "arguments": {
                                "action": "status",
                                "project_path": str(root),
                                "session_id": session.id,
                                # Эффективный target указывается всегда, включая antigravity
                                "target": effective_target,
                            },
                        },
                    }
            except Exception as e_recovery:
                logger.warning(
                    "Не удалось собрать recovery-контекст для StageMismatchError: %s", e_recovery
                )
                recovery = {
                    "session_id": session.id,
                    "flow_name": session.flow_name,
                    "current_stage_id": session.current_stage_id,
                    "instruction": "Проверьте статус сессии через step_flow(action='status').",
                    "required_inputs": [],
                    "next_call": {
                        "tool": "step_flow",
                        "arguments": {
                            "action": "status",
                            "project_path": str(root),
                            "session_id": session.id,
                            # Эффективный target указывается всегда, включая antigravity
                            "target": effective_target,
                        },
                    },
                }

            raise StageMismatchError(
                msg,
                expected=expected_stage_id,
                actual=session.current_stage_id,
                recovery=recovery,
            )

        if deliverable is not None:
            deliv_bytes = (
                deliverable.encode("utf-8")
                if isinstance(deliverable, str)
                else json.dumps(deliverable, ensure_ascii=False).encode("utf-8")
            )
            if len(deliv_bytes) > MAX_DELIVERABLE_BYTES:
                msg = f"Превышен лимит deliverable 64КБ ({len(deliv_bytes)} байт)"
                raise ValueError(msg)

        if session.status == FlowStatus.BLOCKED_ESCALATE:
            msg = (
                f"Сессия '{session_id}' заблокирована Circuit Breaker. "
                "Вызовите step_flow(action='escalation_resolve', resolution=...) для разрешения."
            )
            raise SessionConflictError(msg)

        if session.status in (FlowStatus.COMPLETED, FlowStatus.ABORTED):
            msg = f"Невозможно продвинуть флоу в терминальном статусе '{session.status.value}'"
            raise FlowError(msg)

        flow_def = load_flow_definition(session.flow_name, root, skills_dir=self.skills_dir)
        current_stage = flow_def.stages.get(session.current_stage_id)
        if current_stage is None:
            msg = f"Текущий этап '{session.current_stage_id}' не найден в определении флоу"
            raise InvalidFlowFormatError(msg)

        gate_error_payload = self._check_quality_gate(
            session,
            current_stage,
            deliverable,
            root,
            storage,
            verdict=verdict,
            notes=notes,
            human_confirmed=human_confirmed,
            target=effective_target,
        )
        if gate_error_payload is not None:
            return gate_error_payload

        # Gate прошёл: фиксируем привязку QA-отчёта к сессии здесь, ДО перехода —
        # сохранение произойдёт тем же update_session, что и переход этапа
        # (шаг 7, план строка 92). При провале gate мы возвращаемся выше
        # и флаг не остаётся в сессии.
        return self._advance_after_gate(
            session,
            flow_def,
            current_stage,
            transition,
            verdict,
            notes,
            root,
            storage,
            effective_target,
            deliverable=deliverable,
        )

    def _advance_after_gate(
        self,
        session: FlowSession,
        flow_def: FlowDefinition,
        current_stage: FlowStage,
        transition: str | None,
        verdict: FlowVerdict | str | None,
        notes: str,
        root: Path,
        storage: FlowStorage,
        target: str,
        deliverable: Any = None,
    ) -> dict[str, Any]:
        if current_stage.meta.validator in ("validate_qa_report", "validate_report"):
            session.context_data["qa_report_bound"] = True
        if transition is not None:
            return self._advance_named_transition(
                session,
                flow_def,
                current_stage,
                transition,
                verdict,
                notes,
                root,
                storage,
                target,
                deliverable=deliverable,
            )
        if (
            current_stage.meta.transition_required
            and _normalize_verdict(verdict) != FlowVerdict.NEEDS_WORK
        ):
            raise ValueError(f"Этап '{current_stage.id}' требует именованный transition")
        if current_stage.meta.agent is None:
            return self._advance_auto_stage(
                session,
                flow_def,
                current_stage,
                notes,
                root,
                storage,
                verdict=verdict,
                target=target,
                deliverable=deliverable,
            )
        return self._advance_review_stage(
            session,
            flow_def,
            current_stage,
            verdict,
            notes,
            root,
            storage,
            target=target,
            deliverable=deliverable,
        )

    def resolve_flow_escalation(
        self,
        session_id: str,
        resolution: Literal["override_approved", "retry", "abort"] | str,
        notes: str = "",
        project_root: Path | None = None,
        target: str | None = None,
    ) -> dict[str, Any]:
        """Снимает блокировку Circuit Breaker по решению оператора/пользователя."""
        valid_resolutions = ("override_approved", "retry", "abort")
        if resolution not in valid_resolutions:
            msg = f"Недопустимая резолюция '{resolution}'. Допустимые: {list(valid_resolutions)}"
            raise ValueError(msg)

        root = self._get_root(project_root)
        storage = self._get_storage(root)

        session = storage.get_session(session_id)
        if session is None:
            msg = f"Сессия '{session_id}' не найдена"
            raise SessionNotFoundError(msg)

        if session.status != FlowStatus.BLOCKED_ESCALATE:
            msg = (
                f"Сессия '{session_id}' не находится в статусе blocked_escalate "
                f"(текущий статус: '{session.status.value}')"
            )
            raise SessionConflictError(msg)

        if target is not None:
            effective_target = _validate_target(target)
            session.context_data["target"] = effective_target
        else:
            effective_target = session.context_data.get("target", "antigravity")

        flow_def = load_flow_definition(session.flow_name, root, skills_dir=self.skills_dir)

        if resolution == "override_approved":
            previous_stage_id = session.current_stage_id
            previous_iteration = session.iteration_count
            previous_stage = flow_def.stages.get(previous_stage_id)
            previous_validator = previous_stage.meta.validator if previous_stage else None
            session.iteration_count = 1
            session.status = FlowStatus.ACTIVE

            idx = flow_def.stage_order.index(session.current_stage_id)
            if idx + 1 < len(flow_def.stage_order):
                next_stage_id = flow_def.stage_order[idx + 1]
                session.current_stage_id = next_stage_id
                renamed_from = _maybe_update_orchestration(
                    session,
                    flow_def,
                    root,
                    stage_id=previous_stage_id,
                    stage_validator=previous_validator,
                    stage_iteration=previous_iteration,
                    verdict="approved",
                    notes=notes,
                    final_status_override="IN_PROGRESS",
                )
                prepared_payload = _build_stage_payload(
                    flow_def.stages[session.current_stage_id],
                    len(flow_def.stage_order),
                    session,
                    root,
                    self.bundler,
                    self.skills_storage,
                    self.skills_dir,
                    target=effective_target,
                )
                storage.update_session(
                    session,
                    history_events=[
                        {
                            "stage_id": previous_stage_id,
                            "iteration": previous_iteration,
                            "event": "escalation_override_approved",
                            "notes": notes or "Принудительное одобрение этапа после эскалации",
                        }
                    ],
                )
                _sync_orchestration_after_commit(root, session, renamed_from)

                return prepared_payload
            else:
                session.status = FlowStatus.COMPLETED
                renamed_from = _maybe_update_orchestration(
                    session,
                    flow_def,
                    root,
                    stage_id=previous_stage_id,
                    stage_validator=previous_validator,
                    stage_iteration=previous_iteration,
                    verdict="approved",
                    notes=notes,
                    final_status_override="COMPLETED",
                )
                storage.update_session(
                    session,
                    history_events=[
                        {
                            "stage_id": previous_stage_id,
                            "iteration": previous_iteration,
                            "event": "escalation_override_approved",
                            "notes": notes or "Принудительное одобрение этапа после эскалации",
                        },
                        {
                            "stage_id": previous_stage_id,
                            "iteration": previous_iteration,
                            "event": "completed",
                            "notes": f"Флоу '{session.flow_name}' успешно завершен",
                        },
                    ],
                )
                _sync_orchestration_after_commit(root, session, renamed_from)
                return _completed_response(session, flow_def)

        if resolution == "retry":
            session.iteration_count = 1
            session.status = FlowStatus.ACTIVE
            renamed_from = _maybe_update_orchestration(
                session,
                flow_def,
                root,
                stage_id=session.current_stage_id,
                notes=notes,
                final_status_override="IN_PROGRESS",
            )
            prepared_payload = _build_stage_payload(
                flow_def.stages[session.current_stage_id],
                len(flow_def.stage_order),
                session,
                root,
                self.bundler,
                self.skills_storage,
                self.skills_dir,
                target=effective_target,
            )
            storage.update_session(
                session,
                history_events=[
                    {
                        "stage_id": session.current_stage_id,
                        "iteration": session.iteration_count,
                        "event": "escalation_retry",
                        "notes": notes or "Перезапуск итераций этапа после эскалации",
                    }
                ],
            )
            _sync_orchestration_after_commit(root, session, renamed_from)

            return prepared_payload

        session.status = FlowStatus.ABORTED
        renamed_from = _maybe_update_orchestration(
            session,
            flow_def,
            root,
            stage_id=session.current_stage_id,
            notes=notes,
            final_status_override="ABORTED",
        )
        storage.update_session(
            session,
            history_events=[
                {
                    "stage_id": session.current_stage_id,
                    "iteration": session.iteration_count,
                    "event": "escalation_abort",
                    "notes": notes or "Аборт сценария по решению оператора",
                }
            ],
        )
        _sync_orchestration_after_commit(root, session, renamed_from)
        return _aborted_response(session, f"Флоу '{session.flow_name}' прерван по эскалации.")

    def abort_flow(
        self,
        session_id: str,
        reason: str = "",
        project_root: Path | None = None,
    ) -> dict[str, Any]:
        """Принудительно прерывает выполнение сессии флоу."""
        root = self._get_root(project_root)
        storage = self._get_storage(root)

        session = storage.get_session(session_id)
        if session is None:
            msg = f"Сессия '{session_id}' не найдена"
            raise SessionNotFoundError(msg)

        if session.status in (FlowStatus.COMPLETED, FlowStatus.ABORTED):
            msg = f"Сессия уже находится в терминальном статусе '{session.status.value}'"
            raise FlowError(msg)

        session.status = FlowStatus.ABORTED
        renamed_from = _maybe_update_orchestration(
            session,
            None,
            root,
            stage_id=session.current_stage_id,
            notes=reason,
            final_status_override="ABORTED",
        )
        storage.update_session(
            session,
            history_events=[
                {
                    "stage_id": session.current_stage_id,
                    "iteration": session.iteration_count,
                    "event": "aborted",
                    "notes": reason,
                }
            ],
        )
        _sync_orchestration_after_commit(root, session, renamed_from)
        return _aborted_response(session, f"Флоу '{session.flow_name}' прерван: {reason}")

    def get_flow_status(
        self,
        session_id: str | None = None,
        project_root: Path | None = None,
        target: str | None = None,
    ) -> dict[str, Any]:
        """Возвращает статус текущей или указанной сессии флоу."""
        root = self._get_root(project_root)
        storage = self._get_storage(root)

        target_session: FlowSession | None = None
        if session_id:
            target_session = storage.get_session(session_id)
            if target_session is None:
                msg = f"Сессия '{session_id}' не найдена"
                raise SessionNotFoundError(msg)
        else:
            branch = get_current_branch(root)
            active_sessions = storage.find_active_sessions(str(root), branch)
            if not active_sessions:
                # Read-only fallback (D5): показываем сессии проекта даже с другой
                # ветки; ветку сессии не обновляем (только advance с переходом).
                active_sessions = storage.find_active_sessions(
                    str(root), branch, allow_project_fallback=True
                )
            if len(active_sessions) == 1:
                target_session = active_sessions[0]
            elif len(active_sessions) > 1:
                return {
                    "status": "multiple_active",
                    "count": len(active_sessions),
                    "sessions": [s.model_dump() for s in active_sessions],
                    "message": "Найдено несколько активных сессий. Укажите конкретный session_id.",
                    "required_inputs": [],
                    "next_call": None,
                }
            else:
                return {
                    "status": "no_active_session",
                    "message": "Активных сессий флоу для данного проекта/ветки не найдено.",
                    "required_inputs": [],
                    "next_call": None,
                }

        if target is not None:
            effective_target = _validate_target(target)
        elif target_session is not None and isinstance(target_session.context_data, dict):
            effective_target = target_session.context_data.get("target", "antigravity")
        else:
            effective_target = "antigravity"

        history = storage.get_history(target_session.id)
        flow_def = load_flow_definition(target_session.flow_name, root, skills_dir=self.skills_dir)
        current_stage = flow_def.stages.get(target_session.current_stage_id)

        res: dict[str, Any] = {
            "session": target_session.model_dump(),
            "current_stage": current_stage.model_dump() if current_stage else None,
            "total_stages": len(flow_def.stage_order),
            "history_count": len(history),
            "recent_events": history[-5:] if history else [],
        }
        orch_summary = (
            summarize_orchestration_state(target_session.context_data.get("orchestration"))
            if isinstance(target_session.context_data, dict)
            else None
        )
        if orch_summary is not None:
            res["orchestration_state"] = orch_summary
        if target_session.status in (FlowStatus.COMPLETED, FlowStatus.ABORTED):
            res["required_inputs"] = []
            res["next_call"] = None
        elif target_session.status == FlowStatus.BLOCKED_ESCALATE:
            res["directive"] = (
                "Сессия заблокирована Circuit Breaker. "
                "Задайте уточняющий вопрос пользователю и вызовите step_flow(action='escalation_resolve', resolution=...)."
            )
            res["required_inputs"] = ["resolution"]
            res["next_call"] = _compute_escalate_next_call(
                target_session, root, target=effective_target
            )
        else:
            if current_stage is not None:
                res["required_inputs"] = _compute_stage_required_inputs(current_stage)
                res["next_call"] = _compute_stage_next_call(
                    target_session, current_stage, root, target=effective_target
                )
            else:
                res["required_inputs"] = []
                res["next_call"] = None
        return res

    def list_flows(self, project_root: Path | None = None) -> dict[str, Any]:
        """Возвращает список всех доступных определений сценариев (N13)."""
        root = self._get_root(project_root)
        flows = list_available_flows(root, skills_dir=self.skills_dir)
        return {
            "flows": flows,
            "count": len(flows),
        }


# Модульные функции-фасады для регистрации в MCP Server и удобного вызова
def start_flow(
    name: str,
    task_id: str | None = None,
    project_root: Path | None = None,
    bundler: AgentBundler | None = None,
    storage: FlowStorage | None = None,
    skills_storage: SkillsStorage | None = None,
    target: str = "antigravity",
    flow_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    engine = FlowEngine(
        project_root=project_root,
        storage=storage,
        skills_storage=skills_storage,
        bundler=bundler,
    )
    return engine.start_flow(
        name=name,
        task_id=task_id,
        project_root=project_root,
        target=target,
        flow_options=flow_options,
    )


def advance_flow(
    session_id: str,
    deliverable: Any = None,
    verdict: FlowVerdict | str | None = None,
    notes: str = "",
    expected_stage_id: str | None = None,
    project_root: Path | None = None,
    human_confirmed: bool = False,
    update_git_branch: str | None = None,
    bundler: AgentBundler | None = None,
    storage: FlowStorage | None = None,
    skills_storage: SkillsStorage | None = None,
    target: str | None = None,
    transition: str | None = None,
) -> dict[str, Any]:
    engine = FlowEngine(
        project_root=project_root,
        storage=storage,
        skills_storage=skills_storage,
        bundler=bundler,
    )
    return engine.advance_flow(
        session_id=session_id,
        deliverable=deliverable,
        verdict=verdict,
        notes=notes,
        expected_stage_id=expected_stage_id,
        project_root=project_root,
        human_confirmed=human_confirmed,
        update_git_branch=update_git_branch,
        target=target,
        transition=transition,
    )


def resolve_flow_escalation(
    session_id: str,
    resolution: Literal["override_approved", "retry", "abort"] | str,
    notes: str = "",
    project_root: Path | None = None,
    bundler: AgentBundler | None = None,
    storage: FlowStorage | None = None,
    skills_storage: SkillsStorage | None = None,
    target: str | None = None,
) -> dict[str, Any]:
    engine = FlowEngine(
        project_root=project_root,
        storage=storage,
        skills_storage=skills_storage,
        bundler=bundler,
    )
    return engine.resolve_flow_escalation(
        session_id=session_id,
        resolution=resolution,
        notes=notes,
        project_root=project_root,
        target=target,
    )


def abort_flow(
    session_id: str,
    reason: str = "",
    project_root: Path | None = None,
    storage: FlowStorage | None = None,
) -> dict[str, Any]:
    engine = FlowEngine(project_root=project_root, storage=storage)
    return engine.abort_flow(session_id=session_id, reason=reason, project_root=project_root)


def get_flow_status(
    session_id: str | None = None,
    project_root: Path | None = None,
    storage: FlowStorage | None = None,
    target: str | None = None,
) -> dict[str, Any]:
    engine = FlowEngine(project_root=project_root, storage=storage)
    return engine.get_flow_status(session_id=session_id, project_root=project_root, target=target)


def _resolve_session_id(
    store: FlowStorage,
    root: Path,
    session_id: str | None,
    action: str,
) -> str:
    """Определяет ID сессии: явный ID или единственная активная сессия проекта.

    Точный поиск идёт по (project_root, git_branch); при пустом результате —
    read-only fallback по всему проекту (D5): сессии, начатые в другой ветке,
    остаются достижимыми для retry/abort/escalation_resolve. Ветка сессии
    при этом не обновляется (обновление — только вместе с переходом этапа).
    """
    if session_id:
        return session_id
    branch = get_current_branch(root)
    active = store.find_active_sessions(str(root), branch)
    if not active:
        active = store.find_active_sessions(str(root), branch, allow_project_fallback=True)
    if len(active) == 1:
        return active[0].id
    if len(active) > 1:
        raise AmbiguousSessionError(
            "Найдено несколько активных сессий. Укажите конкретный session_id."
        )
    raise SessionNotFoundError(f"Активная сессия для {action} не найдена.")


def _handle_step_retry(
    store: FlowStorage,
    root: Path,
    session_id: str,
    notes: str,
    bundler: AgentBundler | None,
    skills_storage: SkillsStorage | None,
    skills_dir: Path | None = None,
    target: str | None = None,
) -> dict[str, Any]:
    """Обрабатывает повтор текущего этапа (action='retry').

    Возвращает инструкции и контекст текущего этапа без изменения iteration_count.
    Лимит итераций (iteration_count / loop_max) расходуется только при вызове
    action='advance' с вердиктом verdict='needs_work'.
    """
    session = store.get_session(session_id)
    if session is None:
        raise SessionNotFoundError(f"Сессия '{session_id}' не найдена")

    if session.status in (FlowStatus.COMPLETED, FlowStatus.ABORTED):
        raise FlowError(
            f"Невозможно повторить этап для сессии в терминальном статусе '{session.status.value}'"
        )

    if session.status == FlowStatus.BLOCKED_ESCALATE:
        raise SessionConflictError(
            f"Сессия '{session_id}' заблокирована Circuit Breaker. "
            "Используйте action='escalation_resolve' с resolution='retry'."
        )

    if target is not None:
        effective_target = _validate_target(target)
    elif isinstance(session.context_data, dict):
        effective_target = session.context_data.get("target", "antigravity")
    else:
        effective_target = "antigravity"

    flow_def = load_flow_definition(session.flow_name, root, skills_dir=skills_dir)
    current_stage = flow_def.stages.get(session.current_stage_id)
    if current_stage is None:
        raise InvalidFlowFormatError(f"Этап '{session.current_stage_id}' не найден")

    # Сборка payload ДО смены target (R5): при ошибке сборки сессия не
    # остаётся с частично применённой сменой target.
    prepared_payload = _build_stage_payload(
        current_stage,
        len(flow_def.stage_order),
        session,
        root,
        bundler,
        skills_storage,
        skills_dir,
        target=effective_target,
    )

    if target is not None and session.context_data.get("target") != effective_target:
        # update_session, а не save_session: последний выполняет INSERT и
        # падает sqlite3.IntegrityError (PK id) для уже существующей сессии.
        # Смена target атомарна с history-событием target_changed.
        session.context_data["target"] = effective_target
        store.update_session(
            session,
            history_events=[
                {
                    "stage_id": session.current_stage_id,
                    "iteration": session.iteration_count,
                    "event": "target_changed",
                    "notes": f"target -> {effective_target}",
                }
            ],
        )
    store.record_history(
        session_id=session.id,
        stage_id=session.current_stage_id,
        iteration=session.iteration_count,
        event="retry_stage",
        notes=notes or "Повтор этапа агентом",
    )
    return prepared_payload


def step_flow(
    action: Literal["advance", "status", "retry", "abort", "escalation_resolve"] = "advance",
    flow: str | None = None,
    deliverable: Any = None,
    verdict: Literal["approved", "needs_work"] | FlowVerdict | str | None = None,
    session_id: str | None = None,
    expected_stage_id: str | None = None,
    resolution: Literal["override_approved", "retry", "abort"] | str | None = None,
    notes: str = "",
    human_confirmed: bool = False,
    target: str | None = None,
    project_root: Path | None = None,
    bundler: AgentBundler | None = None,
    storage: FlowStorage | None = None,
    skills_storage: SkillsStorage | None = None,
    skills_dir: Path | None = None,
    flow_options: dict[str, Any] | None = None,
    transition: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Единый фасад управления жизненным циклом и стейт-машиной сценария (Flow Runner).

    Decision Table:
    - action='status': Read-only статус текущей или указанной сессии (безопасен без закрепления).
    - action='advance':
      * если сессии нет и передан flow -> старт новой сессии (start_flow)
      * если сессия активна -> переход к следующему этапу (advance_flow) с проверкой gates/verdict
      * если по текущей ветке сессий нет, но в проекте есть единственная незавершённая
        (смена ветки) -> продолжить её, обновив git_branch вместе с переходом этапа (D5)
    - action='retry': повторное получение контекста/инструкций этапа (iteration_count сохраняется; бюджет расходуется только при verdict='needs_work')
    - action='abort': принудительное завершение активной сессии (abort_flow)
    - action='escalation_resolve': снятие блокировки Circuit Breaker с обязательным resolution

    Target-семантика (R5): target=None (по умолчанию) — сохранить target
    сессии; для новой сессии — 'antigravity'. Явный target (включая
    'antigravity') НЕ нормализуется и переключает target сессии.
    """
    root = project_root or _find_default_project_root()
    store = storage or FlowStorage(project_root=root)
    engine = FlowEngine(
        project_root=root,
        storage=store,
        skills_storage=skills_storage,
        bundler=bundler,
        skills_dir=skills_dir,
    )

    _valid_actions = ("advance", "status", "retry", "abort", "escalation_resolve")
    if action not in _valid_actions:
        raise ValueError(
            f"Неизвестное действие '{action}'. Допустимые значения: {list(_valid_actions)}"
        )

    _validate_start_only_arguments(action, flow_options, transition)

    if action == "status":
        return engine.get_flow_status(session_id=session_id, project_root=root, target=target)

    if action == "escalation_resolve":
        if not resolution:
            raise ValueError(
                "Для action='escalation_resolve' параметр 'resolution' обязателен. "
                "Допустимые значения: ['override_approved', 'retry', 'abort']"
            )
        resolved_sid = _resolve_session_id(store, root, session_id, action)
        return engine.resolve_flow_escalation(
            session_id=resolved_sid,
            resolution=resolution,
            notes=notes,
            project_root=root,
            target=target,
        )

    if action == "abort":
        resolved_sid = _resolve_session_id(store, root, session_id, action)
        return engine.abort_flow(session_id=resolved_sid, reason=notes, project_root=root)

    if action == "retry":
        resolved_sid = _resolve_session_id(store, root, session_id, action)
        return _handle_step_retry(
            store, root, resolved_sid, notes, bundler, skills_storage, skills_dir, target=target
        )

    # action == "advance"
    update_branch: str | None = None
    if session_id is None:
        branch = get_current_branch(root)
        active = store.find_active_sessions(str(root), branch)
        if not active and branch is not None:
            # D5: точный поиск по ветке пуст (смена ветки) — смотрим незавершённые
            # сессии всего проекта; единственная продолжается с переносом ветки.
            active = store.find_active_sessions(str(root), branch, allow_project_fallback=True)
        if not active:
            if not flow:
                raise ValueError(
                    "Активная сессия флоу не найдена. "
                    "Для старта нового сценария передайте параметр 'flow' (например, flow='feature-delivery')."
                )
            # Новая сессия: target=None означает antigravity (R5);
            # явный target валидируется и применяется как есть.
            if transition is not None:
                raise ValueError("transition недопустим при старте новой сессии")
            return engine.start_flow(
                name=flow,
                task_id=task_id,
                project_root=root,
                target=_validate_target(target),
                flow_options=flow_options,
            )
        if len(active) == 1:
            if flow and not _flows_equivalent(flow, active[0].flow_name, root, skills_dir):
                raise SessionConflictError(
                    f"Активная сессия '{active[0].id}' сценария '{active[0].flow_name}' уже выполняется. "
                    f"Завершите или прервите её перед запуском '{flow}'."
                )
            session_id = active[0].id
            # Переносим ветку сессии на текущую только при fallback-резолве;
            # при точном попадании session.git_branch == branch и записи не будет.
            update_branch = branch
        else:
            raise AmbiguousSessionError(
                "Найдено несколько активных сессий. Укажите конкретный session_id."
            )

    if flow_options is not None:
        raise ValueError("flow_options нельзя изменять после старта сессии")

    return engine.advance_flow(
        session_id=session_id,
        deliverable=deliverable,
        verdict=verdict,
        notes=notes,
        expected_stage_id=expected_stage_id,
        project_root=root,
        human_confirmed=human_confirmed,
        update_git_branch=update_branch,
        target=target,
        transition=transition,
    )


def list_flows(project_root: Path | None = None) -> dict[str, Any]:
    engine = FlowEngine(project_root=project_root)
    return engine.list_flows(project_root=project_root)
