"""Серверное управление состоянием оркестрации планов (aud-skills-2ab9).

Инкапсулирует состояние волн, задач и ревью внутри ``session.context_data['orchestration']``
(SQLite ``flows.db``) и атомарно материализует канонический markdown-трекер
``.agents/plans/.orchestration/<slug>/orchestration-state.md`` (или
``docs/plans/.orchestration/<slug>/orchestration-state.md`` в offline-режиме)
строго после успешного коммита сессии.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp_core.sync import _atomic_write_text
from mcp_skills.models import FlowDefinition, FlowSession, FlowStatus
from mcp_skills.stack_detector import detect_mode

logger = logging.getLogger(__name__)

_PLAN_EXECUTION_FLOWS: frozenset[str] = frozenset(
    {
        "execute-plan-flow",
        "parallel-execute-flow",
        "plan-execution-auto-flow",
        "plan-execution-flow",
    }
)
_DEFAULT_BRANCH_NAMES: frozenset[str] = frozenset({"main", "master", "develop", "head"})
_VALID_TASK_STATUSES: frozenset[str] = frozenset({"PENDING", "IN_PROGRESS", "DONE", "BLOCKED"})
_VALID_WAVE_STATUSES: frozenset[str] = frozenset({"PENDING", "IN_PROGRESS", "DONE", "BLOCKED"})


def is_orchestration_flow(flow_def: FlowDefinition) -> bool:
    """Возвращает True для сценариев исполнения планов, использующих трекер оркестрации."""
    return flow_def.name in _PLAN_EXECUTION_FLOWS or flow_def.intent_group == "plan-execution"


def _sanitize_slug(raw: str | None, fallback: str) -> str:
    """Нормализует slug к безопасному имени каталога без path traversal."""
    if raw is None:
        return fallback
    cleaned = raw.strip().lower()
    if "/" in cleaned or "\\" in cleaned:
        cleaned = cleaned.replace("\\", "/").rstrip("/").split("/")[-1]
    if cleaned.endswith(".md"):
        cleaned = cleaned[:-3]
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip(".-")
    if not cleaned or cleaned in {".", ".."} or ".." in cleaned:
        return fallback
    return cleaned[:64]


def _branch_slug_candidate(branch: str | None) -> str | None:
    if not branch:
        return None
    tail = branch.strip().replace("\\", "/").split("/")[-1]
    if tail.lower() in _DEFAULT_BRANCH_NAMES:
        return None
    return tail


def _read_head_sha(root: Path) -> str | None:
    """Читает текущий HEAD SHA, если проект является git-репозиторием."""
    git_bin = shutil.which("git")
    if not git_bin or not (root / ".git").exists():
        return None
    try:
        proc = subprocess.run(  # noqa: S603
            [git_bin, "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Не удалось получить HEAD SHA для %s: %s", root, exc)
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


def resolve_orchestration_path(root: Path, slug: str) -> tuple[Path, str, str]:
    """Возвращает ``(abs_path, rel_path_posix, mode)`` согласно ADR и режиму проекта."""
    mode = detect_mode(root) or "mcp"
    base_rel = (
        Path("docs/plans/.orchestration")
        if mode == "offline"
        else Path(".agents/plans/.orchestration")
    )
    safe_slug = _sanitize_slug(slug, "session")
    rel_path = base_rel / safe_slug / "orchestration-state.md"
    abs_path = root / rel_path

    resolved_root = root.resolve()
    resolved_target = abs_path.resolve()
    if not resolved_target.is_relative_to(resolved_root):
        msg = f"Небезопасный путь состояния оркестрации вне корня проекта: '{abs_path}'"
        raise ValueError(msg)

    return abs_path, rel_path.as_posix(), mode


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_deliverable_dict(deliverable: Any) -> dict[str, Any] | None:
    """Извлекает словарь из dict или JSON-строки deliverable."""
    if deliverable is None:
        return None
    if isinstance(deliverable, dict):
        return deliverable
    if isinstance(deliverable, str):
        text = deliverable.strip()
        if not text or not text.startswith("{"):
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.debug("deliverable не является JSON-объектом: %s", exc)
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _normalize_task_entry(raw: dict[str, Any]) -> dict[str, Any] | None:
    task_id = str(raw.get("id") or "").strip()
    if not task_id:
        return None
    status_raw = str(raw.get("status") or "PENDING").strip().upper()
    status = status_raw if status_raw in _VALID_TASK_STATUSES else "PENDING"
    files_raw = raw.get("files")
    if isinstance(files_raw, list):
        files = [str(f).strip() for f in files_raw if f is not None and str(f).strip()]
    elif isinstance(files_raw, str) and files_raw.strip():
        files = [files_raw.strip()]
    else:
        files = []
    return {
        "id": task_id,
        "name": str(raw.get("name") or raw.get("title") or task_id).strip(),
        "files": files,
        "status": status,
        "notes": str(raw.get("notes") or "").strip(),
    }


def _merge_tasks(existing: list[dict[str, Any]], incoming: list[Any]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in existing:
        if isinstance(item, dict) and item.get("id"):
            tid = str(item["id"])
            by_id[tid] = dict(item)
            order.append(tid)

    for raw in incoming:
        if not isinstance(raw, dict):
            continue
        normalized = _normalize_task_entry(raw)
        if normalized is None:
            continue
        tid = normalized["id"]
        if tid in by_id:
            prev = by_id[tid]
            if raw.get("name") is None and raw.get("title") is None:
                normalized["name"] = prev.get("name", tid)
            if raw.get("files") is None:
                normalized["files"] = prev.get("files", [])
            if raw.get("notes") is None:
                normalized["notes"] = prev.get("notes", "")
            by_id[tid] = normalized
        else:
            by_id[tid] = normalized
            order.append(tid)

    return [by_id[tid] for tid in order]


def _merge_waves(existing: list[dict[str, Any]], incoming: list[Any]) -> list[dict[str, Any]]:
    by_wave: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in existing:
        if isinstance(item, dict) and item.get("wave") is not None:
            wkey = str(item["wave"])
            by_wave[wkey] = dict(item)
            order.append(wkey)

    for raw in incoming:
        if not isinstance(raw, dict) or raw.get("wave") is None:
            continue
        wkey = str(raw["wave"]).strip()
        if not wkey:
            continue
        status_raw = str(raw.get("status") or "PENDING").strip().upper()
        status = status_raw if status_raw in _VALID_WAVE_STATUSES else "PENDING"
        tids_raw = raw.get("task_ids") or raw.get("tasks") or []
        task_ids = (
            [str(t).strip() for t in tids_raw if t is not None and str(t).strip()]
            if isinstance(tids_raw, list)
            else []
        )
        entry = {
            "wave": raw["wave"],
            "task_ids": task_ids
            if (raw.get("task_ids") is not None or raw.get("tasks") is not None)
            else by_wave.get(wkey, {}).get("task_ids", []),
            "status": status,
        }
        if wkey not in by_wave:
            order.append(wkey)
        by_wave[wkey] = entry

    return [by_wave[k] for k in order]


def init_orchestration_state(
    session: FlowSession,
    flow_def: FlowDefinition,
    root: Path,
) -> dict[str, Any]:
    """Инициализирует начальное состояние оркестрации в ``session.context_data``."""
    slug_src = session.task_id or _branch_slug_candidate(session.git_branch)
    slug = _sanitize_slug(slug_src, session.id)
    _, rel_path, mode = resolve_orchestration_path(root, slug)
    now = _now_iso()

    state: dict[str, Any] = {
        "slug": slug,
        "auto_slug": slug_src is None,
        "auto_branch": True,
        "mode": mode,
        "path": rel_path,
        "plan_name": session.task_id or flow_def.title or flow_def.name,
        "plan_path": None,
        "task_id": session.task_id,
        "base_git_sha": _read_head_sha(root),
        "feature_branch": session.git_branch,
        "created_at": now,
        "updated_at": now,
        "tasks": [],
        "waves": [],
        "review": {
            "status": "PENDING",
            "iteration": 0,
            "max_iterations": 5,
            "review_log": None,
            "open_issues": [],
        },
        "merge_log": [],
        "final_status": "PENDING",
    }
    session.context_data["orchestration"] = state
    return state


def _apply_metadata_patch(
    state: dict[str, Any],
    session: FlowSession,
    patch: dict[str, Any],
) -> None:
    """Обновляет идентификаторы плана, ветку, SHA и slug из патча deliverable."""
    task_id_val = patch.get("task_id")
    if isinstance(task_id_val, str) and task_id_val.strip():
        state["task_id"] = task_id_val.strip()
        if not session.task_id:
            session.task_id = state["task_id"]
    plan_path_val = patch.get("plan_path")
    if isinstance(plan_path_val, str) and plan_path_val.strip():
        state["plan_path"] = plan_path_val.strip()
    plan_name_val = patch.get("plan_name")
    if isinstance(plan_name_val, str) and plan_name_val.strip():
        state["plan_name"] = plan_name_val.strip()

    base_sha = patch.get("base_git_sha") or patch.get("base_sha")
    if isinstance(base_sha, str) and base_sha.strip():
        state["base_git_sha"] = base_sha.strip()

    explicit_branch = patch.get("feature_branch") or patch.get("branch")
    if isinstance(explicit_branch, str) and explicit_branch.strip():
        state["feature_branch"] = explicit_branch.strip()
        state["auto_branch"] = False
    elif state.get("auto_branch", True) and session.git_branch:
        state["feature_branch"] = session.git_branch

    explicit_slug = patch.get("slug")
    if state.get("auto_slug") or (isinstance(explicit_slug, str) and explicit_slug.strip()):
        cand = explicit_slug or task_id_val or plan_path_val
        if isinstance(cand, str) and cand.strip():
            new_slug = _sanitize_slug(cand, state["slug"])
            if new_slug != state["slug"]:
                state["slug"] = new_slug
            state["auto_slug"] = False


_REVIEW_STAGE_IDS: frozenset[str] = frozenset(
    {
        "review",
        "independent_review",
        "code_review",
        "plan_review",
        "pr_code_review",
        "pr_design_review",
    }
)


def _safe_int(value: Any, default: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        logger.debug("Нечисловое значение итерации ревью %r: %s", value, exc)
        return default
    return parsed if parsed > 0 else default


def _apply_review_patch(
    state: dict[str, Any],
    session: FlowSession,
    patch: dict[str, Any],
    *,
    stage_id: str,
    stage_validator: str | None = None,
    stage_iteration: int | None = None,
    verdict: str | None,
    notes: str,
) -> None:
    """Обновляет секцию ревью при прохождении review-этапа или передаче review_id."""
    rev_raw = patch.get("review")
    is_review_stage = stage_validator == "validate_review" or stage_id in _REVIEW_STAGE_IDS
    if not is_review_stage and not patch.get("review_id") and not isinstance(rev_raw, dict):
        return
    review_state = dict(state.get("review") or {})
    rev_patch = rev_raw if isinstance(rev_raw, dict) else {}
    if verdict == "approved":
        review_state["status"] = "APPROVED"
    elif verdict == "needs_work":
        review_state["status"] = "NEEDS_FIX"
    elif rev_patch.get("status"):
        review_state["status"] = str(rev_patch["status"]).strip().upper()
    fallback_iter = stage_iteration if stage_iteration is not None else session.iteration_count
    raw_iter = rev_patch.get("iteration") or patch.get("round")
    review_state["iteration"] = _safe_int(raw_iter, fallback_iter)
    if patch.get("review_id"):
        review_state["review_log"] = str(patch["review_id"]).strip()
    elif rev_patch.get("review_log"):
        review_state["review_log"] = str(rev_patch["review_log"]).strip()
    if isinstance(rev_patch.get("open_issues"), list):
        review_state["open_issues"] = [str(i) for i in rev_patch["open_issues"] if i is not None]
    elif verdict == "needs_work" and (patch.get("summary") or notes):
        issue_note = str(patch.get("summary") or notes).strip()
        if issue_note:
            review_state["open_issues"] = [issue_note]
    elif verdict == "approved":
        review_state["open_issues"] = []
    state["review"] = review_state


def _resolve_final_status(
    state: dict[str, Any],
    session: FlowSession,
    patch: dict[str, Any],
    final_status_override: str | None,
) -> str:
    if final_status_override:
        return final_status_override
    patch_status = patch.get("final_status")
    if isinstance(patch_status, str) and patch_status.strip():
        return patch_status.strip().upper()
    if session.status == FlowStatus.COMPLETED:
        return "COMPLETED"
    if session.status == FlowStatus.ABORTED:
        return "ABORTED"
    if session.status == FlowStatus.BLOCKED_ESCALATE:
        return "BLOCKED"
    current = str(state.get("final_status") or "PENDING")
    return "IN_PROGRESS" if current == "PENDING" else current


def update_orchestration_state(
    session: FlowSession,
    flow_def: FlowDefinition,
    root: Path,
    *,
    stage_id: str,
    stage_validator: str | None = None,
    stage_iteration: int | None = None,
    deliverable: Any = None,
    verdict: str | None = None,
    notes: str = "",
    final_status_override: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Обновляет ``session.context_data['orchestration']`` из этапа и deliverable.

    Возвращает ``(state, previous_path_if_renamed)``.
    """
    state = session.context_data.get("orchestration")
    if not isinstance(state, dict):
        state = init_orchestration_state(session, flow_def, root)

    previous_path: str | None = state.get("path")
    payload = _extract_deliverable_dict(deliverable) or {}
    orch_patch = payload.get("orchestration")
    merged_patch: dict[str, Any] = dict(payload)
    if isinstance(orch_patch, dict):
        merged_patch.update(orch_patch)

    _apply_metadata_patch(state, session, merged_patch)

    if isinstance(merged_patch.get("tasks"), list):
        state["tasks"] = _merge_tasks(state.get("tasks", []), merged_patch["tasks"])
    if isinstance(merged_patch.get("waves"), list):
        state["waves"] = _merge_waves(state.get("waves", []), merged_patch["waves"])
    if isinstance(merged_patch.get("merge_log"), list):
        existing_log = list(state.get("merge_log", []))
        for entry in merged_patch["merge_log"]:
            text = str(entry).strip() if entry is not None else ""
            if text and text not in existing_log:
                existing_log.append(text)
        state["merge_log"] = existing_log

    _apply_review_patch(
        state,
        session,
        merged_patch,
        stage_id=stage_id,
        stage_validator=stage_validator,
        stage_iteration=stage_iteration,
        verdict=verdict,
        notes=notes,
    )
    state["final_status"] = _resolve_final_status(
        state, session, merged_patch, final_status_override
    )

    _, new_rel_path, mode = resolve_orchestration_path(root, state["slug"])
    state["mode"] = mode
    state["path"] = new_rel_path
    state["updated_at"] = _now_iso()
    session.context_data["orchestration"] = state

    renamed_from = previous_path if previous_path and previous_path != new_rel_path else None
    return state, renamed_from


def _md_cell(value: Any) -> str:
    """Экранирует переводы строк и разделители таблиц Markdown."""
    return str(value or "").replace("\n", " ").replace("|", "\\|").strip()


def render_orchestration_markdown(state: dict[str, Any]) -> str:
    """Рендерит детерминированный markdown-трекер по формату orchestration-state.template.md."""
    tasks = state.get("tasks") or []
    waves = state.get("waves") or []
    total_tasks = len(tasks)
    done_tasks = sum(1 for t in tasks if isinstance(t, dict) and t.get("status") == "DONE")
    pct = round((done_tasks / total_tasks) * 100) if total_tasks > 0 else 0
    filled = round(pct / 10)
    bar = "█" * filled + "░" * (10 - filled)

    lines = [
        f"<!-- Location: {state.get('path')}. Ephemeral execution tracker managed by mcp_skills.step_flow - never commit to git. -->",
        f"# Orchestration State: {state.get('plan_name') or state.get('slug')}",
        f"Created: {state.get('created_at') or ''}",
        f"Updated: {state.get('updated_at') or ''}",
        f"Plan: {state.get('plan_path') or state.get('task_id') or '—'}",
        f"Base SHA: {state.get('base_git_sha') or '—'}",
        f"Feature Branch: {state.get('feature_branch') or '—'}",
        f"Progress: [{bar}] {pct}% ({done_tasks}/{total_tasks} tasks)",
        "",
        "> 🛑 **Инвариант устойчивости к компакции (Compaction Guard):**",
        "> Перед выполнением любых действий оркестратор ОБЯЗАН сверить этот файл с `git log`.",
        "> Задачи со статусом `DONE`, чьи коммиты уже присутствуют в ветке, **КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО запускать повторно**.",
        "> Возобновление работы происходит строго с первой задачи со статусом `PENDING`.",
        "",
        "## Tasks",
        "",
        "| ID | Name | Files | Status | Notes |",
        "|----|------|-------|--------|-------|",
    ]
    if tasks:
        for t in tasks:
            files_str = _md_cell(", ".join(t.get("files") or [])) or "—"
            notes_str = _md_cell(t.get("notes"))
            lines.append(
                f"| {_md_cell(t.get('id'))} | {_md_cell(t.get('name'))} | {files_str} | {_md_cell(t.get('status') or 'PENDING')} | {notes_str} |"
            )
    else:
        lines.append("| — | (задачи ещё не переданы) | — | PENDING | |")

    lines.extend(
        [
            "",
            "## Waves (волны параллельного выполнения)",
            "",
            "| Wave | Task IDs | Status |",
            "|------|----------|--------|",
        ]
    )
    if waves:
        for w in waves:
            tids = _md_cell(", ".join(str(x) for x in (w.get("task_ids") or []))) or "—"
            lines.append(
                f"| {_md_cell(w.get('wave'))} | {tids} | {_md_cell(w.get('status') or 'PENDING')} |"
            )
    else:
        lines.append("| — | — | PENDING |")

    review = state.get("review") or {}
    open_issues = review.get("open_issues") or []
    issues_str = "; ".join(str(i) for i in open_issues) if open_issues else "—"
    lines.extend(
        [
            "",
            "## Review",
            f"Status: {review.get('status', 'PENDING')}",
            f"Iteration: {review.get('iteration', 0)}",
            f"Max Iterations: {review.get('max_iterations', 5)}",
            f"Review Log: {review.get('review_log') or '—'}",
            f"Open Issues: {issues_str}",
            "",
            "## Merge Log",
        ]
    )
    merge_log = state.get("merge_log") or []
    if merge_log:
        lines.extend(f"- {entry}" for entry in merge_log)
    else:
        lines.append("(пусто)")

    lines.extend(
        [
            "",
            "## Final Status",
            str(state.get("final_status") or "PENDING"),
            "",
        ]
    )
    return "\n".join(lines)


def sync_orchestration_file(
    root: Path,
    state: dict[str, Any],
    *,
    previous_path: str | None = None,
) -> Path:
    """Атомарно записывает ``orchestration-state.md`` на диск после коммита в БД."""
    target_path, _, _ = resolve_orchestration_path(root, str(state.get("slug") or "session"))
    target_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(target_path, render_orchestration_markdown(state))

    if previous_path:
        old_path = (root / previous_path).resolve()
        resolved_root = root.resolve()
        if (
            old_path != target_path.resolve()
            and old_path.is_relative_to(resolved_root)
            and ".orchestration" in old_path.parts
            and old_path.is_file()
        ):
            old_path.unlink(missing_ok=True)
            try:
                old_path.parent.rmdir()
            except OSError as exc:
                logger.debug("Каталог оркестрации %s не удалён (не пуст): %s", old_path.parent, exc)
    return target_path


def summarize_orchestration_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    """Формирует компактное JSON-резюме состояния оркестрации для ответов step_flow."""
    if not isinstance(state, dict):
        return None
    tasks = state.get("tasks") or []
    waves = state.get("waves") or []
    done_tasks = sum(1 for t in tasks if isinstance(t, dict) and t.get("status") == "DONE")
    review = state.get("review") or {}
    return {
        "slug": state.get("slug"),
        "mode": state.get("mode"),
        "path": state.get("path"),
        "plan_path": state.get("plan_path"),
        "task_id": state.get("task_id"),
        "base_git_sha": state.get("base_git_sha"),
        "feature_branch": state.get("feature_branch"),
        "tasks_total": len(tasks),
        "tasks_done": done_tasks,
        "waves_total": len(waves),
        "review_status": review.get("status", "PENDING"),
        "final_status": state.get("final_status", "PENDING"),
    }
