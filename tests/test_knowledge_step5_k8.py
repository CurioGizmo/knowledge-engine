"""Шаг 5 плана AUDIT-VERIFY-2026-10: K8 — повторный verify идемпотентен.

До фикса: каждый вызов verify добавлял VerificationRecord и строку лога
VERIFIED, даже если идентичная запись уже есть (дубли в frontmatter и логе).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult

from mcp_knowledge.parser import parse_card_markdown
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
async def test_repeated_verify_is_idempotent(env, tmp_path: Path):
    """K8: два идентичных verify подряд → один VerificationRecord, одна строка VERIFIED."""
    storage, service, server = env

    res = await server.call_tool(
        "record_knowledge",
        {
            "title": "K8 idempotent verify card",
            "category": "backend",
            "type": "lesson",
            "source": "user_confirmed",
            "symptom": "Sufficient symptom text for the content gate validation",
            "root_cause": "Sufficient root cause text for the content gate validation",
            "solution": "Sufficient solution text for the content gate validation (min 50 chars).",
            "rationale": "Rationale is required to pass the content gate",
            "slug": "k8-verify-card",
        },
    )
    data = _wire_data(res)
    assert not res.is_error, data
    card_id = data["id"]

    note = "bench verification passed on 3.12"
    for _ in range(2):
        res = await server.call_tool(
            "update_knowledge_status",
            {"id": card_id, "action": "verify", "note": note, "by": "human"},
        )
        data = _wire_data(res)
        assert not res.is_error, data
        assert data["new_status"] == "verified", data

    card_file = tmp_path / "knowledge" / "backend" / f"{card_id}.md"
    fm, sections = parse_card_markdown(card_file.read_text(encoding="utf-8"))
    assert len(fm.verified) == 1, f"дубликат VerificationRecord: {fm.verified}"
    log_text = "\n".join(sections.values())
    assert log_text.count("VERIFIED") == 1, "дубликат строки VERIFIED в логе"


@pytest.mark.asyncio
async def test_verify_with_new_note_appends(env, tmp_path: Path):
    """K8: verify с ДРУГОЙ note — новая запись добавляется (не блокируется)."""
    _, _, server = env

    res = await server.call_tool(
        "record_knowledge",
        {
            "title": "K8 second card",
            "category": "backend",
            "type": "lesson",
            "source": "user_confirmed",
            "symptom": "Sufficient symptom text for the content gate validation",
            "root_cause": "Sufficient root cause text for the content gate validation",
            "solution": "Sufficient solution text for the content gate validation (min 50 chars).",
            "rationale": "Rationale is required to pass the content gate",
            "slug": "k8-verify-card-2",
        },
    )
    data = _wire_data(res)
    card_id = data["id"]

    res = await server.call_tool(
        "update_knowledge_status",
        {"id": card_id, "action": "verify", "note": "first bench run passed", "by": "human"},
    )
    assert not _wire_data(res).get("error"), _wire_data(res)

    res = await server.call_tool(
        "update_knowledge_status",
        {
            "id": card_id,
            "action": "verify",
            "note": "second bench run on 3.13 passed",
            "by": "human",
        },
    )
    assert not _wire_data(res).get("error"), _wire_data(res)

    card_file = tmp_path / "knowledge" / "backend" / f"{card_id}.md"
    fm, _ = parse_card_markdown(card_file.read_text(encoding="utf-8"))
    assert len(fm.verified) == 2, f"новая note должна добавлять запись: {fm.verified}"
