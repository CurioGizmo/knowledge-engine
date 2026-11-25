"""Тесты для версий редакций планов audit-engine (AUDIT-PLAN-VERSIONS, этап 1 / T1)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import pytest

from mcp_audit.help_docs import get_audit_help_text
from mcp_audit.models import (
    PlanArtifact,
    PlanStatus,
    PlanStep,
    PlanVersionMetadata,
    PlanVersionsResponse,
)
from mcp_audit.parser import (
    compute_plan_definition_projection,
    compute_plan_definition_sha256,
    parse_plan_markdown,
    serialize_plan_markdown,
)
from mcp_audit.repository import AmbiguousPlanError, PlanRepository
from mcp_audit.server import (
    get_plan_versions,
    get_task_context,
    record_plan,
    restore_plan_version,
    search_artifacts,
    step_plan,
    update_plan,
)
from mcp_audit.storage import AuditStorage
from mcp_audit.synchronizer import AuditSynchronizer
from mcp_core.card_codec import split_frontmatter


@pytest.fixture
def temp_audit_env(
    tmp_path: Path,
) -> tuple[Path, AuditStorage, AuditSynchronizer]:
    """Изолирует окружение audit-engine на временный каталог."""
    audit_dir = tmp_path / ".agents" / "audit"
    db_path = tmp_path / ".agents" / ".cache" / "audit_fts.db"
    storage = AuditStorage(db_path)
    synchronizer = AuditSynchronizer(storage, audit_dir=audit_dir)
    return audit_dir, storage, synchronizer


def test_record_plan_creates_v1(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """A1: record_plan создаёт план с plan_version=1 и неизменяемым снимком v0001.md."""
    _, storage, synchronizer = temp_audit_env

    resp = record_plan(
        task_id="AUDIT-T1",
        title="Plan Versions T1",
        steps=["1. Step one", "2. Step two"],
        target_files=["src/mcp_audit/models.py"],
        category="backend",
        details="Context for T1.",
        synchronizer=synchronizer,
        storage=storage,
    )

    assert resp["status"] == "created"
    assert resp["plan_version"] == 1

    plan_file = Path(resp["file_path"])
    assert plan_file.exists()
    content = plan_file.read_text(encoding="utf-8")

    plan = parse_plan_markdown(content, file_path=plan_file)
    assert plan.plan_version == 1
    assert plan.definition_sha256 is not None

    expected_projection = compute_plan_definition_projection(content)
    expected_sha256 = compute_plan_definition_sha256(expected_projection)
    assert plan.definition_sha256 == expected_sha256

    # Проверка создания снимка v0001.md
    version_dir = synchronizer.plan_versions_dir / plan.id
    v1_path = version_dir / "v0001.md"
    assert v1_path.exists(), f"Snapshot v0001.md must exist at {v1_path}"

    raw_snapshot = v1_path.read_text(encoding="utf-8")
    meta_dict, snap_body = split_frontmatter(raw_snapshot)
    assert meta_dict is not None

    meta = PlanVersionMetadata.model_validate(meta_dict)
    assert meta.version == 1
    assert meta.parent_version is None
    assert meta.reason == "initial creation"
    assert meta.source == "mcp"
    assert meta.restored_from is None
    assert meta.definition_sha256 == expected_sha256

    # Тело снимка — полный Markdown карточки на момент редакции
    snap_plan = parse_plan_markdown(snap_body)
    assert snap_plan.id == plan.id
    assert snap_plan.title == plan.title
    assert snap_plan.plan_version == 1


def test_plan_definition_projection_fenced_blocks() -> None:
    """Чекбоксы внутри code fence не вырезаются, а чекбоксы шагов нормализуются."""
    content1 = """---
id: plan-general-FENCE-1
schema_version: 1
task_id: FENCE-1
title: Fenced Blocks Test
category: general
status: discussed
target_files:
  - src/foo.py
---

# План: FENCE-1 Fenced Blocks Test

> **Статус:** ⚪ Обсуждено (`discussed`)
> 📌 **Задача:** FENCE-1

---

## 🎯 Цель и критерии приёмки
Пример чеклиста в коде:
```markdown
- [ ] чекбокс внутри кода
- [x] выполненный чекбокс в коде
```

## 📋 Этапы и шаги
- [ ] 1. Первый шаг
- [x] 2. Второй шаг
"""

    proj1 = compute_plan_definition_projection(content1)

    # Проверяем сохранение чекбоксов внутри code fence
    assert "- [ ] чекбокс внутри кода" in proj1
    assert "- [x] выполненный чекбокс в коде" in proj1

    # Проверяем нормализацию чекбоксов шагов плана вне code fence
    assert "- [ ] 1. Первый шаг" not in proj1
    assert "- [x] 2. Второй шаг" not in proj1
    assert "- 1. Первый шаг" in proj1
    assert "- 2. Второй шаг" in proj1

    # Исключение служебных строк
    assert "# План:" not in proj1
    assert "> **Статус:**" not in proj1
    assert "> 📌 **Задача:**" not in proj1

    # Смена чекбокса выполнения шага НЕ меняет проекцию
    content_step_done = content1.replace("- [ ] 1. Первый шаг", "- [x] 1. Первый шаг")
    proj2 = compute_plan_definition_projection(content_step_done)
    assert proj1 == proj2
    assert compute_plan_definition_sha256(proj1) == compute_plan_definition_sha256(proj2)

    # Добавление заметки step_plan (> 📝) НЕ меняет проекцию
    content_with_note = content_step_done.replace(
        "- [x] 1. Первый шаг",
        "- [x] 1. Первый шаг\n  > 📝 2026-11-23: Готово",
    )
    proj_with_note = compute_plan_definition_projection(content_with_note)
    assert proj1 == proj_with_note

    # Изменение содержимого внутри code fence МЕНЯЕТ проекцию
    content_fence_changed = content1.replace(
        "- [ ] чекбокс внутри кода",
        "- [x] чекбокс внутри кода",
    )
    proj3 = compute_plan_definition_projection(content_fence_changed)
    assert proj1 != proj3
    assert compute_plan_definition_sha256(proj1) != compute_plan_definition_sha256(proj3)


def test_step_plan_and_status_no_version_bump(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Закрытие шага через step_plan и смена статуса не повышают plan_version."""
    _, storage, synchronizer = temp_audit_env

    rec = record_plan(
        task_id="AUDIT-STEP",
        title="Step and Status Test",
        steps=["1. First step", "2. Second step"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    assert rec["plan_version"] == 1

    # Закрытие шага
    step_resp = step_plan(
        task_id="AUDIT-STEP",
        step=1,
        status="done",
        note="Completed step 1",
        synchronizer=synchronizer,
        storage=storage,
    )
    assert step_resp["plan_version"] == 1

    # Смена статуса
    upd_resp = update_plan(
        task_id="AUDIT-STEP",
        status="approved",
        synchronizer=synchronizer,
        storage=storage,
    )
    assert upd_resp["plan_version"] == 1

    # Добавление PR URL
    upd_pr = update_plan(
        task_id="AUDIT-STEP",
        pr_url="https://github.com/org/repo/pull/1",
        synchronizer=synchronizer,
        storage=storage,
    )
    assert upd_pr["plan_version"] == 1

    # Проверка каталога версий: v0001.md есть, v0002.md нет
    version_dir = synchronizer.plan_versions_dir / rec["id"]
    assert (version_dir / "v0001.md").exists()
    assert not (version_dir / "v0002.md").exists()


def test_update_plan_definition_creates_v2(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Изменение title, target_files, details или add_steps повышает plan_version."""
    _, storage, synchronizer = temp_audit_env

    rec = record_plan(
        task_id="AUDIT-V2",
        title="Initial Title",
        steps=["1. Alpha step"],
        target_files=["src/alpha.py"],
        category="backend",
        details="Initial context.",
        synchronizer=synchronizer,
        storage=storage,
    )
    assert rec["plan_version"] == 1
    version_dir = synchronizer.plan_versions_dir / rec["id"]

    # 1. Изменение title повышает до v2
    u1 = update_plan(
        task_id="AUDIT-V2",
        title="Updated Title",
        reason="Title clarification",
        synchronizer=synchronizer,
        storage=storage,
    )
    assert u1["plan_version"] == 2
    v2_path = version_dir / "v0002.md"
    assert v2_path.exists()

    meta_v2_dict, snap_body = split_frontmatter(v2_path.read_text(encoding="utf-8"))
    assert meta_v2_dict is not None
    meta_v2 = PlanVersionMetadata.model_validate(meta_v2_dict)
    assert meta_v2.version == 2
    assert meta_v2.parent_version == 1
    assert meta_v2.reason == "Title clarification"
    assert meta_v2.source == "mcp"

    snap_plan = parse_plan_markdown(snap_body)
    assert snap_plan.title == "Updated Title"
    assert snap_plan.plan_version == 2

    # 2. Изменение target_files повышает до v3
    u2 = update_plan(
        task_id="AUDIT-V2",
        target_files=["src/alpha.py", "src/beta.py"],
        synchronizer=synchronizer,
        storage=storage,
    )
    assert u2["plan_version"] == 3
    assert (version_dir / "v0003.md").exists()

    # 3. Добавление шагов (add_steps) повышает до v4
    u3 = update_plan(
        task_id="AUDIT-V2",
        add_steps=["2. Beta step"],
        synchronizer=synchronizer,
        storage=storage,
    )
    assert u3["plan_version"] == 4
    assert (version_dir / "v0004.md").exists()

    # 4. Изменение details повышает до v5
    u4 = update_plan(
        task_id="AUDIT-V2",
        details="Updated context for task.",
        reason="Expanded context",
        synchronizer=synchronizer,
        storage=storage,
    )
    assert u4["plan_version"] == 5
    assert (version_dir / "v0005.md").exists()


def test_update_plan_noop_no_version_bump(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Повторный вызов update_plan с теми же параметрами не создает новую версию."""
    _, storage, synchronizer = temp_audit_env

    rec = record_plan(
        task_id="AUDIT-NOOP",
        title="Stable Plan",
        steps=["1. Work item"],
        target_files=["src/stable.py"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    assert rec["plan_version"] == 1
    version_dir = synchronizer.plan_versions_dir / rec["id"]

    # Повторный вызов с тем же title и target_files
    u1 = update_plan(
        task_id="AUDIT-NOOP",
        title="Stable Plan",
        target_files=["src/stable.py"],
        synchronizer=synchronizer,
        storage=storage,
    )
    assert u1["plan_version"] == 1
    assert not (version_dir / "v0002.md").exists()


def test_legacy_plan_without_version(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """План без plan_version при первой содержательной правке фиксирует v1 и создаёт v2."""
    _, storage, synchronizer = temp_audit_env

    # Создаём легаси-карточку на диске напрямую (без plan_version и definition_sha256)
    target_dir = synchronizer.plans_active_dir / "backend"
    target_dir.mkdir(parents=True, exist_ok=True)
    plan_file = target_dir / "plan-backend-LEGACY-1.md"

    legacy_content = """---
id: plan-backend-LEGACY-1
schema_version: 1
task_id: LEGACY-1
title: Legacy Plan Without Version
category: backend
status: discussed
target_files:
- src/legacy.py
---

# План: LEGACY-1 Legacy Plan Without Version

> **Статус:** ⚪ Обсуждено (`discussed`)
> 📌 **Задача:** LEGACY-1

---

## 🎯 Цель и критерии приёмки
Legacy plan context.

## 📋 Этапы и шаги
- [ ] 1. Legacy step 1
"""
    plan_file.write_text(legacy_content, encoding="utf-8")
    synchronizer.sync_artifacts()

    # Проверяем виртуальный plan_version=1 при чтении
    parsed = parse_plan_markdown(legacy_content, file_path=plan_file)
    assert parsed.plan_version == 1
    assert parsed.definition_sha256 is None

    version_dir = synchronizer.plan_versions_dir / "plan-backend-LEGACY-1"
    assert not version_dir.exists()

    # Первая содержательная правка: меняем заголовок
    upd = update_plan(
        task_id="LEGACY-1",
        title="Updated Legacy Plan",
        reason="First substantive update",
        synchronizer=synchronizer,
        storage=storage,
    )
    assert upd["plan_version"] == 2

    # Проверяем, что создался базовый снимок v0001.md для старого состояния
    v1_path = version_dir / "v0001.md"
    assert v1_path.exists()
    meta1_dict, snap1_body = split_frontmatter(v1_path.read_text(encoding="utf-8"))
    assert meta1_dict is not None
    meta_v1 = PlanVersionMetadata.model_validate(meta1_dict)
    assert meta_v1.version == 1
    assert meta_v1.source == "manual"
    snap1_plan = parse_plan_markdown(snap1_body)
    assert snap1_plan.title == "Legacy Plan Without Version"

    # Проверяем, что создался снимок v0002.md для нового состояния
    v2_path = version_dir / "v0002.md"
    assert v2_path.exists()
    meta2_dict, snap2_body = split_frontmatter(v2_path.read_text(encoding="utf-8"))
    assert meta2_dict is not None
    meta_v2 = PlanVersionMetadata.model_validate(meta2_dict)
    assert meta_v2.version == 2
    assert meta_v2.parent_version == 1
    assert meta_v2.reason == "First substantive update"
    assert meta_v2.source == "mcp"
    snap2_plan = parse_plan_markdown(snap2_body)
    assert snap2_plan.title == "Updated Legacy Plan"
    assert snap2_plan.plan_version == 2


def test_get_plan_versions_list(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """get_plan_versions(view='list') возвращает список метаданных версий с сортировкой и лимитом."""
    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="VERSIONS-LIST",
        title="Initial Title",
        steps=["1. Step one"],
        target_files=["src/f1.py"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    update_plan(
        task_id="VERSIONS-LIST",
        title="Updated Title",
        reason="Change title",
        synchronizer=synchronizer,
        storage=storage,
    )
    update_plan(
        task_id="VERSIONS-LIST",
        add_steps=["2. Step two"],
        reason="Add step",
        synchronizer=synchronizer,
        storage=storage,
    )

    res = get_plan_versions(
        task_id="VERSIONS-LIST",
        view="list",
        synchronizer=synchronizer,
        storage=storage,
    )
    assert res["task_id"] == "VERSIONS-LIST"
    assert res["plan_id"] == "plan-backend-VERSIONS-LIST"
    assert res["current_version"] == 3
    assert res["total_count"] == 3
    assert res["object_type"] == "plan_card"
    assert len(res["versions"]) == 3
    assert [v["version"] for v in res["versions"]] == [3, 2, 1]
    assert res["versions"][0]["reason"] == "Add step"
    assert res["versions"][1]["reason"] == "Change title"
    assert res["versions"][2]["reason"] == "initial creation"

    # Проверка лимита
    res_lim = get_plan_versions(
        task_id="VERSIONS-LIST",
        view="list",
        limit=2,
        synchronizer=synchronizer,
        storage=storage,
    )
    assert res_lim["total_count"] == 3
    assert len(res_lim["versions"]) == 2
    assert [v["version"] for v in res_lim["versions"]] == [3, 2]


def test_get_plan_versions_show(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """get_plan_versions(view='show') возвращает точный снимок карточки плана и ошибку при отсутствии."""
    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="VERSIONS-SHOW",
        title="Show Initial Title",
        steps=["1. Step one"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    update_plan(
        task_id="VERSIONS-SHOW",
        title="Show Updated Title",
        reason="Show title change",
        synchronizer=synchronizer,
        storage=storage,
    )

    res_v1 = get_plan_versions(
        task_id="VERSIONS-SHOW",
        view="show",
        version=1,
        synchronizer=synchronizer,
        storage=storage,
    )
    assert res_v1["version"] == 1
    assert res_v1["object_type"] == "plan_card"
    assert res_v1["metadata"]["version"] == 1
    assert "Show Initial Title" in res_v1["raw_markdown"]

    res_cur = get_plan_versions(
        task_id="VERSIONS-SHOW",
        view="show",
        synchronizer=synchronizer,
        storage=storage,
    )
    assert res_cur["version"] == 2
    assert res_cur["object_type"] == "plan_card"
    assert res_cur["metadata"]["version"] == 2
    assert "Show Updated Title" in res_cur["raw_markdown"]

    # Ошибка при несуществующей версии
    with pytest.raises(ValueError, match="PLAN_VERSION_NOT_FOUND"):
        get_plan_versions(
            task_id="VERSIONS-SHOW",
            view="show",
            version=99,
            synchronizer=synchronizer,
            storage=storage,
        )


def test_get_plan_versions_diff(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """get_plan_versions(view='diff') возвращает unified diff определений между редакциями."""
    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="VERSIONS-DIFF",
        title="Diff Initial Title",
        steps=["1. Alpha step"],
        target_files=["src/diff1.py"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    update_plan(
        task_id="VERSIONS-DIFF",
        title="Diff Updated Title",
        add_steps=["2. Beta step"],
        reason="Add beta step",
        synchronizer=synchronizer,
        storage=storage,
    )

    res_diff = get_plan_versions(
        task_id="VERSIONS-DIFF",
        view="diff",
        version=2,
        compare_to=1,
        synchronizer=synchronizer,
        storage=storage,
    )
    assert res_diff["version"] == 2
    assert res_diff["compare_to"] == 1
    assert res_diff["object_type"] == "plan_card"
    assert "-title: Diff Initial Title" in res_diff["diff"]
    assert "+title: Diff Updated Title" in res_diff["diff"]
    assert "Beta step" in res_diff["diff"]

    # Default compare_to = version - 1
    res_default_diff = get_plan_versions(
        task_id="VERSIONS-DIFF",
        view="diff",
        version=2,
        synchronizer=synchronizer,
        storage=storage,
    )
    assert res_default_diff["compare_to"] == 1
    assert res_default_diff["diff"] == res_diff["diff"]

    # version=1 без compare_to возвращает пустой diff
    res_v1_diff = get_plan_versions(
        task_id="VERSIONS-DIFF",
        view="diff",
        version=1,
        synchronizer=synchronizer,
        storage=storage,
    )
    assert res_v1_diff["diff"] == ""

    # Несуществующая версия вызывает PLAN_VERSION_NOT_FOUND
    with pytest.raises(ValueError, match="PLAN_VERSION_NOT_FOUND"):
        get_plan_versions(
            task_id="VERSIONS-DIFF",
            view="diff",
            version=99,
            compare_to=1,
            synchronizer=synchronizer,
            storage=storage,
        )


def test_get_plan_versions_empty_history_error(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """A8: при пустом/отсутствующем каталоге истории запрос версии возвращает PLAN_VERSION_NOT_FOUND."""
    import shutil

    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="EMPTY-HIST",
        title="Empty History Plan",
        steps=["1. Lone step"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )

    version_dir = synchronizer._plan_version_dir("plan-backend-EMPTY-HIST")
    if version_dir.exists():
        shutil.rmtree(version_dir)

    with pytest.raises(ValueError, match="PLAN_VERSION_NOT_FOUND"):
        get_plan_versions(
            task_id="EMPTY-HIST",
            view="show",
            version=1,
            synchronizer=synchronizer,
            storage=storage,
        )

    with pytest.raises(ValueError, match="PLAN_VERSION_NOT_FOUND"):
        get_plan_versions(
            task_id="EMPTY-HIST",
            view="diff",
            version=2,
            compare_to=1,
            synchronizer=synchronizer,
            storage=storage,
        )


def test_get_task_context_plan_state_etag(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """view='full' возвращает plan_state_etag; step_plan меняет ETag, но сохраняет plan_version."""
    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="ETAG-1",
        title="ETag Test Plan",
        steps=["1. First step", "2. Second step"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )

    # view='summary' не возвращает plan_state_etag
    ctx_summary = get_task_context(
        task_id="ETAG-1",
        view="summary",
        synchronizer=synchronizer,
        storage=storage,
    )
    assert ctx_summary["plan_state_etag"] is None

    # view='full' возвращает SHA-256 ETag
    ctx1 = get_task_context(
        task_id="ETAG-1",
        view="full",
        synchronizer=synchronizer,
        storage=storage,
    )
    etag1 = ctx1["plan_state_etag"]
    assert etag1 is not None and len(etag1) == 64
    assert ctx1["plan"]["plan_version"] == 1

    # Закрытие шага меняет байты карточки (чекбокс), но не меняет plan_version
    step_plan(
        task_id="ETAG-1",
        step=1,
        status="done",
        synchronizer=synchronizer,
        storage=storage,
    )

    ctx2 = get_task_context(
        task_id="ETAG-1",
        view="full",
        synchronizer=synchronizer,
        storage=storage,
    )
    etag2 = ctx2["plan_state_etag"]
    assert etag2 is not None and len(etag2) == 64
    assert etag1 != etag2
    assert ctx2["plan"]["plan_version"] == 1

    # Несуществующий task_id возвращает plan_state_etag=None
    ctx_non = get_task_context(
        task_id="NON-EXIST",
        view="full",
        synchronizer=synchronizer,
        storage=storage,
    )
    assert ctx_non["plan_state_etag"] is None


def test_help_docs_includes_get_plan_versions() -> None:
    """get_help содержит get_plan_versions с описанием и ограничениями границы A8."""
    help_text = get_audit_help_text("get_plan_versions")
    assert "get_plan_versions" in help_text
    assert "PLAN_VERSION_NOT_FOUND" in help_text

    overview = get_audit_help_text("overview")
    assert "get_plan_versions" in overview


def test_restore_plan_version_success(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """v1 -> v2 -> restore(v1) -> v3 с restored_from=1, status='discussed'."""
    _, storage, synchronizer = temp_audit_env

    # 1. Создание v1
    rec = record_plan(
        task_id="RESTORE-OK",
        title="Title v1",
        steps=["1. Alpha step"],
        target_files=["src/alpha.py"],
        category="backend",
        details="Context v1",
        synchronizer=synchronizer,
        storage=storage,
    )
    assert rec["plan_version"] == 1
    plan_id = rec["id"]

    # 2. Обновление до v2
    u1 = update_plan(
        task_id="RESTORE-OK",
        title="Title v2",
        reason="Upgrade to v2",
        synchronizer=synchronizer,
        storage=storage,
    )
    assert u1["plan_version"] == 2

    ctx = get_task_context(
        task_id="RESTORE-OK",
        view="full",
        synchronizer=synchronizer,
        storage=storage,
    )
    etag_v2 = ctx["plan_state_etag"]
    assert etag_v2 is not None

    # 3. Восстановление v1 -> создаётся v3
    res = restore_plan_version(
        task_id="RESTORE-OK",
        version=1,
        expected_version=2,
        expected_state_etag=etag_v2,
        reason="Rollback to v1",
        synchronizer=synchronizer,
        storage=storage,
    )
    assert res["status"] == "restored"
    assert res["task_id"] == "RESTORE-OK"
    assert res["plan_id"] == plan_id
    assert res["new_version"] == 3
    assert res["restored_from"] == 1
    assert res["plan_status"] == "discussed"
    assert res["progress_reset"] is False
    assert res["object_type"] == "plan_card"
    assert "Объектом снимка является карточка плана" in res["message"]

    # Проверка текущего файла на диске: определение соответствует v1
    plan_file = Path(res["file_path"])
    assert plan_file.exists()
    current_plan = parse_plan_markdown(plan_file.read_text(encoding="utf-8"))
    assert current_plan.title == "Title v1"
    assert current_plan.plan_version == 3
    assert current_plan.status == PlanStatus.DISCUSSED

    # Проверка снимка v0003.md
    version_dir = synchronizer.plan_versions_dir / plan_id
    v3_path = version_dir / "v0003.md"
    assert v3_path.exists()
    meta3_dict, snap3_body = split_frontmatter(v3_path.read_text(encoding="utf-8"))
    assert meta3_dict is not None
    meta3 = PlanVersionMetadata.model_validate(meta3_dict)
    assert meta3.version == 3
    assert meta3.parent_version == 2
    assert meta3.source == "restore"
    assert meta3.restored_from == 1
    assert meta3.reason == "Rollback to v1"


def test_restore_plan_version_state_etag_conflict(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """get_task_context(view='full') -> step_plan меняет ETag -> restore падает с STATE_ETAG_CONFLICT."""
    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="RESTORE-ETAG",
        title="ETag Test",
        steps=["1. Step one", "2. Step two"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    update_plan(
        task_id="RESTORE-ETAG",
        title="ETag Test v2",
        reason="Bump v2",
        synchronizer=synchronizer,
        storage=storage,
    )

    ctx1 = get_task_context(
        task_id="RESTORE-ETAG",
        view="full",
        synchronizer=synchronizer,
        storage=storage,
    )
    old_etag = ctx1["plan_state_etag"]
    assert old_etag is not None

    # Шаг плана выполняется, меняя байты файла без смены версии плана
    step_plan(
        task_id="RESTORE-ETAG",
        step=1,
        status="done",
        synchronizer=synchronizer,
        storage=storage,
    )

    # Попытка restore со старым ETag отклоняется
    with pytest.raises(ValueError, match="STATE_ETAG_CONFLICT") as exc_info:
        restore_plan_version(
            task_id="RESTORE-ETAG",
            version=1,
            expected_version=2,
            expected_state_etag=old_etag,
            reason="Stale rollback",
            synchronizer=synchronizer,
            storage=storage,
        )
    assert old_etag in str(exc_info.value)


def test_restore_plan_version_expected_version_conflict(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Неверный expected_version падает с VERSION_CONFLICT."""
    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="RESTORE-VER",
        title="Ver Plan",
        steps=["1. Step one"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    ctx = get_task_context(
        task_id="RESTORE-VER",
        view="full",
        synchronizer=synchronizer,
        storage=storage,
    )
    etag = ctx["plan_state_etag"]
    assert etag is not None

    with pytest.raises(ValueError, match="VERSION_CONFLICT") as exc_info:
        restore_plan_version(
            task_id="RESTORE-VER",
            version=1,
            expected_version=99,
            expected_state_etag=etag,
            reason="Wrong expected_version",
            synchronizer=synchronizer,
            storage=storage,
        )
    assert "99" in str(exc_info.value)


def test_restore_plan_version_done_requires_reopen(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """План DONE без reopen=True падает с PLAN_REOPEN_REQUIRED; с reopen=True успешен."""
    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="RESTORE-DONE",
        title="Done Plan",
        steps=["1. Only step"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    update_plan(
        task_id="RESTORE-DONE",
        title="Done Plan v2",
        reason="V2 change",
        synchronizer=synchronizer,
        storage=storage,
    )
    step_plan(
        task_id="RESTORE-DONE",
        step=1,
        status="done",
        synchronizer=synchronizer,
        storage=storage,
    )
    update_plan(
        task_id="RESTORE-DONE",
        status="done",
        synchronizer=synchronizer,
        storage=storage,
    )

    ctx = get_task_context(
        task_id="RESTORE-DONE",
        view="full",
        synchronizer=synchronizer,
        storage=storage,
    )
    assert ctx["status"] == "done"
    etag = ctx["plan_state_etag"]
    assert etag is not None

    # Без reopen=True падает с PLAN_REOPEN_REQUIRED
    with pytest.raises(ValueError, match="PLAN_REOPEN_REQUIRED"):
        restore_plan_version(
            task_id="RESTORE-DONE",
            version=1,
            expected_version=2,
            expected_state_etag=etag,
            reason="Attempt without reopen",
            reopen=False,
            synchronizer=synchronizer,
            storage=storage,
        )

    # С reopen=True восстанавливается и переходит в discussed, перемещаясь из completed в active
    res = restore_plan_version(
        task_id="RESTORE-DONE",
        version=1,
        expected_version=2,
        expected_state_etag=etag,
        reason="Reopen and restore",
        reopen=True,
        synchronizer=synchronizer,
        storage=storage,
    )
    assert res["status"] == "restored"
    assert res["plan_status"] == "discussed"
    assert "active" in res["file_path"]
    assert "completed" not in res["file_path"]


def test_restore_plan_version_progress_conflict_and_reset(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """План с выполненными шагами при несовпадающих шагах падает с PROGRESS_CONFLICT; повтор с reset_progress=True успешен."""
    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="RESTORE-PROG",
        title="Progress Plan",
        steps=["1. First step", "2. Second step"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    update_plan(
        task_id="RESTORE-PROG",
        title="Progress Plan v2",
        add_steps=["3. Third step"],
        reason="Add 3rd step",
        synchronizer=synchronizer,
        storage=storage,
    )
    step_plan(
        task_id="RESTORE-PROG",
        step=1,
        status="done",
        note="Done first step",
        synchronizer=synchronizer,
        storage=storage,
    )

    ctx = get_task_context(
        task_id="RESTORE-PROG",
        view="full",
        synchronizer=synchronizer,
        storage=storage,
    )
    etag = ctx["plan_state_etag"]
    assert etag is not None

    # Попытка restore v1 (2 шага) из v2 (3 шага с выполненным шагом 1) без reset_progress
    with pytest.raises(ValueError, match="PROGRESS_CONFLICT") as exc_info:
        restore_plan_version(
            task_id="RESTORE-PROG",
            version=1,
            expected_version=2,
            expected_state_etag=etag,
            reason="Restore to 2 steps",
            reset_progress=False,
            synchronizer=synchronizer,
            storage=storage,
        )
    err_msg = str(exc_info.value)
    assert "PROGRESS_CONFLICT" in err_msg
    assert etag in err_msg
    assert "First step" in err_msg

    # Повторный вызов с reset_progress=True успешен
    res = restore_plan_version(
        task_id="RESTORE-PROG",
        version=1,
        expected_version=2,
        expected_state_etag=etag,
        reason="Restore with reset_progress",
        reset_progress=True,
        synchronizer=synchronizer,
        storage=storage,
    )
    assert res["status"] == "restored"
    assert res["progress_reset"] is True

    plan = parse_plan_markdown(Path(res["file_path"]).read_text(encoding="utf-8"))
    assert len(plan.steps) == 2
    assert not any(s.completed for s in plan.steps)
    assert not any(s.note for s in plan.steps)


def test_restore_plan_version_safe_progress_transfer(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """При совпадении шагов между целевой версией и текущим планом отметки прогресса переносятся безопасно."""
    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="RESTORE-TRANSFER",
        title="Transfer v1",
        steps=["1. Step A", "2. Step B"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    # v2 меняет только title
    update_plan(
        task_id="RESTORE-TRANSFER",
        title="Transfer v2",
        reason="Title update only",
        synchronizer=synchronizer,
        storage=storage,
    )
    # Отмечаем прогресс шага 1
    step_plan(
        task_id="RESTORE-TRANSFER",
        step=1,
        status="done",
        note="Step A finished",
        synchronizer=synchronizer,
        storage=storage,
    )

    ctx = get_task_context(
        task_id="RESTORE-TRANSFER",
        view="full",
        synchronizer=synchronizer,
        storage=storage,
    )
    etag = ctx["plan_state_etag"]
    assert etag is not None

    # Восстановление v1 (где шаги Step A и Step B такие же)
    res = restore_plan_version(
        task_id="RESTORE-TRANSFER",
        version=1,
        expected_version=2,
        expected_state_etag=etag,
        reason="Revert title back to v1",
        reset_progress=False,
        synchronizer=synchronizer,
        storage=storage,
    )
    assert res["status"] == "restored"
    assert res["progress_reset"] is False

    plan = parse_plan_markdown(Path(res["file_path"]).read_text(encoding="utf-8"))
    assert plan.title == "Transfer v1"
    assert plan.steps[0].completed is True
    assert plan.steps[0].note is not None and "Step A finished" in plan.steps[0].note
    assert plan.steps[1].completed is False


def test_restore_plan_version_history_corrupted(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Повреждение/удаление v0001 в цепочке блокирует restore с VERSION_HISTORY_CORRUPTED."""
    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="RESTORE-CORRUPT",
        title="Corrupt Plan",
        steps=["1. Step one"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    update_plan(
        task_id="RESTORE-CORRUPT",
        title="Corrupt Plan v2",
        reason="Bump v2",
        synchronizer=synchronizer,
        storage=storage,
    )

    ctx = get_task_context(
        task_id="RESTORE-CORRUPT",
        view="full",
        synchronizer=synchronizer,
        storage=storage,
    )
    etag = ctx["plan_state_etag"]
    assert etag is not None

    # 1. Удаление v0001.md
    version_dir = synchronizer.plan_versions_dir / "plan-backend-RESTORE-CORRUPT"
    v1_file = version_dir / "v0001.md"
    v1_file.unlink()

    with pytest.raises(ValueError, match="VERSION_HISTORY_CORRUPTED"):
        restore_plan_version(
            task_id="RESTORE-CORRUPT",
            version=1,
            expected_version=2,
            expected_state_etag=etag,
            reason="Restore with deleted v1",
            synchronizer=synchronizer,
            storage=storage,
        )

    # 2. Повреждение содержимого v0001.md
    v1_file.write_text("corrupted content without frontmatter", encoding="utf-8")
    with pytest.raises(ValueError, match="VERSION_HISTORY_CORRUPTED"):
        restore_plan_version(
            task_id="RESTORE-CORRUPT",
            version=1,
            expected_version=2,
            expected_state_etag=etag,
            reason="Restore with corrupt v1",
            synchronizer=synchronizer,
            storage=storage,
        )


def test_restore_plan_version_empty_reason(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Пустой reason вызывает ValueError('reason must not be empty')."""
    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="RESTORE-REASON",
        title="Reason Plan",
        steps=["1. Step one"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    ctx = get_task_context(
        task_id="RESTORE-REASON",
        view="full",
        synchronizer=synchronizer,
        storage=storage,
    )
    etag = ctx["plan_state_etag"]

    with pytest.raises(ValueError, match="reason must not be empty"):
        restore_plan_version(
            task_id="RESTORE-REASON",
            version=1,
            expected_version=1,
            expected_state_etag=etag,
            reason="",
            synchronizer=synchronizer,
            storage=storage,
        )

    with pytest.raises(ValueError, match="reason must not be empty"):
        restore_plan_version(
            task_id="RESTORE-REASON",
            version=1,
            expected_version=1,
            expected_state_etag=etag,
            reason="   ",
            synchronizer=synchronizer,
            storage=storage,
        )


def test_update_plan_definition_returns_approved_to_discussed(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Правка approved или in_progress плана возвращает его в discussed для нового согласования."""
    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="UPD-APPR",
        title="Approved Plan",
        steps=["1. Step one"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    # Согласование
    u_appr = update_plan(
        task_id="UPD-APPR",
        status="approved",
        synchronizer=synchronizer,
        storage=storage,
    )
    assert u_appr["plan_status"] == "approved"

    # Содержательная правка (title) без явного статуса
    u_edit = update_plan(
        task_id="UPD-APPR",
        title="Approved Plan Modified",
        reason="Modify definition",
        synchronizer=synchronizer,
        storage=storage,
    )
    assert u_edit["plan_status"] == "discussed"

    plan = parse_plan_markdown(Path(u_edit["file_path"]).read_text(encoding="utf-8"))
    assert plan.status == PlanStatus.DISCUSSED


def test_update_plan_done_requires_reopen(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """Правка DONE плана без reopen=True возвращает PLAN_REOPEN_REQUIRED."""
    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="UPD-DONE",
        title="Done Plan Test",
        steps=["1. Step one"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    update_plan(
        task_id="UPD-DONE",
        status="done",
        synchronizer=synchronizer,
        storage=storage,
    )

    # Любая содержательная правка на DONE без reopen=True отклоняется
    with pytest.raises(ValueError, match="PLAN_REOPEN_REQUIRED"):
        update_plan(
            task_id="UPD-DONE",
            title="Try new title",
            synchronizer=synchronizer,
            storage=storage,
        )

    with pytest.raises(ValueError, match="PLAN_REOPEN_REQUIRED"):
        update_plan(
            task_id="UPD-DONE",
            add_steps=["2. New step"],
            synchronizer=synchronizer,
            storage=storage,
        )

    with pytest.raises(ValueError, match="PLAN_REOPEN_REQUIRED"):
        update_plan(
            task_id="UPD-DONE",
            target_files=["src/extra.py"],
            synchronizer=synchronizer,
            storage=storage,
        )

    # С reopen=True правка принимается, статус становится discussed, файл в active/
    u_reopen = update_plan(
        task_id="UPD-DONE",
        title="Reopened Plan",
        reopen=True,
        reason="Reopening task",
        synchronizer=synchronizer,
        storage=storage,
    )
    assert u_reopen["plan_status"] == "discussed"
    assert "active" in u_reopen["file_path"]
    assert "completed" not in u_reopen["file_path"]


def test_update_plan_replace_steps_progress_conflict(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """replace_steps при наличии прогресса и несовпадающих шагах требует reset_progress=True."""
    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="UPD-REPLACE",
        title="Replace Steps Plan",
        steps=["1. Old step A", "2. Old step B"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    step_plan(
        task_id="UPD-REPLACE",
        step=1,
        status="done",
        note="Finished A",
        synchronizer=synchronizer,
        storage=storage,
    )

    # replace_steps без reset_progress падает с PROGRESS_CONFLICT
    with pytest.raises(ValueError, match="PROGRESS_CONFLICT") as exc_info:
        update_plan(
            task_id="UPD-REPLACE",
            replace_steps=["1. New step X", "2. New step Y"],
            reset_progress=False,
            synchronizer=synchronizer,
            storage=storage,
        )
    assert "PROGRESS_CONFLICT" in str(exc_info.value)

    # replace_steps с reset_progress=True успешен
    u_rep = update_plan(
        task_id="UPD-REPLACE",
        replace_steps=["1. New step X", "2. New step Y"],
        reset_progress=True,
        reason="Replace steps with reset",
        synchronizer=synchronizer,
        storage=storage,
    )
    assert u_rep["plan_version"] == 2

    plan = parse_plan_markdown(Path(u_rep["file_path"]).read_text(encoding="utf-8"))
    assert len(plan.steps) == 2
    assert plan.steps[0].title == "New step X"
    assert plan.steps[0].completed is False
    assert plan.steps[0].note is None


# =========================================================================
# AUDIT-PLAN-VERSIONS Этап 4 (T4): Синхронизация и отказоустойчивость
# =========================================================================


def test_manual_edit_sync_creates_next_version(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """A10: ручная правка определения на диске при sync_artifacts создаёт v2 и обновляет frontmatter."""
    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="MAN-V2",
        title="Original Title",
        steps=["1. Step one"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    plan_file = synchronizer.find_plan_file("MAN-V2")
    assert plan_file is not None

    content = plan_file.read_text(encoding="utf-8")
    modified_content = content.replace("Original Title", "Manually Edited Title")
    plan_file.write_text(modified_content, encoding="utf-8")

    indexed = synchronizer.sync_artifacts()
    assert indexed > 0

    # Проверяем обновление frontmatter в текущем файле
    updated_plan = parse_plan_markdown(plan_file.read_text(encoding="utf-8"))
    assert updated_plan.plan_version == 2
    assert updated_plan.title == "Manually Edited Title"

    # Проверяем снимок v0002.md
    version_dir = synchronizer._plan_version_dir(updated_plan.id)
    v2_path = version_dir / "v0002.md"
    assert v2_path.exists()

    raw_v2 = v2_path.read_text(encoding="utf-8")
    meta_dict, _ = split_frontmatter(raw_v2)
    assert meta_dict is not None
    meta_v2 = PlanVersionMetadata.model_validate(meta_dict)
    assert meta_v2.version == 2
    assert meta_v2.parent_version == 1
    assert meta_v2.reason == "manual edit"
    assert meta_v2.source == "manual"

    # Проверяем контекст задачи
    ctx = get_task_context(task_id="MAN-V2", synchronizer=synchronizer, storage=storage)
    assert ctx["found"] is True
    assert ctx["plan"]["plan_version"] == 2
    assert ctx["plan"]["title"] == "Manually Edited Title"


def test_manual_edit_mtime_collision_detected(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """A10: ручная правка даже с искусственно сохранёнными mtime_ns и размером определяется и версионируется."""
    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="MAN-MTIME",
        title="Title Alpha A",
        steps=["1. Step one"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    plan_file = synchronizer.find_plan_file("MAN-MTIME")
    assert plan_file is not None

    st_orig = plan_file.stat()
    orig_mtime_ns = st_orig.st_mtime_ns
    orig_size = st_orig.st_size

    content = plan_file.read_text(encoding="utf-8")
    # Заменяем "Title Alpha A" на "Title Alpha B" — размер байт в точности тот же
    modified_content = content.replace("Title Alpha A", "Title Alpha B")
    assert len(modified_content.encode("utf-8")) == orig_size
    plan_file.write_text(modified_content, encoding="utf-8")

    # Искусственно возвращаем прежний mtime_ns
    os.utime(plan_file, ns=(orig_mtime_ns, orig_mtime_ns))
    assert plan_file.stat().st_mtime_ns == orig_mtime_ns
    assert plan_file.stat().st_size == orig_size

    synchronizer.sync_artifacts()

    updated_plan = parse_plan_markdown(plan_file.read_text(encoding="utf-8"))
    assert updated_plan.plan_version == 2
    assert updated_plan.title == "Title Alpha B"

    version_dir = synchronizer._plan_version_dir(updated_plan.id)
    assert (version_dir / "v0002.md").exists()


def test_manual_status_or_progress_only_no_version_bump(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """A10: ручная правка статуса или отметки чекбокса шага не повышает plan_version."""
    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="MAN-NOBUMP",
        title="Title No Bump",
        steps=["Step one"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    plan_file = synchronizer.find_plan_file("MAN-NOBUMP")
    assert plan_file is not None

    content = plan_file.read_text(encoding="utf-8")
    # Меняем только чекбокс шага и бейдж/frontmatter статуса
    modified = content.replace("- [ ] 1. Step one", "- [x] 1. Step one")
    modified = modified.replace("status: discussed", "status: in_progress")
    modified = modified.replace("`discussed`", "`in_progress`")
    plan_file.write_text(modified, encoding="utf-8")

    synchronizer.sync_artifacts()

    current_plan = parse_plan_markdown(plan_file.read_text(encoding="utf-8"))
    assert current_plan.plan_version == 1
    assert current_plan.steps[0].completed is True

    version_dir = synchronizer._plan_version_dir(current_plan.id)
    assert not (version_dir / "v0002.md").exists()

    ctx = get_task_context(task_id="MAN-NOBUMP", synchronizer=synchronizer, storage=storage)
    assert ctx["plan"]["plan_version"] == 1
    assert ctx["plan"]["steps_done"] == 1


def test_move_recovery_target_exists(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """A9: сбой после создания target файла до удаления source: следующий get_unique завершает перенос."""
    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="REC-TGT",
        title="Move Target Exists",
        steps=["1. Step one"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    source = synchronizer.find_plan_file("REC-TGT")
    assert source is not None

    # Имитируем сбой: target создан в completed, source ещё остался в active
    target_dir = synchronizer.plans_completed_dir / "backend"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    target_content = source.read_text(encoding="utf-8").replace("status: discussed", "status: done")
    target.write_text(target_content, encoding="utf-8")

    old_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    new_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    plan_id = parse_plan_markdown(source.read_text(encoding="utf-8")).id

    marker_dir = synchronizer.plans_dir / ".moves"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / f"{plan_id}.json"
    marker_payload = {
        "artifact_id": plan_id,
        "task_id": "REC-TGT",
        "source": str(source.resolve()),
        "target": str(target.resolve()),
        "source_sha256": old_hash,
        "target_sha256": new_hash,
    }
    marker.write_text(json.dumps(marker_payload), encoding="utf-8")

    # Вызов get_unique без category находит и завершает перенос
    resolved = PlanRepository(synchronizer).get_unique("REC-TGT")
    assert resolved is not None
    assert resolved.resolve() == target.resolve()
    assert not source.exists()
    assert not marker.exists()
    assert target.exists()


def test_move_recovery_aborted_before_target(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """A9: сбой до создания target файла: маркер отменяется."""
    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="REC-ABORT",
        title="Move Aborted Before Target",
        steps=["1. Step one"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    source = synchronizer.find_plan_file("REC-ABORT")
    assert source is not None

    plan_id = parse_plan_markdown(source.read_text(encoding="utf-8")).id
    target = synchronizer.plans_completed_dir / "backend" / source.name
    assert not target.exists()

    old_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    marker_dir = synchronizer.plans_dir / ".moves"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / f"{plan_id}.json"
    marker_payload = {
        "artifact_id": plan_id,
        "task_id": "REC-ABORT",
        "source": str(source.resolve()),
        "target": str(target.resolve()),
        "source_sha256": old_hash,
        "target_sha256": "expected_new_hash_that_was_never_written",
    }
    marker.write_text(json.dumps(marker_payload), encoding="utf-8")

    resolved = PlanRepository(synchronizer).get_unique("REC-ABORT")
    assert resolved is not None
    assert resolved.resolve() == source.resolve()
    assert source.exists()
    assert not marker.exists()


def test_move_conflict_foreign_target(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """A9: чужой target файл вызывает MOVE_CONFLICT без удаления файлов."""
    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="REC-CONF",
        title="Move Conflict",
        steps=["1. Step one"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    source = synchronizer.find_plan_file("REC-CONF")
    assert source is not None

    plan_id = parse_plan_markdown(source.read_text(encoding="utf-8")).id
    target = synchronizer.plans_completed_dir / "backend" / source.name
    target_dir = target.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_text("FOREIGN CONTENT", encoding="utf-8")

    marker_dir = synchronizer.plans_dir / ".moves"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / f"{plan_id}.json"
    marker_payload = {
        "artifact_id": plan_id,
        "task_id": "REC-CONF",
        "source": str(source.resolve()),
        "target": str(target.resolve()),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "target_sha256": "different_hash",
    }
    marker.write_text(json.dumps(marker_payload), encoding="utf-8")

    with pytest.raises(ValueError, match="MOVE_CONFLICT"):
        PlanRepository(synchronizer).get_unique("REC-CONF")

    assert source.exists()
    assert target.exists()
    assert marker.exists()


def test_two_plan_files_without_marker_raises_ambiguous(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """A9: два файла с одним task_id без маркера вызывают AMBIGUOUS_PLAN (без выбора по mtime)."""
    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="AMB-NOMARKER",
        title="Plan One",
        steps=["1. Step one"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    f1 = synchronizer.find_plan_file("AMB-NOMARKER")
    assert f1 is not None

    # Создаём второй файл с тем же task_id
    completed_dir = synchronizer.plans_completed_dir / "backend"
    completed_dir.mkdir(parents=True, exist_ok=True)
    f2 = completed_dir / "plan-backend-AMB-NOMARKER-completed.md"
    f2.write_text(f1.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(AmbiguousPlanError) as exc_info:
        PlanRepository(synchronizer).get_unique("AMB-NOMARKER", category="backend")

    assert "AMBIGUOUS_PLAN" in str(exc_info.value)


def test_orphan_snapshot_quarantined(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """A-Orphan: наличие незавершённого v(N+1) при текущей версии N перемещается в .orphans/."""
    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="ORPH-1",
        title="Orphan Test",
        steps=["1. Step one"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    plan_file = synchronizer.find_plan_file("ORPH-1")
    assert plan_file is not None

    plan = parse_plan_markdown(plan_file.read_text(encoding="utf-8"))
    assert plan.plan_version == 1

    vdir = synchronizer._plan_version_dir(plan.id)
    orphan_snap = vdir / "v0002.md"
    orphan_snap.write_text("uncommitted snapshot v2", encoding="utf-8")

    quarantined = synchronizer.recover_orphan_snapshots(plan.id, plan.plan_version)
    assert len(quarantined) == 1
    assert not orphan_snap.exists()
    assert (vdir / ".orphans" / "v0002.md").exists()
    assert (vdir / "v0001.md").exists()


def test_plan_versions_not_indexed_in_fts(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """A2: снимки версий не попадают в FTS-индекс и не ищутся через search_artifacts."""
    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="FTS-VERS",
        title="UniqueSnapshotKeywordA",
        steps=["1. Step one"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    update_plan(
        task_id="FTS-VERS",
        title="UniqueSnapshotKeywordB",
        synchronizer=synchronizer,
        storage=storage,
    )

    synchronizer.sync_all()

    # Поиск по ключевому слову из версии 1
    res1 = search_artifacts(
        query="UniqueSnapshotKeywordA",
        synchronizer=synchronizer,
        storage=storage,
    )
    # Снимок v0001.md не должен выдаваться в результатах
    assert all("v0001" not in r.get("id", "") for r in res1["results"])
    assert all("/.agents/plan_versions/" not in r.get("file_path", "") for r in res1["results"])

    # Поиск по ключевому слову из версии 2
    res2 = search_artifacts(
        query="UniqueSnapshotKeywordB",
        synchronizer=synchronizer,
        storage=storage,
    )
    assert res2["count"] == 1
    assert res2["results"][0]["task_id"] == "FTS-VERS"
    assert "/.agents/plan_versions/" not in res2["results"][0]["file_path"]


def test_sync_error_on_broken_plan_reported_in_get_task_context(
    temp_audit_env: tuple[Path, AuditStorage, AuditSynchronizer],
) -> None:
    """A10: при ошибке синхронизации исходный файл остаётся цел, а get_task_context сообщает SYNC_ERROR."""
    _, storage, synchronizer = temp_audit_env

    record_plan(
        task_id="SYNC-ERR",
        title="Broken Plan Test",
        steps=["1. Step one"],
        category="backend",
        synchronizer=synchronizer,
        storage=storage,
    )
    plan_file = synchronizer.find_plan_file("SYNC-ERR")
    assert plan_file is not None

    broken_content = "title: [broken yaml"
    plan_file.write_text(broken_content, encoding="utf-8")

    synchronizer.sync_all()

    # get_task_context должен сообщать об ошибке синхронизации вместо возврата устаревшего плана
    with pytest.raises(ValueError, match="SYNC_ERROR"):
        get_task_context(task_id="SYNC-ERR", synchronizer=synchronizer, storage=storage)

    # Исходный ручной файл не повреждён и остался на диске
    assert plan_file.read_text(encoding="utf-8") == broken_content
