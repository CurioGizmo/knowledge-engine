"""Исполнительный движок пайплайна воркфлоу (Workflow State Machine) и реестр шлюзов качества."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from mcp_core.paths import (
    _find_default_project_root,
    get_cache_dir,
    resolve_skills_repo_dir,
    safe_read_text,
)
from mcp_skills.adapters import SUPPORTED_TARGETS
from mcp_skills.bundler import AgentBundler, merge_profile_with_override
from mcp_skills.errors import (
    AmbiguousSessionError,
    AssemblyBudgetExceededError,
    FlowError,
    FlowNotFoundError,
    GateValidationError,
    InvalidFlowFormatError,
    SessionConflictError,
    SkillNotFoundError,
    StageMismatchError,
)
from mcp_skills.flow_parser import list_available_flows, load_flow_definition
from mcp_skills.flow_storage import FlowStorage
from mcp_skills.git_manager import get_current_branch
from mcp_skills.models import (
    AgentProfile,
    FlowDefinition,
    FlowSession,
    FlowStage,
    FlowStatus,
    FlowVerdict,
)
from mcp_skills.resources import (
    determine_resource_type,
    is_resource_associated_with_skill,
    resolve_link_to_resource_id,
)
from mcp_skills.slot_compiler import SlotCompiler
from mcp_skills.spam_gate import is_safe_name
from mcp_skills.stack_detector import detect_canonical_project_name
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer
from mcp_skills.token_guard import TokenGuard

logger = logging.getLogger("mcp_skills.flow_engine")

__all__ = [
    "GATE_REGISTRY",
    "FlowEngine",
    "GateResult",
    "abort_flow",
    "advance_flow",
    "get_flow_status",
    "list_flows",
    "resolve_flow_escalation",
    "start_flow",
    "step_flow",
    "validate_plan_gate",
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
        # Эффективный target указывается всегда, включая antigravity (шаг 5):
        # явный antigravity не превращается в None/отсутствующий ключ.
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
        # Эффективный target указывается всегда, включая antigravity (шаг 5).
        "target": target,
    }
    return {
        "tool": "step_flow",
        "arguments": args,
    }


def _apply_needs_work(
    session: FlowSession,
    current_stage: FlowStage,
    notes: str,
    storage: FlowStorage,
    event: str,
    notes_default: str,
    root: Path | None = None,
    target: str = "antigravity",
) -> dict[str, Any]:
    """Фиксирует вердикт needs_work: этап удерживается, итерация +1, Circuit Breaker.

    Общая логика для этапов с ревьюером и без него (D4): счётчик итераций
    растёт, при превышении loop_max сессия уходит в blocked_escalate,
    иначе — активна и ждёт доработки на том же этапе.
    """
    effective_root = root if root is not None else Path(session.project_root)
    session.iteration_count += 1
    if session.iteration_count > current_stage.meta.loop_max:
        session.status = FlowStatus.BLOCKED_ESCALATE
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
        return {
            "status": FlowStatus.BLOCKED_ESCALATE.value,
            "session_id": session.id,
            "stage_id": current_stage.id,
            "iteration_count": session.iteration_count,
            "loop_max": current_stage.meta.loop_max,
            "directive": (
                "Достигнут лимит итераций ревью. Сформулируй вопрос пользователю "
                "для разрешения разногласий и вызови resolve_flow_escalation после получения ответа."
            ),
            "notes": notes,
            "required_inputs": ["resolution"],
            "next_call": _compute_escalate_next_call(session, effective_root, target=target),
        }

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
    return {
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


@dataclass
class GateResult:
    """Результат валидации шлюза качества (Quality Gate)."""

    passed: bool
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _looks_like_path(s: str) -> bool:
    """Эвристика «строка-путь» для deliverable/плана (F2, verdict_6 T1).

    Относительный путь с расширением файла и разделителями каталогов
    ('docs/plans/x.md') — путь; многострочный текст плана или обычная
    фраза без расширения — текст. Абсолютные пути не считаются путями
    относительно project_root (safe_read_text их блокирует и вернёт None
    при выходе за корень — это уже обрабатывается как «файл не найден»).
    """
    s = s.strip()
    if not s or "\n" in s:
        return False
    if s.startswith(("/", "~")):
        return False
    if "/" in s or "\\" in s:
        return True
    return Path(s).suffix in {".md", ".markdown", ".txt", ".rst", ".yaml", ".yml", ".json"}


def _plan_deliverable_source(deliverable: Any) -> tuple[str | None, str | None]:
    """Разбирает deliverable на (текст плана, путь к файлу) — F2.

    Возвращает первый применимый источник: текст (``content`` или строка
    без маркеров пути) либо путь (``file_path`` или строка-путь).
    ``content`` приоритетнее ``file_path``.
    """
    if isinstance(deliverable, dict):
        content = deliverable.get("content")
        if isinstance(content, str):
            return content, None
        file_path = deliverable.get("file_path")
        if isinstance(file_path, str):
            return None, file_path
        return None, None
    if isinstance(deliverable, str):
        stripped = deliverable.strip()
        if _looks_like_path(stripped):
            return None, stripped
        return (stripped or None), None
    return None, None


def validate_plan_gate(
    session: FlowSession,
    stage: FlowStage,
    deliverable: Any,
    project_root: Path,
    storage: SkillsStorage | None = None,
) -> GateResult:
    """Шлюз проверки наличия плана реализации (validate_plan).

    F2 — однозначные типы артефакта (verdict_6 П.8):
    - ``deliverable``-строка, похожая на путь, — ПУТЬ: файл читается,
      отсутствие файла — блокер с явной причиной (раньше несуществующий
      путь молча принимался как «текст плана»);
    - ``deliverable={'content': str}`` — ТЕКСТ плана (приоритетнее file_path);
    - ``deliverable={'file_path': str}`` — путь;
    - ``stage.meta.target_file``: валидный файл-артефакт из deliverable
      снимает требование к целевому файлу (T4 — target_file больше не
      глушит корректный file_path), но текст плана — не снимает:
      объявленный target_file обязан существовать и не быть пустым.
    """
    blockers: list[str] = []
    plan_text: str | None = None
    artifact_file_found = False

    def _read_file(rel: str, label: str, *, is_target: bool = False) -> None:
        nonlocal plan_text, artifact_file_found
        try:
            text = safe_read_text(project_root, rel)
        except ValueError as err:
            raise GateValidationError(f"Ошибка безопасности пути плана: {err}") from err
        if text is None:
            if is_target:
                blockers.append(f"Целевой файл плана '{rel}' не найден в проекте")
            else:
                blockers.append(f"Файл плана '{rel}' не найден в проекте ({label})")
            return
        if not text.strip():
            if is_target:
                blockers.append(f"Целевой файл плана '{rel}' пуст")
            else:
                blockers.append(f"Файл плана '{rel}' пуст")
            return
        if plan_text is None:
            plan_text = text
        artifact_file_found = True

    # 1. deliverable — первый применимый источник (текст или путь)
    text_src, path_src = _plan_deliverable_source(deliverable)
    if text_src is not None:
        plan_text = text_src
    elif path_src is not None:
        _read_file(path_src, "deliverable")

    # 2. target_file: валидный файл-артефакт спасает ненайденный целевой
    # файл (T4); текст плана — нет, объявленный target_file проверяется.
    if stage.meta.target_file and not artifact_file_found:
        _read_file(stage.meta.target_file, "target_file", is_target=True)

    if plan_text is not None and not plan_text.strip():
        return GateResult(passed=False, blockers=["План реализации не предоставлен или пуст"])

    if blockers:
        return GateResult(passed=False, blockers=blockers)

    if plan_text is None:
        return GateResult(passed=False, blockers=["План реализации не предоставлен или пуст"])

    return GateResult(passed=True, blockers=[], warnings=[])


def _extract_qa_report_text(
    deliverable: Any,
    target_file: str | None,
    project_root: Path,
) -> tuple[str | None, list[str]]:
    """Извлекает текст отчёта из deliverable или target_file."""
    blockers: list[str] = []
    report_text: str | None = None

    def _read_file(rel: str, label: str) -> None:
        nonlocal report_text
        try:
            text = safe_read_text(project_root, rel)
        except ValueError as err:
            raise GateValidationError(f"Ошибка безопасности пути отчёта: {err}") from err
        if text is None:
            blockers.append(f"Файл отчёта приёмки '{rel}' не найден в проекте ({label})")
            return
        if not text.strip():
            blockers.append(f"Файл отчёта приёмки '{rel}' пуст ({label})")
            return
        if report_text is None:
            report_text = text

    if isinstance(deliverable, dict):
        if deliverable.get("content"):
            report_text = str(deliverable["content"])
        elif deliverable.get("file_path"):
            _read_file(str(deliverable["file_path"]), "deliverable.file_path")
    elif isinstance(deliverable, str) and deliverable.strip():
        stripped = deliverable.strip()
        if _looks_like_path(stripped):
            _read_file(stripped, "deliverable")
        else:
            report_text = stripped

    if report_text is None and target_file:
        _read_file(target_file, "target_file")

    return report_text, blockers


def _parse_qa_report_table(
    report_text: str,
) -> tuple[list[list[str]], dict[str, int]]:
    """Парсит markdown-таблицу проверок и определяет индексы колонок."""
    lines = report_text.splitlines()
    table_rows: list[list[str]] = []
    header_indices: dict[str, int] = {}
    in_table = False

    for line in lines:
        s = line.strip()
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s[1:-1].split("|")]
            if all(re.match(r"^:?-+:?$", c) for c in cells):
                in_table = True
                continue
            if not in_table:
                for idx, cell in enumerate(cells):
                    cl = cell.lower()
                    # Порядок классификации важен (R4): evidence раньше id —
                    # иначе английская «Evidence» матчится подстрокой «id»
                    # и колонка доказательств теряется.
                    if any(
                        k in cl for k in ("доказательств", "evidence", "вывод", "артефакт", "лог")
                    ):
                        header_indices["evidence"] = idx
                    elif re.search(r"\b(?:id|action)\b|действи|сценари", cl):
                        header_indices["id"] = idx
                    elif any(k in cl for k in ("ожидани", "expected")):
                        header_indices["expected"] = idx
                    elif any(k in cl for k in ("факт", "actual", "наблюда")):
                        header_indices["actual"] = idx
                    elif any(
                        k in cl for k in ("результат", "статус", "result", "status", "вердикт")
                    ):
                        header_indices["result"] = idx
            else:
                table_rows.append(cells)
        else:
            in_table = False

    return table_rows, header_indices


# Валидные статусы строки таблицы проверок (R4: QA-gate не принимает
# произвольные статусы вроде BANANA/PENDING как «успешные»). N/A — ветка
# отдельно (обязательное обоснование неприменимости).
_VALID_ROW_STATUSES: tuple[str, ...] = (
    "PASS",
    "PASSED",
    "OK",
    "УСПЕШНО",
    "ПРОЙДЕН",
    "QA_PASSED",
)


def _validate_qa_table_rows(
    table_rows: list[list[str]],
    header_indices: dict[str, int],
    has_limitations: bool,
) -> tuple[bool, bool, bool, bool, bool, bool, bool]:
    """Проверяет строки таблицы приёмки.

    Возвращает (missing_exp_act, missing_evidence, has_failed, has_blocked,
    missing_na_reason, missing_status, unknown_status). missing_status=True,
    если у какой-либо строки пустая ячейка «Результат» или колонка
    отсутствует вовсе. unknown_status=True, если статус непуст, но не входит
    в _VALID_ROW_STATUSES и не распознан как N/A (R4: BANANA/PENDING —
    блокеры, а не «успех»).
    """
    missing_exp_act = False
    missing_evidence = False
    has_failed = False
    has_blocked = False
    missing_na_reason = False
    missing_status = False
    unknown_status = False

    for row in table_rows:
        exp_cell = (
            row[header_indices["expected"]]
            if "expected" in header_indices and len(row) > header_indices["expected"]
            else ""
        )
        act_cell = (
            row[header_indices["actual"]]
            if "actual" in header_indices and len(row) > header_indices["actual"]
            else ""
        )
        ev_cell = (
            row[header_indices["evidence"]]
            if "evidence" in header_indices and len(row) > header_indices["evidence"]
            else ""
        )
        res_cell = (
            row[header_indices["result"]]
            if "result" in header_indices and len(row) > header_indices["result"]
            else ""
        )

        if not exp_cell or not act_cell:
            missing_exp_act = True
        if not ev_cell:
            missing_evidence = True
        # Пустой или отсутствующий статус строки — блокер: приёмка требует
        # распознанный непустой результат каждой проверки.
        if not res_cell.strip():
            missing_status = True

        res_upper = res_cell.upper()
        if any(k in res_upper for k in ("FAIL", "FAILED", "QA_FAILED")):
            has_failed = True
        elif "BLOCK" in res_upper:
            has_blocked = True
        elif "N/A" in res_upper or "NOT APPLICABLE" in res_upper:
            clean_res = re.sub(r"N/?A", "", res_cell, flags=re.IGNORECASE).strip(" ():-")
            has_act_reason = any(
                k in act_cell.lower()
                for k in ("не применимо", "n/a", "пропуск", "стенд", "ci", "отсутств")
            )
            if not clean_res and not has_act_reason and not has_limitations:
                missing_na_reason = True
        elif res_upper.strip() and not any(k == res_upper.strip() for k in _VALID_ROW_STATUSES):
            # Непустой статус вне whitelist — неизвестный вердикт проверки
            # (BANANA, PENDING, …) не может считаться успехом (R4).
            unknown_status = True

    return (
        missing_exp_act,
        missing_evidence,
        has_failed,
        has_blocked,
        missing_na_reason,
        missing_status,
        unknown_status,
    )


def _validate_qa_freeform_text(
    report_text: str,
    has_limitations: bool,
) -> tuple[bool, bool, bool, bool, bool, bool, bool, bool]:
    """Проверяет неструктурированный (списковый) текст отчёта приёмки.

    Возвращает (has_scenarios, missing_exp_act, missing_evidence, has_failed,
    has_blocked, missing_na_reason, missing_status, unknown_status).
    unknown_status=True, если в тексте есть маркер строки-сценария
    (TC-N/Сценарий/Проверка) или строка «Результат:», но статус не входит в
    распознаваемые (PASS/PASSED/OK/УСПЕШНО/ПРОЙДЕН/QA_PASSED, N/A, FAIL,
    BLOCKED) — закрывает обход через freeform-формат (R4).
    """
    scenario_matches = re.findall(
        r"(?:###|##|-)\s*(?:TC-\d+|Сценарий|Проверка)", report_text, re.IGNORECASE
    )
    has_scenarios = bool(scenario_matches)

    has_exp = bool(re.search(r"(?:ожида(?:ние|лось)|expected)", report_text, re.IGNORECASE))
    has_act = bool(re.search(r"(?:факт|наблюда(?:лось|ем)|actual)", report_text, re.IGNORECASE))
    missing_exp_act = not (has_exp and has_act)

    has_ev = bool(
        re.search(
            r"(?:доказательств|evidence|артефакт|лог|log|\.log|\.txt|\.json|\[.*?\]\(.*?\)|```)",
            report_text,
            re.IGNORECASE,
        )
    )
    missing_evidence = not has_ev

    has_failed = bool(re.search(r"\b(?:FAIL|FAILED|QA_FAILED)\b", report_text, re.IGNORECASE))
    has_blocked = bool(re.search(r"\b(?:BLOCKED|ЗАБЛОКИРОВАН)\b", report_text, re.IGNORECASE))

    missing_na_reason = False
    if (
        re.search(r"\bN/A\b", report_text, re.IGNORECASE)
        and not has_limitations
        and not re.search(r"N/A\s*[:(.]+?[)\n]", report_text, re.IGNORECASE)
    ):
        missing_na_reason = True

    # unknown_status: каждая строка-сценарий с явным полем «Результат»/«Статус»
    # обязана нести распознаваемый вердикт. Общий QA_PASSED в вердикте отчёта
    # не оправдывает строку с невалидным статусом (R4, mixed-случай).
    unknown_status = False
    missing_status = False
    status_lines = re.findall(
        r"(?:результат|статус|result|status)\s*[:*]+\s*(.+?)(?:\n|$)", report_text, re.IGNORECASE
    )
    if status_lines and not has_failed and not has_blocked:
        for status_line in status_lines:
            status_value = status_line.strip().strip("«»\"'").upper()
            recognized = (
                any(
                    k in status_value
                    for k in ("FAIL", "FAILED", "QA_FAILED", "BLOCKED", "ЗАБЛОКИРОВАН")
                )
                or "N/A" in status_value
                or any(k in status_value for k in _VALID_ROW_STATUSES)
            )
            if not status_value:
                missing_status = True
            elif not recognized:
                unknown_status = True

    return (
        has_scenarios,
        missing_exp_act,
        missing_evidence,
        has_failed,
        has_blocked,
        missing_na_reason,
        missing_status,
        unknown_status,
    )


def _check_qa_report_session(report_text: str, session: FlowSession) -> list[str]:
    """Проверяет соответствие session_id в тексте отчёта текущей сессии.

    Точное совпадение только с текущей session.id: task_id сам по себе не
    отличает повторные запуски (шаг 7, план строка 92). Отсутствие session_id —
    ошибка, а не разрешение принять старый отчёт.
    """
    blockers: list[str] = []
    # \** допускает markdown-выделение («**Сессия:** <id>»)
    session_matches = re.findall(
        r"(?:сессия|session_id|session)\s*[:=]\s*\**\s*([a-zA-Z0-9_\-]+)",
        report_text,
        re.IGNORECASE,
    )
    if not session_matches:
        blockers.append(
            "QA report is not bound to the current session: "
            f"specify session_id of the current session ('{session.id}')"
        )
        return blockers
    for sid in session_matches:
        if sid != session.id:
            blockers.append(
                f"Отчёт привязан к сессии '{sid}', ожидалась текущая сессия '{session.id}'"
            )
            break
    return blockers


def _check_qa_report_verdict(report_text: str) -> tuple[bool, bool]:
    """Проверяет явное указание вердикта в тексте отчёта."""
    has_failed = False
    has_blocked = False
    verdict_match = re.search(
        r"(?:вердикт|verdict)\s*[:*]+\s*([A-Za-z0-9_]+)", report_text, re.IGNORECASE
    )
    if verdict_match:
        verdict_val = verdict_match.group(1).upper()
        if any(k in verdict_val for k in ("FAIL", "FAILED", "QA_FAILED")):
            has_failed = True
        elif "BLOCK" in verdict_val:
            has_blocked = True
    return has_failed, has_blocked


def _collect_qa_blockers(
    has_scenarios: bool,
    missing_expected_actual: bool,
    missing_evidence: bool,
    has_failed: bool,
    has_blocked: bool,
    missing_na_reason: bool,
    missing_status: bool = False,
    unknown_status: bool = False,
) -> list[str]:
    """Формирует список блокирующих несоответствий отчёта приёмки."""
    blockers: list[str] = []
    if not has_scenarios:
        blockers.append("Отчёт не содержит перечня проверок или сценариев")
    if missing_expected_actual:
        blockers.append("В отчёте отсутствуют ожидаемые или фактические результаты проверок")
    if missing_evidence:
        blockers.append(
            "В отчёте отсутствуют ссылки на доказательства (evidence: логи, артефакты или вывод команд)"
        )
    if has_failed:
        blockers.append(
            "Отчёт содержит непройденные проверки (FAILED): успешное завершение недопустимо"
        )
    if has_blocked:
        blockers.append(
            "Отчёт содержит заблокированные проверки (BLOCKED): успешное завершение недопустимо"
        )
    if missing_na_reason:
        blockers.append("Проверка со статусом N/A не содержит обоснования неприменимости")
    if missing_status:
        blockers.append(
            "Строка таблицы проверок без статуса результата: колонка «Результат» обязательна и не может быть пустой"
        )
    if unknown_status:
        blockers.append(
            "unknown_status: строка проверки содержит нераспознанный статус результата "
            "(BANANA/PENDING и т.п.): допускаются PASS/PASSED/OK/УСПЕШНО/ПРОЙДЕН/QA_PASSED, "
            "N/A с обоснованием, FAIL/FAILED, BLOCKED"
        )
    return blockers


def validate_qa_report_gate(
    session: FlowSession,
    stage: FlowStage,
    deliverable: Any,
    project_root: Path,
    skills_storage: SkillsStorage | None = None,
) -> GateResult:
    """Шлюз проверки отчёта живой приёмки (validate_qa_report).

    Проверяет:
    1. Наличие и непустоту отчёта (в deliverable или целевом файле).
    2. Привязку артефакта к сессии (сохранение в context_data; проверка session_id при наличии).
    3. Перечень проверок/сценариев (минимум один сценарий).
    4. Ожидаемый и фактический результат для каждой проверки.
    5. Ссылки на evidence (доказательства: файлы, логи, скриншоты, команды).
    6. Соответствие итога результатам (без непройденных/заблокированных, N/A с обоснованием).
    """
    blockers: list[str] = []
    warnings: list[str] = []

    report_text, extract_blockers = _extract_qa_report_text(
        deliverable, stage.meta.target_file, project_root
    )
    blockers.extend(extract_blockers)
    if blockers:
        return GateResult(passed=False, blockers=blockers)

    if report_text is None or not report_text.strip():
        return GateResult(
            passed=False, blockers=["Отчёт приёмки (deliverable) не предоставлен или пуст"]
        )

    # 2. Привязка к текущей сессии. Флаг qa_report_bound НЕ ставится здесь:
    # контекст сессии меняется только в advance-логике после успешной проверки
    # gate, вместе с атомарным сохранением перехода (шаг 7, план строка 92).
    blockers.extend(_check_qa_report_session(report_text, session))

    # 3. Парсинг сценариев / проверок
    table_rows, header_indices = _parse_qa_report_table(report_text)
    has_limitations = bool(
        re.search(r"##\s*(?:ограничения|limitations)", report_text, re.IGNORECASE)
    )

    if table_rows and ("result" in header_indices or "id" in header_indices):
        has_scenarios = True
        (
            missing_expected_actual,
            missing_evidence,
            has_failed,
            has_blocked,
            missing_na_reason,
            missing_status,
            unknown_status,
        ) = _validate_qa_table_rows(table_rows, header_indices, has_limitations)
    else:
        (
            has_scenarios,
            missing_expected_actual,
            missing_evidence,
            has_failed,
            has_blocked,
            missing_na_reason,
            missing_status,
            unknown_status,
        ) = _validate_qa_freeform_text(report_text, has_limitations)

    verdict_failed, verdict_blocked = _check_qa_report_verdict(report_text)
    has_failed = has_failed or verdict_failed
    has_blocked = has_blocked or verdict_blocked

    blockers.extend(
        _collect_qa_blockers(
            has_scenarios,
            missing_expected_actual,
            missing_evidence,
            has_failed,
            has_blocked,
            missing_na_reason,
            missing_status,
            unknown_status,
        )
    )

    if blockers:
        return GateResult(passed=False, blockers=blockers, warnings=warnings)

    return GateResult(passed=True, blockers=[], warnings=warnings)


GateFunc = Callable[[FlowSession, FlowStage, Any, Path, SkillsStorage | None], GateResult]

# Реестр доступных шлюзов качества (GATE_REGISTRY).
# Валидаторы verify_catalog.py динамически запрашивают этот реестр.
GATE_REGISTRY: dict[str, GateFunc] = {
    "validate_plan": validate_plan_gate,
    "validate_qa_report": validate_qa_report_gate,
    "validate_report": validate_qa_report_gate,
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
                # Кэш ещё не построен (первая сессия в новом проекте): AgentBundler
                # требует storage — создаём in-memory и синхронизируем с каталогом
                # скиллов, чтобы бандлы агентов работали с первого запуска.
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
    """Компилирует скиллы этапа (stage.meta.skills) в дополнение инструкции.

    F1: имена скиллов в meta — НЕ контент; сборка идёт тем же путём, что и
    бандлинг агентов: SlotCompiler.assemble_skill со всеми available_slots
    карточки (эталон — AgentBundler.bundle_agent), стек проекта — из единого
    ProjectContext. Объединённая «инструкция + скиллы» проходит TokenGuard:
    бюджет этапа не превышает лимит даже с несколькими скиллами.

    Возвращает (supplement_text, compiled_names, truncated). Отсутствующий
    скилл — предупреждение в тексте, этап не падает (как в бандлере агентов).
    """
    if not stage.meta.skills:
        return "", [], False

    from mcp_skills.project_context import get_project_context, supplement_mode_axis
    from mcp_skills.slot_compiler import SlotCompiler

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
        meta.gate
        or meta.require_verdict
        or meta.require_human_confirm
        or meta.deliverable_must_exist
    ):
        return None
    note = (
        f"При следующем advance гейт '{meta.gate}' проверит deliverable"
        if meta.gate
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

    return payload


def _completed_response(session: FlowSession, flow_def: FlowDefinition) -> dict[str, Any]:
    """Completed-ответ сессии с мостики «куда идти дальше» (F1).

    next_flows берётся из определения флоу (frontmatter next_flows, смерженный
    с оверрайдом); флоу без мостиков получает прежний ответ без новых ключей.
    """
    res: dict[str, Any] = {
        "status": FlowStatus.COMPLETED.value,
        "session_id": session.id,
        "flow_name": session.flow_name,
        "message": f"Флоу '{session.flow_name}' успешно завершен!",
        "required_inputs": [],
        "next_call": None,
    }
    if flow_def.next_flows:
        res["next_flows"] = list(flow_def.next_flows)
        res["next_flow_hint"] = "Следующие сценарии после завершения: " + ", ".join(
            f"'{nf}' (step_flow(flow='{nf}'))" for nf in flow_def.next_flows
        )
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

        return prepared_payload

    def _check_deliverable_file(
        self, stage: FlowStage, deliverable: Any, root: Path, blockers: list[str]
    ) -> None:
        """Проверяет артефакт-файл с однозначной семантикой типов (F2).

        ``target_file`` и ``deliverable.file_path`` проверяются независимо:
        валидный файл из любого источника «спасает» ненайденные остальные
        (verdict_6 T4 — target_file больше не глушит корректный file_path).
        Строка-deliverable, похожая на путь, трактуется как путь
        (согласовано с validate_plan_gate); текст без маркеров пути —
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
        """Проверяет шлюз качества перед обработкой вердикта.

        Включает декларативные проверки (require_verdict, require_human_confirm, deliverable_must_exist)
        и регистровый гейт из GATE_REGISTRY (если задан stage.meta.gate).
        Проверка require_human_confirm идёт по явному флагу human_confirmed (D4):
        непустые notes не трактуются как прокси-подтверждение человека —
        подтверждение фиксирует вызывающая сторона после согласования.
        """
        has_declarative = (
            stage.meta.require_verdict
            or stage.meta.require_human_confirm
            or stage.meta.deliverable_must_exist
        )
        if not stage.meta.gate and not has_declarative:
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

        if stage.meta.gate:
            gate_func = GATE_REGISTRY.get(stage.meta.gate)
            if not gate_func:
                msg = f"Неизвестный шлюз качества '{stage.meta.gate}'"
                raise GateValidationError(msg)

            gate_res = gate_func(session, stage, deliverable, root, self.skills_storage)
            blockers.extend(gate_res.blockers)
            warnings.extend(gate_res.warnings)

        if not blockers:
            return None

        gate_name = stage.meta.gate or "declarative_checks"
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
            )
        idx = flow_def.stage_order.index(current_stage.id)
        if idx + 1 < len(flow_def.stage_order):
            next_stage_id = flow_def.stage_order[idx + 1]
            session.current_stage_id = next_stage_id
            session.iteration_count = 1
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
                        "iteration": session.iteration_count,
                        "event": "advance_auto",
                        "verdict": "auto",
                        "notes": notes or "Автопереход этапа без ревьюера",
                    }
                ],
            )

            return prepared_payload

        session.status = FlowStatus.COMPLETED
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
            )

        # verdict == FlowVerdict.APPROVED
        session.iteration_count = 1
        idx = flow_def.stage_order.index(current_stage.id)

        if idx + 1 < len(flow_def.stage_order):
            next_stage_id = flow_def.stage_order[idx + 1]
            session.current_stage_id = next_stage_id
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
                        "iteration": session.iteration_count,
                        "event": "review_approved",
                        "verdict": verdict.value,
                        "notes": notes,
                    }
                ],
            )

            return prepared_payload

        session.status = FlowStatus.COMPLETED
        storage.update_session(
            session,
            history_events=[
                {
                    "stage_id": current_stage.id,
                    "iteration": session.iteration_count,
                    "event": "review_approved",
                    "verdict": verdict.value,
                    "notes": notes,
                },
                {
                    "stage_id": current_stage.id,
                    "iteration": session.iteration_count,
                    "event": "completed",
                    "notes": "Все этапы флоу успешно завершены",
                },
            ],
        )
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
        """Продвигает сессию флоу по этапам с проверкой шлюзов качества и ревью.

        update_git_branch применяется вместе с переходом этапа в одной
        транзакции update_session (D5): используется при продолжении
        сессии после смены git-ветки. Для явного session_id не передаётся.
        """
        root = self._get_root(project_root)
        storage = self._get_storage(root)

        session = storage.get_session(session_id)
        if session is None:
            msg = f"Сессия '{session_id}' не найдена"
            raise FlowNotFoundError(msg)

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
                "Вызовите resolve_flow_escalation для разрешения."
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
    ) -> dict[str, Any]:
        if current_stage.meta.gate in ("validate_qa_report", "validate_report"):
            session.context_data["qa_report_bound"] = True
        if transition is not None:
            return self._advance_named_transition(
                session, flow_def, current_stage, transition, verdict, notes, root, storage, target
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
            )
        return self._advance_review_stage(
            session, flow_def, current_stage, verdict, notes, root, storage, target=target
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
            raise FlowNotFoundError(msg)

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
            session.iteration_count = 1
            session.status = FlowStatus.ACTIVE

            idx = flow_def.stage_order.index(session.current_stage_id)
            if idx + 1 < len(flow_def.stage_order):
                next_stage_id = flow_def.stage_order[idx + 1]
                session.current_stage_id = next_stage_id
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
                            "iteration": session.iteration_count,
                            "event": "escalation_override_approved",
                            "notes": notes or "Принудительное одобрение этапа после эскалации",
                        }
                    ],
                )

                return prepared_payload
            else:
                session.status = FlowStatus.COMPLETED
                storage.update_session(
                    session,
                    history_events=[
                        {
                            "stage_id": previous_stage_id,
                            "iteration": session.iteration_count,
                            "event": "escalation_override_approved",
                            "notes": notes or "Принудительное одобрение этапа после эскалации",
                        },
                        {
                            "stage_id": previous_stage_id,
                            "iteration": session.iteration_count,
                            "event": "completed",
                            "notes": f"Флоу '{session.flow_name}' успешно завершен",
                        },
                    ],
                )
                return _completed_response(session, flow_def)

        if resolution == "retry":
            session.iteration_count = 1
            session.status = FlowStatus.ACTIVE
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

            return prepared_payload

        session.status = FlowStatus.ABORTED
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
        return {
            "status": FlowStatus.ABORTED.value,
            "session_id": session.id,
            "flow_name": session.flow_name,
            "message": f"Флоу '{session.flow_name}' прерван по эскалации.",
            "required_inputs": [],
            "next_call": None,
        }

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
            raise FlowNotFoundError(msg)

        if session.status in (FlowStatus.COMPLETED, FlowStatus.ABORTED):
            msg = f"Сессия уже находится в терминальном статусе '{session.status.value}'"
            raise FlowError(msg)

        session.status = FlowStatus.ABORTED
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
        return {
            "status": FlowStatus.ABORTED.value,
            "session_id": session.id,
            "flow_name": session.flow_name,
            "message": f"Флоу '{session.flow_name}' прерван: {reason}",
            "required_inputs": [],
            "next_call": None,
        }

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
                raise FlowNotFoundError(msg)
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
        if target_session.status in (FlowStatus.COMPLETED, FlowStatus.ABORTED):
            res["required_inputs"] = []
            res["next_call"] = None
        elif target_session.status == FlowStatus.BLOCKED_ESCALATE:
            res["directive"] = (
                "Сессия заблокирована Circuit Breaker. "
                "Задайте уточняющий вопрос пользователю и вызовите resolve_flow_escalation."
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
    raise FlowNotFoundError(f"Активная сессия для {action} не найдена.")


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
        raise FlowNotFoundError(f"Сессия '{session_id}' не найдена")

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
