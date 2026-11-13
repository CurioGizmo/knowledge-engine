# ADR: Trust Boundary for Promoting Cards from Draft Quarantine

- Status: Accepted (2026-10-19)
- Context: Defect R3 of acceptance report [2026-10-18-post-implementation-review.md](2026-10-18-post-implementation-review.md)
- Scope: `update_knowledge_status(action="promote")`, `draft` quarantine in knowledge-engine agent

## Decision

**The `draft` quarantine and human consent requirement for `promote` is a process convention, not a technical security boundary (соглашение о процессе, а не техническая граница доверия).**

The server accepts `source="user_confirmed"` and `by="human"` as parameter strings. Any caller (including an LLM agent) can pass these values — the server cannot distinguish genuine human consent from substituted strings. Protection is protocol-based: the agent is required to present the card to the user first, obtain explicit consent, and only then call `promote`.

## Rejected Alternative: Technical Boundary

The "trusted channel" approach (CLI flag/environment variable automatically elevating call trust) was rejected:

- An agent with shell access can invoke the CLI itself, pass interactive input, and set environment variables. `KNOWLEDGE_TRUSTED_CHANNEL` and similar mechanisms are secure only under provable environment control by a trusted host — under the current interaction model (LLM agent = MCP client), this is not guaranteed.
- Any check of caller "humanness" that does not rely on OS-level cryptography or authentication can be bypassed by the same agent.

## What is Guaranteed and What is Not

| Property | Guarantee |
| --- | --- |
| `draft` cards are hidden from general search | Technical (index filter) |
| Card provenance (`generated_by`, `confidence`) is preserved upon promotion | Technical |
| Fact of consent is recorded in verified entry and `PROMOTED` log | Technical |
| `promote` is executed ONLY with actual human consent | **Protocol-level** — not verifiable by server |

## Commitments

1. Documentation (`help_docs`, tool docstrings) must explicitly state: there is no technical security boundary; substituted `source`/`by` strings are not rejected by the server.
2. Invariant test checks for the presence of this statement in help docs (see `tests/test_promote_consent_contract.py`).
3. Tests verifying successful promote based on `by`/`source` strings are documented as testing **process convention behavior**, not "security".

## Criteria for Revising Contract

The contract will be revisited upon introduction of:

- A trusted host channel with provable environment control (authenticated CLI call from human terminal, env isolation from agent processes, or cryptographic signature of consent);
- Integration with an identity verification system (SSO/user token).

Until then, any claims of "technical protection against autonomous promotion" are considered invalid and rejected during review.

## Provenance Protection (KS13 Provenance Protection)

Under the KS13 contract (Option B), cards of human origin (`generated.by == "human"` / `source="user_confirmed"`), as well as process cards promoted from `draft` quarantine to `active` via `action="promote"`, are protected from destructive automated mutations (`auto_inferred` calls to `dispute` and `verify`, as well as superseding via `supersedes` or merging via `merge_into`).

### Structural Marker `action: promote`

To prevent false positives (where regular verification `action="verify"` with freeform note text such as `"Promoted to active: checklist"` might inadvertently trigger process card protection), the system employs a strict structural marker in the `verified` block:

```yaml
verified:
  - by: human # or agent relaying user consent
    at: '2026-10-30'
    note: 'Promoted to active: ...'
    action: promote
```

The predicate `CardFrontmatter.is_human_protected` recognizes promotion ONLY when an entry with `action == "promote"` is present.

In the current MCP test harness, migrating legacy cards is not required. KS13 tests create new isolated cards using the current schema format.
