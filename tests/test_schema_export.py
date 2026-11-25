"""Тесты для утилиты экспорта схем инструментов export_server_schemas."""

from __future__ import annotations

from pathlib import Path
import pytest

from mcp_audit.server import create_server as create_audit_server
from mcp_core.schema_export import export_server_schemas
from mcp_knowledge.server import create_server as create_knowledge_server
from mcp_skills.server import create_server as create_skills_server


@pytest.mark.asyncio
async def test_export_server_schemas_skills(tmp_path: Path) -> None:
    """Проверяет экспорт схем для skills-engine: ровно 4 инструмента, ключ parameters."""
    server = create_skills_server(project_root=tmp_path)
    schemas = await export_server_schemas(server)

    assert set(schemas.keys()) == {
        "search_skills",
        "load_skill",
        "create_skill",
        "get_help",
        "step_flow",
    }
    for tool_name, schema in schemas.items():
        assert schema["name"] == tool_name
        assert "description" in schema
        assert "parameters" in schema
        assert isinstance(schema["parameters"], dict)
        assert "properties" in schema["parameters"]
    step_properties = schemas["step_flow"]["parameters"]["properties"]
    assert "flow_options" in step_properties
    assert "transition" in step_properties


@pytest.mark.asyncio
async def test_export_server_schemas_knowledge(tmp_path: Path) -> None:
    """Проверяет экспорт схем для knowledge-engine: ровно 8 инструментов."""
    server = create_knowledge_server(project_root=tmp_path)
    schemas = await export_server_schemas(server)

    assert len(schemas) == 8
    expected = {
        "search_knowledge",
        "get_knowledge",
        "record_knowledge",
        "update_knowledge_status",
        "review_knowledge",
        "get_help",
        "propose_hypothesis",
        "resolve_hypotheses",
    }
    assert set(schemas.keys()) == expected
    for schema in schemas.values():
        assert "parameters" in schema
        assert "properties" in schema["parameters"]


@pytest.mark.asyncio
async def test_export_server_schemas_audit(tmp_path: Path) -> None:
    """Проверяет экспорт схем для audit-engine: 14 инструментов (включая get_plan_versions, restore_plan_version и get_help)."""
    server = create_audit_server(project_root=tmp_path)
    schemas = await export_server_schemas(server)

    assert len(schemas) == 14
    expected = {
        "check_known_debt",
        "get_ready_backlog",
        "search_debt",
        "record_debt",
        "update_debt_status",
        "record_plan",
        "update_plan",
        "step_plan",
        "get_plan_versions",
        "restore_plan_version",
        "record_review",
        "get_task_context",
        "search_artifacts",
        "get_help",
    }
    assert set(schemas.keys()) == expected
    for schema in schemas.values():
        assert "parameters" in schema
