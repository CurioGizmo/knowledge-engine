import json
import pytest
from pathlib import Path
from mcp_skills.flow_engine import FlowEngine, validate_review
import mcp_skills.flow_engine as flow_engine_module
from mcp_skills.flow_storage import FlowStorage
from mcp_skills.models import FlowStage, FlowStageMeta, FlowSession
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


def test_independent_review_validator_enforcement(engine: FlowEngine) -> None:
    started = engine.start_flow(
        "execute-plan-flow",
        flow_options={"pause_policy": "none"},
        target="prompt",
    )
    sid = started["session_id"]

    # 1. inspect_plan -> schedule_wave -> execute_wave -> verify_wave -> independent_review
    engine.advance_flow(sid, expected_stage_id="inspect_plan", target="prompt")
    engine.advance_flow(sid, expected_stage_id="schedule_wave", target="prompt")
    engine.advance_flow(sid, expected_stage_id="execute_wave", verdict="approved", target="prompt")

    wave_res = engine.advance_flow(
        sid,
        expected_stage_id="verify_wave",
        verdict="approved",
        transition="all_done_none",
        target="prompt",
    )
    assert wave_res["stage"]["id"] == "independent_review"

    # 2. Попытка срезать углы (advance без deliverable) -> BLOCKED
    cheat_res = engine.advance_flow(
        sid,
        expected_stage_id="independent_review",
        verdict="approved",
        notes="Я всё сделал сам",
        target="prompt",
    )
    assert cheat_res["status"] == "gate_blocked"
    assert any("deliverable" in b for b in cheat_res["blockers"])

    # 3. Попытка передать пустые сессии субагентов -> BLOCKED
    cheat_res2 = engine.advance_flow(
        sid,
        expected_stage_id="independent_review",
        verdict="approved",
        deliverable={"subagent_sessions": [], "review_id": "rev-fake"},
        target="prompt",
    )
    assert cheat_res2["status"] == "gate_blocked"
    assert any("subagent_sessions" in b for b in cheat_res2["blockers"])

    # 4. Раунд 1: вердикт needs_work с замечаниями -> остаёмся на independent_review, iteration 2
    round1_res = engine.advance_flow(
        sid,
        expected_stage_id="independent_review",
        verdict="needs_work",
        deliverable={
            "subagent_sessions": ["conv-subagent-rev-1"],
            "review_id": "rev-test-round1",
            "round": 1,
            "verdict": "needs_work",
        },
        notes="Найдены замечания, уходим на фикс",
        target="prompt",
    )
    assert round1_res["status"] == "active"
    assert round1_res["stage_id"] == "independent_review"
    assert round1_res["iteration_count"] == 2

    # 5. Раунд 2: дельта-ревью дало approved -> переход на finalize_history
    round2_res = engine.advance_flow(
        sid,
        expected_stage_id="independent_review",
        verdict="approved",
        deliverable={
            "subagent_sessions": ["conv-subagent-rev-1"],
            "review_id": "rev-test-round2",
            "round": 2,
            "verdict": "approved",
        },
        notes="Все замечания устранены",
        target="prompt",
    )
    assert round2_res["status"] == "active"
    assert round2_res["stage"]["id"] == "finalize_history"


def test_flow_stage_meta_validator() -> None:
    m1 = FlowStageMeta(validator="custom_validator")
    assert m1.validator == "custom_validator"
    dump = m1.model_dump()
    assert dump.get("validator") == "custom_validator"
    assert "gate" not in dump


def test_validate_review_gate_unit(tmp_path: Path) -> None:
    session = FlowSession(
        id="test-session",
        project_root=str(tmp_path),
        flow_name="test-flow",
        current_stage_id="review_stage",
        created_at=100.0,
        updated_at=100.0,
    )
    stage_with_dispatch = FlowStage(
        id="review_stage",
        title="Review",
        instruction="Do review",
        meta=FlowStageMeta(validator="validate_review", dispatch_agents=True),
    )
    stage_no_dispatch = FlowStage(
        id="review_stage_nodispatch",
        title="Review Solo",
        instruction="Do review solo",
        meta=FlowStageMeta(validator="validate_review", dispatch_agents=False),
    )

    # Missing deliverable
    res_none = validate_review(
        session, stage_with_dispatch, deliverable=None, project_root=tmp_path
    )
    assert not res_none.passed
    assert any("deliverable" in b for b in res_none.blockers)

    # Missing review_id
    res_no_rid = validate_review(
        session,
        stage_with_dispatch,
        deliverable={"subagent_sessions": ["agent-1"], "review_id": ""},
        project_root=tmp_path,
    )
    assert not res_no_rid.passed
    assert any("review_id" in b for b in res_no_rid.blockers)

    # dispatch_agents=True with [None] or empty string items -> blocked
    res_none_agents = validate_review(
        session,
        stage_with_dispatch,
        deliverable={"subagent_sessions": [None, "   "], "review_id": "rev-123"},
        project_root=tmp_path,
    )
    assert not res_none_agents.passed
    assert any("требует запуска независимых субагентов" in b for b in res_none_agents.blockers)

    # Valid JSON string deliverable with dispatch
    json_deliv = json.dumps({"subagent_sessions": ["agent-1"], "review_id": "rev-123"})
    res_json = validate_review(
        session, stage_with_dispatch, deliverable=json_deliv, project_root=tmp_path
    )
    assert res_json.passed
    assert not res_json.blockers

    # dispatch_agents=False does not require subagent_sessions
    res_nodispatch = validate_review(
        session,
        stage_no_dispatch,
        deliverable={"review_id": "rev-456"},
        project_root=tmp_path,
    )
    assert res_nodispatch.passed
    assert not res_nodispatch.blockers
