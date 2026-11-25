"""Модели данных и структуры Pydantic для MCP Audit Engine."""

from __future__ import annotations

import re
import secrets
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mcp_core.sanitizer import normalize_category_slug, sanitize_task_id

__all__ = [
    "AuditAutonomy",
    "AuditComplexity",
    "AuditIssue",
    "AuditPriority",
    "AuditStatus",
    "KnownDebtCheckResponse",
    "PlanArtifact",
    "PlanStatus",
    "PlanStep",
    "PlanVersionMetadata",
    "PlanVersionsResponse",
    "RestorePlanVersionResponse",
    "ReviewArtifact",
    "ReviewComment",
    "ReviewVerdict",
    "StepPlanResponse",
    "TaskContextResponse",
    "generate_audit_id",
    "generate_plan_id",
    "generate_review_id",
]


class AuditPriority(StrEnum):
    """Приоритет задачи аудита."""

    CRITICAL = "critical"
    IMPORTANT = "important"
    SUGGESTION = "suggestion"


class AuditAutonomy(StrEnum):
    """Степень автономности решения задачи."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AuditStatus(StrEnum):
    """Статус жизненного цикла задачи аудита."""

    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    OBSOLETE = "obsolete"


class AuditComplexity(StrEnum):
    """Оценка сложности задачи."""

    L = "L"
    M = "M"
    H = "H"


class PlanStatus(StrEnum):
    """Статус жизненного цикла плана разработки."""

    DISCUSSED = "discussed"  # Обсуждено (черновик, сбор требований)
    APPROVED = "approved"  # Утверждено (согласовано к разработке)
    IN_PROGRESS = "in_progress"  # В процессе реализации (пишется код)
    DONE = "done"  # Сделано (реализовано и проверено)
    CANCELLED = "cancelled"  # Отменено


class ReviewVerdict(StrEnum):
    """Вердикт код-ревью."""

    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    COMMENTED = "commented"


def generate_audit_id(
    category: str | None = None,
    existing_ids: set[str] | None = None,
) -> str:
    """Генерирует бесконфликтный ID вида aud-<category>-<hash4> с криптографической энтропией."""
    eff = category or "general"
    clean_category = normalize_category_slug(eff)

    for _ in range(100):
        h = secrets.token_hex(2)  # 4 hex-символа
        candidate = f"aud-{clean_category}-{h}"
        if not existing_ids or candidate not in existing_ids:
            return candidate

    return f"aud-{clean_category}-{secrets.token_hex(4)}"


def generate_plan_id(category: str, task_id: str) -> str:
    """Генерирует детерминированный ID плана вида plan-<category>-<clean_task_id>."""
    clean_cat = normalize_category_slug(category) if category else "general"
    clean_task = sanitize_task_id(task_id)
    return f"plan-{clean_cat}-{clean_task}"


def generate_review_id(
    category: str,
    task_id: str | None = None,
    round: int = 1,
    pr_number: int | str | None = None,
) -> str:
    """Генерирует детерминированный ID ревью-лога."""
    clean_cat = normalize_category_slug(category) if category else "general"
    parts = ["rev", clean_cat]
    if task_id:
        parts.append(sanitize_task_id(task_id))
    if pr_number is not None:
        clean_pr = re.sub(r"[^0-9]", "", str(pr_number))
        if clean_pr:
            parts.append(f"pr{clean_pr}")
    parts.append(f"r{round}")
    return "-".join(parts)


class AuditIssue(BaseModel):
    """Карточка задачи аудита / технического долга."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    title: str
    category: str
    priority: AuditPriority = AuditPriority.IMPORTANT
    autonomy: AuditAutonomy = AuditAutonomy.HIGH
    complexity: AuditComplexity = AuditComplexity.M
    status: AuditStatus = AuditStatus.PLANNED
    blocked_by: list[str] = Field(default_factory=list)
    target_file: str | None = None
    symbol: str | None = None
    symptom: str
    root_cause: str | None = None
    solution: str | None = None
    pr_url: str | None = None
    plan_path: str | None = None
    task_id: str | None = None
    aliases: list[str] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
    score: float | None = Field(
        default=None, description="Ранговый балл релевантности BM25 (ранг, не вероятность)"
    )

    def __init__(
        self,
        id: str,
        title: str,
        category: str | None = None,
        priority: AuditPriority = AuditPriority.IMPORTANT,
        autonomy: AuditAutonomy = AuditAutonomy.HIGH,
        complexity: AuditComplexity = AuditComplexity.M,
        status: AuditStatus = AuditStatus.PLANNED,
        blocked_by: list[str] | None = None,
        target_file: str | None = None,
        symbol: str | None = None,
        symptom: str = "",
        root_cause: str | None = None,
        solution: str | None = None,
        pr_url: str | None = None,
        plan_path: str | None = None,
        task_id: str | None = None,
        aliases: list[str] | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
        score: float | None = None,
        **data: Any,
    ) -> None:
        eff_cat = category or "general"
        super().__init__(
            id=id,
            title=title,
            category=eff_cat,
            priority=priority,
            autonomy=autonomy,
            complexity=complexity,
            status=status,
            blocked_by=blocked_by or [],
            target_file=target_file,
            symbol=symbol,
            symptom=symptom,
            root_cause=root_cause,
            solution=solution,
            pr_url=pr_url,
            plan_path=plan_path,
            task_id=task_id,
            aliases=aliases or [],
            created_at=created_at,
            updated_at=updated_at,
            score=score,
            **data,
        )


class PlanStep(BaseModel):
    """Шаг плана разработки."""

    model_config = ConfigDict(extra="ignore")

    index: int
    title: str
    completed: bool = False
    note: str | None = None


class PlanArtifact(BaseModel):
    """Артефакт плана разработки."""

    model_config = ConfigDict(extra="ignore")

    id: str
    schema_version: int = 1
    task_id: str
    title: str
    category: str = "general"
    status: PlanStatus = PlanStatus.DISCUSSED
    plan_version: int = 1
    definition_sha256: str | None = None
    pr_url: str | None = None
    target_files: list[str] = Field(default_factory=list)
    related_debts: list[str] = Field(default_factory=list)
    steps: list[PlanStep] = Field(default_factory=list)
    content: str = ""
    created_at: str | None = None
    updated_at: str | None = None

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def _coerce_date_to_str(cls, v: Any) -> str | None:
        if v is None:
            return None
        if hasattr(v, "isoformat"):
            return str(v.isoformat())
        return str(v)


class ReviewComment(BaseModel):
    """Замечание код-ревью."""

    model_config = ConfigDict(extra="forbid")

    file_path: str
    line: int | None = None
    symptom: str
    status: Literal["open", "resolved", "deferred"] = "open"


class ReviewArtifact(BaseModel):
    """Артефакт раунда код-ревью."""

    model_config = ConfigDict(extra="ignore")

    id: str
    task_id: str | None = None
    category: str = "general"
    round: int = Field(
        default=1,
        ge=1,
        le=50,
        description="Номер раунда ревью (1–50); при переполнении создайте новый PR/задачу",
    )
    verdict: ReviewVerdict = ReviewVerdict.APPROVED
    summary: str = ""
    comments: list[ReviewComment] = Field(default_factory=list)
    pr_url: str | None = None
    reviewer: str | None = None
    spawned_debts: list[str] = Field(default_factory=list)
    created_at: str | None = None

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_date_to_str(cls, v: Any) -> str | None:
        if v is None:
            return None
        if hasattr(v, "isoformat"):
            return str(v.isoformat())
        return str(v)


class PlanVersionMetadata(BaseModel):
    """Метаданные снимка версии плана."""

    model_config = ConfigDict(extra="ignore")

    version: int
    parent_version: int | None = None
    created_at_utc: str
    reason: str
    source: Literal["mcp", "manual", "restore"]
    restored_from: int | None = None
    definition_sha256: str


class StepPlanResponse(BaseModel):
    """Ответ инструмента step_plan."""

    status: str = "ok"
    task_id: str
    progress: str  # "2/5"
    current_step: str | None = None
    remaining_steps: list[str] = Field(default_factory=list)
    status_changed: bool = False
    new_plan_status: PlanStatus | None = None
    file_path: str
    plan_version: int = 1


class TaskContextResponse(BaseModel):
    """Единый агрегированный контекст задачи для LLM."""

    found: bool
    task_id: str | None = None
    pr_url: str | None = None
    status: Literal["discussed", "approved", "in_progress", "done", "cancelled", "unknown"]
    plan: dict[str, Any] | None = None
    reviews: list[dict[str, Any]] = Field(default_factory=list)
    debts: list[dict[str, Any]] = Field(default_factory=list)
    plan_state_etag: str | None = None
    sync_error: str | None = None


class PlanVersionsResponse(BaseModel):
    """Ответ инструмента get_plan_versions."""

    model_config = ConfigDict(extra="ignore")

    task_id: str
    plan_id: str
    object_type: Literal["plan_card"] = "plan_card"
    current_version: int | None = None
    total_count: int | None = None
    versions: list[PlanVersionMetadata] = Field(default_factory=list)
    version: int | None = None
    metadata: PlanVersionMetadata | None = None
    raw_markdown: str | None = None
    compare_to: int | None = None
    diff: str | None = None


class RestorePlanVersionResponse(BaseModel):
    """Ответ инструмента restore_plan_version."""

    model_config = ConfigDict(extra="ignore")

    status: Literal["restored"] = "restored"
    task_id: str
    plan_id: str
    new_version: int
    restored_from: int
    plan_status: str = "discussed"
    progress_reset: bool = False
    object_type: Literal["plan_card"] = "plan_card"
    message: str = "Объектом снимка является карточка плана (.agents/plans/...)"
    file_path: str | None = None


class KnownDebtCheckResponse(BaseModel):
    """Ответ инструмента проверки известного долга для code-review."""

    found: bool
    id: str | None = None
    title: str | None = None
    status: str | None = None
    is_known_debt: bool = False
    is_regression: bool = False
    target_file: str | None = None
    symbol: str | None = None
    message: str | None = None
    details: dict[str, Any] | None = None
