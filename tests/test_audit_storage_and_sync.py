"""Тесты хранилища SQLite FTS5 и синхронизатора файлов для mcp_audit."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from mcp_audit.models import (
    AuditAutonomy,
    AuditComplexity,
    AuditIssue,
    AuditPriority,
    AuditStatus,
)
from mcp_audit.storage import AuditStorage
from mcp_audit.synchronizer import AuditSynchronizer


@pytest.fixture
def temp_audit_env(tmp_path: Path) -> tuple[Path, AuditStorage, AuditSynchronizer]:
    """Создаёт временную изолированную среду для тестов хранилища и синхронизатора."""
    audit_dir = tmp_path / ".agents" / "audit"
    db_path = tmp_path / ".agents" / ".cache" / "audit_fts.db"

    storage = AuditStorage(db_path)
    synchronizer = AuditSynchronizer(storage, audit_dir=audit_dir)
    return audit_dir, storage, synchronizer


def test_audit_storage_crud_and_drift(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Проверяет CRUD операции в хранилище и инициализацию схемы."""
    _, storage, synchronizer = temp_audit_env

    # Сохраняем и индексируем задачу
    issue = AuditIssue(
        id="aud-backend-1a2b",
        title="Fix memory leak in buffer pool",
        category="backend",
        priority=AuditPriority.CRITICAL,
        autonomy=AuditAutonomy.HIGH,
        complexity=AuditComplexity.M,
        status=AuditStatus.PLANNED,
        blocked_by=[],
        target_file="src/backend/buffer.py",
        symbol="BufferPool.allocate",
        symptom="OOM error when allocating large buffers",
        root_cause="Buffers are not released after stream EOF",
        solution="Use weakref or explicit close hook",
    )
    file_path = synchronizer.save_and_index_card(issue)
    assert file_path.exists()
    assert "todo/backend" in str(file_path)

    # Проверяем получение по ID
    loaded = storage.get_issue("aud-backend-1a2b")
    assert loaded is not None
    assert loaded.id == "aud-backend-1a2b"
    assert loaded.title == "Fix memory leak in buffer pool"
    assert loaded.status == AuditStatus.PLANNED
    assert loaded.priority == AuditPriority.CRITICAL
    assert loaded.target_file == "src/backend/buffer.py"
    assert loaded.symbol == "BufferPool.allocate"

    # get_all_ids
    assert "aud-backend-1a2b" in storage.get_all_ids()

    # list_all_issues
    all_issues = storage.list_all_issues()
    assert len(all_issues) == 1
    assert all_issues[0].id == "aud-backend-1a2b"

    # list_all_issues с фильтром по категории
    assert len(storage.list_all_issues(category="backend")) == 1
    assert len(storage.list_all_issues(category="audio")) == 0


def test_audit_storage_search(temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer]) -> None:
    """Проверяет полнотекстовый поиск с ранжированием BM25."""
    _, storage, synchronizer = temp_audit_env

    issue1 = AuditIssue(
        id="aud-audio-1111",
        title="FFmpeg SIGTTIN freeze in subprocess",
        category="audio",
        priority=AuditPriority.CRITICAL,
        symptom="FFmpeg freezes when running in background subshell",
        root_cause="SIGTTIN signal when reading stdin",
        solution="Pass stdin=DEVNULL",
    )
    issue2 = AuditIssue(
        id="aud-backend-2222",
        title="Postgres connection pool exhaustion",
        category="backend",
        priority=AuditPriority.IMPORTANT,
        symptom="Timeout acquiring connection from pool under high concurrency",
        root_cause="Connections not returned on exception",
        solution="Wrap in try/finally or async context manager",
    )
    synchronizer.save_and_index_card(issue1)
    synchronizer.save_and_index_card(issue2)

    # Поиск по симптому FFmpeg
    res = storage.search_issues("SIGTTIN freeze")
    assert len(res) == 1
    assert res[0]["id"] == "aud-audio-1111"

    # Поиск по категории backend
    res_backend = storage.search_issues("connection pool", category="backend")
    assert len(res_backend) == 1
    assert res_backend[0]["id"] == "aud-backend-2222"

    # Поиск без совпадений
    res_empty = storage.search_issues("NonExistentKeywordXYZ")
    assert len(res_empty) == 0


def test_check_known_debt_open_and_regression(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Проверяет сценарии вызова check_known_debt (открытый долг vs регрессия)."""
    _, storage, synchronizer = temp_audit_env

    issue_open = AuditIssue(
        id="aud-desktop-a1b2",
        title="Tray icon click handler unhandled exception",
        category="desktop",
        priority=AuditPriority.IMPORTANT,
        status=AuditStatus.PLANNED,
        target_file="apps/desktop/tray.py",
        symbol="on_tray_click",
        symptom="Clicking tray icon causes UnhandledException in Qt event loop",
        solution="Wrap callback in try/except",
    )
    synchronizer.save_and_index_card(issue_open)

    # 1. Проверка открытого долга
    resp = storage.check_known_debt(
        symptom="UnhandledException in tray icon click",
        file_path="apps/desktop/tray.py",
        symbol="on_tray_click",
    )
    assert resp.found is True
    assert resp.id == "aud-desktop-a1b2"
    assert resp.is_known_debt is True
    assert resp.is_regression is False

    # 2. Переводим в COMPLETED и проверяем как регрессию
    synchronizer.update_issue_status("aud-desktop-a1b2", AuditStatus.COMPLETED)
    resp_reg = storage.check_known_debt(
        symptom="UnhandledException in tray icon click",
        file_path="apps/desktop/tray.py",
        symbol="on_tray_click",
    )
    assert resp_reg.found is True
    assert resp_reg.id == "aud-desktop-a1b2"
    assert resp_reg.is_known_debt is False
    assert resp_reg.is_regression is True
    assert "ВНИМАНИЕ: Регрессия!" in resp_reg.message


def test_check_known_debt_file_scope_matching(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Проверяет File Scope Matching: отклонение совпадения, если файлы разные."""
    _, storage, synchronizer = temp_audit_env

    issue = AuditIssue(
        id="aud-audio-8888",
        title="Hardware acceleration device detection crash",
        category="audio",
        status=AuditStatus.PLANNED,
        target_file="src/audio/hardware.py",
        symbol="detect_cuda",
        symptom="CUDA initialization crash on unsupported driver",
    )
    synchronizer.save_and_index_card(issue)

    # 1. Проверяем тот же симптом, но для другого файла routes.py -> должно быть отклонено!
    resp_diff_file = storage.check_known_debt(
        symptom="CUDA initialization crash on unsupported driver",
        file_path="src/web/routes.py",
    )
    assert resp_diff_file.found is False
    assert "привязаны к другим файлам" in resp_diff_file.message

    # 2. Проверяем с суффиксом того же файла -> должно совпасть
    resp_same_file = storage.check_known_debt(
        symptom="CUDA initialization crash",
        file_path="repo/src/audio/hardware.py",
    )
    assert resp_same_file.found is True
    assert resp_same_file.id == "aud-audio-8888"

    # 3. Проверяем с однокомпонентным именем файла -> должно совпасть
    resp_single_comp = storage.check_known_debt(
        symptom="CUDA initialization crash",
        file_path="hardware.py",
    )
    assert resp_single_comp.found is True
    assert resp_single_comp.id == "aud-audio-8888"


def test_synchronizer_file_moving_and_deletion(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Проверяет перемещение файлов между todo/ и done/ и очистку удаленных файлов."""
    _, storage, synchronizer = temp_audit_env

    issue = AuditIssue(
        id="aud-backend-3333",
        title="Rate limiter redis timeout",
        category="backend",
        status=AuditStatus.PLANNED,
        symptom="Redis timeout in rate limiter",
    )
    file_path = synchronizer.save_and_index_card(issue)
    assert file_path.exists()
    assert "todo/backend" in str(file_path)

    # Находим файл
    found_path = synchronizer.find_card_file("aud-backend-3333")
    assert found_path is not None
    assert found_path.resolve() == file_path.resolve()

    # Перемещаем в completed
    updated_issue, new_path = synchronizer.update_issue_status(
        "aud-backend-3333",
        new_status=AuditStatus.COMPLETED,
        pr_url="https://github.com/org/repo/pull/42",
        resolution_note="Added fallback in-memory token bucket",
    )
    assert updated_issue.status == AuditStatus.COMPLETED
    assert updated_issue.pr_url == "https://github.com/org/repo/pull/42"
    assert new_path.exists()
    assert "done/backend" in str(new_path)
    assert not file_path.exists()

    # Проверяем в базе
    in_db = storage.get_issue("aud-backend-3333")
    assert in_db is not None
    assert in_db.status == AuditStatus.COMPLETED

    # Удаляем файл с диска и запускаем sync() -> должно удалиться из FTS
    new_path.unlink()
    synchronizer.sync()
    assert storage.get_issue("aud-backend-3333") is None


def test_row_to_issue_maps_row_to_model(temp_audit_env) -> None:
    """_row_to_issue собирает AuditIssue из сырой строки audit_index с enum-конвертацией."""
    _, storage, _ = temp_audit_env

    row = (
        "aud-x-1",
        "T",
        "S",
        "RC",
        "SOL",
        "backend",
        "in_progress",
        "critical",
        '["aud-x-2"]',
        "src/b.py",
        "fn",
    )
    issue = AuditStorage._row_to_issue(row)
    assert issue.id == "aud-x-1"
    assert issue.title == "T"
    assert issue.symptom == "S"
    assert issue.root_cause == "RC"
    assert issue.solution == "SOL"
    assert issue.category == "backend"
    assert issue.status == AuditStatus.IN_PROGRESS
    assert issue.priority == AuditPriority.CRITICAL
    assert issue.blocked_by == ["aud-x-2"]
    assert issue.target_file == "src/b.py"
    assert issue.symbol == "fn"


def test_row_to_issue_handles_empty_blocked_by(temp_audit_env) -> None:
    """_row_to_issue возвращает пустой список blocked_by при NULL в колонке."""
    row = (
        "aud-x-1",
        "T",
        "S",
        "RC",
        "SOL",
        "backend",
        "planned",
        "important",
        None,
        None,
        None,
    )
    issue = AuditStorage._row_to_issue(row)
    assert issue.blocked_by == []


def test_row_to_dict_keeps_raw_status_priority(temp_audit_env) -> None:
    """_row_to_dict возвращает 11 ключей со статусом и приоритетом как сырыми строками."""
    row = (
        "aud-x-1",
        "T",
        "S",
        "RC",
        "SOL",
        "backend",
        "completed",
        "suggestion",
        "[]",
        "src/b.py",
        "fn",
    )
    d = AuditStorage._row_to_dict(row)
    assert set(d.keys()) == {
        "id",
        "title",
        "symptom",
        "root_cause",
        "solution",
        "category",
        "status",
        "priority",
        "blocked_by",
        "target_file",
        "symbol",
    }
    assert d["category"] == "backend"
    assert "domain" not in d
    assert d["status"] == "completed"
    assert d["priority"] == "suggestion"
    assert d["blocked_by"] == []


def test_matches_file_scope_two_component_suffix(temp_audit_env) -> None:
    """_matches_file_scope совпадает по 2-компонентному суффиксу путей."""
    assert AuditStorage._matches_file_scope("src/old/mod.py", "src/new/mod.py") is False
    assert AuditStorage._matches_file_scope("src/mod.py", "src/mod.py") is True
    assert AuditStorage._matches_file_scope("a/mod.py", "b/mod.py") is False


def test_matches_file_scope_single_component_fallback(temp_audit_env) -> None:
    """_matches_file_scope сравнивает только имена файлов, когда путь из 1 компонента."""
    # У query 1 компонент -> fallback на сравнение имён
    assert AuditStorage._matches_file_scope("src/b.py", "b.py") is True
    # У card 1 компонент -> fallback на сравнение имён
    assert AuditStorage._matches_file_scope("b.py", "src/b.py") is True
    assert AuditStorage._matches_file_scope("b.py", "c.py") is False


def test_target_dir_routes_by_status(temp_audit_env) -> None:
    """_target_dir выбирает done/<category> при COMPLETED и todo/<category> в остальных случаях."""
    audit_dir, _, synchronizer = temp_audit_env

    done = synchronizer._target_dir(AuditStatus.COMPLETED, "backend")
    todo = synchronizer._target_dir(AuditStatus.PLANNED, "backend")
    in_prog = synchronizer._target_dir(AuditStatus.IN_PROGRESS, "backend")

    assert done == audit_dir / "done" / "backend"
    assert todo == audit_dir / "todo" / "backend"
    assert in_prog == audit_dir / "todo" / "backend"


def test_storage_sync_methods_roundtrip(temp_audit_env) -> None:
    """index_card/get_sync_state/find_card_path/drop_path образуют целостный цикл."""
    _, storage, _ = temp_audit_env

    issue = AuditIssue(
        id="aud-x-9",
        title="T",
        category="backend",
        status=AuditStatus.PLANNED,
        symptom="S",
    )
    p = "/fake/some/card.md"
    storage.index_card(issue, file_path=p, mtime=1.5, size=100, location="todo")

    state = storage.get_sync_state()
    assert state[p] == (1.5, 100, "aud-x-9")

    assert storage.find_card_path("aud-x-9") == p
    assert storage.find_card_path("aud-x-none") is None

    # drop_path с card_id удаляет и карточку, и путь
    storage.drop_path(p, card_id="aud-x-9")
    assert storage.find_card_path("aud-x-9") is None
    assert storage.get_issue("aud-x-9") is None
    assert storage.get_sync_state() == {}


def test_storage_drop_path_without_card_keeps_index(temp_audit_env) -> None:
    """drop_path без card_id удаляет только file_sync-запись, карточка остаётся в audit_index."""
    _, storage, _ = temp_audit_env

    issue = AuditIssue(
        id="aud-x-10",
        title="T",
        category="backend",
        status=AuditStatus.PLANNED,
        symptom="S",
    )
    storage.index_card(issue, file_path="/fake/old.md", mtime=1.0, size=10, location="todo")

    storage.drop_path("/fake/old.md")
    assert storage.find_card_path("aud-x-10") is None
    assert storage.get_issue("aud-x-10") is not None


def test_find_card_file_falls_back_when_stale(temp_audit_env) -> None:
    """find_card_file игнорирует стейл-путь из file_sync и находит карточку сканом диска."""
    audit_dir, storage, synchronizer = temp_audit_env

    issue = AuditIssue(
        id="aud-backend-7777",
        title="Stale index card",
        category="backend",
        status=AuditStatus.PLANNED,
        symptom="Stale",
    )
    real_path = synchronizer.save_and_index_card(issue)

    # Симулируем стейл: индекс указывает на несуществующий путь
    storage.drop_path(str(real_path.resolve()))
    storage.index_card(
        issue,
        file_path="/nonexistent/gone.md",
        mtime=1.0,
        size=1,
        location="todo",
    )

    found = synchronizer.find_card_file("aud-backend-7777")
    assert found is not None
    assert found.resolve() == real_path.resolve()


def test_search_debt_and_ready_backlog_return_dates(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
    tmp_path: Path,
) -> None:
    """A-DATES (ce17): search_debt и get_ready_backlog возвращают created_at/updated_at."""
    from mcp_audit.server import get_ready_backlog, search_debt

    audit_dir, storage, synchronizer = temp_audit_env

    issue = AuditIssue(
        id="aud-backend-dates",
        title="Dates check debt issue",
        category="backend",
        priority=AuditPriority.IMPORTANT,
        status=AuditStatus.PLANNED,
        symptom="Dates missing in search results",
        solution="Store created_at and updated_at in audit_index",
        created_at="2026-11-01",
        updated_at="2026-11-02",
    )
    synchronizer.save_and_index_card(issue)

    # 1. get_issue возвращает даты
    loaded = storage.get_issue("aud-backend-dates")
    assert loaded is not None
    assert loaded.created_at == "2026-11-01"
    assert loaded.updated_at == "2026-11-02"

    # 2. search_debt возвращает даты
    debt_resp = search_debt(
        query="Dates missing in search results",
        synchronizer=synchronizer,
        storage=storage,
    )
    assert debt_resp["count"] == 1
    card_dict = debt_resp["results"][0]
    assert card_dict.get("created_at") == "2026-11-01"
    assert card_dict.get("updated_at") == "2026-11-02"

    # 3. get_ready_backlog возвращает даты
    backlog_resp = get_ready_backlog(
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    assert backlog_resp["count"] == 1
    ready_issue = backlog_resp["ready_issues"][0]
    assert ready_issue.get("created_at") == "2026-11-01"
    assert ready_issue.get("updated_at") == "2026-11-02"

    # 4. Обновление статуса обновляет updated_at
    synchronizer.update_issue_status("aud-backend-dates", AuditStatus.IN_PROGRESS)
    updated_card = storage.get_issue("aud-backend-dates")
    assert updated_card is not None
    assert updated_card.created_at == "2026-11-01"
    assert updated_card.updated_at is not None

    # 5. Пересоздание кэша (rebuild из Markdown) восстанавливает даты
    new_db_path = tmp_path / "fresh_cache.db"
    new_storage = AuditStorage(new_db_path)
    new_sync = AuditSynchronizer(new_storage, audit_dir=audit_dir)
    new_sync.sync()
    reindexed = new_storage.get_issue("aud-backend-dates")
    assert reindexed is not None
    assert reindexed.created_at == "2026-11-01"
    assert reindexed.updated_at is not None
