"""A newer catalog must fail before flow discovery or execution."""

from pathlib import Path

import pytest

from mcp_skills.errors import InvalidFlowFormatError
from mcp_skills.flow_parser import list_available_flows, load_flow_definition


def test_flow_discovery_and_load_reject_newer_catalog(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    flows = catalog / "flows"
    flows.mkdir(parents=True)
    (catalog / "catalog.yaml").write_text("min_engine_version: 99.0.0\n", encoding="utf-8")
    (flows / "sample-flow.md").write_text(
        "# Sample\n\n## Start [id: start]\n\nDo work.\n", encoding="utf-8"
    )

    with pytest.raises(InvalidFlowFormatError, match="min_engine_version '99.0.0'"):
        list_available_flows(project_root=tmp_path, skills_dir=catalog)
    with pytest.raises(InvalidFlowFormatError, match="min_engine_version '99.0.0'"):
        load_flow_definition("sample-flow", project_root=tmp_path, skills_dir=catalog)

    (catalog / "catalog.yaml").write_text("min_engine_version: 0.5.0\n", encoding="utf-8")
    assert "sample-flow" in [
        flow["name"] for flow in list_available_flows(tmp_path, skills_dir=catalog)
    ]
