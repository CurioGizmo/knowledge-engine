=== create_skill: Modular Skill Authoring ===

Skill = Skeleton + Blocks + Stack Conventions. All configured via a single tool.

1. SKELETON (content): Invariants, activation triggers, and common guidelines. Variation points use markers: <!-- SLOT: <slot> -->. Stack-specific code does NOT belong in the skeleton.
   Use kebab-case slot names; a slot represents a meaningful variation point (mode, core, integration).

2. BLOCKS (blocks: {slot: {value: markdown}}): files blocks/<slot>/<value>.md.
   value=default — fallback without specific stack; other values = assembly axes (mcp/offline/python/fastapi/...). Block content is a self-contained markdown snippet injected into the slot marker.

3. STACK & CONVENTIONS: stack=['python', 'fastapi'] — skill axes; new values automatically append to docs/stack-conventions.yaml. stack_descriptions={value: 'when to use'} — axis semantics: supply for new axes to document their purpose.

Dual-mode pattern (mcp/offline): If a skill operates both via MCP tools and file conventions — define slot mode (or integration) with blocks mcp.md, offline.md, and default.md (probe: verify MCP availability and choose branch).

Rules:
- available_slots: slot names declared in skeleton markers (for indexing).
- description in frontmatter: activation trigger used by LLM in search.
- aliases: search synonyms, up to 30.
- Update: pass overwrite=True (overwrites SKILL.md and specified blocks; conventions are append-only).
- Engine validation: kebab-case names verified via is_safe_name; SpamGate content checks (secrets detection, 100 KB limit).

Reference architecture (dual-mode): skills/general/technical-debt-management — skeleton + blocks/mode/{default,mcp,offline}.md.