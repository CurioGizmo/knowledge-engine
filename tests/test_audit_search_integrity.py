"""Тесты пункта D плана search-and-data-integrity-followup: точные схемы audit и единый поиск артефактов.

Покрывает:
- comments: строго list[ReviewComment] | None (без альтернативной dict-схемы);
- enum-отвержение: неверные priority/autonomy/complexity/status -> ошибка валидации (не молчаливый default);
- canonical artifact_type 'debt' в ответе search_artifacts (alias 'audit' только на входе);
- unknown status в search_debt/search_artifacts -> понятная ошибка (не пустой результат);
- search_artifacts(all): RRF-слияние кандидатов из обоих источников до общего limit;
- пустой запрос: общая сортировка по времени (mtime) со стабильным tie-break по id;
- get_help audit: доступен и не требует project_path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_audit.server import (
    create_server,
    record_debt,
    record_review,
    search_artifacts,
    search_debt,
)


@pytest.fixture
def test_project_root(tmp_path: Path) -> Path:
    """Создаёт изолированный проект с корневыми маркерами (anchored root)."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def server(test_project_root: Path):
    """MCP-сервер audit на изолированном проекте."""
    return create_server(project_root=test_project_root)


# ---------------------------------------------------------------------------
# 1. comments: строгая типизация ReviewComment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_review_comments_schema_is_strict(server) -> None:
    """Инструмент record_review публикует строгую вложенную схему ReviewComment."""
    tools = await server.list_tools()
    review_tool = next(t for t in tools if t.name == "record_review")
    schema = review_tool.input_schema
    props = schema["properties"]
    assert "comments" in props
    comments_prop = props["comments"]
    # Схема массива строго ссылается на $defs/ReviewComment (без dict-альтернативы)
    if "anyOf" in comments_prop:
        array_variant = next(v for v in comments_prop["anyOf"] if v.get("type") == "array")
    else:
        array_variant = comments_prop
    assert array_variant["items"] == {"$ref": "#/$defs/ReviewComment"}
    # Строгая объектная схема ReviewComment из модели
    rc = schema["$defs"]["ReviewComment"]
    assert rc["type"] == "object"
    # Обязательные поля из модели ReviewComment
    assert set(rc.get("required", [])) == {"file_path", "symptom"}
    assert set(rc["properties"].keys()) == {"file_path", "line", "symptom", "status"}
    assert rc["properties"]["status"]["enum"] == ["open", "resolved", "deferred"]
    # Модель не допускает лишних полей
    from mcp_audit.models import ReviewComment

    import pydantic

    with pytest.raises(pydantic.ValidationError):
        ReviewComment.model_validate({"file_path": "a.py", "symptom": "s", "unknown_field": 1})


@pytest.mark.asyncio
async def test_record_review_comments_listed_at_mcp_boundary(server) -> None:
    """Уровень MCP принимает корректные comments и отвергает невалидные поля."""
    from mcp.server.mcpserver.exceptions import ToolError

    res = await server.call_tool(
        "record_review",
        {
            "task_id": "DSCHEMA-1",
            "verdict": "changes_requested",
            "summary": "Review with comments",
            "comments": [
                {
                    "file_path": "src/auth.py",
                    "line": 42,
                    "symptom": "Token expiry boundary issue",
                    "status": "open",
                }
            ],
        },
    )
    data = res.structured_content
    assert data["status"] == "created"
    assert Path(data["file_path"]).exists()

    # Лишнее поле в комментарии → ошибка валидации схемы на MCP-границе
    with pytest.raises(ToolError, match="extra|validation error|unknown_field"):
        await server.call_tool(
            "record_review",
            {
                "task_id": "DSCHEMA-2",
                "verdict": "commented",
                "summary": "Bad comment shape",
                "comments": [{"file_path": "src/auth.py", "symptom": "x", "unknown_field": 1}],
            },
        )


# ---------------------------------------------------------------------------
# 2. enum-отвержение: неверные значения -> ошибка, не default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_debt_invalid_enum_rejected_via_mcp(server) -> None:
    """Неверный priority/autonomy/complexity в record_debt → ошибка валидации схемы, НЕ default."""
    from mcp.server.mcpserver.exceptions import ToolError

    for bad_kwargs in (
        {"priority": "urgent"},
        {"autonomy": "auto"},
        {"complexity": "XL"},
    ):
        with pytest.raises(ToolError, match="literal_error|validation error"):
            await server.call_tool(
                "record_debt",
                {"title": f"Bad {bad_kwargs}", "category": "backend", "symptom": "s", **bad_kwargs},
            )


def test_record_debt_invalid_enum_rejected_direct(test_project_root: Path) -> None:
    """Прямой вызов доменной функции: неверный enum поднимает ValueError (не default)."""
    with pytest.raises(ValueError, match="priority"):
        record_debt(
            title="Direct bad priority",
            category="backend",
            symptom="s",
            priority="urgent",
            storage=None,
        )


@pytest.mark.asyncio
async def test_update_debt_status_invalid_rejected(server) -> None:
    """Неверный status в update_debt_status → ошибка валидации, не молчаливое поведение."""
    from mcp.server.mcpserver.exceptions import ToolError

    res = await server.call_tool(
        "record_debt",
        {"title": "Enum check debt", "category": "backend", "symptom": "s"},
    )
    debt_id = res.structured_content["id"]
    with pytest.raises(ToolError, match="literal_error|validation error"):
        await server.call_tool(
            "update_debt_status",
            {"id": debt_id, "status": "deleted"},
        )


# ---------------------------------------------------------------------------
# 3. Canonical artifact_type 'debt'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_artifacts_debt_canonical_type(server) -> None:
    """artifact_type в ответе для долгов — канонический 'debt' (не 'audit')."""
    await server.call_tool(
        "record_debt",
        {"title": "Canonical debt type", "category": "backend", "symptom": "canonical symptom"},
    )
    res_all = await server.call_tool("search_artifacts", {"artifact_type": "all"})
    for item in res_all.structured_content["results"]:
        if item.get("symptom") is not None:
            assert item["artifact_type"] == "debt", (
                f"Долг должен иметь artifact_type='debt', получено {item['artifact_type']}"
            )


@pytest.mark.asyncio
async def test_search_artifacts_audit_alias_accepted(server) -> None:
    """'audit' остаётся входным alias для artifact_type='debt'."""
    await server.call_tool(
        "record_debt",
        {"title": "Alias check debt", "category": "backend", "symptom": "alias symptom"},
    )
    res = await server.call_tool("search_artifacts", {"artifact_type": "audit"})
    assert res.is_error is False
    assert res.structured_content["count"] >= 1
    for item in res.structured_content["results"]:
        assert item["artifact_type"] == "debt"


# ---------------------------------------------------------------------------
# 4. Unknown status: понятная ошибка, не пустой результат
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_artifacts_unknown_status_rejected(server) -> None:
    """Полностью неизвестный статус отклоняется понятной ошибкой."""
    res = await server.call_tool(
        "search_artifacts",
        {"artifact_type": "all", "status": "approved"},
    )
    # 'approved' — валидный статус плана, но не долг; для all он НЕ применяется к долгам
    # молча. Полностью неизвестный статус:
    res_unknown = await server.call_tool(
        "search_artifacts",
        {"artifact_type": "all", "status": "bogus_status"},
    )
    assert res_unknown.is_error is True
    assert "bogus_status" in str(res_unknown)


@pytest.mark.asyncio
async def test_search_debt_unknown_status_rejected(server) -> None:
    """Неизвестный статус в search_debt → ошибка (не пустой результат)."""
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="literal_error|validation error"):
        await server.call_tool(
            "search_debt",
            {"status": "unknown_status_x"},
        )


# ---------------------------------------------------------------------------
# 5. RRF merge: релевантный долг среди >10 планов находит при limit=1/odd
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_artifacts_relevant_debt_not_starved(server) -> None:
    """Релевантный долг находится среди >10 нерелевантных планов (limit=1 и нечётный)."""
    # >10 планов с общим словом
    for i in range(12):
        await server.call_tool(
            "record_plan",
            {
                "task_id": f"RRF-PLAN-{i}",
                "title": f"Generic plan filler {i}",
                "steps": [f"Step {i}"],
                "category": "backend",
            },
        )
    # Один релевантный долг
    await server.call_tool(
        "record_debt",
        {
            "title": "Unique rrf marker debt",
            "category": "backend",
            "symptom": "rrf marker symptom",
        },
    )

    res = await server.call_tool(
        "search_artifacts",
        {"query": "rrf marker", "artifact_type": "all", "limit": 1},
    )
    data = res.structured_content
    assert data["count"] == 1
    assert data["results"][0]["artifact_type"] == "debt"
    assert "rrf marker" in data["results"][0]["title"].lower()

    # Нечётный limit > 1: релевантный долг остаётся первым
    res3 = await server.call_tool(
        "search_artifacts",
        {"query": "rrf marker", "artifact_type": "all", "limit": 3},
    )
    data3 = res3.structured_content
    assert data3["count"] >= 1
    assert data3["results"][0]["artifact_type"] == "debt"


# ---------------------------------------------------------------------------
# 6. Пустой запрос: общая сортировка по времени + стабильный tie-break
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_artifacts_empty_query_unified_time_sort(server) -> None:
    """Пустой запрос: одна общая выдача, отсортированная по времени (свежее — раньше)."""
    await server.call_tool(
        "record_plan",
        {"task_id": "EMPTY-1", "title": "Plan first", "steps": ["s"], "category": "backend"},
    )
    await server.call_tool(
        "record_debt",
        {"title": "Debt second", "category": "backend", "symptom": "s"},
    )
    res = await server.call_tool(
        "search_artifacts",
        {"query": "", "artifact_type": "all", "limit": 10},
    )
    results = res.structured_content["results"]
    assert len(results) >= 2
    types = {r["artifact_type"] for r in results}
    assert "plan" in types and "debt" in types
    # Долг создан позже плана → должен быть раньше в выдаче
    first_debt_idx = next(i for i, r in enumerate(results) if r["artifact_type"] == "debt")
    first_plan_idx = next(i for i, r in enumerate(results) if r["artifact_type"] == "plan")
    assert first_debt_idx < first_plan_idx


# ---------------------------------------------------------------------------
# 7. get_help audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_get_help_registered_no_project_path(server) -> None:
    """get_help доступен в audit-engine и не требует project_path."""
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert "get_help" in names

    help_tool = next(t for t in tools if t.name == "get_help")
    assert "project_path" not in help_tool.input_schema.get("properties", {})

    res = await server.call_tool("get_help", {})
    assert res.is_error is False
    # Текстовая выдача: без structured_content, читаем text-блок
    text = res.structured_content["result"] if res.structured_content else res.content[0].text
    assert text  # непустая справка


@pytest.mark.asyncio
async def test_audit_get_help_covers_tools_and_compat(server) -> None:
    """Справка упоминает инструменты, статусы и совместимость artifact_type."""
    res = await server.call_tool("get_help", {"tool_name": "search_artifacts"})
    text = res.structured_content["result"] if res.structured_content else res.content[0].text
    assert "search_artifacts" in text
    assert "debt" in text
    # Совместимость: 'audit' — входной alias
    assert "audit" in text


# ---------------------------------------------------------------------------
# 8. Фильтры статусов/типов
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_artifacts_plan_status_filter(server) -> None:
    """Фильтр по статусу плана работает и не применяется к несовместимым типам молча."""
    await server.call_tool(
        "record_plan",
        {"task_id": "FILT-1", "title": "Plan filter target", "steps": ["s"], "category": "backend"},
    )
    res = await server.call_tool(
        "search_artifacts",
        {"query": "", "artifact_type": "plan", "status": "discussed"},
    )
    assert any(r["task_id"] == "FILT-1" for r in res.structured_content["results"])

    res_other = await server.call_tool(
        "search_artifacts",
        {"query": "", "artifact_type": "plan", "status": "in_progress"},
    )
    assert not any(r["task_id"] == "FILT-1" for r in res_other.structured_content["results"])


@pytest.mark.asyncio
async def test_search_artifacts_debt_status_filter(server) -> None:
    """Фильтр статуса долга: 'completed' — валидный статус долга; 'approved' — нет."""
    await server.call_tool(
        "record_debt",
        {"title": "Debt status filter", "category": "backend", "symptom": "filter symptom"},
    )
    res = await server.call_tool(
        "search_artifacts",
        {"query": "", "artifact_type": "debt", "status": "planned"},
    )
    assert res.structured_content["count"] >= 1

    # 'approved' не является статусом долга → для artifact_type='debt' это ошибка
    res_bad = await server.call_tool(
        "search_artifacts",
        {"query": "", "artifact_type": "debt", "status": "approved"},
    )
    assert res_bad.is_error is True


@pytest.mark.asyncio
async def test_search_artifacts_all_approved_not_silently_applied_to_debt(server) -> None:
    """'approved' (статус плана) при all не подставляет долгам completed/молчание."""
    await server.call_tool(
        "record_plan",
        {"task_id": "APPR-1", "title": "Approved plan", "steps": ["s"], "category": "backend"},
    )
    await server.call_tool(
        "update_plan",
        {"task_id": "APPR-1", "status": "approved"},
    )
    await server.call_tool(
        "record_debt",
        {"title": "Unrelated debt", "category": "backend", "symptom": "unrelated symptom"},
    )
    res = await server.call_tool(
        "search_artifacts",
        {"query": "", "artifact_type": "all", "status": "approved"},
    )
    assert res.is_error is False
    results = res.structured_content["results"]
    # План approved найден
    assert any(r["task_id"] == "APPR-1" for r in results)
    # Долги с другим статусом не подмешаны под 'approved'
    assert not any(r["artifact_type"] == "debt" for r in results)


@pytest.mark.asyncio
async def test_audit_search_score_precision_non_zero(server) -> None:
    """SCORE (41e9): поиск долга сохраняет ненулевой score для малых значений BM25."""
    long_symptom = "микросекунда " + "фоновый процесс базы данных " * 300
    await server.call_tool(
        "record_debt",
        {
            "title": "Случайный заголовок без ключевых слов",
            "category": "backend",
            "symptom": long_symptom,
        },
    )
    res = await server.call_tool(
        "search_debt",
        {"query": "микросекунда"},
    )
    assert res.structured_content["count"] == 1
    score = res.structured_content["results"][0]["score"]
    assert score is not None
    assert score > 0.0, f"Score не должен округляться в 0.0, получено {score}"


def test_round_bm25_score_preserves_tiny_values() -> None:
    """Ревью шага 3 (п. 5): ненулевой score не округляется до 0.0.

    Воспроизведение из отзыва: raw 4.594321416308766e-09 публиковался как 0.0
    (round(..., 6) и запасной round(..., 8) оба дают 0.0 для значений < 1e-8).
    """
    from mcp_core.fts import round_bm25_score

    # Обычные значения округляются до 6 знаков
    assert round_bm25_score(0.123456789) == 0.123457
    assert round_bm25_score(1.5) == 1.5

    # Крошечные ненулевые значения сохраняются как есть
    tiny = 4.594321416308766e-09
    assert round_bm25_score(tiny) == tiny
    assert round_bm25_score(tiny) > 0.0

    # Отрицательные и нулевые — как прежде
    assert round_bm25_score(0.0) == 0.0
    assert round_bm25_score(-1.2e-09) == -0.0 or round_bm25_score(-1.2e-09) == 0.0
