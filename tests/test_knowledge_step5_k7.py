"""Шаг 5 плана AUDIT-VERIFY-2026-10: K7 — content gate покрывает note
для suspect/dispute; propose_hypothesis получает двухфазный force-путь.

До фикса: note при suspect/dispute не проверялся на report-like контент
(чеклисты/трейсбеки попадали в лог карточки); content gate гипотез
отвергал force без подтверждения (нет confirmation_required-фазы).
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

CHECKLIST_NOTE = "Проверки:\n- [ ] тесты зелёные\n- [ ] линт чистый"
TRACEBACK_NOTE = 'File "src/a.py", line 1, in f\nFile "src/b.py", line 2, in g'


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


async def _record_active_card(server) -> str:
    """Активная auto_inferred-карточка: создана с force (гейт не мешает),
    затем промоутнута (барьер user_confirmed не блокирует dispute)."""
    res = await server.call_tool(
        "record_knowledge",
        {
            "title": "K7 gate card",
            "category": "backend",
            "type": "lesson",
            "source": "auto_inferred",
            "symptom": "Sufficient symptom text for the content gate validation",
            "root_cause": "Sufficient root cause text for the content gate validation",
            "solution": "Sufficient solution text for the content gate validation (min 50 chars).",
            "rationale": "Rationale is required to pass the content gate",
            "slug": "k7-gate-card",
            "force": True,
            "confirmed_force": True,
        },
    )
    data = _wire_data(res)
    assert not res.is_error, data
    card_id = data["id"]

    # промоутим draft → active (согласие предполагается на стороне вызывающего)
    res = await server.call_tool(
        "update_knowledge_status",
        {"id": card_id, "action": "promote", "source": "user_confirmed", "by": "human"},
    )
    data = _wire_data(res)
    assert not res.is_error, data
    assert data.get("new_status") == "active", data
    return card_id


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["suspect", "dispute", "append_note", "verify"])
async def test_note_content_gate_covers_all_actions(env, action):
    """K7: report-like note отвергается для всех note-несущих действий."""
    _, _, server = env
    card_id = await _record_active_card(server)

    res = await server.call_tool(
        "update_knowledge_status",
        {"id": card_id, "action": action, "note": CHECKLIST_NOTE, "by": "human"},
    )
    assert res.is_error, _wire_data(res)
    data = _wire_data(res)
    assert data.get("reason") == "REPORT_LIKE_CONTENT", data


@pytest.mark.asyncio
async def test_note_content_gate_immunity_for_user_confirmed(env):
    """Иммунитет user_confirmed сохранён: карточка подтверждена человеком,
    note с чеклистом от user_confirmed-источника проходит без REPORT_LIKE_CONTENT."""
    _, _, server = env
    res = await server.call_tool(
        "record_knowledge",
        {
            "title": "K7 immune card",
            "category": "backend",
            "type": "lesson",
            "source": "user_confirmed",
            "symptom": "Sufficient symptom text for the content gate validation",
            "root_cause": "Sufficient root cause text for the content gate validation",
            "solution": "Sufficient solution text for the content gate validation (min 50 chars).",
            "rationale": "Rationale is required to pass the content gate",
            "slug": "k7-immune-card",
        },
    )
    data = _wire_data(res)
    assert not res.is_error, data
    card_id = data["id"]

    res = await server.call_tool(
        "update_knowledge_status",
        {
            "id": card_id,
            "action": "append_note",
            "note": CHECKLIST_NOTE,
            "source": "user_confirmed",
        },
    )
    data = _wire_data(res)
    assert not res.is_error, data


@pytest.mark.asyncio
async def test_hypothesis_force_gate_two_phase(env):
    """K7: propose_hypothesis force без confirmed_force → confirmation_required,
    с confirmed_force — принимает report-like контент."""
    _, _, server = env

    bad = {
        "title": "K7 hypothesis",
        "category": "backend",
        "symptom": CHECKLIST_NOTE,
        "hypothesis": "some hypothesis",
        "force": True,
    }
    res = await server.call_tool("propose_hypothesis", bad)
    data = _wire_data(res)
    assert data.get("status") == "confirmation_required", data
    assert data.get("reason") == "REPORT_CONFIRMATION_REQUIRED", data

    bad["confirmed_force"] = True
    res = await server.call_tool("propose_hypothesis", bad)
    data = _wire_data(res)
    assert not res.is_error, data
    assert data.get("status") == "proposed", data
