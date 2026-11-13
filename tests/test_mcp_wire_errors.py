"""Тесты MCP wire-level контракта ошибок (isError=true при доменных ошибках).

Проверяет требование Шага 1:
- Доменный error-envelope преобразуется в CallToolResult(is_error=True, structured_content=..., content=[...]);
- Успешные вызовы и штатные состояния (gate_blocked, no_active_session) возвращают is_error=False;
- Wire-level stdio клиент получает isError=true/false в JSON-RPC ответе.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from mcp.types import CallToolResult

from mcp_audit.server import create_server as create_audit_server
from mcp_knowledge.server import create_server as create_knowledge_server
from mcp_skills.server import create_server as create_skills_server


@pytest.mark.asyncio
async def test_skills_missing_skill_returns_is_error_true(tmp_path: Path) -> None:
    """Вызов load_skill с несуществующим скиллом должен возвращать is_error=True."""
    server = create_skills_server(project_root=tmp_path)
    res = await server.call_tool("load_skill", {"name": "definitely-missing-skill-12345"})

    assert isinstance(res, CallToolResult)
    assert res.is_error is True, f"Ожидался is_error=True, получено {res.is_error}"
    data = res.structured_content or json.loads(res.content[0].text)
    assert data.get("error") is True
    assert data.get("reason") == "SKILL_NOT_FOUND"


@pytest.mark.asyncio
async def test_skills_unanchored_multi_tenant_returns_is_error_true() -> None:
    """В multi-tenant вызов search_skills без project_path отклоняется входной схемой.

    Волна D (план followup, пункт «optional project_path»): в multi_tenant
    project_path обязателен на уровне pydantic-схемы тулза — SDK отклоняет
    вызов до доменного слоя (ToolError), что гарантирует is_error на wire
    без зависимости от доменного envelope.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    server = create_skills_server(multi_tenant=True)
    with pytest.raises(ToolError, match="project_path"):
        await server.call_tool("search_skills", {})


@pytest.mark.asyncio
async def test_knowledge_invalid_args_returns_is_error_true(tmp_path: Path) -> None:
    """Вызов record_knowledge с невалидными аргументами должен возвращать is_error=True."""
    server = create_knowledge_server(project_root=tmp_path)
    # Пустой title должен вызвать ошибку валидации
    res = await server.call_tool("record_knowledge", {"title": "ab", "category": "general"})

    assert isinstance(res, CallToolResult)
    assert res.is_error is True, f"Ожидался is_error=True, получено {res.is_error}"
    data = res.structured_content or json.loads(res.content[0].text)
    assert data.get("error") is True


@pytest.mark.asyncio
async def test_audit_unanchored_write_returns_is_error_true() -> None:
    """В multi-tenant вызов record_debt без project_path отклоняется входной схемой.

    Волна D: как и в skills, обязательность project_path в multi_tenant
    гарантируется pydantic-схемой (ToolError до доменного слоя).
    """
    from mcp.server.mcpserver.exceptions import ToolError

    server = create_audit_server(multi_tenant=True)
    with pytest.raises(ToolError, match="project_path"):
        await server.call_tool("record_debt", {"title": "Test debt", "category": "general"})


@pytest.mark.asyncio
async def test_successful_call_returns_is_error_false(tmp_path: Path) -> None:
    """Успешные вызовы тулов должны возвращать is_error=False."""
    k_server = create_knowledge_server(project_root=tmp_path)
    res_k = await k_server.call_tool("search_knowledge", {"query": ""})
    assert isinstance(res_k, CallToolResult)
    assert res_k.is_error is False

    s_server = create_skills_server(project_root=tmp_path)
    res_s = await s_server.call_tool("search_skills", {})
    assert isinstance(res_s, CallToolResult)
    assert res_s.is_error is False


@pytest.mark.asyncio
async def test_gate_blocked_returns_is_error_false(tmp_path: Path) -> None:
    """Штатное состояние gate_blocked НЕ является доменной ошибкой и возвращает is_error=False."""
    server = create_skills_server(project_root=tmp_path)
    # Начинаем флоу feature-delivery
    start_res = await server.call_tool("step_flow", {"flow": "feature-delivery"})
    start_data = start_res.structured_content or json.loads(start_res.content[0].text)
    session_id = start_data["session_id"]

    # Продвигаем без выполнения требований гейта -> gate_blocked
    res = await server.call_tool("step_flow", {"session_id": session_id, "action": "advance"})
    assert isinstance(res, CallToolResult)
    assert res.is_error is False, f"gate_blocked не должен превращаться в ошибку протокола: {res}"
    data = res.structured_content or json.loads(res.content[0].text)
    assert data.get("status") == "gate_blocked"


# --- Реальный stdio wire-тест (план, шаг 1: «Проверка через настоящий stdio») ---
#
# In-process call_tool() не проверяет сериализацию/десериализацию JSON-RPC.
# Ниже — представительская выборка доменных ошибок через настоящий stdio-транспорт
# (паттерн из tests/test_mcp_trace_proxy.py:57-88).


async def _run_knowledge_stdio_checks(
    root: Path,
    extra_args: list[str],
    calls: list[tuple[str, dict]],
) -> list[CallToolResult]:
    """Запускает knowledge-сервер по stdio и выполняет заданные tools/call."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_knowledge.server", *extra_args],
        env={"PYTHONPATH": str(root / "src")},
    )
    results: list[CallToolResult] = []
    async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
        await session.initialize()
        listed = await session.list_tools()
        assert "get_help" in {tool.name for tool in listed.tools}
        for name, args in calls:
            results.append(await session.call_tool(name, args))
    return results


def test_stdio_knowledge_domain_errors_and_success(tmp_path: Path) -> None:
    """Настоящий stdio: доменные ошибки → isError=true, успех → isError=false.

    Покрывает представительную выборку контракта шага 1 плана:
    1. record_knowledge на незакреплённом корне (multi-tenant, ROOT_NOT_ANCHORED) — isError=true;
    2. успешный search_knowledge — isError=false;
    3. get_help — isError=false.
    """
    import asyncio
    import mcp.types

    root = Path(__file__).resolve().parents[1]
    unanchored = tmp_path / "unanchored"
    unanchored.mkdir()
    anchored = tmp_path / "anchored"
    anchored.mkdir()
    (anchored / "AGENTS.md").write_text("Test project root marker.\n")

    calls = [
        (
            "record_knowledge",
            {
                "title": "stdio wire test card",
                "category": "backend",
                "project_path": str(unanchored),
            },
        ),
        ("search_knowledge", {"query": "ffmpeg", "project_path": str(anchored)}),
        ("get_help", {"tool_name": "overview"}),
    ]

    results = asyncio.run(
        _run_knowledge_stdio_checks(
            root,
            extra_args=["--multi-tenant"],
            calls=calls,
        )
    )

    # 1. Незакреплённый root → доменная ошибка с is_error=True и envelope
    unanchored_res = results[0]
    assert unanchored_res.is_error is True, (
        f"ROOT_NOT_ANCHORED через stdio должен давать isError=true: {unanchored_res}"
    )
    data = json.loads(unanchored_res.content[0].text)
    assert data.get("error") is True
    assert data.get("reason") == "ROOT_NOT_ANCHORED"

    # 2. Успешный вызов → isError=false
    assert results[1].is_error is False, f"search_knowledge не должен быть ошибкой: {results[1]}"

    # 3. Глобальный get_help без project_path → isError=false
    assert results[2].is_error is False, f"get_help не должен быть ошибкой: {results[2]}"
    assert "Overview" in results[2].content[0].text or results[2].content[0].text.strip()


def test_stdio_skills_missing_skill_returns_is_error_true(tmp_path: Path) -> None:
    """Настоящий stdio (skills): отсутствующий skill → isError=true со структурированным envelope."""
    import asyncio
    import mcp.types

    from mcp import ClientSession

    root = Path(__file__).resolve().parents[1]
    (tmp_path / "AGENTS.md").write_text("Local stdio test project.\n")

    async def scenario() -> mcp.types.CallToolResult:
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "mcp_skills.server",
                "--project-root",
                str(tmp_path),
                "--skills-dir",
                str(tmp_path / "skills"),
                "--db-path",
                str(tmp_path / "index.db"),
            ],
            env={"PYTHONPATH": str(root / "src")},
        )
        async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
            await session.initialize()
            return await session.call_tool("load_skill", {"name": "definitely-missing-skill-12345"})

    res = asyncio.run(scenario())
    assert res.is_error is True, (
        f"Отсутствующий skill через stdio должен давать isError=true: {res}"
    )
    data = json.loads(res.content[0].text)
    assert data.get("error") is True
    assert data.get("reason") == "SKILL_NOT_FOUND"


def test_stdio_get_help_forced_failure_returns_structured_error(tmp_path: Path) -> None:
    """Настоящий stdio: принудительный сбой help-инструмента → isError=true с envelope.

    Дефект А: раньше клиент получал строку «⚠️ Ошибка...» с isError=false.
    Через настоящий stdio подменяем get_help_text на бросающую исключение версию
    (модуль-обёртка — see help_probe) и проверяем структурированный error-envelope.
    """
    import asyncio
    import mcp.types

    root = Path(__file__).resolve().parents[1]
    probe = tmp_path / "help_probe.py"
    probe.write_text(
        "import mcp_knowledge.server as srv\n"
        "from mcp_knowledge.help_docs import get_help_text as _real\n"
        "def _failing(tool_name=None):\n"
        "    raise RuntimeError('forced help failure')\n"
        "srv.get_help_text = _failing\n"
        "srv.run_server()\n"
    )

    async def scenario() -> mcp.types.CallToolResult:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(probe)],
            env={"PYTHONPATH": str(root / "src")},
        )
        async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
            await session.initialize()
            return await session.call_tool("get_help", {"tool_name": "search"})

    res = asyncio.run(scenario())
    assert res.is_error is True, (
        f"Сбой get_help через stdio должен давать isError=true (структурированный envelope): {res}"
    )
    data = json.loads(res.content[0].text)
    assert data.get("error") is True
    assert data.get("status") == "rejected"
    assert data.get("error_type") == "RuntimeError"
    assert "forced help failure" in data.get("message", "")
