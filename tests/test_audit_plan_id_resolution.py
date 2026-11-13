"""R1: точная резолюция artifact_id планов — регрессии коллизии соседних ID.

Отчёт приёмки 2026-10-18-post-implementation-review.md, R1 (P1, потеря данных):
save_plan(id="plan-general-task-1") перезаписывал файл плана "plan-general-task-10",
потому что _find_plan_files_by_id принимал совпадение clean_id in p.stem (подстрока
имени файла) без проверки точного ID в frontmatter.

Контракт после фикса: файлы планов сопоставляются artifact_id ТОЛЬКО по точному
совпадению frontmatter id; подстроки имён (соседние task-1/task-10, чужой slug с
ID-подстрокой) не являются совпадением.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from mcp_audit.models import PlanArtifact, PlanStatus, PlanStep
from mcp_audit.parser import parse_plan_markdown
from mcp_audit.storage import AuditStorage
from mcp_audit.synchronizer import AuditSynchronizer


@pytest.fixture
def plan_env(tmp_path: Path) -> tuple[Path, AuditStorage, AuditSynchronizer]:
    audit_dir = tmp_path / ".agents" / "audit"
    db_path = tmp_path / ".agents" / ".cache" / "audit_fts.db"
    storage = AuditStorage(db_path)
    sync = AuditSynchronizer(storage=storage, audit_dir=audit_dir)
    return audit_dir, storage, sync


def _make_plan(plan_id: str, task_id: str, title: str) -> PlanArtifact:
    return PlanArtifact(
        id=plan_id,
        task_id=task_id,
        title=title,
        category="general",
        status=PlanStatus.DISCUSSED,
        steps=[PlanStep(index=1, title="Step 1", completed=False)],
        content="Goal",
        created_at="2026-10-19",
        updated_at="2026-10-19",
    )


def _read_plan_id(path: Path) -> str:
    plan = parse_plan_markdown(path.read_text(encoding="utf-8"), file_path=path)
    return plan.id


def test_adjacent_task_ids_do_not_collide(plan_env) -> None:
    """Воспроизведение R1: task-10, затем task-1 -> два файла, оба со своими ID.

    До фикса: оба вызова возвращали один путь, файл task-10 перезаписывался
    содержимым task-1 (потеря данных).
    """
    _, _, sync = plan_env

    path_10 = sync.save_plan(_make_plan("plan-general-task-10", "task-10", "Ten"))
    path_1 = sync.save_plan(_make_plan("plan-general-task-1", "task-1", "One"))

    # Два РАЗНЫХ файла
    assert path_1 != path_10, f"collision: both plans resolved to {path_1}"

    # Оба существуют и содержат собственные ID
    assert path_10.exists() and path_1.exists()
    assert _read_plan_id(path_10) == "plan-general-task-10"
    assert _read_plan_id(path_1) == "plan-general-task-1"

    # Третья запись task-10 не портит task-1 и наоборот: reject PLAN_EXISTS
    with pytest.raises(ValueError, match="PLAN_EXISTS"):
        sync.save_plan(_make_plan("plan-general-task-10", "task-10", "Ten v2"))
    assert _read_plan_id(path_1) == "plan-general-task-1"
    # Внутренний upsert: тот же артефакт, тот же файл
    path_10_again = sync.save_plan(
        _make_plan("plan-general-task-10", "task-10", "Ten v2"), exist_ok=True
    )
    assert path_10_again == path_10
    assert _read_plan_id(path_1) == "plan-general-task-1"


def test_plan_id_prefix_collision_two_char(plan_env) -> None:
    """task-1 vs task-12: более короткий ID не матчит чужой файл по подстроке."""
    _, _, sync = plan_env

    path_12 = sync.save_plan(_make_plan("plan-general-task-12", "task-12", "Twelve"))
    path_1 = sync.save_plan(_make_plan("plan-general-task-1", "task-1", "One"))

    assert path_1 != path_12
    assert _read_plan_id(path_12) == "plan-general-task-12"
    assert _read_plan_id(path_1) == "plan-general-task-1"


def test_same_id_new_title_single_file(plan_env) -> None:
    """A2: повторный save_plan того же task_id — reject; внутренний upsert -> один файл.

    Внешний контракт record_plan: перезапись запрещена (update_plan для правок).
    Внутренний upsert (exist_ok): резолюция по frontmatter id находит
    существующий файл с тем же ID — дублей не возникает.
    """
    _, _, sync = plan_env

    first = sync.save_plan(_make_plan("plan-general-stable-1", "stable-1", "Title A"))

    # Внешний контракт: тот же task_id — reject
    with pytest.raises(ValueError, match="PLAN_EXISTS"):
        sync.save_plan(_make_plan("plan-general-stable-1", "stable-1", "Title B"))

    # Внутренний upsert: тот же artifact_id — тот же файл
    second = sync.save_plan(
        _make_plan("plan-general-stable-1", "stable-1", "Title B"), exist_ok=True
    )

    assert second == first
    all_files = list(first.parent.glob("*.md"))
    assert len(all_files) == 1, f"Duplicate plan files: {all_files}"
    plan = parse_plan_markdown(first.read_text(encoding="utf-8"), file_path=first)
    assert plan.title == "Title B"


def test_foreign_slug_with_id_substring_not_matched(plan_env) -> None:
    """Файл с чужим slug, содержащим ID другого плана, не подхватывается.

    До фикса: _find_plan_files_by_id("plan-x-1") матчил файл plan-x-10-....md
    по подстроке имени. Имя plan-x-x-10-foreign-slug содержит подстроку
    "plan-x-1" (префикс plan-x-1 совпадает с началом plan-x-x-1...), т.е.
    воспроизводит дофиксный подстрочный матч, но frontmatter id = plan-x-10.
    """
    _, _, sync = plan_env

    # Кладём план с ID plan-x-10 и категорией "x" — соседний ID план-x-1
    # отличается на один символ; до фикса подстрочный фильтр по имени
    # (sanitize_task_id("plan-x-1") -> "x-1" ⊃ вхождение в stem соседа при
    # совпадающих префиксах) давал коллизию. Проверяем фактическую защиту.
    foreign = _make_plan("plan-x-10", "x-10", "Foreign slug")
    foreign.category = "x"
    path_10 = sync.save_plan(foreign)

    # План с ID plan-x-1 должен получить СВОЙ файл, а не перезаписать plan-x-10
    path_1 = sync.save_plan(_make_plan("plan-x-1", "x-1", "Own file"))

    assert path_1 != path_10
    assert _read_plan_id(path_10) == "plan-x-10"
    assert _read_plan_id(path_1) == "plan-x-1"


def test_completed_dir_search_not_confused_by_similar_ids(plan_env) -> None:
    """Планы в разных директориях (active/completed) с соседними ID не путаются."""
    _, _, sync = plan_env

    done = _make_plan("plan-general-done-20", "done-20", "Completed plan")
    done.status = PlanStatus.DONE
    path_done = sync.save_plan(done)

    active = _make_plan("plan-general-done-2", "done-2", "Active plan")
    path_active = sync.save_plan(active)

    assert path_done != path_active
    assert _read_plan_id(path_done) == "plan-general-done-20"
    assert _read_plan_id(path_active) == "plan-general-done-2"

    # Повторная запись DONE-плана не задевает active-план: A2-контракт — reject
    with pytest.raises(ValueError, match="PLAN_EXISTS"):
        sync.save_plan(done)
    assert _read_plan_id(path_active) == "plan-general-done-2"
    # Внутренний upsert: тот же артефакт, тот же файл
    path_done_again = sync.save_plan(done, exist_ok=True)
    assert path_done_again == path_done
    assert _read_plan_id(path_active) == "plan-general-done-2"


def test_frontmatter_id_swap_stale_index_no_overwrite(
    plan_env, caplog: pytest.LogCaptureFixture
) -> None:
    """Ревизия 4, R1: подмена frontmatter id не позволяет перезапись по старому ID.

    Ожидание однозначно: файл с изменённым ID больше не находится по старому
    ID (даже при прежнем имени файла) — save_plan старого ID создаёт НОВЫЙ
    файл. Устаревший индекс БД (find_artifact_path по старому ID, указывающий
    на этот же файл) также не позволяет перезаписать его: до фикса
    _resolve_plan_file_path брал indexed-путь БЕЗ проверки frontmatter id.
    """
    _, _, sync = plan_env

    # План сохранён и проиндексирован в БД под artifact_id plan-front-swap-7
    path_7 = sync.save_plan(_make_plan("plan-front-swap-7", "front-swap-7", "Original"))

    # Ручная правка frontmatter: id подменён, имя файла прежнее
    text = path_7.read_text(encoding="utf-8")
    swapped = text.replace("id: plan-front-swap-7", "id: plan-front-swap-99")
    assert swapped != text, "подмена frontmatter id не применилась"
    path_7.write_text(swapped, encoding="utf-8")

    # Сохранение НОВОГО плана со старым ID plan-front-swap-7:
    # файл с подменённым id=99 не должен быть перезаписан.
    # A2: тот же task_id 'front-swap-7' уже занят (файл с подменённым id) — reject.
    with pytest.raises(ValueError, match="PLAN_EXISTS"):
        sync.save_plan(_make_plan("plan-front-swap-7", "front-swap-7", "New plan"))
    # Внутренний upsert-путь (сценарий resave артефакта): R1-гарантии ниже.
    with caplog.at_level(logging.WARNING, logger="mcp_audit.synchronizer"):
        path_new = sync.save_plan(
            _make_plan("plan-front-swap-7", "front-swap-7", "New plan"), exist_ok=True
        )
    assert "STALE_INDEX" in caplog.text, (
        "отклонение indexed-пути по frontmatter должно отражаться "
        "warning-логом (R1 MINOR-1), не только debug"
    )

    # Старый файл сохраняет подменённый ID (не перезаписан ни по диску, ни
    # по стейл-индексу БД)
    assert _read_plan_id(path_7) == "plan-front-swap-99", (
        "файл с подменённым frontmatter id перезаписан (потеря данных R1)"
    )
    assert "New plan" not in path_7.read_text(encoding="utf-8")

    # Новый план получил СВОЙ файл (имя прежнее ≠ совпадение ID)
    assert path_new != path_7, "save_plan вернул путь чужого (подменённого) файла"
    assert _read_plan_id(path_new) == "plan-front-swap-7"


def test_same_title_same_task_swapped_id_no_overwrite(
    plan_env, caplog: pytest.LogCaptureFixture
) -> None:
    """R1 edge (ревизия 4, MINOR-2): коллизия ИМЕНИ файла при подменённом id.

    План A (task X, title T) сохранён; frontmatter id вручную подменён —
    имя файла (task X + slug T) при этом не менялось. Новый план со
    СТАРЫМ id и теми же task_id/title резолвится в «новый» путь
    target_dir/filename — который совпадает с файлом A. До фикса
    _atomic_write_text перезаписал бы чужую ручную правку. После фикса:
    отдельный файл (суффикс имени), A не тронут, коллизия видима в логе.
    """
    _, _, sync = plan_env

    path_a = sync.save_plan(_make_plan("plan-same-7", "same-7", "Equal Title"))
    text = path_a.read_text(encoding="utf-8")
    swapped = text.replace("id: plan-same-7", "id: plan-same-99")
    assert swapped != text, "подмена frontmatter id не применилась"
    path_a.write_text(swapped, encoding="utf-8")

    # A2: тот же task_id 'same-7' уже занят (файл с подменённым id) — reject
    with pytest.raises(ValueError, match="PLAN_EXISTS"):
        sync.save_plan(_make_plan("plan-same-7", "same-7", "Equal Title"))
    # Внутренний upsert-путь: сценарий коллизии имени (R1 MINOR-2) ниже.
    with caplog.at_level(logging.WARNING, logger="mcp_audit.synchronizer"):
        path_b = sync.save_plan(_make_plan("plan-same-7", "same-7", "Equal Title"), exist_ok=True)

    # Ручная правка (подменённый id) сохранена: файл A не перезаписан
    assert _read_plan_id(path_a) == "plan-same-99", (
        "файл с подменённым id перезаписан при коллизии имени (потеря данных R1)"
    )
    # Новый план получил СВОЙ файл: имя коллидировало → суффикс
    assert path_b != path_a, "save_plan вернул путь чужого файла при коллизии имени"
    assert _read_plan_id(path_b) == "plan-same-7"
    # Коллизия имени видима warning-логом (не молча)
    assert "PLAN_PATH_COLLISION" in caplog.text
