"""Behavioral checks against the companion unified-flow catalog."""

from pathlib import Path

import pytest

import mcp_skills.flow_engine as flow_engine_module
from mcp_skills.flow_engine import FlowEngine
from mcp_skills.flow_storage import FlowStorage
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer
from tests.conftest import get_companion_skills_dir


@pytest.fixture
def catalog_root() -> Path:
    root = get_companion_skills_dir()
    if root is None:
        pytest.skip("companion mcp_skills catalog is not checked out")
    return root


@pytest.fixture
def engine(tmp_path: Path, catalog_root: Path, monkeypatch: pytest.MonkeyPatch) -> FlowEngine:
    monkeypatch.setattr(flow_engine_module, "get_current_branch", lambda _root: "test")
    skills = SkillsStorage(tmp_path / "skills.db")
    SkillsSynchronizer(skills, [catalog_root / "skills"]).sync()
    return FlowEngine(
        project_root=tmp_path,
        storage=FlowStorage(db_path=tmp_path / "flows.db"),
        skills_storage=skills,
        skills_dir=catalog_root,
    )


@pytest.mark.parametrize(
    ("pause_policy", "all_done_transition"),
    [("wave", "all_done_wave"), ("task", "all_done_task")],
)
def test_execute_plan_can_reenter_checkpoint_after_another_wave(
    engine: FlowEngine, pause_policy: str, all_done_transition: str
) -> None:
    started = engine.start_flow(
        "execute-plan-flow", flow_options={"pause_policy": pause_policy}, target="prompt"
    )
    session_id = started["session_id"]
    assert started["stage"]["id"] == "inspect_plan"

    def advance(stage: str, **kwargs: object) -> dict:
        return engine.advance_flow(session_id, expected_stage_id=stage, target="prompt", **kwargs)

    advance("inspect_plan")
    for round_number in (1, 2):
        advance("schedule_wave")
        advance("execute_wave", verdict="approved")
        checkpoint = advance("verify_wave", verdict="approved", transition=all_done_transition)
        assert checkpoint["stage"]["id"] == "progress_checkpoint"
        if round_number == 1:
            advance("progress_checkpoint", transition="next_wave", human_confirmed=True)


def test_empty_review_object_can_finish_without_reviewer(engine: FlowEngine) -> None:
    started = engine.start_flow(
        "review-flow", flow_options={"target_kind": "code"}, target="prompt"
    )
    assert "no_object" in started["available_transitions"]
    result = engine.advance_flow(
        started["session_id"], expected_stage_id="determine_scope", transition="no_object"
    )
    assert result["stage"]["id"] == "finalize"
    assert "NO_CHANGES" in result["stage"]["instruction"]
    assert "BLOCKED" in result["stage"]["instruction"]
    assert "без reviewer не ставь `APPROVED`" in result["stage"]["instruction"]
    assert "subagent" not in result


def test_review_flow_stages_require_verdict(catalog_root: Path) -> None:
    from mcp_skills.flow_parser import load_flow_definition

    review = load_flow_definition("review-flow", catalog_root)
    assert review is not None
    assert review.stages["plan_review"].meta.require_verdict is True
    assert review.stages["code_review"].meta.require_verdict is True
