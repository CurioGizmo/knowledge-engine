---
name: skills-flow
description: >-
  Skills catalog and development workflows via MCP skills-engine.
  Use when searching or creating skills, selecting processes for planning, implementation,
  plan or code review, QA verification, and release. Skills provide stack-specific instructions,
  while flows guide step-by-step execution.
---

# Development Workflows and Modular Skills Management (Skills Flow)

Step-by-step execution of engineering processes via the `skills-engine` MCP server (5 native tools: search_skills, load_skill, create_skill, step_flow, get_help). After selecting a flow and before reading code (до чтения кода), if `mcp_audit` is available, call `get_task_context(task_id=..., view="summary")` for the current task; call it again when resuming work.

## Process Entry and Scope Boundaries

- "Let's discuss...", "I want to implement...", "Thinking of adding..." regarding a feature or architecture triggers process selection: before substantive discussion, call `search_skills(type="flow")`, then select and launch the appropriate workflow per arbitration rules below. Routine technical questions do not require a development flow.
- "Don't do anything yet" and "don't write code yet" mean staying in the requirements and alternatives discussion phase. They do not prevent searching and selecting a process. Starting a flow does not authorize implementation: do not modify code or advance stages beyond agreed discussion.
- "I want to implement..." without explicit permission to skip discussion leads through discussion and planning. Small task size does not grant permission to skip stages.
- When explicitly instructed "do it directly" or "without a plan", search for an appropriate streamlined route in the catalog. Never invent a non-existent flow and never mark unexecuted stages of a full flow as completed. If no compatible route exists, state the limitation and agree on next steps.
- **Review request = entry into `review-flow`.** Flow classifies plan or code (including diffs, working copy, branch, and PR). Pass known `target_kind` and `scope_kind` via `flow_options`; "comments only" keeps report mode without applying changes. An independent primary reviewer is mandatory; domain reviewers are selected based on risks and affected areas (1–4 reviewers).
- **Executing an approved plan = entry into `execute-plan-flow`.** Defaults: `autonomy=auto`, `scheduling=adaptive`, `pause_policy=none`, `publish_policy=confirm`. Adaptive scheduling permits parallel execution when dependencies are resolved and file changes do not overlap; explicit "sequential" sets `scheduling=sequential`. For semi-automatic mode, use `autonomy=semi`: pauses before independent review and before push, without pausing after each subtask.
- **Publishing requires a human.** In automatic and semi-automatic modes, before the initial push, creating, or updating a PR, display the final diff, verification results, commit history, remote, target branch, and publication plan. Never pass `human_confirmed=true` without explicit user confirmation.
- **Changing requirements returns to plan review.** Update the approved plan and repeat independent review via `review-flow` with `target_kind=plan` before continuing implementation.
- Legacy specialized flows remain accessible for direct invocation and backwards compatibility, but new requests should enter via public entrypoints.

## Plans and Audit

When drafting or revising a plan, use the task/plan retrieved via `get_task_context`. If `mcp_audit` is available, maintain plans, steps, statuses, and reviews there; register the finalized plan after independent plan review. Do not create parallel `docs/plans` files without reason. Record technical debt within the current project audit context and link to the task. If `mcp_audit` is unavailable, use available project file storage.

## Choice: Skill or Flow?

- **Skill** — HOW to do something: stack-tailored instructions (assemble via `load_skill` and follow them).
- **Flow** — MANAGED PROCESS: stages, deliverables, quality gates, and reviewer verdicts.
  For code review, planning, feature acceptance, and releases — prefer flows: they prevent skipping stages.

## 🧭 1. Catalog Navigation and Overview (`search_skills`)

1. **Catalog Summary (Zero-query Overview):**
   Call `search_skills()` without parameters. The server returns a compact summary:
   - Categories and skill counts in the current project.
   - List of available interactive workflows (flows) with short descriptions.
   - `stack_values` — dictionary of assembly axes {value: when to use} for `load_skill(stack=[...])`.
2. **Category Drilldown:** `search_skills(category="backend-db")` — skill cards {name, title, description}.
3. **Full-text Search:**
   - Search skills: `search_skills(query="fastapi", category="backend-db", type="skill")`
   - Search workflows: `search_skills(query="feature", type="flow")`
   - Unified search: `search_skills(query="auth", type="all")`

## 🧩 2. Dynamic Skill Assembly Tailored to Stack (`load_skill`)

Skills in the system are modular: they contain a base skeleton and dynamic slots tailored to specific project frameworks and technologies.

1. **Assembling a skill:**
   ```python
   load_skill(name="writing-plans", stack=["python", "mcp"])
   ```
2. `stack` — assembly axis values (language / mcp vs. offline mode / framework), shared across the engine:
   see `stack_values` in overview or `slot_variants` in `load_skill` response. YOU select values
   matching the project's actual stack; the engine injects corresponding blocks into skeleton slots.
   Response includes `stack_applied` (what was matched) and `slot_variants` (available variations).

## Creating Skills (`create_skill`)

When creating a skill, formulate `aliases` from its purpose and usage conditions, passing them in the same `create_skill` call. Typically 4–8 concise search queries across English and user languages suffice; if the user communicates in English, English alone is sufficient.

Use natural phrasing of action, symptom, or technical term: e.g. `["slow sql queries", "query optimization", "медленные sql запросы"]`. Include only phrases reflecting the skill content; avoid generic words, duplicates, or listing the entire stack. Preserve standard technology spelling. When updating, preserve valid existing aliases.

After creation, return to the main task: do not call `search_skills` merely to verify generated aliases. Search quality validation belongs in dedicated MCP testing.

## ⚡ 3. Interactive Workflow Runner (`step_flow`)

The state machine guarantees stage-by-stage quality and prevents skipped steps via Quality Gates.

### Flow Selection and Arbitration:
1. **Search:** Start with `search_skills(type="flow")` (or with `query="topic"`) to discover public entrypoints.
2. **Unambiguous match:** Launch the matching flow directly without repeatedly querying the user about mode. Use `review-flow` for reviews, `execute-plan-flow` for plan execution; pass known options as `flow_options`.
3. **Significant ambiguity:** If the goal cannot be inferred from the request and context, clarify only the missing decision using the client's native question tool.

**Dynamic Grouping by intent_group:**
`intent_group` itself does not require asking questions. Follow explicit user preferences and unified entry flow defaults. Do not re-ask already answered questions.

> [!NOTE]
> **Declarative Stage Quality Gates:**
> Workflow stages declare requirements in `<!-- meta: {...} -->`:
> - `require_verdict: true` — requires explicit verdict (`approved` or `needs_work`). For stages with `agent`, verdict is mandatory by state machine; `require_verdict` applies to automated stages where deliverables are accepted without review by default.
> - `require_human_confirm: true` — requires explicit user agreement and `human_confirmed=true`; non-empty `notes` do not substitute confirmation.
> - `deliverable_must_exist: true` — requires existence and non-empty artifact file (via `target_file` or path passed in `deliverable`).

### Workflow Lifecycle:

1. **Starting a workflow:**
   ```python
   step_flow(flow="execute-plan-flow", flow_options={"autonomy": "auto"})
   ```
   Server initializes a session in `flows.db`, binds to current git branch, and returns first stage instructions (`spec_analysis`).
   Sessions persist in SQLite: upon context loss (new session, restart), call
   `step_flow(action="status")` — returns active stage and its instructions; do not restart flow.

2. **Advancing to next stage (`advance`):**
   After completing stage instructions, supply the deliverable:
   ```python
   step_flow(
       action="advance",
       deliverable="docs/plans/feature_plan.md",  # file path or artifact text
       expected_stage_id="spec_analysis",  # protects against desynchronization
   )
   ```
   - Quality Gate automatically validates the deliverable.
   - If validation fails: status `gate_blocked` with detailed blockers list (`blockers`). Fix artifact and call `advance` again.
   - For declared branches, pass `transition="<name>"`. Arbitrary stage jumping and bypassing gates is prohibited. `flow_options` are configured only at start and persist across resume.

3. **Review Stages and Circuit Breaker:**
   On stages involving a reviewer (`verdict`):
   ```python
   step_flow(action="advance", verdict="approved")  # or verdict="needs_work"
   ```
   - `verdict="approved"`: advances to next stage.
   - `verdict="needs_work"`: increments loop counter.
   - If `loop_max` exceeded: status `blocked_escalate` (Circuit Breaker).

4. **Resolving Escalation (`escalation_resolve`):**
   If session is blocked by Circuit Breaker, agree on resolution with user:
   ```python
   step_flow(
       action="escalation_resolve",
       resolution="override_approved",  # "override_approved" | "retry" | "abort"
       notes="User approved continuation",
   )
   ```

5. **Checking Status (`status`):**
   Read-only call (safe without anchored project root):
   ```python
   step_flow(action="status")
   ```

6. **Retrying Stage (`retry`) and Aborting (`abort`):**
   - Retry current stage: `step_flow(action="retry")`
   - Cancel workflow: `step_flow(action="abort", notes="Cancellation reason")`

## Section Selection and Shared Library

`stack` specifies technologies/integration axes; `touches` specifies section names. Without touches, all mandatory sections are included; optional_slots are selected explicitly alongside required primary sections. The `load_skill` response returns available_slots, optional_slots, and slot_variants of local/shared blocks. SLOT selects the first matching variant, SLOT+ aggregates matches. Root blocks/ are reused across the catalog; a local tech block overrides the shared block for the same technology. The default block is used only when no matches exist.
