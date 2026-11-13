"""План C: тесты стабильного artifact_id, блокировки по ID и resolution-секции долга.

Пункт C плана docs/plans/2026-10-18-search-and-data-integrity-followup.md:
- save_plan обновляет единственный файл по стабильному artifact_id;
- lock имя стабильно по artifact_id и не зависит от каталога active/completed;
- повторное закрытие долга не дублирует заметку в секции «Фактическое решение».
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from mcp_audit.models import (
    AuditAutonomy,
    AuditComplexity,
    AuditIssue,
    AuditPriority,
    AuditStatus,
    PlanArtifact,
    PlanStatus,
    PlanStep,
)
from mcp_audit.parser import (
    ACTUAL_RESOLUTION_KEYWORDS,
    parse_audit_markdown,
    parse_plan_markdown,
)
from mcp_audit.storage import AuditStorage
from mcp_audit.synchronizer import AuditSynchronizer, _plan_lock_path


@pytest.fixture
def plan_env(tmp_path: Path) -> tuple[Path, AuditStorage, AuditSynchronizer]:
    """Создаёт временную среду для тестов планов."""
    audit_dir = tmp_path / ".agents" / "audit"
    db_path = tmp_path / ".agents" / ".cache" / "audit_fts.db"
    storage = AuditStorage(db_path)
    sync = AuditSynchronizer(storage=storage, audit_dir=audit_dir)
    return audit_dir, storage, sync


def _make_plan(task_id: str, title: str, steps: list[str]) -> PlanArtifact:
    return PlanArtifact(
        id=f"plan-general-{task_id}",
        task_id=task_id,
        title=title,
        category="general",
        status=PlanStatus.DISCUSSED,
        steps=[PlanStep(index=i, title=s, completed=False) for i, s in enumerate(steps, 1)],
        content="Goal",
        created_at="2026-10-18",
        updated_at="2026-10-18",
    )


# ---------------------------------------------------------------------------
# 1. Стабильный artifact_id: rename title не создаёт второй файл
# ---------------------------------------------------------------------------


def test_save_plan_rename_keeps_single_file(plan_env) -> None:
    """A2 (rev. плана AUDIT-VERIFY-2026-10): повторный save_plan с тем же task_id — reject.

    Переименование title через record_plan/save_plan больше не допускается
    (молчаливая перезапись теряла steps/details) — правки идут через
    update_plan_details. Внутренний upsert-путь (exist_ok=True) сохраняет
    прежнюю гарантию: один файл по artifact_id, rename title не создаёт дубль.
    """
    _, _, sync = plan_env

    first_path = sync.save_plan(_make_plan("TASK-1", "First Title", ["Step 1"]))
    assert first_path.exists()

    # Внешний контракт: повторная запись того же task_id — PLAN_EXISTS
    with pytest.raises(ValueError, match="PLAN_EXISTS"):
        sync.save_plan(_make_plan("TASK-1", "Totally Different Title", ["Step 1"]))

    # Правки title — через update_plan_details
    plan, new_path = sync.update_plan_details("TASK-1", title="Totally Different Title")
    assert new_path == first_path
    all_plan_files = list((first_path.parent).glob("*.md"))
    assert len(all_plan_files) == 1, f"Duplicate plan files: {all_plan_files}"

    content = first_path.read_text(encoding="utf-8")
    assert "Totally Different Title" in content
    plan = parse_plan_markdown(content, file_path=first_path)
    assert plan.title == "Totally Different Title"

    # Внутренний upsert (exist_ok): тот же артефакт перезаписывается в тот же файл
    second_path = sync.save_plan(
        _make_plan("TASK-1", "Totally Different Title", ["Step 1"]), exist_ok=True
    )
    assert second_path == first_path
    all_plan_files = list((first_path.parent).glob("*.md"))
    assert len(all_plan_files) == 1, f"Duplicate plan files: {all_plan_files}"


def test_save_plan_detects_existing_duplicates(plan_env, caplog: pytest.LogCaptureFixture) -> None:
    """Существующие дубликаты (старый файл + новый slug) выявляются логом, не удаляются.

    A2: повторный save_plan того же task_id — reject; сценарий дубликатов
    проверяется через внутренний exist_ok-путь (upsert артефакта).
    """
    audit_dir, _, sync = plan_env

    # Создаём план, затем руками создаём «легаси-дубликат» с другим slug в имени
    first_path = sync.save_plan(_make_plan("TASK-2", "Alpha Title", ["Step 1"]))
    legacy_dup = first_path.with_name("plan-general-TASK-2-legacyslug.md")
    legacy_dup.write_text(first_path.read_text(encoding="utf-8"), encoding="utf-8")

    # Внешний контракт: тот же task_id — PLAN_EXISTS
    with pytest.raises(ValueError, match="PLAN_EXISTS"):
        sync.save_plan(_make_plan("TASK-2", "Beta Title", ["Step 1"]))

    with caplog.at_level("WARNING", logger="mcp_audit.synchronizer"):
        sync.save_plan(_make_plan("TASK-2", "Beta Title", ["Step 1"]), exist_ok=True)

    assert any("DUPLICATE" in r.message for r in caplog.records), caplog.text

    # Конфликтующие дубли не удаляются автоматически
    assert legacy_dup.exists()
    assert first_path.exists()


def test_plan_id_resolution_uses_category(tmp_path: Path) -> None:
    """A2+A13: task_id глобально уникален — второй план с тем же task_id (даже в другой
    категории) через save_plan/record_plan reject'ится; существующие legacy-дубли
    задачам разных категорий доступны через find_plan_file (последний по mtime).
    """
    audit_dir = tmp_path / ".agents" / "audit"
    storage = AuditStorage(tmp_path / ".agents" / ".cache" / "audit_fts.db")
    sync = AuditSynchronizer(storage=storage, audit_dir=audit_dir)

    backend_plan = _make_plan("SHARED-1", "Backend plan", ["S1"])
    backend_plan.category = "backend"
    backend_plan.id = "plan-backend-SHARED-1"
    sync.save_plan(backend_plan)

    core_plan = _make_plan("SHARED-1", "Core plan", ["S1"])
    core_plan.category = "core"
    core_plan.id = "plan-core-SHARED-1"
    # Глобальная уникальность task_id: reject даже при другой категории
    with pytest.raises(ValueError, match="PLAN_EXISTS"):
        sync.save_plan(core_plan)

    # legacy-сценарий (планы уже сосуществуют на диске — например, созданы до
    # A2-фикса): find_plan_file находит один из них
    core_dir = audit_dir.parent / "plans" / "active" / "core"
    core_dir.mkdir(parents=True, exist_ok=True)
    (core_dir / "plan-core-SHARED-1-core-plan.md").write_text(
        "---\nid: plan-core-SHARED-1\ntask_id: SHARED-1\ntitle: Core plan\n"
        "category: core\nstatus: discussed\n---\n# Core plan\n\n## 📋 Этапы и шаги\n"
        "- [ ] 1. S1\n",
        encoding="utf-8",
    )

    # Две разные категории -> два разных файла, оба существуют
    backend_files = list((audit_dir.parent / "plans" / "active" / "backend").glob("*.md"))
    core_files = list((audit_dir.parent / "plans" / "active" / "core").glob("*.md"))
    assert len(backend_files) == 1
    assert len(core_files) == 1
    assert backend_files[0] != core_files[0]

    # find_plan_file без категории при двух планах одного task_id —
    # A13: отказ (AMBIGUOUS_PLAN), а не произвольный файл; с категорией —
    # детерминированный выбор плана этой категории
    with pytest.raises(ValueError, match="AMBIGUOUS_PLAN"):
        sync.find_plan_file("SHARED-1")
    found = sync.find_plan_file("SHARED-1", category="core")
    assert found is not None
    # ID соответствует запрошенной категории
    content = found.read_text(encoding="utf-8")
    plan = parse_plan_markdown(content, file_path=found)
    assert plan.id == "plan-core-SHARED-1"


# ---------------------------------------------------------------------------
# 2. Блокировка по стабильному ID
# ---------------------------------------------------------------------------


def test_plan_lock_path_stable_across_dirs(plan_env) -> None:
    """Lock путь стабилен по artifact_id и не зависит от active/completed каталога."""
    _, _, sync = plan_env

    active_dir = sync.plans_active_dir / "general"
    completed_dir = sync.plans_completed_dir / "general"
    plan_id = "plan-general-L1"

    lock_active = _plan_lock_path(sync, plan_id, active_dir / "plan-general-L1-first.md")
    lock_completed = _plan_lock_path(sync, plan_id, completed_dir / "plan-general-L1-done.md")

    assert lock_active == lock_completed
    # Лок не зависит от имени файла
    lock_other_name = _plan_lock_path(sync, plan_id, active_dir / "plan-general-L1-other.md")
    assert lock_other_name == lock_active


def test_plan_lock_is_advisory_no_unlink(plan_env) -> None:
    """Lock-файл не удаляется после unlock (стабильное имя по ID)."""
    _, _, sync = plan_env
    lock_path = sync.plans_dir / ".locks" / "plan-general-L2.lock"

    from mcp_audit.synchronizer import _advisory_lock

    with _advisory_lock(lock_path):
        assert lock_path.exists()

    assert lock_path.exists()


def test_concurrent_save_plan_no_loss(plan_env) -> None:
    """A2: конкурентные записи одного task_id — один created, остальные PLAN_EXISTS.

    Гонка двух потоков record_plan (как отдельные соединения) закрывается
    task_id-локом с проверкой существования внутри лока (TOCTOU): оба писателя
    не могут одновременно увидеть «плана нет». Внутренний upsert (exist_ok)
    остаётся безопасным: один файл на artifact_id, данные не теряются.
    """
    _, storage, sync = plan_env

    # Второе соединение к той же БД (как отдельный процесс/поток)
    db_path = storage.db_path
    storage2 = AuditStorage(db_path)
    sync2 = AuditSynchronizer(storage=storage2, audit_dir=sync.audit_dir)

    created: list[Path] = []
    rejected: list[ValueError] = []
    lock = threading.Lock()

    def writer(s: AuditSynchronizer, title: str) -> None:
        for _ in range(3):
            plan = _make_plan("RACE-1", title, ["Step A"])
            # plan.id одинаковый -> file_path разрешается одинаково
            try:
                p = s.save_plan(plan)
            except ValueError as err:  # PLAN_EXISTS — ожидаем для проигравших
                if "PLAN_EXISTS" not in str(err):
                    raise
                with lock:
                    rejected.append(err)
            else:
                with lock:
                    created.append(p)

    t1 = threading.Thread(target=writer, args=(sync, "Writer One"))
    t2 = threading.Thread(target=writer, args=(sync2, "Writer Two"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Ровно один created (первый), остальные — PLAN_EXISTS
    assert len(created) == 1, created
    assert rejected, "хотя бы один писатель должен получить PLAN_EXISTS"

    # Все файлы планов лежат в active/general — ровно один файл на task_id
    plans_dir = sync.plans_active_dir / "general"
    all_md = [p for p in plans_dir.glob("*.md") if not p.name.startswith(".")]
    race_files = [p for p in all_md if "RACE-1" in p.stem]
    assert len(race_files) == 1, race_files

    # Содержимое валидно, шаги не потерялись
    content = race_files[0].read_text(encoding="utf-8")
    plan = parse_plan_markdown(content, file_path=race_files[0])
    assert plan.task_id == "RACE-1"
    assert len(plan.steps) == 1
    assert plan.steps[0].title == "Step A"

    # Внутренний upsert (exist_ok) конкурентно безопасен: один файл
    def upserter(s: AuditSynchronizer, title: str) -> None:
        for _ in range(3):
            s.save_plan(_make_plan("RACE-1", title, ["Step A"]), exist_ok=True)

    t3 = threading.Thread(target=upserter, args=(sync, "Writer One"))
    t4 = threading.Thread(target=upserter, args=(sync2, "Writer Two"))
    t3.start()
    t4.start()
    t3.join()
    t4.join()

    race_files = [p for p in plans_dir.glob("*.md") if "RACE-1" in p.stem]
    assert len(race_files) == 1, race_files
    content = race_files[0].read_text(encoding="utf-8")
    plan = parse_plan_markdown(content, file_path=race_files[0])
    assert plan.title in ("Writer One", "Writer Two")
    assert len(plan.steps) == 1


# ---------------------------------------------------------------------------
# 3. resolution_note: отдельная секция «Фактическое решение»
# ---------------------------------------------------------------------------


@pytest.fixture
def debt_env(tmp_path: Path) -> tuple[Path, AuditStorage, AuditSynchronizer]:
    """Создаёт временную среду для тестов карточек долга."""
    audit_dir = tmp_path / ".agents" / "audit"
    db_path = tmp_path / ".agents" / ".cache" / "audit_fts.db"
    storage = AuditStorage(db_path)
    sync = AuditSynchronizer(storage=storage, audit_dir=audit_dir)
    return audit_dir, storage, sync


def _make_issue_with_solution() -> AuditIssue:
    return AuditIssue(
        id="aud-backend-res1",
        title="Rate limiter redis timeout",
        category="backend",
        priority=AuditPriority.IMPORTANT,
        autonomy=AuditAutonomy.HIGH,
        complexity=AuditComplexity.M,
        status=AuditStatus.PLANNED,
        blocked_by=[],
        symptom="Redis timeout in rate limiter",
        root_cause="Missing timeout fallback",
        solution="Add explicit redis timeout parameter",
    )


def test_resolution_note_in_actual_section_not_solution(debt_env) -> None:
    """resolution_note пишется в отдельную секцию, предложенное solution не трогается."""
    _, _, sync = debt_env
    issue = _make_issue_with_solution()
    path = sync.save_and_index_card(issue)

    updated, new_path = sync.update_issue_status(
        issue.id,
        AuditStatus.COMPLETED,
        pr_url="https://github.com/org/repo/pull/42",
        resolution_note="Added in-memory token bucket fallback",
    )

    content = new_path.read_text(encoding="utf-8")
    assert "Фактическое решение" in content
    assert "Added in-memory token bucket fallback" in content

    # Предложенное solution сохранено как есть
    parsed = parse_audit_markdown(content, file_path=new_path)
    assert parsed.solution == "Add explicit redis timeout parameter"
    assert "## 🛠 Предлагаемое решение" in content

    # issue.solution не перезаписан заметкой
    assert updated.solution == "Add explicit redis timeout parameter"
    _ = path  # исходный путь больше не существует после move
    assert not path.exists()


def test_resolution_note_idempotent_no_duplicate(debt_env) -> None:
    """Повторное идентичное закрытие не дублирует заметку в секции."""
    _, _, sync = debt_env
    issue = _make_issue_with_solution()
    sync.save_and_index_card(issue)

    note = "Fixed by switching to pinned connection"
    sync.update_issue_status(issue.id, AuditStatus.COMPLETED, resolution_note=note)
    sync.update_issue_status(issue.id, AuditStatus.COMPLETED, resolution_note=note)

    card_file = sync.find_card_file(issue.id)
    assert card_file is not None
    content = card_file.read_text(encoding="utf-8")
    assert content.count(note) == 1
    assert content.count("Фактическое решение") == 1


def test_resolution_note_distinct_notes_both_kept(debt_env) -> None:
    """Разные заметки при повторных закрытиях сохраняются обе (история)."""
    _, _, sync = debt_env
    issue = _make_issue_with_solution()
    sync.save_and_index_card(issue)

    sync.update_issue_status(issue.id, AuditStatus.COMPLETED, resolution_note="First fix")
    sync.update_issue_status(issue.id, AuditStatus.COMPLETED, resolution_note="Second fix")

    card_file = sync.find_card_file(issue.id)
    content = card_file.read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert "First fix" in content
    assert "Second fix" in content
    assert content.count("Фактическое решение") == 1


def test_actual_resolution_keywords_constant() -> None:
    """Ключевые слова секции фактического решения стабильны (RU/EN)."""
    assert "фактическ" in " ".join(ACTUAL_RESOLUTION_KEYWORDS).lower()
    assert "actual" in " ".join(ACTUAL_RESOLUTION_KEYWORDS).lower()


def test_concurrent_record_plan_same_task_id_one_success(plan_env) -> None:
    """A2-TOCTOU: два конкурентных record_plan одного task_id в разных
    категориях — ровно один успех (task_id-лок удерживается на всю запись)."""
    import threading

    _, _, sync = plan_env
    results: list[tuple[str, str]] = []
    lock = threading.Lock()

    def _save(cat: str) -> None:
        plan = _make_plan("RACE-1", f"Race {cat}", [f"Step {cat}"])
        plan.category = cat
        plan.id = f"plan-{cat}-RACE-1"
        try:
            path = sync.save_plan(plan)
            with lock:
                results.append((cat, str(path)))
        except ValueError as err:
            with lock:
                results.append((cat, f"ERR:{err}"))

    t1 = threading.Thread(target=_save, args=("backend",))
    t2 = threading.Thread(target=_save, args=("core",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    successes = [r for r in results if not r[1].startswith("ERR:")]
    rejections = [r for r in results if r[1].startswith("ERR:")]
    assert len(successes) == 1, results
    assert len(rejections) == 1, results
    assert "PLAN_EXISTS" in rejections[0][1], rejections
