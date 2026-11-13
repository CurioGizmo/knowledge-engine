"""Contracts for unified-flow metadata and public discoverability."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from mcp_skills.errors import InvalidFlowFormatError
from mcp_skills.flow_parser import (
    list_available_flows,
    load_flow_definition,
    merge_flow_override,
    parse_flow_markdown,
)
from mcp_skills.models import FlowDefinition, FlowDispatchPolicy, FlowVisibility


def _flow(frontmatter: str = "", *, stage_meta: str = "{}") -> str:
    prefix = f"---\n{frontmatter.strip()}\n---\n" if frontmatter.strip() else ""
    return (
        prefix
        + "# Test flow\n\n"
        + "## Start [id: start]\n"
        + f"<!-- meta: {stage_meta} -->\n"
        + "Run the stage.\n\n"
        + "## Finish [id: finish]\nDone.\n"
    )


def test_legacy_flow_defaults_to_public_entrypoint() -> None:
    flow = parse_flow_markdown(_flow(), "legacy-flow")

    assert flow.visibility is FlowVisibility.PUBLIC
    assert flow.entrypoint is True
    assert flow.deprecated is False
    assert flow.superseded_by is None
    assert flow.option_schema == {}
    assert flow.defaults == {}
    assert flow.option_rules == []
    assert flow.dispatch_policy is None


def test_parse_unified_metadata_and_stage_contracts() -> None:
    flow = parse_flow_markdown(
        _flow(
            """
visibility: public
entrypoint: true
deprecated: false
option_schema:
  autonomy:
    allowed: [auto, semi]
    default: auto
  pause_policy:
    allowed: [none, ready_for_review, wave, task]
    default: none
defaults:
  autonomy: auto
option_rules:
  - when: {autonomy: semi}
    defaults: {pause_policy: ready_for_review}
dispatch_policy:
  min_agents: 1
  max_agents: 4
  parallel: when_safe
  selection: risk_and_touched_domains
""",
            stage_meta=(
                '{"transition_required": true, "transitions": '
                '{"complete": {"target": "finish", "max_uses": 2, '
                '"when_options": {"autonomy": "auto"}}}, '
                '"dispatch_policy": {"min_agents": 1, "max_agents": 2, '
                '"parallel": "when_safe", "selection": "risk_and_touched_domains"}}'
            ),
        ),
        "unified-flow",
    )

    assert flow.option_schema["autonomy"].allowed == ["auto", "semi"]
    assert flow.option_rules[0].when == {"autonomy": "semi"}
    assert flow.dispatch_policy is not None
    assert flow.dispatch_policy.max_agents == 4
    stage = flow.stages["start"]
    assert stage.meta.transition_required is True
    assert stage.meta.transitions["complete"].target == "finish"
    assert stage.meta.dispatch_policy is not None
    assert stage.meta.dispatch_policy.max_agents == 2


@pytest.mark.parametrize(
    "frontmatter",
    [
        "visibility: internal\nentrypoint: true",
        "deprecated: true\nsuperseded_by: same-flow",
    ],
)
def test_invalid_flow_definition_metadata_is_rejected(frontmatter: str) -> None:
    with pytest.raises(InvalidFlowFormatError):
        parse_flow_markdown(_flow(frontmatter), "same-flow")


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [(0, 1), (2, 1), (1, 5)],
)
def test_dispatch_policy_bounds_are_fail_closed(minimum: int, maximum: int) -> None:
    with pytest.raises(ValidationError):
        FlowDispatchPolicy(
            min_agents=minimum,
            max_agents=maximum,
            parallel="when_safe",
            selection="risk_and_touched_domains",
        )


def test_option_schema_rejects_non_scalar_and_unknown_default() -> None:
    with pytest.raises(InvalidFlowFormatError):
        parse_flow_markdown(
            _flow(
                """
option_schema:
  autonomy:
    allowed: [auto, semi]
    default: invalid
"""
            ),
            "bad-options",
        )

    with pytest.raises(InvalidFlowFormatError):
        parse_flow_markdown(
            _flow(
                """
option_schema:
  bad:
    allowed: [{nested: value}]
    default: {nested: value}
"""
            ),
            "bad-scalars",
        )


def test_transition_target_must_exist() -> None:
    with pytest.raises(InvalidFlowFormatError, match="missing"):
        parse_flow_markdown(
            _flow(stage_meta='{"transitions": {"repeat": {"target": "missing"}}}'),
            "bad-transition",
        )


@pytest.mark.parametrize(
    ("when_options", "message"),
    [
        ('{"unknown": "auto"}', "неизвестный option"),
        ('{"autonomy": "invalid"}', "недопустимое значение"),
    ],
)
def test_transition_when_options_match_declared_schema(when_options: str, message: str) -> None:
    with pytest.raises(InvalidFlowFormatError, match=message):
        parse_flow_markdown(
            _flow(
                """
option_schema:
  autonomy:
    allowed: [auto, semi]
    default: auto
""",
                stage_meta=(
                    '{"transitions": {"finish_now": {"target": "finish", '
                    f'"when_options": {when_options}'
                    "}}}"
                ),
            ),
            "bad-transition-options",
        )


def test_override_omission_preserves_base_metadata() -> None:
    base = parse_flow_markdown(
        _flow(
            """
visibility: internal
entrypoint: false
deprecated: true
superseded_by: target-flow
defaults: {autonomy: semi}
"""
        ),
        "legacy-flow",
    )
    override = parse_flow_markdown(
        _flow("description: Project-specific instructions"),
        "legacy-flow_override",
    )

    merged = merge_flow_override(base, override)

    assert merged.visibility is FlowVisibility.INTERNAL
    assert merged.entrypoint is False
    assert merged.deprecated is True
    assert merged.superseded_by == "target-flow"
    assert merged.defaults == {"autonomy": "semi"}


def test_override_transition_can_target_stage_from_base() -> None:
    base = parse_flow_markdown(_flow(), "base-flow")
    override = parse_flow_markdown(
        """# Override
## Start [id: start]
<!-- meta: {"transitions": {"finish_now": {"target": "finish"}}} -->
Use the base finish stage.
""",
        "base-flow_override",
        validate_transition_targets=False,
    )

    merged = merge_flow_override(base, override)

    assert merged.stages["start"].meta.transitions["finish_now"].target == "finish"


def test_override_transition_options_validate_after_schema_merge() -> None:
    base = parse_flow_markdown(
        _flow(
            """
option_schema:
  autonomy:
    allowed: [auto, semi]
    default: auto
"""
        ),
        "base-flow",
    )
    valid_override = parse_flow_markdown(
        _flow(
            stage_meta='{"transitions": {"finish_now": {"target": "finish", '
            '"when_options": {"autonomy": "semi"}}}}'
        ),
        "base-flow_override",
        validate_transition_targets=False,
    )
    assert merge_flow_override(base, valid_override).stages["start"].meta.transitions[
        "finish_now"
    ].when_options == {"autonomy": "semi"}

    invalid_override = parse_flow_markdown(
        _flow(
            stage_meta='{"transitions": {"finish_now": {"target": "finish", '
            '"when_options": {"autonomy": "invalid"}}}}'
        ),
        "base-flow_override",
        validate_transition_targets=False,
    )
    with pytest.raises(ValidationError, match="недопустимое значение"):
        merge_flow_override(base, invalid_override)


def test_listing_hides_internal_deprecated_and_non_entrypoint_but_direct_load_works(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "catalog"
    flows_dir = skills_dir / "flows"
    flows_dir.mkdir(parents=True)
    (flows_dir / "public-flow.md").write_text(_flow(), encoding="utf-8")
    (flows_dir / "internal-flow.md").write_text(
        _flow("visibility: internal\nentrypoint: false"), encoding="utf-8"
    )
    (flows_dir / "support-flow.md").write_text(
        _flow("visibility: public\nentrypoint: false"), encoding="utf-8"
    )
    (flows_dir / "deprecated-flow.md").write_text(
        _flow("visibility: public\ndeprecated: true"), encoding="utf-8"
    )

    public_names = {item["name"] for item in list_available_flows(tmp_path, skills_dir=skills_dir)}
    all_items = list_available_flows(tmp_path, skills_dir=skills_dir, discoverable_only=False)
    all_names = {item["name"] for item in all_items}

    assert "public-flow" in public_names
    assert "internal-flow" not in public_names
    assert "support-flow" not in public_names
    assert "deprecated-flow" not in public_names
    assert {"public-flow", "internal-flow", "support-flow", "deprecated-flow"}.issubset(all_names)
    summary = next(item for item in all_items if item["name"] == "internal-flow")
    assert summary["visibility"] == "internal"
    assert summary["entrypoint"] is False
    assert load_flow_definition("internal-flow", tmp_path, skills_dir=skills_dir).name == (
        "internal-flow"
    )


def test_flow_definition_rejects_unsafe_superseded_name() -> None:
    with pytest.raises(ValidationError):
        FlowDefinition(name="safe-flow", superseded_by="../unsafe")
