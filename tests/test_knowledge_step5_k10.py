"""Шаг 5 плана AUDIT-VERIFY-2026-10: K10 — одна generic-алиаса не даёт
ложный DUPLICATE_FOUND.

До фикса: any(len(a) >= 5) на общих алиасах внутри категории — одна
generic-алиаса («python», 6 симв.) при непохожих title/symptom ловила
DUPLICATE_FOUND. Теперь требуется >= 2 общих алиас (контракт
кросс-категориальной ветки).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult

from mcp_knowledge.models import CardRecordRequest, KnowledgeSource, KnowledgeType
from mcp_knowledge.server import create_server
from mcp_knowledge.service import KnowledgeService
from mcp_knowledge.storage import KnowledgeStorage


@pytest.fixture
def env(tmp_path: Path):
    k_dir = tmp_path / "knowledge"
    cache_db = tmp_path / "cache.db"
    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)
    server = create_server(service=service)
    return storage, service, server


def _wire_data(res: CallToolResult) -> dict[str, Any]:
    assert isinstance(res, CallToolResult)
    if res.structured_content:
        return res.structured_content
    return json.loads(res.content[0].text)


@pytest.mark.asyncio
async def test_single_generic_alias_not_duplicate(env):
    """K10: одна общая generic-алиаса при непохожих текстах — не дубликат."""
    _, _, server = env

    first = {
        "title": "Uvicorn worker signal handling",
        "category": "backend",
        "type": "lesson",
        "source": "user_confirmed",
        "symptom": "Uvicorn workers ignore SIGTERM during graceful shutdown window",
        "root_cause": "Signal handlers are installed after the event loop starts",
        "solution": "Install signal handlers before loop start or use lifespan events properly.",
        "rationale": "Documented uvicorn behaviour differs from gunicorn expectations",
        "aliases": ["python"],
        "slug": "k10-first-card",
    }
    res = await server.call_tool("record_knowledge", first)
    data = _wire_data(res)
    assert not res.is_error, data

    second = dict(first)
    second.update(
        {
            "title": "SQLite WAL mode under concurrent writers",
            "symptom": "Database is locked errors appear when multiple writers hold WAL",
            "root_cause": "WAL allows single writer; readers do not block it but writers queue",
            "solution": "Use busy_timeout and short transactions, or serialize writes in queue.",
            "slug": "k10-second-card",
            "aliases": ["python"],
        }
    )
    res = await server.call_tool("record_knowledge", second)
    data = _wire_data(res)
    assert not res.is_error, "одна generic-алиаса не должна давать DUPLICATE_FOUND: " + json.dumps(
        data
    )


@pytest.mark.asyncio
async def test_two_common_aliases_still_duplicate(env):
    """K10: две общие специфичные алиасы — дубликат по-прежнему ловится."""
    _, _, server = env

    base = {
        "title": "Celery task retry storm",
        "category": "backend",
        "type": "lesson",
        "source": "user_confirmed",
        "symptom": "Celery retries exhausted tasks and floods the broker queue",
        "root_cause": "Retry policy without backoff and max attempts cap",
        "solution": "Configure exponential backoff and max_retries on the task decorator.",
        "rationale": "Retry storms take down the whole worker fleet",
        "aliases": ["celery", "retry-storm"],
        "slug": "k10-dup-first",
    }
    res = await server.call_tool("record_knowledge", base)
    assert not res.is_error, _wire_data(res)

    duplicate = dict(base)
    duplicate["slug"] = "k10-dup-second"
    res = await server.call_tool("record_knowledge", duplicate)
    data = _wire_data(res)
    # дубликат ловится (reject или confirmation_required)
    assert data.get("status") in ("rejected", "confirmation_required"), data
