=== Skills Engine: Core Workflows & Patterns (5 tools) ===

Code and plan review is initiated via `review-flow`; execution of approved plans is driven by `execute-plan-flow`. Mode options can be configured via `flow_options` upon launch. If `mcp_audit` is available, verify the existing implementation plan first and maintain plan steps and statuses there.

Selecting a skill for a task:
  search_skills() → search_skills(category='...') → load_skill(name, stack)
  Search automatically filters out skills incompatible with the project stack. `stack_values` in the response indicates available assembly axes; when in doubt, call `get_help('load_skill')`.

Topic and tech search:
  search_skills(query='git' | 'postgres' | 'audio') — FTS over tags, descriptions, and content.

Assembly tailored to project environment:
  load_skill(name, stack=['python', 'fastapi']) — returns instructions customized for your tech stack.
  load_skill(name, stack=['ruby']) — same skill for another language: common skeleton, distinct technology blocks.
  load_skill without stack — auto-detects from project environment (manifests + active mode); see `stack_source` in response.
  `slot_variants` in response lists all available block variants for this skill.

Online / MCP service vs. Local / Offline fallback:
  If a service/tool is available: stack=['mcp'] supplies tool-calling protocols.
  If unavailable: stack=['offline'] supplies local file-based workflows.
  Unified skill definition dynamically adapts to the current execution environment.

Authoring new skills:
  Call `get_help('create_skill')` for structural instructions (skeleton + blocks + conventions),
  then invoke `create_skill(name, content, blocks, stack_descriptions, ...)`.

Sequential workflow execution:
  step_flow(action='advance', flow='...') — start session; step_flow(action='status') — check current step.

Combining axes:
  load_skill(name, stack=['python', 'fastapi', 'mcp']) — combines axes; each slot matches corresponding technology blocks.

