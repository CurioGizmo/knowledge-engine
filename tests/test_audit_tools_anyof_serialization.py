"""Тесты устранения anyOf схем и валидации сериализованных JSON-аргументов в MCP Audit Engine.

Проверяет:
1. Отсутствие anyOf у list/int параметров в экспортируемых схемах инструментов.
2. Приём JSON-сериализованных строк для массивов и чисел (защита от багов сериализации MCP-мостов).
3. Корректную работу при нативных списках и числах.
4. Авторасчёт round при round=0 в record_review.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from mcp_audit.installer import get_audit_tool_schemas
from mcp_audit.server import create_server


def _data(res: Any) -> dict[str, Any]:
    assert isinstance(res, CallToolResult)
    assert isinstance(res.structured_content, dict)
    return res.structured_content


def test_audit_tool_schemas_have_no_anyof_for_arrays_and_ints() -> None:
    """Схемы инструментов не должны содержать anyOf для list и int параметров."""
    schemas = get_audit_tool_schemas()

    # 1. record_plan
    plan_props = schemas["record_plan"]["parameters"]["properties"]
    assert plan_props["steps"].get("type") == "array"
    assert "anyOf" not in plan_props["steps"]
    assert plan_props["target_files"].get("type") == "array"
    assert "anyOf" not in plan_props["target_files"]

    # 2. update_plan
    update_plan_props = schemas["update_plan"]["parameters"]["properties"]
    assert update_plan_props["add_steps"].get("type") == "array"
    assert "anyOf" not in update_plan_props["add_steps"]
    assert update_plan_props["target_files"].get("type") == "array"
    assert "anyOf" not in update_plan_props["target_files"]

    # 3. record_review
    review_props = schemas["record_review"]["parameters"]["properties"]
    assert review_props["comments"].get("type") == "array"
    assert "anyOf" not in review_props["comments"]
    assert review_props["round"].get("type") == "integer"
    assert "anyOf" not in review_props["round"]
    assert review_props["spawned_debts"].get("type") == "array"
    assert "anyOf" not in review_props["spawned_debts"]

    # 4. record_debt
    debt_props = schemas["record_debt"]["parameters"]["properties"]
    assert debt_props["blocked_by"].get("type") == "array"
    assert "anyOf" not in debt_props["blocked_by"]


@pytest.mark.asyncio
async def test_record_plan_and_update_plan_via_mcp_with_stringified_json(tmp_path: Path) -> None:
    """record_plan и update_plan успешно принимают строковые JSON-массивы через MCPServer."""
    (tmp_path / "pyproject.toml").touch()
    server = create_server(project_root=tmp_path)

    # 1. record_plan со строковыми steps и target_files
    create_args: dict[str, Any] = {
        "task_id": "TEST-ANYOF-01",
        "title": "Test AnyOf Support",
        "steps": '["1. Step one", "2. Step two"]',
        "target_files": '["src/foo.py", "src/bar.py"]',
    }
    res1 = _data(await server.call_tool("record_plan", create_args))
    assert res1.get("status") == "created"
    assert res1.get("steps_count") == 2

    # 2. update_plan со строковыми add_steps и target_files
    update_args: dict[str, Any] = {
        "task_id": "TEST-ANYOF-01",
        "add_steps": '["3. Step three"]',
        "target_files": '["src/baz.py"]',
    }
    res2 = _data(await server.call_tool("update_plan", update_args))
    assert res2.get("status") == "updated"

    # Проверяем итоговый контекст задачи
    ctx_res = _data(
        await server.call_tool(
            "get_task_context",
            {"task_id": "TEST-ANYOF-01", "view": "full"},
        )
    )
    assert ctx_res.get("found") is True
    plan = ctx_res["plan"]
    assert len(plan["steps"]) == 3
    assert plan["target_files"] == ["src/baz.py"]


@pytest.mark.asyncio
async def test_record_review_via_mcp_with_stringified_json(tmp_path: Path) -> None:
    """record_review успешно принимает строковые comments, round, spawned_debts."""
    (tmp_path / "pyproject.toml").touch()
    server = create_server(project_root=tmp_path)

    # Создадим план для привязки ревью
    await server.call_tool(
        "record_plan",
        {
            "task_id": "TEST-REV-01",
            "title": "Review Plan",
            "steps": ["Step 1"],
        },
    )

    review_args: dict[str, Any] = {
        "task_id": "TEST-REV-01",
        "verdict": "changes_requested",
        "summary": "Found issues",
        "comments": '[{"file_path": "src/api.py", "symptom": "Syntax error", "line": 42}]',
        "round": "2",
        "spawned_debts": '["aud-test-debt-1"]',
    }
    res = _data(await server.call_tool("record_review", review_args))
    assert res.get("status") == "created"
    assert res.get("round") == 2

    # Проверим через get_task_context, что комментарии и порождённые долги корректно записались
    ctx_res = _data(
        await server.call_tool(
            "get_task_context",
            {"task_id": "TEST-REV-01", "view": "full"},
        )
    )
    assert ctx_res.get("found") is True
    reviews = ctx_res.get("reviews", [])
    assert len(reviews) == 1
    assert reviews[0]["round"] == 2
    assert len(reviews[0]["comments"]) == 1
    assert reviews[0]["comments"][0]["file_path"] == "src/api.py"
    assert reviews[0]["comments"][0]["line"] == 42
    assert reviews[0]["spawned_debts"] == ["aud-test-debt-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "comment",
    [
        {"file_path": "src/api.py", "symptom": "Single comment"},
        '{"file_path": "src/api.py", "symptom": "Single comment"}',
    ],
)
async def test_record_review_accepts_single_comment_object(tmp_path: Path, comment: Any) -> None:
    """Одиночный комментарий сохраняется как словарь или JSON-объект."""
    (tmp_path / "pyproject.toml").touch()
    server = create_server(project_root=tmp_path)
    await server.call_tool(
        "record_plan",
        {"task_id": "TEST-REV-SINGLE", "title": "Review Plan", "steps": ["Step 1"]},
    )

    review = _data(
        await server.call_tool(
            "record_review",
            {
                "task_id": "TEST-REV-SINGLE",
                "verdict": "changes_requested",
                "comments": comment,
            },
        )
    )
    assert review["status"] == "created"
    context = _data(
        await server.call_tool("get_task_context", {"task_id": "TEST-REV-SINGLE", "view": "full"})
    )
    assert len(context["reviews"][0]["comments"]) == 1
    assert context["reviews"][0]["comments"][0]["symptom"] == "Single comment"


@pytest.mark.asyncio
async def test_record_review_rejects_malformed_json_comment(tmp_path: Path) -> None:
    """Повреждённый JSON не должен превращаться в пустое ревью."""
    (tmp_path / "pyproject.toml").touch()
    server = create_server(project_root=tmp_path)
    with pytest.raises(ToolError, match="comments|JSON|json"):
        await server.call_tool(
            "record_review",
            {"task_id": "TEST-REV-BAD", "comments": '{"file_path": "src/api.py"'},
        )


@pytest.mark.asyncio
async def test_record_plan_splits_multiline_steps_only(tmp_path: Path) -> None:
    """Многострочные steps и add_steps дают отдельные этапы плана."""
    (tmp_path / "pyproject.toml").touch()
    server = create_server(project_root=tmp_path)
    created = _data(
        await server.call_tool(
            "record_plan",
            {
                "task_id": "TEST-STEPS-MULTILINE",
                "title": "Multiline steps",
                "steps": "1. Build provider\n2. Add tests\n3. Update docs",
            },
        )
    )
    assert created["steps_count"] == 3

    updated = _data(
        await server.call_tool(
            "update_plan",
            {"task_id": "TEST-STEPS-MULTILINE", "add_steps": "4. Verify\n5. Release"},
        )
    )
    assert updated["status"] == "updated"

    context = _data(
        await server.call_tool(
            "get_task_context", {"task_id": "TEST-STEPS-MULTILINE", "view": "full"}
        )
    )
    assert [step["title"] for step in context["plan"]["steps"]] == [
        "1. Build provider",
        "2. Add tests",
        "3. Update docs",
        "4. Verify",
        "5. Release",
    ]


@pytest.mark.asyncio
async def test_record_review_round_zero_auto_calculates(tmp_path: Path) -> None:
    """Передача round=0 в record_review запускает авторасчёт раунда."""
    (tmp_path / "pyproject.toml").touch()
    server = create_server(project_root=tmp_path)

    await server.call_tool(
        "record_plan",
        {
            "task_id": "TEST-ROUND-01",
            "title": "Round Auto Plan",
            "steps": ["Step 1"],
        },
    )

    # Раунд 1: round=0 -> авторасчёт даёт 1
    res1 = _data(
        await server.call_tool(
            "record_review",
            {
                "task_id": "TEST-ROUND-01",
                "summary": "Round 1 review",
                "round": 0,
            },
        )
    )
    assert res1.get("round") == 1

    # Раунд 2: без round (дефолт 0) -> авторасчёт даёт 2
    res2 = _data(
        await server.call_tool(
            "record_review",
            {
                "task_id": "TEST-ROUND-01",
                "summary": "Round 2 review",
            },
        )
    )
    assert res2.get("round") == 2


@pytest.mark.asyncio
async def test_record_debt_with_stringified_blocked_by(tmp_path: Path) -> None:
    """record_debt принимает blocked_by как строковый JSON-массив и как нативный список."""
    (tmp_path / "pyproject.toml").touch()
    server = create_server(project_root=tmp_path)

    # Сначала создадим карточки, которые будут выступать блокерами
    blocker1 = _data(
        await server.call_tool(
            "record_debt",
            {"title": "Blocker 1", "category": "backend"},
        )
    )
    b1_id = blocker1["id"]

    blocker2 = _data(
        await server.call_tool(
            "record_debt",
            {"title": "Blocker 2", "category": "backend"},
        )
    )
    b2_id = blocker2["id"]

    # 1. Строковый JSON
    res1 = _data(
        await server.call_tool(
            "record_debt",
            {
                "title": "Debt with string blocked_by",
                "category": "backend",
                "blocked_by": f'["{b1_id}"]',
            },
        )
    )
    assert res1.get("status") == "created"
    assert res1["issue"]["blocked_by"] == [b1_id]

    # 2. Нативный список
    res2 = _data(
        await server.call_tool(
            "record_debt",
            {
                "title": "Debt with native list blocked_by",
                "category": "backend",
                "blocked_by": [b2_id],
            },
        )
    )
    assert res2.get("status") == "created"
    assert res2["issue"]["blocked_by"] == [b2_id]
