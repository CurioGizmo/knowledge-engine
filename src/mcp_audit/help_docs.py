"""Reference docs for the get_help tool in MCP Audit Engine."""

from __future__ import annotations

__all__ = ["get_audit_help_text"]

HELP_DOCS: dict[str, str] = {
    "overview": """# MCP Audit Engine — Overview

Operational tracker for development plans, code reviews and technical debt (11 tools).

Tool groups:
- Debt: check_known_debt, get_ready_backlog, search_debt, record_debt, update_debt_status
- Plans: record_plan, update_plan, step_plan
- Reviews & context: record_review, get_task_context
- Unified search: search_artifacts

Statuses by artifact type:
- plan: discussed, approved, in_progress, done, cancelled
- debt: planned, in_progress, completed, obsolete
- review (verdict): approved, changes_requested, commented

A status applies only to compatible artifact types; a fully unknown status is rejected with a validation error.

Plan tracking channel: while this server is reachable, plans are created and updated only through
the API tools (record_plan / update_plan / step_plan). A `docs/plans/<date>-<slug>.md` file in the
repository is an explicit offline fallback (server unreachable); after access is restored, re-import
the plan via record_plan so search and backlog stay single-source.

Use get_help(tool_name) for per-tool details, e.g. get_help('record_debt').
""",
    "check_known_debt": """# check_known_debt

Purpose: check whether a symptom matches a known debt or regression (for the code-review skill).
When to call: MANDATORY during code review, BEFORE issuing a verdict (known debt does not block a merge, regression does).
Key params: symptom (required text), file_path, symbol, category.
Example: check_known_debt(symptom="method longer than 50 lines", file_path="src/api.py", symbol="handler")
""",
    "get_ready_backlog": """# get_ready_backlog

Purpose: list planned tasks whose blocked_by blockers are all closed (completed or obsolete) — DAG resolution is automatic.
When to call: when picking the next backlog task to work on.
Key params: category (optional filter).
Example: get_ready_backlog(category="backend")
""",
    "search_debt": """# search_debt

Purpose: full-text search over audit and debt cards.
When to call: before recording a new debt (duplicate check) and for topic backlog review.
Key params: query, category, limit.
Statuses (debt only): planned, in_progress, completed, obsolete; status="all" disables the filter.
An unknown status is rejected with a validation error, not a silent empty result.
Example: search_debt(query="race condition", status="planned")
""",
    "record_debt": """# record_debt

Purpose: create a new atomic debt card in .agents/audit/todo/<category>/.
When to call: when a legacy defect is found outside the scope of the current task, after check_known_debt.
Key params: title, category, symptom, root_cause, solution, target_file, symbol, blocked_by, task_id.
Enums: priority: critical | important | suggestion; autonomy: high | medium | low; complexity: L | M | H.
An invalid enum value is rejected with a validation error — it is NOT silently replaced by a default.
Example: record_debt(title="Remove DEVNULL", category="backend", symptom="subprocess hang", target_file="src/run.py")
""",
    "update_debt_status": """# update_debt_status

Purpose: update the status of a debt card ('completed' moves the card to done/).
When to call: after a debt task is finished and tests pass (status="completed" + pr_url).
Key params: id, status, resolution_note, pr_url.
Statuses: planned, in_progress, completed, obsolete; an invalid value is a validation error.
Example: update_debt_status(id="aud-backend-abc123", status="completed", pr_url="https://github.com/org/repo/pull/85")
""",
    "record_plan": """# record_plan

Purpose: create a new development plan in .agents/plans/active/<category>/ (initial status discussed).
When to call: at the start of a feature/refactoring to fix goals and steps.
Key params: task_id, title, steps, target_files, category, pr_url, details.
Plan tracking channel: use this API tool whenever the server is reachable; a `docs/plans/<date>-<slug>.md`
file is an explicit offline fallback — re-import it via record_plan after access is restored.
Example: record_plan(task_id="PROJ-142", title="OAuth2", steps=["1. TokenProvider", "2. Middleware"])
""",
    "update_plan": """# update_plan

Purpose: update plan metadata — approval, cancellation, title, PR URL, added steps.
When to call: to approve a plan (status="approved"), attach a PR URL, or cancel.
Key params: task_id, title, add_steps, target_files, status, pr_url.
Plan statuses: discussed, approved, in_progress, done, cancelled; an invalid value is a validation error.
Example: update_plan(task_id="PROJ-142", status="approved", pr_url="https://github.com/org/repo/pull/85")
""",
    "step_plan": """# step_plan

Purpose: check/uncheck a plan step checkbox and log a dated note — no manual Markdown editing.
When to call: MANDATORY when closing a plan step, instead of editing the Markdown file by hand.
Key params: task_id, step (index), status ("done" | "open"), note, allow_out_of_order.
Example: step_plan(task_id="PROJ-142", step=1, note="Unit tests green")
""",
    "record_review": """# record_review

Purpose: store a code-review round in .agents/reviews/<category>/ with a verdict and comments.
When to call: MANDATORY when finishing a PR/hotfix review (works with a plan or standalone via pr_url).
Key params: task_id, pr_url, verdict, summary, comments, round, category, spawned_debts.
Verdicts: approved, changes_requested, commented.
comments is a strict list of ReviewComment objects: file_path (required), symptom (required), line, status (open | resolved | deferred). Fields outside the schema are rejected by validation.
Example: record_review(pr_url="https://github.com/org/repo/pull/85", verdict="approved", summary="Clean")
""",
    "get_task_context": """# get_task_context

Purpose: aggregated task context — current plan, review history and related debts in one response.
When to call: MANDATORY before starting work on a task or resuming in a new session (call it BEFORE reading code).
Key params: task_id, pr_url, view ("summary" | "full").
Example: get_task_context(task_id="PROJ-142", view="summary")
""",
    "search_artifacts": """# search_artifacts

Purpose: full-text and faceted search across plans, reviews and debt cards.
When to call: to find plans/reviews by keywords or review the active backlog (status="in_progress").
Key params: query, artifact_type, category, status, limit.
artifact_type: 'plan', 'review', 'debt' or 'all'. Compatibility: 'audit' is an accepted input alias for 'debt'; the response always uses the canonical 'debt'.
Statuses by type (applied only to compatible types):
- plan: discussed, approved, in_progress, done, cancelled
- debt: planned, in_progress, completed, obsolete
- review (verdict): approved, changes_requested, commented
status='all' disables the filter; a fully unknown status is rejected with a validation error.
Ranking: with a text query and artifact_type='all', ranks from both sources are merged via reciprocal rank fusion (RRF) instead of comparing raw BM25 scores from different indexes; an empty query produces a single time-sorted list (newest first, stable id tie-break).
Example: search_artifacts(query="OAuth2", artifact_type="plan")
""",
}

_ALIASES: dict[str, str] = {
    "debt": "record_debt",
    "known": "check_known_debt",
    "backlog": "get_ready_backlog",
    "plan": "record_plan",
    "review": "record_review",
    "context": "get_task_context",
    "search": "search_artifacts",
}


def get_audit_help_text(tool_name: str | None = None) -> str:
    """Returns the help text for the given audit tool or the overview."""
    clean_name = "overview" if not tool_name or not tool_name.strip() else tool_name.strip().lower()
    target_name = _ALIASES.get(clean_name, clean_name)
    if target_name in HELP_DOCS:
        return HELP_DOCS[target_name]

    available = ", ".join(f"'{k}'" for k in HELP_DOCS)
    return f"Unknown tool or help section: '{tool_name}'.\nAvailable help sections: {available}."
