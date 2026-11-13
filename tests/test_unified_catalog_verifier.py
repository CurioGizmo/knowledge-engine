"""Contracts enforced by the sibling catalog's verifier."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from mcp_skills.flow_parser import parse_flow_markdown
from mcp_skills.models import FlowDefinition, FlowStage, FlowTransition
from tests.conftest import get_companion_skills_dir


def _catalog_root() -> Path:
    root = get_companion_skills_dir()
    if root is None:
        pytest.skip("companion mcp_skills catalog is not checked out")
    return root


def _validator():
    path = _catalog_root() / "scripts/verify_catalog.py"
    if not path.is_file():
        pytest.skip("sibling mcp_skills catalog is not checked out")
    spec = importlib.util.spec_from_file_location("unified_catalog_verifier", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate_unified_flows


def _stage(stage_id: str, meta: dict | None = None) -> FlowStage:
    return FlowStage(id=stage_id, title=stage_id, instruction=stage_id, meta=meta or {})


def _flows() -> dict[str, FlowDefinition]:
    review = FlowDefinition(
        name="review-flow",
        option_schema={
            "target_kind": {"allowed": ["auto", "plan", "code", "pr"], "default": "auto"},
            "scope_kind": {
                "allowed": ["branch", "diff", "worktree", "source", "pr"],
                "default": "worktree",
            },
        },
        dispatch_policy={
            "min_agents": 1,
            "max_agents": 4,
            "parallel": "when_safe",
            "selection": "risk_and_touched_domains",
        },
        stages={
            "determine_scope": _stage(
                "determine_scope",
                {
                    "transition_required": True,
                    "transitions": {
                        "plan": {"target": "plan_review", "when_options": {"target_kind": "plan"}},
                        "code": {"target": "code_review", "when_options": {"target_kind": "code"}},
                        "pr": {"target": "pr_design_review", "when_options": {"target_kind": "pr"}},
                        "auto_plan": {
                            "target": "plan_review",
                            "when_options": {"target_kind": "auto"},
                        },
                        "auto_code": {
                            "target": "code_review",
                            "when_options": {"target_kind": "auto"},
                        },
                        "auto_pr": {
                            "target": "pr_design_review",
                            "when_options": {"target_kind": "auto"},
                        },
                    },
                },
            ),
            "plan_review": _stage(
                "plan_review", {"skills": ["plan-review"], "require_verdict": True}
            ),
            "code_review": _stage(
                "code_review", {"skills": ["code-review"], "require_verdict": True}
            ),
            "pr_design_review": _stage(
                "pr_design_review",
                {
                    "skills": ["plan-review"],
                    "require_verdict": True,
                    "transition_required": True,
                    "transitions": {
                        "continue_code": {
                            "target": "pr_code_review",
                            "max_uses": 4,
                            "allow_needs_work": True,
                        }
                    },
                },
            ),
            "pr_code_review": _stage(
                "pr_code_review",
                {
                    "skills": ["code-review"],
                    "require_verdict": True,
                    "transition_required": True,
                    "transitions": {
                        "finish": {"target": "finalize"},
                        "recheck_design": {
                            "target": "pr_design_review",
                            "max_uses": 3,
                            "allow_needs_work": True,
                        },
                    },
                },
            ),
            "finalize": _stage("finalize"),
        },
        stage_order=[
            "determine_scope",
            "plan_review",
            "code_review",
            "pr_design_review",
            "pr_code_review",
            "finalize",
        ],
    )
    execute = FlowDefinition(
        name="execute-plan-flow",
        option_schema={
            "autonomy": {"allowed": ["auto", "semi"], "default": "auto"},
            "scheduling": {"allowed": ["adaptive", "sequential"], "default": "adaptive"},
            "pause_policy": {
                "allowed": ["none", "ready_for_review", "wave", "task"],
                "default": "none",
            },
            "publish_policy": {"allowed": ["confirm"], "default": "confirm"},
        },
        option_rules=[
            {"when": {"autonomy": "semi"}, "defaults": {"pause_policy": "ready_for_review"}}
        ],
        stages={
            "start": _stage("start"),
            "publish": _stage("publish", {"require_human_confirm": True}),
            "perform_publish": _stage("perform_publish"),
        },
        stage_order=["start", "publish", "perform_publish"],
    )
    flows = {review.name: review, execute.name: execute}
    for name, target in {
        "code-review-flow": review.name,
        "deep-review-flow": review.name,
        "plan-execution-flow": execute.name,
        "plan-execution-auto-flow": execute.name,
        "parallel-execute-flow": execute.name,
    }.items():
        flows[name] = FlowDefinition(
            name=name,
            visibility="internal",
            entrypoint=False,
            deprecated=True,
            superseded_by=target,
        )
    return flows


def test_unified_catalog_verifier_accepts_valid_gate_graph() -> None:
    validate = _validator()
    assert validate(_flows()) == []


def test_unified_catalog_verifier_rejects_gate_bypass_and_public_wrapper() -> None:
    validate = _validator()
    flows = _flows()
    flows["execute-plan-flow"].stages["start"].meta.transitions["skip_gate"] = FlowTransition(
        target="perform_publish"
    )
    flows["code-review-flow"].visibility = "public"
    errors = validate(flows)
    assert any("bypasses the publish confirmation gate" in error for error in errors)
    assert any("code-review-flow" in error for error in errors)


def test_live_catalog_has_no_unified_flow_contract_errors() -> None:
    validate = _validator()
    flows_dir = _catalog_root() / "flows"
    flows = {
        path.stem: parse_flow_markdown(path.read_text(encoding="utf-8"), path.stem)
        for path in flows_dir.glob("*.md")
    }
    assert validate(flows) == []
