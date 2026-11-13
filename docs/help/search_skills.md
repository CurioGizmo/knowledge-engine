=== search_skills: Catalog, Drilldown, and FTS Search ===

Modes (Progressive Disclosure — minimal token usage at each tier):
- Zero-query: {mode: overview} — categories with counts, flows {name, description}, stack_values {value: when to use}. ~800-900 tokens.
- category='...' without query: {mode: category} — skill cards {name, title, description} for that category + stack_values. Lightweight drilldown for selecting skills.
- query='...': FTS5 search across name/title/description/stack/content with BM25 ranking. Topic tags (git, postgres, testing) are SEARCH PARAMETERS, not stack values.
- type='flow': public entrypoint flows; with query — alias search with disambiguation arbitration. Use `review-flow` for plan or code reviews; use `execute-plan-flow` for plan execution. Internal compatibility flows can be called directly, but are hidden from overview, FTS, and default search.

Search automatically hides cards bound to a different stack (requires_stack) or incompatible mode (activation); the stack_filter block in the response displays the applied project stack.

The search-mode response returns cards with non-empty values (default fields are omitted for Progressive Disclosure; name, title, category/categories, and score are always present); long content truncation can only occur on the client side.
