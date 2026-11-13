"""Regression coverage for delivered content, using API boundaries and complete payloads."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from mcp_skills.bundler import AgentBundler
from mcp_skills.errors import AssemblyBudgetExceededError
from mcp_skills.flow_engine import FlowEngine
from mcp_skills.models import AgentPermissions, AgentProfile
from mcp_skills.resources import is_resource_associated_with_skill, read_resource_chunk
from mcp_skills.server import create_server, load_skill, search_skills
from mcp_skills.slot_compiler import SlotCompiler
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer


def write_skill(catalog: Path, name: str, body: str, front: str = "") -> Path:
    base = catalog / "skills" / name
    base.mkdir(parents=True, exist_ok=True)
    (base / "SKILL.md").write_text(f"---\nname: {name}\ntitle: {name}\n{front}---\n{body}\n")
    return base


def index_catalog(
    catalog: Path, project: Path
) -> tuple[SkillsStorage, SlotCompiler, SkillsSynchronizer]:
    (catalog / "blocks").mkdir(exist_ok=True)
    store = SkillsStorage(project / "skills.db")
    sync = SkillsSynchronizer(store, [catalog / "skills", project / ".agents" / "skills"])
    sync.sync()
    return store, SlotCompiler(store, catalog_root=catalog), sync


@pytest.mark.parametrize("query", ["sqlite performance", "sqlite-performance"])
def test_unknown_exact_skill_beats_many_generic_matches(tmp_path: Path, query: str) -> None:
    catalog = tmp_path / "catalog"
    write_skill(
        catalog,
        "sqlite-performance",
        "# SQLite performance\nUse transactions.",
        "requires_stack: [sqlite]\n",
    )
    for i in range(25):
        write_skill(
            catalog,
            f"generic-{i:02d}",
            "# Generic workflow\nConsider sqlite performance when relevant.",
        )
    store, _, sync = index_catalog(catalog, tmp_path)
    response = search_skills(
        query=query,
        type="skill",
        project_stack=["python"],
        project_root=tmp_path,
        storage=store,
        synchronizer=sync,
    )
    assert response["results"][0]["name"] == "sqlite-performance"
    assert response["results"][0]["applicability"] == "unknown"


@pytest.mark.asyncio
async def test_actual_catalog_default_mcp_search_finds_new_stack(tmp_path: Path) -> None:
    catalog = Path(__file__).resolve().parents[2] / "mcp_skills"
    if not (catalog / "skills").is_dir():
        pytest.skip("Companion catalog is not installed")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="python-only"\ndependencies=["pydantic"]\n'
    )
    server = create_server(project_root=tmp_path, skills_dir=catalog, db_path=tmp_path / "index.db")
    blocks = await server.call_tool(
        "search_skills", {"query": "sqlite performance", "type": "skill"}
    )
    # FastMCP may include a structured counterpart as the second tuple item.
    content = (
        blocks.content
        if hasattr(blocks, "content")
        else (blocks[0] if isinstance(blocks, tuple) else blocks)
    )
    response = json.loads(content[0].text)
    names = [item["name"] for item in response["results"]]
    assert names.index("sqlite-performance") < 3


def test_offline_resource_is_complete_and_explicit_empty_is_respected(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    base = write_skill(catalog, "sample", "# Core\n[Details](references/details.md)")
    (base / "references").mkdir()
    reference = "# Reference\n" + "resource detail\n" * 850 + "END_OF_REQUIRED_REFERENCE"
    (base / "references" / "details.md").write_text(reference)
    store, compiler, _ = index_catalog(catalog, tmp_path)
    bundler = AgentBundler(storage=store, compiler=compiler, skills_dir=catalog)
    profile = AgentProfile(
        name="reader", role="Reader", skills=["sample"], permissions=AgentPermissions(mcp=False)
    )
    bundle = bundler.bundle_agent(profile=profile, project_root=tmp_path)
    assert reference in json.loads(bundle)["system_prompt"]
    assert bundle.count("END_OF_REQUIRED_REFERENCE") == 1
    no_resources = bundler.bundle_agent(
        profile=profile, project_root=tmp_path, skill_options={"sample": {"resources": []}}
    )
    assert "END_OF_REQUIRED_REFERENCE" not in no_resources
    (base / "references" / "details.md").write_text(
        "Required resource line\n" * 5000 + "FINAL_MANDATORY_RESOURCE"
    )
    with pytest.raises(AssemblyBudgetExceededError):
        bundler.bundle_agent(profile=profile, project_root=tmp_path)


@pytest.mark.asyncio
async def test_local_skill_resources_round_trip_through_mcp(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    write_skill(catalog, "central", "# Central")
    local = tmp_path / ".agents" / "skills" / "local-note"
    (local / "references").mkdir(parents=True)
    (local / "SKILL.md").write_text(
        "---\nname: local-note\ntitle: Local Note\n---\n# Local\n[Detail](references/detail.md)"
    )
    (local / "references" / "detail.md").write_text("LOCAL_REFERENCE_DETAIL")
    server = create_server(project_root=tmp_path, skills_dir=catalog, db_path=tmp_path / "index.db")

    async def call(arguments: dict[str, Any]) -> dict[str, Any]:
        blocks = await server.call_tool("load_skill", arguments)
        content = (
            blocks.content
            if hasattr(blocks, "content")
            else (blocks[0] if isinstance(blocks, tuple) else blocks)
        )
        return json.loads(content[0].text)

    loaded = await call({"name": "local-note"})
    rid = loaded["resources"][0]["resource_id"]
    assert rid == "project-skills/local-note/references/detail.md"
    resource = await call({"name": "local-note", "resource_id": rid})
    assert resource["content"] == "LOCAL_REFERENCE_DETAIL"
    denied = await call({"name": "central", "resource_id": rid})
    assert denied.get("reason") == "PERMISSION_DENIED"


@pytest.mark.parametrize("strategy", ["append", "prepend"])
def test_inherited_links_normalized_once_at_each_origin(tmp_path: Path, strategy: str) -> None:
    catalog = tmp_path / "catalog"
    parent = write_skill(catalog, "parent", "# Parent\n[Parent](references/parent.md)")
    (parent / "references").mkdir()
    (parent / "references" / "parent.md").write_text("PARENT_REFERENCE")
    write_skill(catalog, "child", "# Child", f"extends: parent\nmerge_strategy: {strategy}\n")
    store, compiler, sync = index_catalog(catalog, tmp_path)
    loaded = load_skill(
        "child",
        stack=[],
        compiler=compiler,
        storage=store,
        synchronizer=sync,
        project_root=tmp_path,
    )
    assert "[Parent](skills/parent/references/parent.md)" in loaded["content"]
    assert len(loaded["resources"]) == 1
    rid = loaded["resources"][0]["resource_id"]
    assert (
        load_skill(
            "child",
            resource_id=rid,
            compiler=compiler,
            storage=store,
            synchronizer=sync,
            project_root=tmp_path,
        )["content"]
        == "PARENT_REFERENCE"
    )


def test_override_discards_parent_skeleton_slots_and_resources(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    parent = write_skill(
        catalog,
        "parent",
        "# Parent\n<!-- SLOT: rules -->\n[Parent](references/parent.md)",
        "available_slots: [rules]\n",
    )
    (parent / "references").mkdir()
    (parent / "references" / "parent.md").write_text("PARENT_REFERENCE")
    (parent / "blocks" / "rules").mkdir(parents=True)
    (parent / "blocks" / "rules" / "default.md").write_text("PARENT_RULE")
    write_skill(catalog, "child", "# Child only", "extends: parent\nmerge_strategy: override\n")
    _, compiler, _ = index_catalog(catalog, tmp_path)
    _, content, resources = compiler.assemble_skill_with_resources("child", project_root=tmp_path)
    assert content == "# Child only"
    assert resources == []
    assert compiler.collect_available_slots("child") == []


@pytest.mark.parametrize("origin", ["catalog", "local", "override"])
def test_flow_relative_resource_uses_configured_catalog_and_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    catalog = tmp_path / "catalog"
    write_skill(catalog, "owner", "# Owner\n[Guide](../../templates/guide.md)")
    (catalog / "templates").mkdir()
    (catalog / "templates" / "guide.md").write_text("COMPLETE_GUIDE")
    (catalog / "flows").mkdir()
    (catalog / "flows" / "resource-flow.md").write_text(
        "# Flow\n## 1. Research [id: s1]\n[Guide](../templates/guide.md)"
    )
    if origin != "catalog":
        local_flows = tmp_path / ".agents" / "flows"
        local_flows.mkdir(parents=True)
        suffix = ".override.md" if origin == "override" else ".md"
        (local_flows / ("resource-flow" + suffix)).write_text(
            "# Local Flow\n## 1. Research [id: s1]\n[Guide](../../catalog/templates/guide.md)"
        )
    store, _, _ = index_catalog(catalog, tmp_path)
    monkeypatch.setenv("MCP_SKILLS_PATH", str(tmp_path / "wrong-catalog"))
    engine = FlowEngine(project_root=tmp_path, skills_storage=store, skills_dir=catalog)
    response = engine.start_flow("resource-flow")
    assert response["stage"]["resources"] == [
        {
            "resource_id": "templates/guide.md",
            "owner_skill": "owner",
            "title": "Guide",
            "type": "template",
        }
    ]


def test_overflow_does_not_advance_session_or_history(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    write_skill(catalog, "small", "# Small")
    store, _, _ = index_catalog(catalog, tmp_path)
    (catalog / "flows").mkdir()
    mandatory = "- Mandatory core contract\n" * 3000 + "TAIL_MANDATORY_RULE"
    (catalog / "flows" / "budget-flow.md").write_text(
        f"# Flow\n## 1. Small [id: s1]\nInspect.\n## 2. Large [id: s2]\n{mandatory}"
    )
    engine = FlowEngine(project_root=tmp_path, skills_storage=store, skills_dir=catalog)
    started = engine.start_flow("budget-flow")
    flow_store = engine._get_storage(tmp_path)
    before = flow_store.get_session(started["session_id"])
    assert before is not None
    with pytest.raises(AssemblyBudgetExceededError):
        engine.advance_flow(session_id=started["session_id"])
    after = flow_store.get_session(started["session_id"])
    assert after is not None and after.current_stage_id == "s1" and after.version == before.version
    with flow_store.get_connection() as connection:
        assert connection.execute("SELECT count(*) FROM flow_history").fetchone()[0] == 1


def test_cursor_bound_to_resource_anchor_revision_and_nonnegative_offset(tmp_path: Path) -> None:
    body = "## Alpha\n" + "A" * 9000 + "\n## Beta\n" + "B" * 9000
    (tmp_path / "guide.md").write_text(body)
    (tmp_path / "other.md").write_text(body)
    chunk = read_resource_chunk("guide.md#alpha", tmp_path)
    cursor = chunk["next_cursor"]
    assert cursor
    assert read_resource_chunk("guide.md#alpha", tmp_path, cursor=cursor)["offset"] == 8000
    for target in ["guide.md#beta", "other.md#alpha"]:
        with pytest.raises(ValueError, match="другому ресурсу или якорю"):
            read_resource_chunk(target, tmp_path, cursor=cursor)
    for invalid in ["-1", "8000", "-5:abc", "bad-cursor"]:
        with pytest.raises(ValueError, match="формат курсора"):
            read_resource_chunk("guide.md#alpha", tmp_path, cursor=invalid)
    data = json.loads(base64.urlsafe_b64decode(cursor))
    data["offset"] = -1
    invalid = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
    with pytest.raises(ValueError, match="формат курсора"):
        read_resource_chunk("guide.md#alpha", tmp_path, cursor=invalid)


def test_same_basename_does_not_grant_unrelated_resource(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    base = write_skill(catalog, "owner", "# Owner\n[Details](references/details.md)")
    (base / "references").mkdir()
    (base / "references" / "details.md").write_text("Owned")
    other = write_skill(catalog, "other", "# Other")
    (other / "details.md").write_text("Unrelated")
    store, _, _ = index_catalog(catalog, tmp_path)
    assert not is_resource_associated_with_skill(
        "owner", "skills/other/details.md", catalog, base, storage=store
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["skill", "stage", "subagent", "instruction"])
async def test_mcp_overflow_is_explicit_and_does_not_create_a_session(
    tmp_path: Path, delivery: str
) -> None:
    catalog = tmp_path / "catalog"
    mandatory = "- Mandatory core contract\n" * 3000 + "TAIL_MANDATORY_RULE"
    write_skill(catalog, "huge", "# Huge\n" + mandatory)
    agent = catalog / "agents" / "reviewer"
    agent.mkdir(parents=True)
    (agent / "AGENT.yaml").write_text(
        "name: reviewer\nrole: Reviewer\nskills: [huge]\npermissions:\n  mcp: true\n"
    )
    (catalog / "flows").mkdir()
    meta = (
        '{"skills": ["huge"]}'
        if delivery == "stage"
        else '{"agent": "reviewer"}'
        if delivery == "subagent"
        else "{}"
    )
    body = mandatory if delivery == "instruction" else "Inspect the target."
    (catalog / "flows" / "huge-flow.md").write_text(
        f"# Flow\n## 1. Inspect [id: s1]\n<!-- meta: {meta} -->\n{body}"
    )
    (tmp_path / "AGENTS.md").write_text("Isolated project.")
    server = create_server(project_root=tmp_path, skills_dir=catalog, db_path=tmp_path / "index.db")
    name, arguments = (
        ("load_skill", {"name": "huge"})
        if delivery == "skill"
        else ("step_flow", {"flow": "huge-flow"})
    )
    result = await server.call_tool(name, arguments)
    response = json.loads(result.content[0].text)
    assert response["error"] and response["reason"] == "ASSEMBLY_BUDGET_EXCEEDED"
    assert response["estimated_tokens"] > response["budget"] and response["remediation"]
    from mcp_skills.flow_storage import FlowStorage

    assert FlowStorage(project_root=tmp_path).find_active_sessions(str(tmp_path)) == []
