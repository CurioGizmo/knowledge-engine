"""PlanRepository — единственный публичный контракт доступа к планам.

AUDIT-ARCH-2026-10 (шаг 3): до этого решение «какой план для task_id»
было размазано по synchronizer.find_plan_file / save_plan /
_raise_if_plan_exists и вызывалось из server и use-cases напрямую —
каждый решал неоднозначность по-своему (и один SQLite-шорткат обходил
политику). Теперь атомарные операции планов выражены здесь:

- ``get_unique(task_id, category=None)`` — единственный lookup:
  1 план → он, 0 → None, >1 без category → AmbiguousPlanError;
- ``create_unique(plan)`` — атомарное создание с глобальной
  уникальностью task_id (task_id-лок на ВСЮ критическую секцию,
  artifact-лок вложен внутрь; повторный create → PlanExistsError).

Synchronizer остаётся реализацией (индексация, дисковый скан,
перемещение) и используется репозиторием как делегат, но его
план-методы больше не являются публичным контрактом для consumers.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from mcp_audit.models import PlanArtifact, PlanStatus
from mcp_core.sanitizer import normalize_category_slug, sanitize_task_id, title_to_slug

if TYPE_CHECKING:
    from mcp_audit.synchronizer import AuditSynchronizer


class AmbiguousPlanError(ValueError):
    """Несколько планов одного task_id при неуказанной категории."""

    def __init__(self, task_id: str, matches: list[Path]) -> None:
        super().__init__(
            f"AMBIGUOUS_PLAN: task_id '{task_id}' найден в {len(matches)} планах "
            f"разных категорий: {[str(p) for p in matches]}. "
            f"Укажите category для выбора нужного плана."
        )
        self.task_id = task_id
        self.matches = matches


class PlanExistsError(ValueError):
    """План с данным task_id уже существует (глобальная уникальность)."""

    def __init__(self, task_id: str, existing: Path | None = None) -> None:
        super().__init__(
            f"PLAN_EXISTS: план для task_id '{task_id}' уже существует"
            + (f" ({existing})" if existing else "")
            + " — используйте update_plan/step_plan для правок, "
            "record_plan только создаёт новые планы"
        )
        self.task_id = task_id
        self.existing = existing


class PlanRepository:
    """Публичный контракт планов: атомарный create_unique / get_unique."""

    def __init__(self, synchronizer: AuditSynchronizer) -> None:
        self._sync = synchronizer

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------
    def get_unique(self, task_id: str, category: str | None = None) -> Path | None:
        """Единственный lookup плана: 1 → он; 0 → None; >1 (без category) → AmbiguousPlanError.

        Indexed- и filesystem-поиск имеют одну семантику: индексные хиты
        не шорткатят решение, а лишь пополняют общий набор совпадений.
        """
        clean_task = sanitize_task_id(task_id)
        clean_cat = normalize_category_slug(category) if category else None

        index_matches = self._plan_index_matches(clean_task, clean_cat)
        matches = index_matches + [
            p for p in self._plan_disk_matches(clean_task, clean_cat) if p not in index_matches
        ]
        unique = {p.resolve(): p for p in matches}
        matches = list(unique.values())

        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1 and category:
            # внутри одной категории (дубли slug) — детерминированно
            # первый по mtime, как прежде
            matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return matches[0]
        if len(matches) > 1:
            raise AmbiguousPlanError(clean_task, matches)
        return None

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------
    def create_unique(self, plan: PlanArtifact) -> Path:
        """Атомарное создание плана с глобальной уникальностью task_id.

        Reject (PlanExistsError) безусловен: для правок существует
        update_plan / step_plan. Пред-проверка здесь — fast-fail без
        захвата лока; собственно атомарность обеспечивается
        ``save_plan``: задача проверяется под task_id-локом повторно
        (дважды — под task- и artifact-локами), так что окно TOCTOU
        между пред-проверкой и записью закрыто внутри реализации.
        """
        clean_task = sanitize_task_id(plan.task_id)
        existing = self.get_unique(clean_task)
        if existing is not None:
            raise PlanExistsError(clean_task, existing)
        return self._sync.save_plan(plan)

    # ------------------------------------------------------------------
    # Делегаты к реализации (synchronizer)
    # ------------------------------------------------------------------
    def _plan_index_matches(self, clean_task: str, clean_cat: str | None) -> list[Path]:
        return self._sync._plan_index_matches(clean_task, clean_cat)

    def _plan_disk_matches(self, clean_task: str, clean_cat: str | None) -> list[Path]:
        return self._sync._plan_disk_matches(clean_task, clean_cat)

    def update(self, plan: PlanArtifact) -> Path:
        """Пересохранение существующего плана (upsert по artifact_id)."""
        return self._sync.save_plan(plan, exist_ok=True)

    def target_dir(self, status: PlanStatus, category: str) -> Path:
        return self._sync._plan_target_dir(status, category)

    def filename_for(self, plan: PlanArtifact) -> str:
        clean_slug = title_to_slug(plan.title, fallback="")
        clean_cat = normalize_category_slug(plan.category)
        clean_task = sanitize_task_id(plan.task_id)
        if clean_slug:
            return f"plan-{clean_cat}-{clean_task}-{clean_slug}.md"
        return f"plan-{clean_cat}-{clean_task}.md"
