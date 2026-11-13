=== step_flow: Workflow Execution Facade ===

Actions (Decision Table):
- status: inspect current active session instructions (read-only safe).
- advance + flow='review-flow' or 'execute-plan-flow': start session; advance without flow: progress to next step after deliverable/verdict.
- flow_options: configure parameters at session start; effective values and origins persist across session steps and are returned with status. E.g., `{"autonomy": "semi", "scheduling": "sequential"}`.
- transition: named branch transition declared by current stage. Only passed with advance; transition counter and preconditions are validated by engine. With `verdict='needs_work'`, transition is only allowed if `allow_needs_work: true` is explicitly configured in metadata.
- retry: repeat current stage.
- abort: cancel session.
- escalation_resolve (resolution: override_approved|retry|abort): resolve Circuit Breaker escalation.

Mutating actions require an anchored project root; expected_stage_id guards against stage desynchronization. `execute-plan-flow` defaults to automatic adaptive scheduling; `autonomy=semi` pauses before independent review. In both modes, publishing requires explicit user confirmation and `human_confirmed=true` before git push or pull request.
