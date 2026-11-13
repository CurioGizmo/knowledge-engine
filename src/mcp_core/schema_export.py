"""Утилита автоматического экспорта схем инструментов из MCPServer в формат Antigravity."""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any

from mcp.server.mcpserver import MCPServer

__all__ = ["export_server_schemas", "export_server_schemas_sync", "run_sync"]


async def export_server_schemas(server: MCPServer) -> dict[str, dict[str, Any]]:
    """Извлекает зарегистрированные инструменты из MCPServer и форматирует их в схему Antigravity.

    Маппинг:
    - name -> name
    - description -> description
    - input_schema (JSON Schema) -> parameters
    """
    tools = await server.list_tools()
    schemas: dict[str, dict[str, Any]] = {}
    for tool in tools:
        input_schema = getattr(tool, "input_schema", None)
        if input_schema is None and hasattr(tool, "model_dump"):
            dump = tool.model_dump()
            input_schema = dump.get("input_schema") or dump.get("inputSchema") or {}

        schemas[tool.name] = {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": input_schema or {"type": "object", "properties": {}},
        }
    return schemas


def run_sync(coro: Any) -> Any:
    """Синхронно исполняет корутину, безопасно работая как вне, так и внутри запущенного event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()


def export_server_schemas_sync(server: MCPServer) -> dict[str, dict[str, Any]]:
    """Синхронная обёртка над export_server_schemas."""
    return run_sync(export_server_schemas(server))
