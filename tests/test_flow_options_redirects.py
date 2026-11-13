"""Canonical redirects and immutable flow option resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

import mcp_skills.flow_engine as module
from mcp_skills.errors import InvalidFlowFormatError
from mcp_skills.flow_storage import FlowStorage
from mcp_skills.models import (
    FlowDefinition,
    FlowDispatchPolicy,
    FlowOptionRule,
    FlowOptionSpec,
    FlowStage,
    FlowStageMeta,
)


def _definition(name: str, **kwargs: object) -> FlowDefinition:
    return FlowDefinition(
        name=name,
        stages={"start": FlowStage(id="start", title="Start", instruction="Do it")},
        stage_order=["start"],
        **kwargs,
    )


@pytest.fixture
def definitions(monkeypatch: pytest.MonkeyPatch) -> dict[str, FlowDefinition]:
    items: dict[str, FlowDefinition] = {}
    monkeypatch.setattr(module, "load_flow_definition", lambda name, *_args, **_kw: items[name])
    monkeypatch.setattr(module, "get_current_branch", lambda _root: "test-branch")
    return items


def test_redirect_options_precedence_and_immutability(
    tmp_path: Path, definitions: dict[str, FlowDefinition]
) -> None:
    schema = {
        "autonomy": FlowOptionSpec(allowed=["auto", "semi"], default="auto"),
        "pause_policy": FlowOptionSpec(
            allowed=["none", "ready_for_review", "wave", "task"], default="none"
        ),
    }
    definitions["canonical"] = _definition(
        "canonical",
        option_schema=schema,
        option_rules=[
            FlowOptionRule(when={"autonomy": "semi"}, defaults={"pause_policy": "ready_for_review"})
        ],
    )
    definitions["legacy"] = _definition(
        "legacy",
        deprecated=True,
        superseded_by="canonical",
        defaults={"autonomy": "semi", "pause_policy": "task"},
    )
    store = FlowStorage(db_path=tmp_path / "flows.db")
    started = module.step_flow(flow="legacy", project_root=tmp_path, storage=store)
    session = store.get_session(started["session_id"])
    assert session is not None
    assert session.flow_name == "canonical"
    assert session.context_data["requested_flow"] == "legacy"
    assert session.context_data["flow_options"] == {"autonomy": "semi", "pause_policy": "task"}
    with pytest.raises(ValueError, match="flow_options"):
        module.step_flow(
            action="advance",
            session_id=session.id,
            project_root=tmp_path,
            storage=store,
            flow_options={"autonomy": "auto"},
        )
    assert (
        store.get_session(session.id).context_data["flow_options"]
        == session.context_data["flow_options"]
    )


def test_rules_apply_only_to_schema_defaults_and_explicit_wins(
    tmp_path: Path, definitions: dict[str, FlowDefinition]
) -> None:
    definitions["canonical"] = _definition(
        "canonical",
        option_schema={
            "autonomy": FlowOptionSpec(allowed=["auto", "semi"], default="auto"),
            "pause_policy": FlowOptionSpec(
                allowed=["none", "ready_for_review", "wave"], default="none"
            ),
        },
        option_rules=[
            FlowOptionRule(when={"autonomy": "semi"}, defaults={"pause_policy": "ready_for_review"})
        ],
    )
    engine = module.FlowEngine(
        project_root=tmp_path, storage=FlowStorage(db_path=tmp_path / "f.db")
    )
    first = engine.start_flow("canonical", flow_options={"autonomy": "semi"})
    assert first["flow_options"]["pause_policy"] == "ready_for_review"
    second = engine.start_flow(
        "canonical", flow_options={"autonomy": "semi", "pause_policy": "wave"}
    )
    assert second["flow_options"]["pause_policy"] == "wave"
    with pytest.raises(ValueError, match="Неизвестный"):
        engine.start_flow("canonical", flow_options={"unknown": "value"})


def test_redirect_cycle_is_rejected(tmp_path: Path, definitions: dict[str, FlowDefinition]) -> None:
    definitions["one"] = _definition("one", deprecated=True, superseded_by="two")
    definitions["two"] = _definition("two", deprecated=True, superseded_by="one")
    with pytest.raises(InvalidFlowFormatError, match="Цикл"):
        module.FlowEngine(project_root=tmp_path).start_flow("one")


def test_alias_equivalence_resumes_existing_legacy_session(
    tmp_path: Path, definitions: dict[str, FlowDefinition]
) -> None:
    definitions["legacy"] = FlowDefinition(
        name="legacy",
        stages={
            "old_start": FlowStage(id="old_start", title="Old", instruction="Old instructions"),
            "old_end": FlowStage(id="old_end", title="End", instruction="Finish"),
        },
        stage_order=["old_start", "old_end"],
    )
    definitions["canonical"] = _definition("canonical")
    store = FlowStorage(db_path=tmp_path / "flows.db")
    first = module.step_flow(flow="legacy", project_root=tmp_path, storage=store)
    assert first["stage"]["id"] == "old_start"
    definitions["legacy"].deprecated = True
    definitions["legacy"].superseded_by = "canonical"

    resumed = module.step_flow(flow="canonical", project_root=tmp_path, storage=store)
    assert resumed["session_id"] == first["session_id"]
    assert resumed["flow_name"] == "legacy"
    assert resumed["stage"]["id"] == "old_end"


def test_nearest_wrapper_wins_and_flow_dispatch_policy_is_exposed(
    tmp_path: Path, definitions: dict[str, FlowDefinition]
) -> None:
    policy = FlowDispatchPolicy(
        min_agents=1,
        max_agents=4,
        parallel="when_safe",
        selection="risk_and_touched_domains",
    )
    definitions["canonical"] = FlowDefinition(
        name="canonical",
        option_schema={"mode": FlowOptionSpec(allowed=["base", "near", "outer"], default="base")},
        dispatch_policy=policy,
        stages={
            "start": FlowStage(
                id="start",
                title="Start",
                instruction="Dispatch",
                meta=FlowStageMeta(dispatch_agents=True),
            )
        },
        stage_order=["start"],
    )
    definitions["near"] = _definition(
        "near", deprecated=True, superseded_by="canonical", defaults={"mode": "near"}
    )
    definitions["outer"] = _definition(
        "outer", deprecated=True, superseded_by="near", defaults={"mode": "outer"}
    )
    result = module.FlowEngine(
        project_root=tmp_path, storage=FlowStorage(db_path=tmp_path / "flows.db")
    ).start_flow("outer")
    assert result["flow_options"] == {"mode": "outer"}
    assert result["dispatch_policy"] == policy.model_dump()
