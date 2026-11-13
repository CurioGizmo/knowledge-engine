"""Тест соответствия зарегистрированных имён MCP-инструментов каноническим схемам и спискам."""

from pathlib import Path
import pytest

from mcp_audit.installer import AUDIT_TOOLS_LIST, get_audit_tool_schemas
from mcp_audit.server import create_server as create_audit_server
from mcp_knowledge.installer import KNOWLEDGE_TOOLS_LIST
from mcp_knowledge.server import create_server as create_knowledge_server
from mcp_skills.installer import SKILLS_TOOLS_LIST, get_skills_tool_schemas
from mcp_skills.server import create_server as create_skills_server


@pytest.mark.asyncio
async def test_tool_names_match_schemas(tmp_path: Path) -> None:
    """Проверяет, что list_tools() каждого сервера возвращает в точности канонические имена без суффикса _tool."""
    # 1. Knowledge Engine (8 инструментов)
    k_server = create_knowledge_server(project_root=tmp_path)
    k_tools = await k_server.list_tools()
    k_names = {t.name for t in k_tools}
    assert k_names == set(KNOWLEDGE_TOOLS_LIST)
    assert len(k_names) == 8

    # 2. Audit Engine (12 tools)
    a_server = create_audit_server(project_root=tmp_path)
    a_tools = await a_server.list_tools()
    a_names = {t.name for t in a_tools}
    assert a_names == set(AUDIT_TOOLS_LIST)
    assert a_names == set(get_audit_tool_schemas().keys())
    assert len(a_names) == 12
    for name in a_names:
        assert not name.endswith("_tool")

    # 3. Skills Engine (4 инструмента)
    s_server = create_skills_server(project_root=tmp_path)
    s_tools = await s_server.list_tools()
    s_names = {t.name for t in s_tools}
    assert s_names == set(SKILLS_TOOLS_LIST)
    assert s_names == set(get_skills_tool_schemas().keys())
    assert len(s_names) == 5
    for name in s_names:
        assert not name.endswith("_tool")
