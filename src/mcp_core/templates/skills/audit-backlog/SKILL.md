---
name: audit-backlog
description: >-
  Development plans, code reviews, and technical debt tracking via MCP audit-engine.
  Use when starting or resuming tasks with a known task_id/PR, querying task status,
  planning, reviewing, picking ready tasks from the backlog, or resolving technical debt.
  Load task context before work; record plans, review outcomes, and debt cards via server tools.
---

# Technical Debt, Plans, and Code Review Management (Audit Backlog)

This skill defines rules for managing implementation plans, code review cycles, and technical debt/audit cards across the project using the `audit-engine` MCP server (11 native tools).

## 1. Task Context Before Starting Work (`get_task_context`)

If a `task_id` or PR URL is known, always call `get_task_context` before reading or modifying code when starting a task or resuming in a new session. Never fabricate an identifier for a task without a record in audit-engine:

```json
{
  "task_id": "PROJ-142",
  "view": "summary"
}
```

The server returns aggregated context: the active plan (with step statuses), code review history, and associated technical debt cards.

### Status Query ("where did we stop?", "what remains to be done?")
If the user only asks for the task status:
1. Identify the task before reading files or running commands. If `task_id` or PR URL is known, use it. If only the topic is known, find the plan via `search_artifacts(query="...", artifact_type="plan")`; if no topic is specified, find active plans via `search_artifacts(artifact_type="plan", status="in_progress")`. When multiple plans match, ask for clarification instead of guessing.
2. Call `get_task_context(task_id="...", view="full")` or `get_task_context(pr_url="...", view="full")` for the matched task. If no matching plan is found, report this rather than inventing an ID.
3. Respond with a structured summary: plan status, completed steps with dates, notes, and evidence from context, followed by remaining open steps. Never invent missing details.
4. Stop there: do not apply migrations, modify code, execute work commands, or mark steps done via `step_plan`. A status query alone is not an instruction to implement.

If the user in the same message also instructs you to proceed with work, use the retrieved context and proceed without waiting for the next turn. Once completed, verify the result and close the corresponding step via `step_plan(task_id="...", step=N, note="...")`. If no instruction to proceed was given, wait for it before executing work.

## 2. Implementation Plans (`record_plan` / `update_plan` / `step_plan`)

1. **Recording a plan:** When a task requires a plan or the user requests one, call `record_plan` (creates a plan with status `discussed` in `.agents/plans/active/<category>/`):
   ```json
   {
     "task_id": "PROJ-142",
     "title": "OAuth2 Authorization",
     "steps": ["1. TokenProvider", "2. Middleware", "3. Tests"],
     "target_files": ["src/auth.py"],
     "category": "backend"
   }
   ```

### Plan Approval
- After calling `record_plan`, the plan enters `discussed` status. If the user only requested a plan or discussion, present it and await instructions to implement.
- If the user already instructed you to execute the task, do not demand redundant re-approval merely because of the `discussed` status. Pass plan verification if required by the chosen flow, then promote the plan to `approved` via `update_plan` and proceed with implementation. During execution, transition the plan to `in_progress`.

2. **Approval and updates:** `update_plan(task_id="...", status="approved", pr_url="...")` — status progression: `discussed` → `approved` → `in_progress` → `done` (or `cancelled`).
3. **Closing steps:** ALWAYS use `step_plan` INSTEAD of manually editing Markdown files (marks `[x]` and logs verification notes and evidence):
   ```json
   { "task_id": "PROJ-142", "step": 1, "note": "Unit tests pass" }
   ```
   *Plan completion:* When all steps are closed, the engine automatically transitions the plan to `done` and archives it to `.agents/plans/completed/<category>/`.

## 3. Code Review Rounds (`record_review`)

Upon completing a PR review or hotfix review, call `record_review` (saves an artifact in `.agents/reviews/<category>/` and synchronizes the status of the linked plan):

```json
{
  "task_id": "PROJ-142",
  "pr_url": "https://github.com/org/repo/pull/85",
  "verdict": "approved",
  "summary": "Clean implementation",
  "spawned_debts": ["aud-backend-abc123"]
}
```

Verdicts: `approved` (plan transitions to `done` if all steps are closed), `changes_requested` (plan reverts to `in_progress`), `commented`.

### Mandatory Structural Linkage of Debt to Review (`spawned_debts`)
If a defect discovered during review or self-check is deferred to technical debt:
- It is NOT sufficient to merely mention the card ID in the comment text, notes, or summary!
- The server does not extract IDs from free text — structural linkage between artifacts is established **exclusively** via the `spawned_debts: ["aud-backend-..."]` argument when calling `record_review`.

## 4. Checking Known Debt (Code Review)

During code review, the `code-reviewer` subagent MUST verify whether an identified defect is already tracked as technical debt:

1. Call the `check_known_debt` tool from `audit-engine`:
   ```json
   {
     "symptom": "Brief technical issue description (e.g. method > 50 lines, missing DEVNULL)",
     "file_path": "relative/path/to/file.py",
     "symbol": "function_or_class_name"
   }
   ```
2. **Verdict Arbitration:**
   * If `is_known_debt == true`: defect is already tracked as an open backlog issue → report as `[Known Debt: <id>]` and **do not block merge**.
   * If `is_regression == true`: the issue was previously resolved but has recurred → mark as `🔴 Critical: REGRESSION_OF_<id>` and **block merge**.
   * If `found == false`: defect is new → mark comment as `[Status: New]` (requires auto-fix or user approval to defer to debt).

## 5. Recording New Debt (Legacy / Won't Fix) and Linking to Review

When legacy code or out-of-scope defects are discovered during review or audit (or deferred with user consent):

### Unified Procedure (MANDATORY SEQUENCE):
1. **Deduplication check (BEFORE recording debt):**
   ALWAYS verify that an identical debt card does not already exist:
   - Call `check_known_debt(symptom="...", file_path="...")` or `search_debt(query="...", category="...")`.
   - Never call `record_debt` blindly without prior duplicate checking!
2. **Card creation (if no duplicate exists):**
   Call `record_debt`:
   ```json
   {
     "title": "Brief task title",
     "category": "backend / desktop / audio / ui",
     "priority": "important",
     "symptom": "Problem description",
     "root_cause": "Where and why it occurs",
     "solution": "Proposed resolution approach",
     "target_file": "path/to/file.py",
     "symbol": "function_name",
     "autonomy": "high",
     "complexity": "M",
     "task_id": "PROJ-142"
   }
   ```
   *Parameters strictly adhere to enums:* priority (`critical`/`important`/`suggestion`), autonomy (`high`/`medium`/`low`), complexity (`L`/`M`/`H`).
   The server creates a card in `.agents/audit/todo/<category>/` and returns a unique ID: `"id": "aud-<category>-<hash>"`.
3. **Establishing structural linkage in review:**
   Take the returned ID (e.g. `"aud-backend-2fed"`) and ALWAYS pass it in the `spawned_debts` argument when calling `record_review`:
   ```json
   {
     "task_id": "PROJ-142",
     "verdict": "commented",
     "summary": "Review complete, deferred defect recorded as debt",
     "spawned_debts": ["aud-backend-2fed"]
   }
   ```
   This guarantees structural linkage between the review artifact and the debt card.

## 6. Planning and Taking Tasks into Work

1. **Retrieving ready tasks:**
   * Call `get_ready_backlog(category=None)`.
   * The server automatically resolves the dependency DAG (`blocked_by`) and returns only those tasks with status `planned` whose blockers are already closed.
2. **Searching technical debt:**
   * Call `search_debt(query="...", category="...")`.
3. **Completing a task:**
   * After implementation and successful tests, call `update_debt_status(id="<id>", status="completed", pr_url="...")`.
   * The server updates status and moves the card to `.agents/audit/done/<category>/`.

## 7. Artifact Search (`search_artifacts`)

To search plans and reviews by keyword and inspect the active task backlog:

```json
{
  "query": "OAuth2",
  "artifact_type": "plan",
  "status": "all"
}
```

Artifact types: `plan`, `review`, `debt` (or `all` — default); statuses are filtered with the `status` parameter.
