"""Шаг 5 плана AUDIT-VERIFY-2026-10: K5 — ValidationError маппится в
reason=VALIDATION_FAILED, а не ANTI_SPAM_REJECTED.

До фикса: record_knowledge/update_knowledge_status без validation_reason
в _telemetry — pydantic ValidationError получал дефолт ANTI_SPAM_REJECTED
(ложный сигнал «отклонено антиспам-гейтом»).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult

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
async def test_record_knowledge_validation_error_reason(env):
    """K5: мусорный enum (type) -> ValidationError -> VALIDATION_FAILED."""
    _, _, server = env

    res = await server.call_tool(
        "record_knowledge",
        {
            "title": "K5 validation mapping card",
            "category": "backend",
            "type": "not-a-valid-type",
            "symptom": "symptom",
            "root_cause": "cause",
            "solution": "solution",
        },
    )
    assert res.is_error, _wire_data(res)
    data = _wire_data(res)
    assert data.get("error") is True
    assert data.get("reason") == "VALIDATION_FAILED", data
    assert data.get("error_type") == "ValidationError", data


@pytest.mark.asyncio
async def test_update_status_validation_error_reason(env):
    """K5: мусорный action -> ValidationError -> VALIDATION_FAILED (не ANTI_SPAM)."""
    _, _, server = env

    res = await server.call_tool(
        "record_knowledge",
        {
            "title": "K5 card for update",
            "category": "backend",
            "type": "lesson",
            "source": "user_confirmed",
            "symptom": "Sufficient symptom text for the content gate validation",
            "root_cause": "Sufficient root cause text for the content gate validation",
            "solution": "Sufficient solution text for the content gate validation (min 50 chars).",
            "rationale": "Rationale is required to pass the content gate",
            "slug": "k5-update-card",
        },
    )
    data = _wire_data(res)
    assert not res.is_error, data
    card_id = data["id"]

    # невалидный action -> ValidationError на уровне request-модели
    res = await server.call_tool(
        "update_knowledge_status",
        {"id": card_id, "action": "not-an-action", "note": "x"},
    )
    assert res.is_error, _wire_data(res)
    data = _wire_data(res)
    assert data.get("reason") == "VALIDATION_FAILED", data
