"""Тесты для MCP stdio сервера audit-engine."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import mcp_audit.server as server_module
from mcp_audit.models import AuditIssue, AuditPriority, AuditStatus
from mcp_audit.server import (
    check_known_debt,
    create_server,
    get_ready_backlog,
    record_debt,
    search_debt,
    update_debt_status,
)
from mcp_audit.storage import AuditStorage
from mcp_audit.synchronizer import AuditSynchronizer


@pytest.fixture
def temp_audit_server_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, AuditStorage, AuditSynchronizer]:
    """Изолирует окружение audit-engine на временный каталог."""
    audit_dir = tmp_path / ".agents" / "audit"
    db_path = tmp_path / ".agents" / ".cache" / "audit_fts.db"

    storage = AuditStorage(db_path)
    synchronizer = AuditSynchronizer(storage, audit_dir=audit_dir)

    monkeypatch.setattr(server_module, "_STORAGE", storage)
    monkeypatch.setattr(server_module, "_STORAGE_ROOT", tmp_path.resolve())
    monkeypatch.setattr(server_module, "_SYNCHRONIZER", synchronizer)
    monkeypatch.setattr(server_module, "_SYNCHRONIZER_ROOT", tmp_path.resolve())

    return audit_dir, storage, synchronizer


@pytest.mark.asyncio
async def test_audit_tools_registration(
    temp_audit_server_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Проверяет регистрацию инструментов аудита в MCPServer."""
    audit_dir, storage, synchronizer = temp_audit_server_env
    server = create_server(audit_dir=audit_dir, synchronizer=synchronizer, storage=storage)
    tools = await server.list_tools()
    tool_names = {t.name for t in tools}

    assert any("check_known_debt" in name for name in tool_names)
    assert any("get_ready_backlog" in name for name in tool_names)
    assert any("search_debt" in name for name in tool_names)
    assert any("record_debt" in name for name in tool_names)
    assert any("update_debt_status" in name for name in tool_names)


def test_record_and_search_debt(
    temp_audit_server_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Проверяет record_debt и последующий search_debt."""
    res_rec = record_debt(
        title="Handle SQLite busy lock retry",
        category="database",
        priority="critical",
        symptom="sqlite3.OperationalError: database is locked",
        root_cause="Concurrent writers on default WAL timeout",
        solution="Set busy_timeout=5000 and exponential backoff retry",
        target_file="src/mcp_core/db.py",
        symbol="get_connection",
    )

    assert res_rec["status"] == "created"
    assert res_rec["id"].startswith("aud-database-")
    assert Path(res_rec["file_path"]).exists()

    # Поиск по симптому
    res_search = search_debt(query="OperationalError database is locked")
    assert res_search["count"] == 1
    assert res_search["results"][0]["id"] == res_rec["id"]


def test_check_known_debt_tool(
    temp_audit_server_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Проверяет работу check_known_debt через tool API."""
    res_rec = record_debt(
        title="Uncaught promise rejection on disconnect",
        category="ui",
        priority="important",
        symptom="Uncaught PromiseRejection on socket disconnect",
        target_file="src/ui/socket.ts",
        symbol="SocketClient.connect",
    )

    # 1. Проверяем тот же симптом и тот же файл
    chk1 = check_known_debt(
        symptom="Uncaught PromiseRejection on socket disconnect",
        file_path="frontend/src/ui/socket.ts",
    )
    assert chk1["found"] is True
    assert chk1["id"] == res_rec["id"]
    assert chk1["is_known_debt"] is True
    assert chk1["is_regression"] is False

    # 2. Проверяем для другого файла (должен отклонить совпадение)
    chk2 = check_known_debt(
        symptom="Uncaught PromiseRejection on socket disconnect",
        file_path="frontend/src/ui/navbar.ts",
    )
    assert chk2["found"] is False


def test_get_ready_backlog_dag_resolution(
    temp_audit_server_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Проверяет получение готовых задач с учетом графа зависимостей."""
    # Задача A (без блокеров)
    rec_a = record_debt(
        title="Base database migration",
        category="backend",
        priority="critical",
        symptom="Missing schema migration runner",
    )
    # Задача B (заблокирована задачей A)
    rec_b = record_debt(
        title="User profile table indexing",
        category="backend",
        priority="critical",
        symptom="Slow query on user profile",
        blocked_by=[rec_a["id"]],
    )

    # Изначально готова только A
    backlog_1 = get_ready_backlog(category="backend")
    assert backlog_1["count"] == 1
    assert backlog_1["ready_issues"][0]["id"] == rec_a["id"]

    # Переводим A в completed
    update_debt_status(id=rec_a["id"], status="completed")

    # Теперь задача B готова!
    backlog_2 = get_ready_backlog(category="backend")
    assert backlog_2["count"] == 1
    assert backlog_2["ready_issues"][0]["id"] == rec_b["id"]


def test_update_debt_status_tool(
    temp_audit_server_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Проверяет update_debt_status инструмент."""
    rec = record_debt(
        title="Clean up legacy temp files",
        category="core",
        priority="suggestion",
        symptom="Temp directory fills up over time",
    )

    res_upd = update_debt_status(
        id=rec["id"],
        status="completed",
        resolution_note="Added daily cleanup cron task",
        pr_url="https://github.com/org/repo/pull/99",
    )

    assert res_upd["status"] == "updated"
    assert res_upd["new_status"] == "completed"
    assert "done/core" in res_upd["file_path"]


def test_check_known_debt_obsolete_status_no_regression(
    temp_audit_server_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Проверяет, что статус obsolete НЕ считается регрессией."""
    rec = record_debt(
        title="Legacy Python 2 compatibility layer",
        category="core",
        symptom="Python 2 syntax checks in CI",
    )
    update_debt_status(id=rec["id"], status="obsolete", resolution_note="Dropped Python 2 support")

    chk = check_known_debt(symptom="Python 2 syntax checks in CI", category="core")
    assert chk["found"] is True
    assert chk["is_regression"] is False
    assert chk["is_known_debt"] is False
    assert "неактуальной (obsolete)" in chk["message"]


def test_record_debt_unknown_blocker_rejected(
    temp_audit_server_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Проверяет отклонение неизвестных ID в blocked_by."""
    with pytest.raises(ValueError, match="Указаны несуществующие ID в blocked_by"):
        record_debt(
            title="Dependent task",
            category="backend",
            symptom="Waiting for non-existent task",
            blocked_by=["aud-backend-fake99"],
        )


def test_get_ready_backlog_cross_category_blocker(
    temp_audit_server_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Проверяет корректность вычисления готовности при кросс-категорийных блокерах (H3)."""
    _, storage, synchronizer = temp_audit_server_env

    # 1. Задача в database закрыта
    db_issue = AuditIssue(
        id="aud-database-001",
        title="DB Migration",
        category="database",
        symptom="Need migration",
        status=AuditStatus.COMPLETED,
    )
    synchronizer.save_and_index_card(db_issue)

    # 2. Задача в backend заблокирована задачей из database
    backend_issue = AuditIssue(
        id="aud-backend-001",
        title="Backend API",
        category="backend",
        symptom="Need API",
        status=AuditStatus.PLANNED,
        blocked_by=["aud-database-001"],
    )
    synchronizer.save_and_index_card(backend_issue)

    # 3. get_ready_backlog с фильтром по backend должен вернуть backend_issue
    ready = get_ready_backlog(category="backend")
    assert ready["count"] == 1
    assert ready["ready_issues"][0]["id"] == "aud-backend-001"


def test_record_debt_with_unrelated_cycle_in_store_succeeds(
    temp_audit_server_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Проверяет, что предсуществующий цикл в базе не блокирует создание несвязанной карточки (H4)."""
    _, storage, synchronizer = temp_audit_server_env
    rec_a = record_debt(title="Task A", category="backend", symptom="A")

    fake_cycle_issue = AuditIssue(
        id=rec_a["id"],
        title="Task A modified",
        category="backend",
        symptom="A",
        status=AuditStatus.PLANNED,
        blocked_by=["aud-backend-cycle"],
    )
    fake_cycle_issue2 = AuditIssue(
        id="aud-backend-cycle",
        title="Task Cycle",
        category="backend",
        symptom="Cycle",
        status=AuditStatus.PLANNED,
        blocked_by=[rec_a["id"]],
    )
    synchronizer.save_and_index_card(fake_cycle_issue)
    synchronizer.save_and_index_card(fake_cycle_issue2)

    # Несвязанная новая карточка должна создаваться успешно
    res = record_debt(
        title="Task New",
        category="backend",
        symptom="New",
        blocked_by=[rec_a["id"]],
    )
    assert res["status"] == "created"


def test_record_debt_cycle_rejected(
    monkeypatch: pytest.MonkeyPatch,
    temp_audit_server_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Проверяет отклонение при замыкании цикла зависимостей новой карточкой (H4)."""
    _, storage, synchronizer = temp_audit_server_env
    existing_issue = AuditIssue(
        id="aud-backend-001",
        title="Task 1",
        category="backend",
        symptom="Symptom 1",
        status=AuditStatus.PLANNED,
        blocked_by=["aud-backend-future"],
    )
    synchronizer.save_and_index_card(existing_issue)

    monkeypatch.setattr(server_module, "generate_audit_id", lambda *a, **kw: "aud-backend-future")

    with pytest.raises(ValueError, match="Обнаружен цикл зависимостей в blocked_by"):
        record_debt(
            title="Task Future",
            category="backend",
            symptom="Closing cycle",
            blocked_by=["aud-backend-001"],
        )


def test_record_debt_invalid_category_rejected(
    temp_audit_server_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Проверяет отклонение недопустимого category (path traversal / недопустимые символы)."""
    with pytest.raises(ValueError, match="Недопустимое имя категории"):
        record_debt(
            title="Traversing category",
            category="../../etc",
            symptom="Trying path traversal in category",
        )


def test_search_debt_limit_clamping(
    temp_audit_server_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Проверяет клампинг параметра limit в search_debt (1..100, <=0 -> 10) (M6)."""
    _, storage, synchronizer = temp_audit_server_env
    for i in range(15):
        record_debt(
            title=f"Sample issue {i}",
            category="backend",
            symptom=f"Issue symptom {i}",
        )

    # 1. limit <= 0 -> default 10
    res_zero = search_debt(query="Issue", limit=0)
    assert res_zero["count"] == 10

    # 2. limit negative -> default 10
    res_neg = search_debt(query="Issue", limit=-5)
    assert res_neg["count"] == 10

    # 3. limit=3 -> 3
    res_three = search_debt(query="Issue", limit=3)
    assert res_three["count"] == 3


# ---------------------------------------------------------------------------
# A-DEBT (1191, 4044): Валидация полей и дедупликация карточек долга
# ---------------------------------------------------------------------------


def test_record_debt_empty_title_rejected(
    temp_audit_server_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """record_debt отвергает пустой или пробельный заголовок."""
    with pytest.raises(ValueError, match="Заголовок"):
        record_debt(title="", category="backend", symptom="Some symptom")

    with pytest.raises(ValueError, match="Заголовок"):
        record_debt(title="   \t\n  ", category="backend", symptom="Some symptom")


def test_record_debt_unknown_priority_rejected(
    temp_audit_server_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Неизвестный priority отвергается ошибкой валидации."""
    with pytest.raises(ValueError, match="Invalid priority"):
        record_debt(title="Fix bug", priority="unknown_prio", symptom="Some symptom")


def test_record_debt_target_file_validation(
    temp_audit_server_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Валидация target_file: запрет абсолютных путей, traversal за корень и контрольных символов."""
    # 1. Абсолютный POSIX путь
    with pytest.raises(ValueError, match="target_file"):
        record_debt(title="Fix bug", target_file="/etc/passwd", symptom="symptom")

    # 2. Абсолютный Windows путь
    with pytest.raises(ValueError, match="target_file"):
        record_debt(title="Fix bug", target_file="C:\\Windows\\System32", symptom="symptom")

    # 3. Traversal за корень
    with pytest.raises(ValueError, match="target_file"):
        record_debt(title="Fix bug", target_file="../../secret.py", symptom="symptom")

    # 4. Управляющие символы
    with pytest.raises(ValueError, match="target_file"):
        record_debt(title="Fix bug", target_file="src/\x00null.py", symptom="symptom")

    # 5. Относительный путь внутри проекта разрешен
    res = record_debt(title="Valid file", target_file="src/mcp_audit/server.py", symptom="symptom")
    assert res["status"] == "created"


def test_record_debt_target_file_windows_separators_and_canonicalization(
    temp_audit_server_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Ревью шага 3 (п. 4): Windows-разделители и канонизация пути.

    - ``..\\\\..\\\\etc\\\\passwd`` (Windows-строка) должна блокироваться и на POSIX;
    - ``src/x.py``, ``./src/x.py``, ``foo/../src/x.py``, ``src\\\\x.py`` —
      одна и та же карточка после нормализации (не три дубля);
    - нормализованный путь сохраняется как POSIX-строка.
    """
    # 1. Windows-traversal блокируется на POSIX
    with pytest.raises(ValueError, match="target_file"):
        record_debt(title="Fix bug", target_file="..\\..\\etc\\passwd", symptom="symptom")

    # 2. Канонизация: все варианты одного пути дают один target_file
    res1 = record_debt(title="Canonical one", target_file="src/x.py", symptom="s1")
    assert res1["issue"]["target_file"] == "src/x.py"

    res2 = record_debt(title="Canonical two", target_file="./src/x.py", symptom="s2", force=True)
    assert res2["issue"]["target_file"] == "src/x.py"

    res3 = record_debt(
        title="Canonical three", target_file="foo/../src/x.py", symptom="s3", force=True
    )
    assert res3["issue"]["target_file"] == "src/x.py"

    res4 = record_debt(title="Canonical four", target_file="src\\x.py", symptom="s4", force=True)
    assert res4["issue"]["target_file"] == "src/x.py"

    # 3. Дубликаты одной сигнатуры детектируются (канонизированный путь един)
    with pytest.raises(Exception, match="duplicate|Duplicate|дубл"):
        record_debt(
            title="Canonical one",
            target_file="src\\x.py",
            symptom="s1",
        )


def test_record_debt_symbol_validation(
    temp_audit_server_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Валидация symbol: запрет пустых строк и управляющих символов; поддержка Class.method / crate::item."""
    # 1. Пустая строка
    with pytest.raises(ValueError, match="symbol"):
        record_debt(title="Fix bug", symbol="   ", symptom="symptom")

    # 2. Управляющие символы
    with pytest.raises(ValueError, match="symbol"):
        record_debt(title="Fix bug", symbol="bad\nsymbol", symptom="symptom")

    # 3. Допустимый синтаксис
    res1 = record_debt(title="Method symbol", symbol="Class.method", symptom="s1")
    assert res1["status"] == "created"

    res2 = record_debt(title="Crate symbol", symbol="crate::item::sub", symptom="s2")
    assert res2["status"] == "created"


def test_record_debt_different_symptoms_same_symbol_not_duplicate(
    temp_audit_server_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Разные симптомы в одном файле/символе остаются разными карточками."""
    res1 = record_debt(
        title="Crash on init",
        category="backend",
        target_file="src/api.py",
        symbol="init_app",
        symptom="SIGSEGV on memory allocation",
    )
    res2 = record_debt(
        title="Slow startup",
        category="backend",
        target_file="src/api.py",
        symbol="init_app",
        symptom="Timeout 30s while connecting to external service",
    )
    assert res1["id"] != res2["id"]
    assert res1["status"] == "created"
    assert res2["status"] == "created"


def test_record_debt_exact_duplicate_rejected_and_force_flag(
    temp_audit_server_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Точный повтор (title, symptom, target_file, symbol) возвращает DUPLICATE_FOUND; force обходит дедуп."""
    from mcp_audit.server import DuplicateDebtError

    # 1. Создаем первую карточку
    res1 = record_debt(
        title="Uncaught exception in worker",
        category="backend",
        target_file="src/worker.py",
        symbol="Worker.run",
        symptom="Unhandled ZeroDivisionError in worker loop",
    )
    card1_id = res1["id"]
    # 2. Повторная попытка без force выбрасывает DuplicateDebtError с ID существующей карточки
    with pytest.raises(DuplicateDebtError) as exc_info:
        record_debt(
            title="Uncaught exception in worker",
            category="backend",
            target_file="src/worker.py",
            symbol="Worker.run",
            symptom="Unhandled ZeroDivisionError in worker loop",
        )
    assert exc_info.value.existing_id == card1_id

    # 3. С force=True дедуп обходится и карточка создается
    res_force = record_debt(
        title="Uncaught exception in worker",
        category="backend",
        target_file="src/worker.py",
        symbol="Worker.run",
        symptom="Unhandled ZeroDivisionError in worker loop",
        force=True,
    )
    assert res_force["status"] == "created"
    assert res_force["id"] != card1_id

    # 4. Завершённая карточка не блокирует новую
    update_debt_status(id=card1_id, status="completed")
    update_debt_status(id=res_force["id"], status="completed")
    res_after_completed = record_debt(
        title="Uncaught exception in worker",
        category="backend",
        target_file="src/worker.py",
        symbol="Worker.run",
        symptom="Unhandled ZeroDivisionError in worker loop",
    )
    assert res_after_completed["status"] == "created"


def test_record_debt_dedup_normalizes_legacy_target_file(
    temp_audit_server_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Ревью r2: существующая карточка с ненормализованным target_file («./src/x.py»)
    блокирует новую с каноническим «src/x.py» — дубль не создаётся."""
    from mcp_audit.server import DuplicateDebtError
    from mcp_audit.synchronizer import AuditSynchronizer

    sync = temp_audit_server_env[2]

    # 1. Легаси-карточка: пишем напрямую в markdown с «./»-путём,
    #    минуя канонизацию record_debt (эмуляция карточки до фикса).
    from mcp_audit.models import AuditIssue, AuditStatus, AuditAutonomy, AuditComplexity

    legacy = AuditIssue(
        id="aud-backend-legacy",
        title="Legacy path card",
        category="backend",
        priority=AuditPriority.IMPORTANT,
        autonomy=AuditAutonomy.HIGH,
        complexity=AuditComplexity.M,
        status=AuditStatus.PLANNED,
        symptom="Legacy symptom text",
        target_file="./src/legacy.py",
        symbol="legacy_func",
    )
    sync.save_and_index_card(legacy)

    # 2. Новая карточка с тем же контентом, но каноническим путём → дубликат
    with pytest.raises(DuplicateDebtError) as exc_info:
        record_debt(
            title="Legacy path card",
            category="backend",
            target_file="src/legacy.py",
            symbol="legacy_func",
            symptom="Legacy symptom text",
        )
    assert exc_info.value.existing_id == "aud-backend-legacy"

    # 3. Обратное направление тоже детектируется: легаси с backslash
    sync.save_and_index_card(
        AuditIssue(
            id="aud-backend-legacy2",
            title="Backslash card",
            category="backend",
            priority=AuditPriority.IMPORTANT,
            autonomy=AuditAutonomy.HIGH,
            complexity=AuditComplexity.M,
            status=AuditStatus.PLANNED,
            symptom="Backslash symptom text",
            target_file="src\\win.py",
            symbol="win_func",
        )
    )
    with pytest.raises(DuplicateDebtError):
        record_debt(
            title="Backslash card",
            category="backend",
            target_file="src/win.py",
            symbol="win_func",
            symptom="Backslash symptom text",
        )


def test_record_debt_dedup_invalid_legacy_path_does_not_block_empty(
    temp_audit_server_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Ревью r4 (п. 4): невалидный target_file легаси-карточки («../../outside»)
    не блокирует новую карточку без target_file.

    Прежний _normalize_target_file_for_cmp превращал невалидный путь в "",
    и «пустой == пустой» давал ложный DuplicateDebtError.
    """
    from mcp_audit.models import (
        AuditAutonomy,
        AuditComplexity,
        AuditIssue,
        AuditPriority,
        AuditStatus,
    )

    sync = temp_audit_server_env[2]
    sync.save_and_index_card(
        AuditIssue(
            id="aud-backend-invalidpath",
            title="Invalid path card",
            category="backend",
            priority=AuditPriority.IMPORTANT,
            autonomy=AuditAutonomy.HIGH,
            complexity=AuditComplexity.M,
            status=AuditStatus.PLANNED,
            symptom="Some symptom text",
            target_file="../../outside",
            symbol="some_func",
        )
    )

    # Новая карточка БЕЗ target_file с тем же title/symptom/symbol — создаётся
    res = record_debt(
        title="Invalid path card",
        category="backend",
        target_file=None,
        symbol="some_func",
        symptom="Some symptom text",
    )
    assert res["status"] == "created"

    # И карточка с валидным target_file — тоже создаётся (не совпадает)
    res2 = record_debt(
        title="Invalid path card",
        category="backend",
        target_file="src/valid.py",
        symbol="some_func",
        symptom="Some symptom text",
    )
    assert res2["status"] == "created"


def test_record_debt_force_does_not_bypass_field_validation(
    temp_audit_server_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """force=True обходит только дедуп, но не отменяет валидацию полей."""
    with pytest.raises(ValueError, match="Заголовок"):
        record_debt(title="", force=True)

    with pytest.raises(ValueError, match="target_file"):
        record_debt(title="Valid title", target_file="/etc/hosts", force=True)


def test_record_debt_parallel_duplicate_returns_single_card(
    temp_audit_server_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Параллельный повтор под локом создаёт ровно одну карточку, остальные завершаются DuplicateDebtError."""
    from concurrent.futures import ThreadPoolExecutor
    from mcp_audit.server import DuplicateDebtError

    created_ids: list[str] = []
    duplicate_errors: list[str] = []

    def _worker() -> None:
        try:
            r = record_debt(
                title="Parallel Race Condition",
                category="backend",
                target_file="src/race.py",
                symbol="race_func",
                symptom="Race condition in parallel task processing",
            )
            created_ids.append(r["id"])
        except DuplicateDebtError as err:
            duplicate_errors.append(err.existing_id)

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(_worker) for _ in range(4)]
        for f in futures:
            f.result()

    assert len(created_ids) == 1
    assert len(duplicate_errors) == 3
    assert all(eid == created_ids[0] for eid in duplicate_errors)
