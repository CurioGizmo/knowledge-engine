---
name: knowledge-memory
description: >-
  Long-term project memory via MCP knowledge-engine.
  Apply before designing features, debugging, reverse engineering, or investigating subtle bugs:
  search for past solutions and antipatterns. When encountering non-trivial forks, record hypotheses;
  persist only institutional knowledge valuable beyond the current task.
---

# Knowledge Engine Lifecycle

This skill defines the 3-phase lifecycle for managing long-term project memory via the `knowledge-engine` MCP server (8 native tools).

> 💡 **Plans and Tasks:** Working plans and task tracking are NOT stored in knowledge base cards. While the MCP server is available, maintain plans exclusively via `audit-engine` API tools (`record_plan` / `update_plan` / `step_plan` / `record_review`) — this is the Single Source of Truth. The file `docs/plans/<date>-<slug>.md` is an explicit offline fallback used only when the API server is unavailable: once MCP access is restored, import the plan into the server and reindex, avoiding duplicate sources.

## 🛫 1. Pre-check (Step 0: Preparation and Exploratory Search)

Before writing code, changing architecture, or planning:
1. **Project Knowledge Overview (Zero-query):**
   Call `search_knowledge()` without parameters to inspect the current topic summary (Project Overview) and category distribution.
2. **Faceted Search by Task and Category:**
   Call `search_knowledge` specifying target categories (`categories`):
   ```python
   search_knowledge(query="feature name or error message", categories=["backend", "database"])
   ```
3. **ALWAYS check the `antipatterns` section:**
   If the search results include antipatterns (`⛔ REFUTED`), inspect their root causes to avoid repeating dead-end approaches and disproven hypotheses.
4. **Inspect matched cards** via `get_knowledge(ids=[...])` (up to 10 cards at once) and account for them in implementation plans or reviews.

## 🔬 2. Investigation & Debugging (Step 1: Working Hypotheses During Research and Debugging)

> **Mandatory Rule:** Never keep potential root causes or implementation alternatives solely in hidden internal monologue (CoT). If a bug or task has > 1 possible cause, subtle failure mode, architectural fork, or requires exploratory solutions — immediately record hypotheses via `propose_hypothesis`.

If you explain to the user or yourself *why* a failure occurred, compare alternatives, or test a technical solution — that is a working hypothesis:

```python
propose_hypothesis(
    title="Brief theory / approach name",
    category="backend",
    symptom="Observed behavior / problem being solved",
    hypothesis="Why it happens / core idea of proposed solution",
    test_plan="How to verify it worked",  # optional
    task_id="my-task-123",  # optional, recommended for grouping
)
```

❌ **DO NOT create hypotheses (skip this step):**
- Fixing trivial typos, file paths, syntax errors;
- Routine CRUD code and basic business logic without trade-offs;
- Mechanical edits and refactorings requested directly by the user.

**Rapid Feedback Loop:**
- **Refuted:** As soon as a test or log demonstrates a false lead — immediately call `resolve_hypotheses(task_id="...", resolutions=[{"id": "...", "action": "refute", "refutation_reason": "..."}])`. The card is immediately recorded as an antipattern (`⛔`), preventing repeated attempts.
- **Confirmed:** Promote to a permanent lesson via `action="confirm"`, supplying `solution`.
- Multiple competing hypotheses can share the same `task_id` (e.g., hypothesis A and hypothesis B).

## 🛬 3. Resolution & Summary (Step 2: Recording Engineering Decisions and Lessons Learned)

The knowledge base is long-term institutional memory, NOT a work report or a bug tracker!

> 💡 **The normal outcome of a standard task is 0 new cards.** Clean code, tests, and commit messages fully document standard development work. Create a card ONLY if the gained experience must outlive the current session and protect future developers from hidden traps or architectural regressions.

1. **Resolving Hypotheses (`resolve_hypotheses`) — IF hypotheses were proposed:**
   If hypotheses were recorded via `propose_hypothesis` during debugging, close them by `task_id` (never leave orphaned hypotheses):
   - `action="confirm"`: hypothesis confirmed **and** provides long-term architectural value or subtle lesson → supply `solution` (agent hypotheses remain in `draft` quarantine until human confirmation; human-confirmed cards become active lessons).
   - `action="refute"`: hypothesis disproven → provide `refutation_reason`; with `merge_into`, the reason is appended to the target card's nuances; without it, recorded as an antipattern `⛔`.
   - `action="drop"`: hypothesis was noise, outdated, or trivial (typo, obvious fix) → deletes draft without polluting the database.

   ```python
   resolve_hypotheses(
       task_id="my-task-123",
       resolutions=[
           {
               "id": "backend-cache-race",
               "action": "confirm",
               "solution": "Use atomic CAS flag",
           },
           {
               "id": "backend-null-ptr",
               "action": "refute",
               "refutation_reason": "Trace revealed different root cause: ...",
           },
           {"id": "backend-stale-config", "action": "drop"},
       ],
   )
   ```

2. **Recording Knowledge Cards (`record_knowledge`) — ONLY for Long-Term Knowledge:**
   Persist ONLY what cannot be prevented by clean code and unit tests:
   - **`type="decision"` (Architectural Decisions & ADRs):** team conventions, structural invariants, library/tooling selections.
   - **`type="lesson"` (Lessons Learned & Gotchas):** counterintuitive or dangerous behavior of third-party libraries, OS, runtime, or network that cannot be deduced from reading clean code.
   - **`type="research"` (Research & Reverse Engineering):** analysis results of external protocols, undocumented APIs, or algorithms.
   - **`type="hint"` (Technical Recommendations & Proven Workarounds):** proven platform workarounds, delicate performance tunings.

   ```python
   record_knowledge(
       title="Brief solution or rule title",
       category="backend",
       type="decision",  # "decision" | "lesson" | "research" | "hint"
       context="Problem context or architectural trade-off",  # or symptom + root_cause
       solution="Reproducible solution or rule (>= 50 chars)",
       rationale="Engineering justification (>= 30 chars)",
       task_id="my-task-123",  # optional, for grouping
   )
   ```

   ⛔ **STRICTLY FORBIDDEN to create cards for:**
   - **Work reports:** lists of completed task steps, sprint checklists, changed file lists, session transcripts, or narratives ("Implementation of plan...", "User requested...");
   - **Micro-bugs and trivial typos:** code bugs belong in unit tests (pytest), not knowledge cards;
   - **User developer preferences:** chat language, LLM model choice, brevity style — these belong in the operator's IDE profile, not the repository knowledge base;
   - **Routine CRUD code** and standard business logic.

3. **Self-Reviewing New Cards (`review_knowledge`) — ONLY if new cards were created:**
   - If cards were created in the session via `record_knowledge`, call `review_knowledge(task_id="...")`.
   - The engine checks quality flags (`plan_markers`, `stale_after_expired`, `thin_solution`, `unverified_auto`) and duplicate suggestions (`similar`).
   - Evaluate each card against criteria: value for future agents, specificity, longevity (not a plan!), absence of duplicates.
   - At the slightest suspicion of work reports or noise, reject the card: `update_knowledge_status(action="dispute", note="...")` or delete the file from disk.

4. **Draft Quarantine and Promotion:**
   - Autonomous agent cards (source=auto_inferred) are created with `draft` status and isolated from general search.
   - Locate drafts within current task: `search_knowledge(query="...", task_id="my-task-123")` or `search_knowledge(include_drafts=True, task_id="my-task-123")` — use `include_drafts` ONLY with `task_id`.
   - At task completion, after `review_knowledge`, present candidates to the user and promote only upon explicit approval:
     ```python
     update_knowledge_status(id="...", action="promote", source="user_confirmed", by="human")
     ```
   - Promotion readiness criteria — **absence of red flags** (`plan_markers`, `stale_after_expired`, `thin_solution`); `draft_candidate` / `unverified_auto` flags are applied to EVERY fresh agent card and are not blockers by themselves.
   - `promote` without human confirmation is prohibited: server requires `source="user_confirmed"` or `by="human"` (guard cannot be weakened). Provenance is preserved on promotion (`generated_by` and `confidence` are not overwritten); confirmation is recorded in verified entries and PROMOTED log.
   - Unapproved cards remain in `draft` — this is a healthy protection against noise, not an error.

## 🏁 4. Completion Checkpoint: Final Answer, walkthrough.md, or git commit

Before formulating the final response, `walkthrough.md`, or `git commit`:
1. **Close open hypotheses:** If hypotheses were created under `task_id`, resolve them via `resolve_hypotheses` (confirm, refute, or drop). Never leave hanging hypotheses.
2. **Evaluate necessity of knowledge cards:**
   - Was a fundamental architectural decision (ADR) made?
   - Was a non-obvious third-party library/OS gotcha discovered that cannot be inferred from clean code and tests?
   - If **NO** — do not call `record_knowledge`! Normal task completion consists of clean code and passing tests.
   - If **YES** — record the card via `record_knowledge`, self-review via `review_knowledge(task_id=...)`, present candidates to user and promote on approval (`update_knowledge_status(action="promote", source="user_confirmed", by="human")`); include the created file in the commit.

## 🔄 Card Self-Correction (`update_knowledge_status`)
- `action="verify"`: confirmation of successful production/testbench operation.
- `action="suspect"`: doubt raised by a failure, with symptom described in note (historical version available via include_archived=True).
- `action="append_note"`: add an observation to the card's history log without altering status.
- `action="dispute"`: exclude an erroneous card from search.
- `action="promote"`: advance card from `draft` to active database — ONLY with explicit human confirmation (`source="user_confirmed"`, `by="human"`).

Transition matrix (K2): suspect/verify apply ONLY to active/verified cards:
- `draft` + suspect/verify → ValueError (quarantine: requires `promote` via human consent first);
- `hypothesis` + verify → ValueError (hypotheses are closed via `resolve_hypotheses`: confirm/refute/drop);
- `draft`/`hypothesis` + append_note/promote — allowed.
