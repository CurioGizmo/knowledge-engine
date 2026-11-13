# load_skill

`stack` — technologies and integration axis (mcp/offline); `touches` — selective section inclusion. Without touches, all sections are included except optional_slots. With explicit touches, only specified sections are included; the base skeleton is preserved. To include an optional topic, list it along with the required sections from available_slots.

SLOT selects the first matching tech block; SLOT+ aggregates all matches. For each technology, a local block takes precedence over a shared catalog block; if no match is found, local default is used, followed by shared default. The default block is not appended to matched blocks. There is no implicit matching of composite names like python-fastapi.

Assembly response contains: content, resources, slots_filled (only actually populated slots), available_slots, optional_slots, slot_variants (local and shared), assembly (manifest: requested_stack, matched_technologies, unmatched_technologies, sections with sources matched/default/missing, selected_resources, revision, status), stack_applied (effective input stack; does not guarantee presence of a specific tech block — see assembly.matched_technologies), stack_source, stack_supplemented, status ('complete'/'incomplete'), and was_truncated. If mandatory core exceeds token budget, ASSEMBLY_BUDGET_EXCEEDED error is returned (mandatory rules are never silently truncated). By default (stack=None or ""), stack is auto-detected from project manifests; explicit stack=[] denotes empty stack with no auto-detection.

Reading associated Markdown resources (reference guides, templates, examples):
`load_skill(name="<owner_skill>", resource_id="<catalog_rel_path>[#<anchor>]", cursor=None)`.
When reading resources, stack and touches parameters are forbidden. For large documents, response provides next_cursor and revision for sequential pagination.

Examples:
- load_skill(name="writing-plans", stack=["python", "fastapi", "sqlite"])
- load_skill(name="onnx-whisper-engine", stack=["python"], touches=["vad-silero"])
- load_skill(name="writing-plans", resource_id="templates/standard-plan.template.md#2.4")

optional_slots is defined in frontmatter or via create_skill. Without this field, legacy catalogs maintain default behavior. Shared library root is resolved via catalog configuration; the engine does not hardcode language or skill names.
