"""Named transitions are guarded, bounded and distinct from linear progress."""

from __future__ import annotations

from pathlib import Path

import pytest

import mcp_skills.flow_engine as module
from mcp_skills.errors import SessionConflictError
from mcp_skills.flow_storage import FlowStorage
from mcp_skills.models import (
    FlowDefinition,
    FlowOptionSpec,
    FlowStage,
    FlowStageMeta,
    FlowTransition,
)


@pytest.fixture
def setup_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, FlowStorage]:
    definition = FlowDefinition(
        name="loop-flow",
        option_schema={"mode": FlowOptionSpec(allowed=["go", "stop"], default="go")},
        stages={
            "start": FlowStage(
                id="start",
                title="Start",
                instruction="Choose",
                meta=FlowStageMeta(
                    transition_required=True,
                    transitions={
                        "again": FlowTransition(
                            target="start", max_uses=1, when_options={"mode": "go"}
                        ),
                        "finish": FlowTransition(target="end", max_uses=1),
                        "finish_negative": FlowTransition(
                            target="end",
                            max_uses=1,
                            when_options={"mode": "stop"},
                            allow_needs_work=True,
                        ),
                    },
                ),
            ),
            "end": FlowStage(id="end", title="End", instruction="Done"),
        },
        stage_order=["start", "end"],
    )
    monkeypatch.setattr(module, "load_flow_definition", lambda *_args, **_kw: definition)
    monkeypatch.setattr(module, "get_current_branch", lambda _root: "test-branch")
    return tmp_path, FlowStorage(db_path=tmp_path / "flows.db")


def test_named_transition_persists_count_and_history(setup_flow: tuple[Path, FlowStorage]) -> None:
    root, store = setup_flow
    first = module.step_flow(flow="loop-flow", project_root=root, storage=store)
    sid = first["session_id"]
    with pytest.raises(ValueError, match="requires|требует"):
        module.step_flow(session_id=sid, project_root=root, storage=store)
    repeated = module.step_flow(
        session_id=sid, transition="again", project_root=root, storage=store
    )
    assert repeated["stage"]["id"] == "start"
    assert "again" not in repeated["available_transitions"]
    assert "finish" in repeated["available_transitions"]
    session = store.get_session(sid)
    assert session is not None
    assert session.iteration_count == 1
    assert session.context_data["transition_counts"]["start:again"] == 1
    assert any("start --again--> start" in row["notes"] for row in store.get_history(sid))
    with pytest.raises(SessionConflictError, match="Лимит"):
        module.step_flow(session_id=sid, transition="again", project_root=root, storage=store)
    finished_stage = module.step_flow(
        session_id=sid, transition="finish", project_root=root, storage=store
    )
    assert finished_stage["stage"]["id"] == "end"


def test_transition_guard_and_needs_work_rejected(setup_flow: tuple[Path, FlowStorage]) -> None:
    root, store = setup_flow
    first = module.step_flow(
        flow="loop-flow", flow_options={"mode": "stop"}, project_root=root, storage=store
    )
    assert first["available_transitions"] == ["finish", "finish_negative"]
    sid = first["session_id"]
    with pytest.raises(ValueError, match="Условия"):
        module.step_flow(session_id=sid, transition="again", project_root=root, storage=store)
    with pytest.raises(ValueError, match="needs_work"):
        module.step_flow(
            session_id=sid,
            transition="finish",
            verdict="needs_work",
            project_root=root,
            storage=store,
        )
    assert store.get_session(sid).context_data.get("transition_counts") is None
    retry = module.step_flow(session_id=sid, verdict="needs_work", project_root=root, storage=store)
    assert retry["stage_id"] == "start"
    assert store.get_session(sid).iteration_count == 2


def test_explicit_negative_transition_finishes_without_retry(
    setup_flow: tuple[Path, FlowStorage],
) -> None:
    root, store = setup_flow
    first = module.step_flow(
        flow="loop-flow", flow_options={"mode": "stop"}, project_root=root, storage=store
    )
    sid = first["session_id"]
    end = module.step_flow(
        session_id=sid,
        transition="finish_negative",
        verdict="needs_work",
        project_root=root,
        storage=store,
    )
    assert end["stage"]["id"] == "end"
    session = store.get_session(sid)
    assert session is not None
    assert session.iteration_count == 1
    assert session.context_data["transition_counts"] == {"start:finish_negative": 1}
    assert store.get_history(sid)[-1]["verdict"] == "needs_work"


def test_negative_transition_accepts_reviewer_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definition = FlowDefinition(
        name="review-flow",
        option_schema={
            "review_mode": FlowOptionSpec(
                allowed=["report-only", "fix-authorized"], default="report-only"
            )
        },
        stages={
            "review": FlowStage(
                id="review",
                title="Review",
                instruction="Return findings",
                meta=FlowStageMeta(
                    agent="code-reviewer",
                    transition_required=True,
                    transitions={
                        "finish_needs_fix": FlowTransition(
                            target="finalize",
                            when_options={"review_mode": "report-only"},
                            allow_needs_work=True,
                        )
                    },
                ),
            ),
            "finalize": FlowStage(id="finalize", title="Final", instruction="Report"),
        },
        stage_order=["review", "finalize"],
    )
    monkeypatch.setattr(module, "load_flow_definition", lambda *_args, **_kw: definition)
    monkeypatch.setattr(module, "get_current_branch", lambda _root: "test-branch")
    store = FlowStorage(db_path=tmp_path / "flows.db")
    started = module.step_flow(flow="review-flow", project_root=tmp_path, storage=store)
    result = module.step_flow(
        session_id=started["session_id"],
        transition="finish_needs_fix",
        verdict="needs_work",
        project_root=tmp_path,
        storage=store,
    )
    assert result["stage"]["id"] == "finalize"
    assert store.get_history(started["session_id"])[-1]["verdict"] == "needs_work"
