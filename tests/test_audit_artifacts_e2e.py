"""E2E интеграционные тесты жизненного цикла артефактов (планы, ревью, контекст, долг)."""

from __future__ import annotations

from pathlib import Path
import pytest

from mcp_audit.server import create_server


@pytest.fixture
def test_project_root(tmp_path: Path) -> Path:
    """Создаёт изолированный проект с корневого маркерами."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n", encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_audit_lifecycle_e2e(test_project_root: Path) -> None:
    """Сквозной e2e сценарий:
    1. record_plan -> статус discussed, файл в active/
    2. update_plan -> статус approved
    3. step_plan(step=1) -> статус in_progress, [x] в файле
    4. step_plan(step=2) -> статус done, авто-перенос в completed/
    5. step_plan(step=2, status="open") -> откат в in_progress, возврат в active/
    6. step_plan(step=2, status="done") -> повторно done
    7. record_review -> approved раунд 1
    8. get_task_context -> единый агрегированный ответ
    9. search_artifacts -> поиск по FTS
    """
    server = create_server(project_root=test_project_root)

    # 1. Создание плана
    plan_res = await server.call_tool(
        "record_plan",
        {
            "task_id": "AUTH-42",
            "title": "OAuth2 Implementation",
            "steps": ["Setup TokenProvider", "Register FastAPI middleware"],
            "target_files": ["src/auth.py"],
            "category": "backend",
            "pr_url": "https://github.com/org/repo/pull/10",
        },
    )
    plan_data = plan_res.structured_content
    assert plan_data["status"] == "created"
    assert plan_data["plan_status"] == "discussed"
    assert "active/backend" in plan_data["file_path"]

    # 2. Согласование плана
    update_res = await server.call_tool(
        "update_plan",
        {"task_id": "AUTH-42", "status": "approved"},
    )
    assert update_res.structured_content["plan_status"] == "approved"

    # 3. Закрытие 1-го шага
    step1_res = await server.call_tool(
        "step_plan",
        {"task_id": "AUTH-42", "step": 1, "note": "TokenProvider unit tested"},
    )
    step1_data = step1_res.structured_content
    assert step1_data["progress"] == "1/2"
    assert step1_data["status_changed"] is True
    assert step1_data["new_plan_status"] == "in_progress"

    # 4. Закрытие 2-го шага -> авто-переход в done и перенос в completed/
    step2_res = await server.call_tool(
        "step_plan",
        {"task_id": "AUTH-42", "step": 2, "note": "Middleware registered"},
    )
    step2_data = step2_res.structured_content
    assert step2_data["progress"] == "2/2"
    assert step2_data["status_changed"] is True
    assert step2_data["new_plan_status"] == "done"
    assert "completed/backend" in step2_data["file_path"]
    assert Path(step2_data["file_path"]).exists()

    # 5. Откат шага 2 -> возврат в in_progress и active/
    rollback_res = await server.call_tool(
        "step_plan",
        {"task_id": "AUTH-42", "step": 2, "status": "open"},
    )
    rb_data = rollback_res.structured_content
    assert rb_data["progress"] == "1/2"
    assert rb_data["status_changed"] is True
    assert rb_data["new_plan_status"] == "in_progress"
    assert "active/backend" in rb_data["file_path"]
    assert Path(rb_data["file_path"]).exists()

    # 6. Повторное закрытие шага 2
    step2_again = await server.call_tool(
        "step_plan",
        {"task_id": "AUTH-42", "step": 2, "status": "done"},
    )
    assert step2_again.structured_content["new_plan_status"] == "done"

    # 7. Фиксация код-ревью
    rev_res = await server.call_tool(
        "record_review",
        {
            "task_id": "AUTH-42",
            "pr_url": "https://github.com/org/repo/pull/10",
            "verdict": "approved",
            "summary": "Everything looks solid and clean",
            "comments": [],
            "category": "backend",
        },
    )
    rev_data = rev_res.structured_content
    assert rev_data["status"] == "created"
    assert rev_data["verdict"] == "approved"
    assert Path(rev_data["file_path"]).exists()

    # 8. Создание техдолга, связанного с задачей
    debt_res = await server.call_tool(
        "record_debt",
        {
            "title": "Token refresh throttle",
            "category": "backend",
            "symptom": "Rapid refresh causes high DB load",
            "task_id": "AUTH-42",
        },
    )
    assert debt_res.structured_content["status"] == "created"

    # 9. Получение контекста задачи (get_task_context)
    ctx_res = await server.call_tool(
        "get_task_context",
        {"task_id": "AUTH-42", "view": "summary"},
    )
    ctx = ctx_res.structured_content
    assert ctx["found"] is True
    assert ctx["task_id"] == "AUTH-42"
    assert ctx["plan"]["title"] == "OAuth2 Implementation"
    assert ctx["plan"]["status"] == "done"
    assert len(ctx["reviews"]) == 1
    assert ctx["reviews"][0]["verdict"] == "approved"
    assert len(ctx["debts"]) == 1
    assert ctx["debts"][0]["task_id"] == "AUTH-42"
    assert ctx["debts"][0]["title"] == "Token refresh throttle"

    # 10. Поиск по артефактам (search_artifacts)
    search_res = await server.call_tool(
        "search_artifacts",
        {"query": "OAuth2", "artifact_type": "all"},
    )
    search_data = search_res.structured_content
    assert search_data["count"] >= 1
    assert any(a["task_id"] == "AUTH-42" for a in search_data["results"])

    # 11. Поиск конкретно по долгам: artifact_type="debt"
    debt_search_res = await server.call_tool(
        "search_artifacts",
        {"query": "throttle", "artifact_type": "debt"},
    )
    debt_search_data = debt_search_res.structured_content
    assert debt_search_data["count"] >= 1
    debt_item = debt_search_data["results"][0]
    assert debt_item["artifact_type"] == "debt"
    assert debt_item["task_id"] == "AUTH-42"
    assert isinstance(debt_item["task_id"], str)

    # 12. State Machine: повторное ревью с CHANGES_REQUESTED откатывает план в IN_PROGRESS
    rev2_res = await server.call_tool(
        "record_review",
        {
            "task_id": "AUTH-42",
            "category": "backend",
            "verdict": "changes_requested",
            "summary": "Fix rate limit edge cases",
            "comments": [
                {
                    "file_path": "src/auth/token.py",
                    "symptom": "Token expiry boundary issue",
                }
            ],
        },
    )
    assert rev2_res.structured_content["verdict"] == "changes_requested"

    # Проверяем, что план откатился в in_progress
    ctx_res2 = await server.call_tool(
        "get_task_context",
        {"task_id": "AUTH-42", "view": "summary"},
    )
    assert ctx_res2.structured_content["plan"]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_audit_artifacts_file_id_change_reindex(test_project_root: Path) -> None:
    """Проверка, что смена artifact_id в YAML frontmatter не вызывает UNIQUE constraint failed: file_path."""
    from mcp_audit.server import get_storage, get_synchronizer

    server = create_server(project_root=test_project_root)
    storage = get_storage(test_project_root)
    synchronizer = get_synchronizer(project_root=test_project_root)

    # 1. Создаем план
    create_res = await server.call_tool(
        "record_plan",
        {
            "task_id": "TASK-RENAME",
            "title": "Initial Plan Title",
            "category": "backend",
            "steps": ["Step 1"],
        },
    )
    plan_path = Path(create_res.structured_content["file_path"])
    assert plan_path.exists()

    initial_id = create_res.structured_content["id"]
    new_id = f"{initial_id}-v2"

    # 2. Имитируем ручное редактирование ID в файле
    content = plan_path.read_text(encoding="utf-8")
    updated_content = content.replace(f"id: {initial_id}", f"id: {new_id}")
    plan_path.write_text(updated_content, encoding="utf-8")

    # 3. Синхронизация не должна падать с IntegrityError
    indexed_plans = synchronizer.sync_artifacts()
    assert indexed_plans >= 1

    # Поиск находит обновленный ID
    found_path = storage.find_artifact_path(new_id)
    assert found_path is not None
    assert str(plan_path.resolve()) == str(Path(found_path).resolve())


@pytest.mark.asyncio
async def test_search_artifacts_by_task_id_exact_matching(test_project_root: Path) -> None:
    """A-ARTIFACTS (479a):
    1. На корпусе из двух task_id запрос по целевому task_id возвращает точные ID его плана и ревью,
       исключая план и ревью другого task_id.
    2. Запрос по уникальному русскому заголовку возвращает только целевой артефакт.
    3. Сравниваются множества ID и шумовые исключения, не count > 0.
    4. Старый неизменённый Markdown переиндексируется после апгрейда схемы (mtime_ns = -1).
    """
    server = create_server(project_root=test_project_root)

    # 1. Создаем артефакты для задачи A
    plan_a_res = await server.call_tool(
        "record_plan",
        {
            "task_id": "TASK-AAA-111",
            "title": "Реализация двухфакторной аутентификации",
            "steps": ["Настройка TOTP генератора"],
            "category": "backend",
        },
    )
    plan_a_id = plan_a_res.structured_content["id"]

    rev_a_res = await server.call_tool(
        "record_review",
        {
            "task_id": "TASK-AAA-111",
            "category": "backend",
            "verdict": "approved",
            "summary": "Проверка безопасности входа",
            "comments": [],
        },
    )
    rev_a_id = rev_a_res.structured_content["id"]

    # 2. Создаем артефакты для задачи B
    plan_b_res = await server.call_tool(
        "record_plan",
        {
            "task_id": "TASK-BBB-222",
            "title": "Оптимизация распределённого кэша",
            "steps": ["Инвалидация Redis ключей"],
            "category": "backend",
        },
    )
    plan_b_id = plan_b_res.structured_content["id"]

    rev_b_res = await server.call_tool(
        "record_review",
        {
            "task_id": "TASK-BBB-222",
            "category": "backend",
            "verdict": "approved",
            "summary": "Анализ сетевого буфера",
            "comments": [],
        },
    )
    rev_b_id = rev_b_res.structured_content["id"]

    # 3. Поиск по task_id задачи A: должен вернуть план A и ревью A, исключая артефакты задачи B
    search_a = await server.call_tool(
        "search_artifacts",
        {"query": "TASK-AAA-111", "artifact_type": "all"},
    )
    found_a_ids = {item["id"] for item in search_a.structured_content["results"]}
    assert found_a_ids == {plan_a_id, rev_a_id}, (
        f"Ожидались {[plan_a_id, rev_a_id]}, получено: {found_a_ids}"
    )
    assert plan_b_id not in found_a_ids
    assert rev_b_id not in found_a_ids

    # 4. Поиск по task_id задачи B: должен вернуть план B и ревью B, исключая артефакты задачи A
    search_b = await server.call_tool(
        "search_artifacts",
        {"query": "TASK-BBB-222", "artifact_type": "all"},
    )
    found_b_ids = {item["id"] for item in search_b.structured_content["results"]}
    assert found_b_ids == {plan_b_id, rev_b_id}, (
        f"Ожидались {[plan_b_id, rev_b_id]}, получено: {found_b_ids}"
    )
    assert plan_a_id not in found_b_ids
    assert rev_a_id not in found_b_ids

    # 5. Поиск по уникальному русскому заголовку возвращает только целевой артефакт
    search_ru_a = await server.call_tool(
        "search_artifacts",
        {"query": "двухфакторной", "artifact_type": "plan"},
    )
    ru_a_ids = {item["id"] for item in search_ru_a.structured_content["results"]}
    assert ru_a_ids == {plan_a_id}

    search_ru_b = await server.call_tool(
        "search_artifacts",
        {"query": "распределённого", "artifact_type": "plan"},
    )
    ru_b_ids = {item["id"] for item in search_ru_b.structured_content["results"]}
    assert ru_b_ids == {plan_b_id}

    # 6. Проверка переиндексации неизменённых Markdown-артефактов после rebuild (mtime_ns = -1)
    from mcp_audit.server import get_storage, get_synchronizer

    storage = get_storage(test_project_root)
    synchronizer = get_synchronizer(project_root=test_project_root)

    # Имитируем ситуацию до обновления: в FTS очищены записи или mtime_ns выставлен в -1
    with storage.get_connection() as conn:
        conn.execute("UPDATE artifacts_file_sync SET mtime_ns = -1;")
        conn.commit()

    # Запускаем синхронизацию: неизменённые файлы должны быть повторно проиндексированы
    reindexed_count = synchronizer.sync_artifacts()
    assert reindexed_count >= 4  # 2 плана + 2 ревью

    # Проверяем, что поиск по-прежнему стабильно находит целевые ID
    search_after_reindex = await server.call_tool(
        "search_artifacts",
        {"query": "TASK-AAA-111", "artifact_type": "all"},
    )
    after_ids = {item["id"] for item in search_after_reindex.structured_content["results"]}
    assert after_ids == {plan_a_id, rev_a_id}


@pytest.mark.asyncio
async def test_update_plan_prefix_matching_does_not_mutate_v2_plan(test_project_root: Path) -> None:
    """aud-backend-0f00: update_plan(task_1) не должен выбирать task_1_v2 при совпадении префикса."""
    server = create_server(project_root=test_project_root)

    # 1. Создаем первый план (TASK-ALPHA)
    p1_res = await server.call_tool(
        "record_plan",
        {
            "task_id": "TASK-ALPHA",
            "title": "Первая версия плана",
            "steps": ["Шаг 1"],
            "category": "backend",
        },
    )
    assert p1_res.structured_content["plan_status"] == "discussed"

    # 2. Создаем второй план с префиксом первого (TASK-ALPHA-V2)
    p2_res = await server.call_tool(
        "record_plan",
        {
            "task_id": "TASK-ALPHA-V2",
            "title": "Вторая версия плана",
            "steps": ["Шаг 1 V2"],
            "category": "backend",
        },
    )
    assert p2_res.structured_content["plan_status"] == "discussed"

    # 3. Вызываем update_plan для TASK-ALPHA
    up_res = await server.call_tool(
        "update_plan",
        {
            "task_id": "TASK-ALPHA",
            "category": "backend",
            "status": "approved",
        },
    )
    assert up_res.structured_content["id"] == "plan-backend-TASK-ALPHA"
    assert up_res.structured_content["plan_status"] == "approved"

    # 4. Проверяем, что TASK-ALPHA-V2 остался в статусе discussed
    ctx_v2 = await server.call_tool(
        "get_task_context",
        {"task_id": "TASK-ALPHA-V2", "view": "summary"},
    )
    assert ctx_v2.structured_content["plan"]["status"] == "discussed"
    assert ctx_v2.structured_content["plan"]["id"] == "plan-backend-TASK-ALPHA-V2"
