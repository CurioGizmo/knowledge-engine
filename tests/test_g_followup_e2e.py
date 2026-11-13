"""E2E acceptance scenarios for followup plan G (docs/plans/2026-10-18-search-and-data-integrity-followup.md).

All scenarios run against REAL tools/call (in-process ``call_tool`` on ``create_server``),
on temporary projects (tmp_path with a pyproject.toml root marker):

1. record -> draft -> trusted promote (knowledge-engine, plan B).
2. hypothesis -> confirm (auto_inferred stays in draft quarantine, plan B).
3. plan lifecycle: record_plan -> update_plan (rename) -> step_plan -> completed -> search finds
   one stable file (audit-engine, plan C).
4. known-debt + all-search: record_debt -> check_known_debt; search_artifacts(artifact_type='all')
   returns plan and debt in one merged result set (plan D RRF).
5. retry with target switch: flow start (target=antigravity) -> advance -> retry(target=opencode):
   next_call.target=opencode, stage unchanged (plan A).
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp.types import CallToolResult

from mcp_audit.server import create_server as create_audit_server
from mcp_knowledge.server import create_server as create_knowledge_server
from mcp_skills.server import create_server as create_skills_server


@pytest.fixture
def test_project_root(tmp_path: Path) -> Path:
    """Isolated project with a root marker (pyproject.toml)."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'e2e-g'\n", encoding="utf-8")
    return tmp_path


def _wire_data(res: CallToolResult) -> dict[str, Any]:
    """Extracts structured (or JSON text) payload from a real CallToolResult."""
    assert isinstance(res, CallToolResult)
    if res.structured_content:
        return res.structured_content
    return json.loads(res.content[0].text)


# ---------------------------------------------------------------------------
# Scenario 1: record -> draft -> trusted promote
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_draft_promote_e2e(test_project_root: Path) -> None:
    """auto_inferred record lands in draft (hidden from general search); promote with
    user_confirmed/human makes it active and searchable."""
    server = create_knowledge_server(project_root=test_project_root)

    # 1. record auto_inferred card
    record_res = await server.call_tool(
        "record_knowledge",
        {
            "title": "E2E draft quarantine card",
            "category": "backend",
            "type": "lesson",
            "symptom": "E2E scenario: auto-inferred card must not appear in general search",
            "root_cause": "Draft quarantine policy isolates agent-authored cards until approval",
            "solution": "Promote via update_knowledge_status only after explicit user consent",
            "rationale": "Keeps unreviewed agent content out of the live knowledge base",
            "slug": "e2e-draft-quarantine",
            "task_id": "g-e2e-1",
            "source": "auto_inferred",
        },
    )
    data = _wire_data(record_res)
    assert data["card_status"] == "draft", data
    card_id = data["id"]

    # 2. general search does NOT find the draft
    search_res = await server.call_tool(
        "search_knowledge",
        {"query": "E2E draft quarantine card"},
    )
    search_data = _wire_data(search_res)
    all_snippets = search_data.get("query_results") or search_data.get("results") or []
    assert not any(s.get("id") == card_id for s in all_snippets), search_data

    # 2b. ...but the card IS visible with explicit task_id / include_drafts
    own_search = await server.call_tool(
        "search_knowledge",
        {"query": "draft quarantine", "task_id": "g-e2e-1"},
    )
    own_data = _wire_data(own_search)
    own_snippets = own_data.get("query_results") or own_data.get("results") or []
    assert any(s.get("id") == card_id for s in own_snippets), own_data
    # found, but explicitly marked with the draft status
    hit = next(s for s in own_snippets if s.get("id") == card_id)
    assert hit.get("status") == "draft", hit

    # 3. trusted promote (by=human, source=user_confirmed, note)
    promote_res = await server.call_tool(
        "update_knowledge_status",
        {
            "id": card_id,
            "action": "promote",
            "by": "human",
            "source": "user_confirmed",
            "note": "User reviewed and approved during G e2e run",
        },
    )
    promote_data = _wire_data(promote_res)
    assert promote_data["status"] == "updated", promote_data
    assert promote_data["new_status"] == "active", promote_data

    # 4. general search now finds the active card
    search_after = await server.call_tool(
        "search_knowledge",
        {"query": "E2E draft quarantine card"},
    )
    after_data = _wire_data(search_after)
    after_snippets = after_data.get("query_results") or after_data.get("results") or []
    assert any(s.get("id") == card_id for s in after_snippets), after_data


# ---------------------------------------------------------------------------
# Scenario 2: hypothesis -> confirm (quarantine contract from plan B)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hypothesis_confirm_quarantine_e2e(test_project_root: Path) -> None:
    """propose_hypothesis -> confirm: an agent confirm (auto_inferred) must keep the card
    in draft quarantine; a human-confirmed card becomes an ACTIVE lesson (not draft)."""
    server = create_knowledge_server(project_root=test_project_root)

    prop_res = await server.call_tool(
        "propose_hypothesis",
        {
            "title": "E2E hypothesis confirm flow",
            "category": "backend",
            "symptom": "E2E scenario: confirmed hypothesis bypassing quarantine check",
            "hypothesis": "resolve(confirm) should respect the same quarantine policy as record",
            "slug": "e2e-hypo-confirm",
            "task_id": "g-e2e-2",
        },
    )
    prop_data = _wire_data(prop_res)
    hypo_id = prop_data["id"]

    confirm_res = await server.call_tool(
        "resolve_hypotheses",
        {
            "task_id": "g-e2e-2",
            "resolutions": [
                {
                    "id": hypo_id,
                    "action": "confirm",
                    "solution": "Keep confirmed hypothesis in draft until explicit human promotion",
                }
            ],
        },
    )
    confirm_data = _wire_data(confirm_res)
    assert confirm_data["status"] == "resolved", confirm_data
    detail = confirm_data["details"][0]
    # auto_inferred confirm -> draft quarantine (plan B contract)
    assert detail["card_status"] == "draft", detail

    # hidden from general search
    search_res = await server.call_tool("search_knowledge", {"query": "hypothesis confirm flow"})
    search_data = _wire_data(search_res)
    snippets = search_data.get("query_results") or search_data.get("results") or []
    assert not any(s.get("id") == hypo_id for s in snippets), search_data

    # promote through the consent path -> active lesson
    promote_res = await server.call_tool(
        "update_knowledge_status",
        {
            "id": hypo_id,
            "action": "promote",
            "by": "human",
            "source": "user_confirmed",
            "note": "User approved the confirmed hypothesis",
        },
    )
    promote_data = _wire_data(promote_res)
    assert promote_data["new_status"] == "active", promote_data

    get_res = await server.call_tool("get_knowledge", {"ids": [hypo_id]})
    get_data = _wire_data(get_res)
    card = get_data["cards"][0]
    assert card["status"] == "active"
    assert card["type"] == "lesson"


# ---------------------------------------------------------------------------
# Scenario 3: plan lifecycle (audit-engine)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_lifecycle_e2e(test_project_root: Path) -> None:
    """record_plan -> update_plan (rename) -> step_plan to in_progress -> completed -> search.
    The plan must be a single file with a stable artifact_id after the rename."""
    server = create_audit_server(project_root=test_project_root)

    create_res = await server.call_tool(
        "record_plan",
        {
            "task_id": "G-E2E-PLAN",
            "title": "Original Plan Title",
            "steps": ["First step of the plan", "Second step of the plan"],
            "category": "backend",
        },
    )
    create_data = _wire_data(create_res)
    assert create_data["status"] == "created", create_data
    assert create_data["plan_status"] == "discussed", create_data
    artifact_id = create_data["id"]
    original_path = Path(create_data["file_path"])
    assert original_path.exists()

    # rename via update_plan
    rename_res = await server.call_tool(
        "update_plan",
        {"task_id": "G-E2E-PLAN", "title": "Renamed Plan Title"},
    )
    rename_data = _wire_data(rename_res)
    assert rename_data["status"] == "updated", rename_data
    assert rename_data["id"] == artifact_id, rename_data
    renamed_path = Path(rename_data["file_path"])

    # single plan file, stable artifact_id after the rename
    plans_root = test_project_root / ".agents" / "plans"
    plan_files = list(plans_root.rglob("*.md")) if plans_root.exists() else []
    assert len(plan_files) == 1, plan_files
    fm = yaml.safe_load(plan_files[0].read_text(encoding="utf-8").split("---")[1])
    assert fm.get("id") == artifact_id, fm
    # the rename landed in the file (frontmatter title or first heading)
    plan_text = plan_files[0].read_text(encoding="utf-8")
    assert "Renamed Plan Title" in plan_text, plan_text
    if original_path != renamed_path:
        assert not original_path.exists()

    # step 1 -> in_progress
    step1_res = await server.call_tool(
        "step_plan",
        {"task_id": "G-E2E-PLAN", "step": 1, "note": "First step done"},
    )
    step1_data = _wire_data(step1_res)
    assert step1_data["new_plan_status"] == "in_progress", step1_data
    assert step1_data["progress"] == "1/2", step1_data

    # step 2 -> completed (plan moves to completed/)
    step2_res = await server.call_tool(
        "step_plan",
        {"task_id": "G-E2E-PLAN", "step": 2, "note": "Second step done"},
    )
    step2_data = _wire_data(step2_res)
    assert step2_data["new_plan_status"] == "done", step2_data
    completed_path = Path(step2_data["file_path"])
    assert "completed" in str(completed_path), step2_data
    assert completed_path.exists()
    # still exactly one plan file on disk
    plan_files_after = list(plans_root.rglob("*.md"))
    assert len(plan_files_after) == 1, plan_files_after

    # reindex + search finds the plan with the CURRENT title and status
    search_res = await server.call_tool(
        "search_artifacts",
        {"query": "Renamed Plan Title", "artifact_type": "plan"},
    )
    search_data = _wire_data(search_res)
    assert search_data["count"] >= 1, search_data
    hits = [r for r in search_data["results"] if r.get("artifact_type") == "plan"]
    assert hits, search_data
    assert any(r.get("title") == "Renamed Plan Title" for r in hits), search_data
    assert any(r.get("status") == "done" for r in hits), search_data

    # get_ready_backlog is consistent (no crash, no phantom tasks)
    backlog_res = await server.call_tool("get_ready_backlog", {})
    backlog_data = _wire_data(backlog_res)
    assert backlog_data["count"] == 0, backlog_data


# ---------------------------------------------------------------------------
# Scenario 4: known-debt and all-search (plan D)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_known_debt_and_all_search_e2e(test_project_root: Path) -> None:
    """record_debt -> check_known_debt finds it; search_artifacts(artifact_type='all')
    returns both the plan and the debt in one merged result set (RRF)."""
    server = create_audit_server(project_root=test_project_root)

    # plan (same as scenario 3 but simplified)
    await server.call_tool(
        "record_plan",
        {
            "task_id": "G-E2E-ALL",
            "title": "Shared search plan artifact",
            "steps": ["Only one step here"],
            "category": "backend",
        },
    )

    debt_res = await server.call_tool(
        "record_debt",
        {
            "title": "Shared search debt artifact",
            "category": "backend",
            "symptom": "Rapid refresh causes high DB load on the shared search path",
            "task_id": "G-E2E-ALL",
        },
    )
    debt_data = _wire_data(debt_res)
    assert debt_data["status"] == "created", debt_data

    # check_known_debt finds the recorded symptom
    known_res = await server.call_tool(
        "check_known_debt",
        {"symptom": "Rapid refresh causes high DB load", "category": "backend"},
    )
    known_data = _wire_data(known_res)
    assert known_data.get("found") is True, known_data
    assert known_data.get("title") == "Shared search debt artifact", known_data

    # all-search: plan AND debt in one result set
    search_res = await server.call_tool(
        "search_artifacts",
        {"query": "shared search", "artifact_type": "all"},
    )
    search_data = _wire_data(search_res)
    types = {r.get("artifact_type") for r in search_data["results"]}
    assert "plan" in types, search_data
    assert "debt" in types, search_data


# ---------------------------------------------------------------------------
# Scenario 5: retry with target switch (plan A)
# ---------------------------------------------------------------------------


@pytest.fixture
def skills_repo(tmp_path: Path) -> Iterator[Path]:
    """Temporary skills repo (MCP_SKILLS_PATH) so no user cache is touched."""
    repo = tmp_path / "skills-repo"
    src = Path(__file__).resolve().parents[1] / "src" / "mcp_skills" / "templates"
    (repo / "skills").mkdir(parents=True)
    (repo / "agents").mkdir(parents=True)
    shutil.copytree(src / "flows", repo / "flows")
    agents_src = src / "agents"
    if agents_src.exists():
        shutil.copytree(agents_src, repo / "agents", dirs_exist_ok=True)
    old = os.environ.get("MCP_SKILLS_PATH")
    os.environ["MCP_SKILLS_PATH"] = str(repo)
    try:
        yield repo
    finally:
        if old is None:
            os.environ.pop("MCP_SKILLS_PATH", None)
        else:
            os.environ["MCP_SKILLS_PATH"] = old


@pytest.mark.asyncio
async def test_retry_target_switch_e2e(
    test_project_root: Path, skills_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """flow start (target=antigravity) -> advance -> retry(target=opencode):
    next_call.target switches to opencode while the stage stays unchanged."""
    # isolate the flows.db cache to the temp project as well
    monkeypatch.setenv("MCP_PROJECT_ROOT", str(test_project_root))
    server = create_skills_server(project_root=test_project_root)

    start_res = await server.call_tool(
        "step_flow",
        {
            "action": "advance",
            "flow": "feature-delivery",
            "target": "antigravity",
            "project_path": str(test_project_root),
        },
    )
    start_data = _wire_data(start_res)
    assert start_data["status"] == "active", start_data
    session_id = start_data["session_id"]
    stage_id = start_data["stage"]["id"]
    assert start_data["next_call"]["arguments"]["target"] == "antigravity", start_data

    # one advance to move past the first stage
    adv_res = await server.call_tool(
        "step_flow",
        {
            "action": "advance",
            "session_id": session_id,
            "expected_stage_id": stage_id,
            "human_confirmed": True,
            "project_path": str(test_project_root),
        },
    )
    adv_data = _wire_data(adv_res)
    assert adv_data["status"] == "active", adv_data
    stage_before_retry = adv_data["stage"]["id"]

    # retry with explicit target switch
    retry_res = await server.call_tool(
        "step_flow",
        {
            "action": "retry",
            "session_id": session_id,
            "target": "opencode",
            "project_path": str(test_project_root),
        },
    )
    retry_data = _wire_data(retry_res)
    # contract A: stage must NOT change on retry
    assert retry_data["stage"]["id"] == stage_before_retry, retry_data
    # next_call carries the effective target (opencode)
    assert retry_data["next_call"]["arguments"]["target"] == "opencode", retry_data

    # the switched target persists on a subsequent status call
    status_res = await server.call_tool(
        "step_flow",
        {
            "action": "status",
            "session_id": session_id,
            "project_path": str(test_project_root),
        },
    )
    status_data = _wire_data(status_res)
    assert status_data["next_call"]["arguments"]["target"] == "opencode", status_data
