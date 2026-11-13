"""Шаг 2 плана AUDIT-VERIFY-2026-10: A1+A16 (update_debt_status через rebuild_card)
и A2 (PLAN_EXISTS в save_plan/record_plan).

A1: update_issue_status обязан сохранять кастомные body-секции и неизвестные
ключи frontmatter — точечная мутация через rebuild_card вместо полной
ресериализации serialize_audit_markdown.
A16: updated_at обновляется при любом изменении, не только с resolution_note.
A2: повторная запись плана с тем же task_id — fail-fast PLAN_EXISTS (внутри
task_id-лока), молчаливая перезапись запрещена; правки — через update_plan.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_audit.models import AuditStatus, PlanArtifact, PlanStep
from mcp_audit.parser import parse_plan_markdown
from mcp_audit.storage import AuditStorage
from mcp_audit.synchronizer import AuditSynchronizer


CARD_WITH_CUSTOM_SECTIONS = """---
id: aud-backend-a1card
title: Карточка с кастомными секциями
category: backend
priority: important
autonomy: high
complexity: M
status: planned
custom_key: сохранить-меня
tags:
  - demo
created_at: '2026-10-01'
---

# aud-backend-a1card: Карточка с кастомными секциями

## ❌ Симптом

Симптом для теста.

## 🔍 Техническая первопричина

Причина для теста.

## 🛠 Предлагаемое решение

Решение для теста.

## ⚠️ Риски

Кастомная секция, которая обязана выжить при обновлении статуса.

## 🔗 Ссылки

- https://example.com/issue/1
"""


@pytest.fixture()
def debt_env(tmp_path: Path) -> tuple[Path, AuditStorage, AuditSynchronizer]:
    audit_dir = tmp_path / ".agents" / "audit"
    storage = AuditStorage(tmp_path / "cache.db")
    sync = AuditSynchronizer(storage=storage, audit_dir=audit_dir)
    card_dir = audit_dir / "todo" / "backend"
    card_dir.mkdir(parents=True, exist_ok=True)
    return card_dir, storage, sync


class TestA1A16UpdateDebtStatus:
    """A1: сохранность тела карточки; A16: updated_at при любом изменении."""

    def test_custom_sections_and_unknown_keys_survive(self, debt_env) -> None:
        card_dir, _, sync = debt_env
        card = card_dir / "aud-backend-a1card.md"
        card.write_text(CARD_WITH_CUSTOM_SECTIONS, encoding="utf-8")

        _, new_path = sync.update_issue_status("aud-backend-a1card", AuditStatus.IN_PROGRESS)
        result = new_path.read_text(encoding="utf-8")

        # A1: кастомные body-секции выживают
        assert "## ⚠️ Риски" in result
        assert "Кастомная секция" in result
        assert "## 🔗 Ссылки" in result
        assert "https://example.com/issue/1" in result
        # A1: неизвестные ключи frontmatter выживают
        assert "custom_key: сохранить-меня" in result
        assert "tags:" in result and "- demo" in result
        # статус и canonical-секции на месте
        assert "status: in_progress" in result
        assert "## ❌ Симптом" in result
        assert "## 🛠 Предлагаемое решение" in result

    def test_unknown_keys_survive_move_to_done(self, debt_env) -> None:
        """Перемещение todo→done не затирает кастомные секции (тот же код-путь)."""
        card_dir, _, sync = debt_env
        card = card_dir / "aud-backend-a1card.md"
        card.write_text(CARD_WITH_CUSTOM_SECTIONS, encoding="utf-8")

        _, new_path = sync.update_issue_status(
            "aud-backend-a1card", AuditStatus.COMPLETED, resolution_note="Закрыто"
        )
        assert "done" in str(new_path)
        result = new_path.read_text(encoding="utf-8")
        assert "## ⚠️ Риски" in result
        assert "custom_key: сохранить-меня" in result
        assert "Закрыто" in result

    def test_updated_at_changes_without_note(self, debt_env) -> None:
        """A16: updated_at обновляется при смене статуса БЕЗ resolution_note."""
        card_dir, _, sync = debt_env
        card = card_dir / "aud-backend-a1card.md"
        card.write_text(CARD_WITH_CUSTOM_SECTIONS, encoding="utf-8")

        _, new_path = sync.update_issue_status("aud-backend-a1card", AuditStatus.IN_PROGRESS)
        result = new_path.read_text(encoding="utf-8")
        assert "status: in_progress" in result
        # created_at остался, updated_at сменился с '2026-10-01'
        assert "created_at: '2026-10-01'" in result
        assert "updated_at: '2026-10-01'" not in result


@pytest.fixture()
def plan_env(tmp_path: Path) -> tuple[AuditStorage, AuditSynchronizer]:
    audit_dir = tmp_path / ".agents" / "audit"
    storage = AuditStorage(tmp_path / "cache.db")
    sync = AuditSynchronizer(storage=storage, audit_dir=audit_dir)
    return storage, sync


def _plan(task_id: str, title: str, category: str = "general") -> PlanArtifact:
    return PlanArtifact(
        id=f"plan-{category}-{task_id}",
        task_id=task_id,
        title=title,
        category=category,
        status="discussed",
        steps=[PlanStep(index=1, title="Шаг 1", completed=False)],
        content="Цель",
        created_at="2026-10-28",
        updated_at="2026-10-28",
    )


class TestA2PlanExists:
    """A2: молчаливая перезапись плана по task_id запрещена."""

    def test_duplicate_task_id_rejected(self, plan_env) -> None:
        _, sync = plan_env

        first = sync.save_plan(_plan("PROJ-1", "Первый план"))
        assert first.exists()

        with pytest.raises(ValueError, match="PLAN_EXISTS"):
            sync.save_plan(_plan("PROJ-1", "Перезапись"))

        # Файл не изменён
        plan = parse_plan_markdown(first.read_text(encoding="utf-8"), file_path=first)
        assert plan.title == "Первый план"
        assert len(plan.steps) == 1

    def test_duplicate_task_id_other_category_rejected(self, plan_env) -> None:
        """Уникальность task_id глобальна: reject даже при другой категории."""
        _, sync = plan_env

        sync.save_plan(_plan("SHARED-1", "Backend plan", category="backend"))
        with pytest.raises(ValueError, match="PLAN_EXISTS"):
            sync.save_plan(_plan("SHARED-1", "Core plan", category="core"))

    def test_update_plan_still_works(self, plan_env) -> None:
        """Путь правок остаётся: update_plan_details меняет существующий план."""
        _, sync = plan_env

        sync.save_plan(_plan("PROJ-2", "Старый заголовок"))
        plan, path = sync.update_plan_details("PROJ-2", title="Новый заголовок")
        assert plan.title == "Новый заголовок"

        plan2 = parse_plan_markdown(path.read_text(encoding="utf-8"), file_path=path)
        assert plan2.title == "Новый заголовок"

    def test_internal_upsert_exist_ok(self, plan_env) -> None:
        """Внутренний upsert-контракт сохранён: exist_ok=True перезаписывает тот же файл."""
        _, sync = plan_env

        first = sync.save_plan(_plan("PROJ-3", "Title A"))
        second = sync.save_plan(_plan("PROJ-3", "Title B"), exist_ok=True)

        assert second == first
        plan = parse_plan_markdown(first.read_text(encoding="utf-8"), file_path=first)
        assert plan.title == "Title B"


class TestA13CrossCategoryTaskId:
    """A13: одинаковый task_id в разных категориях — отказ без category,
    точный выбор с category, чужой план не мутируется."""

    def _seed_two_plans(self, plan_env) -> tuple[Path, Path]:
        from mcp_audit.models import PlanStatus
        from mcp_audit.parser import serialize_plan_markdown

        _, sync = plan_env
        paths = []
        for cat in ("backend", "core"):
            plan = _plan("SHARED-13", f"План {cat}", category=cat)
            plan = plan.model_copy(update={"status": PlanStatus.DISCUSSED})
            target = sync._plan_target_dir(plan.status, cat)
            target.mkdir(parents=True, exist_ok=True)
            path = target / f"{plan.id}.md"
            path.write_text(serialize_plan_markdown(plan), encoding="utf-8")
            paths.append(path)
        return paths[0], paths[1]

    def test_ambiguous_without_category_raises(self, plan_env) -> None:
        _, sync = plan_env
        self._seed_two_plans(plan_env)

        with pytest.raises(ValueError, match="AMBIGUOUS_PLAN"):
            sync.find_plan_file("SHARED-13")

    def test_ambiguous_indexed_plans_without_category_raises(self, plan_env) -> None:
        """A13-блокер: два ПРОИНДЕКСИРОВАННЫХ плана одного task_id —
        индексный LIMIT-1-шорткат не должен возвращать произвольный план."""
        storage, sync = plan_env
        self._seed_two_plans(plan_env)
        # индексируем оба (фаза apply через _index_artifact_file)
        for path in (sync.plans_active_dir / "backend", sync.plans_active_dir / "core"):
            for p in path.glob("*.md"):
                sync._index_artifact_file(p, "active", "plan")

        # планы реально в индексе
        hits = storage.find_plans_by_task_id("SHARED-13")
        assert len(hits) == 2, hits

        with pytest.raises(ValueError, match="AMBIGUOUS_PLAN"):
            sync.find_plan_file("SHARED-13")

    def test_category_selects_exact_plan(self, plan_env) -> None:
        _, sync = plan_env
        be, core = self._seed_two_plans(plan_env)

        found = sync.find_plan_file("SHARED-13", category="core")
        assert found is not None
        assert found == core

    def test_update_plan_details_mutates_only_target_category(self, plan_env) -> None:
        _, sync = plan_env
        be, core = self._seed_two_plans(plan_env)

        _, path = sync.update_plan_details("SHARED-13", title="Обновлён core", category="core")
        assert path == core
        assert "Обновлён core" not in be.read_text(encoding="utf-8")
