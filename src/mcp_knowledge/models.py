"""Pydantic DTO модели для MCP Knowledge Engine.

Полностью изолированный модуль без сторонних зависимостей (только Pydantic).
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mcp_core.sanitizer import (
    normalize_category_slug as normalize_category_slug,
)
from mcp_core.sanitizer import (
    safe_category_slug as safe_category_slug,
)


def _validate_non_empty_stripped(v: Any) -> Any:
    """Рейт-валидатор: строка должна быть непустой после strip() (общий для DTO карточек и гипотез)."""
    if isinstance(v, str):
        v_stripped = v.strip()
        if not v_stripped:
            raise ValueError("Поле не может быть пустым или состоять только из пробелов")
        return v_stripped
    return v


def _coerce_confidence(v: Any, default: float) -> float:
    """Рейт-валидатор confidence: число в [0.0, 1.0] с дефолтом для None (общий для карточек и гипотез)."""
    if v is None:
        return default
    try:
        val = float(v)
        if not (0.0 <= val <= 1.0):
            raise ValueError(f"confidence must be between 0.0 and 1.0, got {val}")
        return val
    except (TypeError, ValueError) as e:
        raise ValueError(f"Недопустимое значение confidence: {v}") from e


def _validate_category_slug(v: Any) -> str:
    """Рейт-валидатор category: нормализация в slug (общий для карточек и гипотез)."""
    return normalize_category_slug(v)


class KnowledgeCategory(StrEnum):
    AUDIO = "audio"
    ML = "ml"
    DATABASE = "database"
    DESKTOP = "desktop"
    UI = "ui"
    CICD = "cicd"
    PLATFORM = "platform"
    RESEARCH = "research"
    BACKEND = "backend"
    FRONTEND = "frontend"
    SECURITY = "security"
    GENERAL = "general"


class KnowledgeType(StrEnum):
    LESSON = "lesson"
    DECISION = "decision"
    RESEARCH = "research"
    HINT = "hint"
    ANTIPATTERN = "antipattern"


class KnowledgeStatus(StrEnum):
    ACTIVE = "active"
    SUSPECT = "suspect"
    VERIFIED = "verified"
    SUPERSEDED = "superseded"
    DISPUTED = "disputed"
    HYPOTHESIS = "hypothesis"
    REFUTED = "refuted"
    DRAFT = "draft"


class StorageUnavailableError(Exception):
    """Исключение при недоступности SQLite/FTS5 хранилища."""


class KnowledgeSource(StrEnum):
    USER_CONFIRMED = "user_confirmed"
    AUTO_INFERRED = "auto_inferred"


HUMAN_CALLER_NAMES: frozenset[str] = frozenset(
    {"human", "user", "developer", "administrator", "admin"}
)


class GeneratedBy(StrEnum):
    HUMAN = "human"
    PROCESS = "process"


class GeneratedMetadata(BaseModel):
    by: GeneratedBy = GeneratedBy.PROCESS
    agent: str | None = None
    created_at: str

    @field_validator("created_at", mode="before")
    @classmethod
    def coerce_created_at(cls, v: Any) -> Any:
        if isinstance(v, (date, datetime)):
            return v.isoformat()
        return str(v) if v is not None else ""


class VerificationRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    by: str = "human"
    at: str
    note: str | None = None
    action: str | None = None

    @field_validator("at", mode="before")
    @classmethod
    def coerce_at(cls, v: Any) -> Any:
        if isinstance(v, (date, datetime)):
            return v.isoformat()
        return str(v) if v is not None else ""

    def __getitem__(self, item: str) -> Any:
        if item not in self.__class__.model_fields:
            raise KeyError(item)
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        if item not in self.__class__.model_fields:
            return default
        return getattr(self, item, default)

    def keys(self) -> list[str]:
        return list(self.__class__.model_fields.keys())

    def __contains__(self, item: str) -> bool:
        return item in self.__class__.model_fields


class CardFrontmatter(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    title: str
    category: str = ""
    type: KnowledgeType
    status: KnowledgeStatus = KnowledgeStatus.ACTIVE
    confidence: float = Field(default=0.75, ge=0.0, le=1.0, exclude=True)
    task_id: str | None = None
    refuted_by: str | None = None
    refutation_reason: str | None = None
    generated: GeneratedMetadata
    verified: list[VerificationRecord] = Field(default_factory=list)
    stale_after: str | None = None
    supersedes: str | None = None
    superseded_by: str | None = None
    related_to: list[str] = Field(default_factory=list)
    derived_from: str | None = None
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @field_validator("confidence", mode="before")
    @classmethod
    def coerce_confidence(cls, v: Any) -> float:
        return _coerce_confidence(v, default=0.75)

    @field_validator("category", mode="before")
    @classmethod
    def validate_category(cls, v: Any) -> str:
        return _validate_category_slug(v)

    @field_validator("stale_after", mode="before")
    @classmethod
    def coerce_stale_after(cls, v: Any) -> Any:
        if isinstance(v, (date, datetime)):
            return v.isoformat()
        return v

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        if not data.get("category"):
            data["category"] = ""

        # Normalize source -> generated
        if "generated" not in data:
            source_val = data.pop("source", None)
            created_at = data.pop("created_at", "")
            if isinstance(created_at, (date, datetime)):
                created_at = created_at.isoformat()
            by_val = GeneratedBy.HUMAN if source_val == "user_confirmed" else GeneratedBy.PROCESS
            data["generated"] = {
                "by": by_val,
                "created_at": created_at,
            }
        else:
            if "created_at" in data:
                data.pop("created_at")
            if "source" in data:
                data.pop("source")

        # Normalize expires_at -> stale_after
        if "stale_after" not in data and "expires_at" in data:
            stale = data.pop("expires_at")
            if isinstance(stale, (date, datetime)):
                stale = stale.isoformat()
            data["stale_after"] = stale
        elif "expires_at" in data:
            data.pop("expires_at")

        # Normalize status: verified -> verified audit entry
        if data.get("status") == "verified" and not data.get("verified"):
            gen = data.get("generated")
            if isinstance(gen, dict):
                created_at = gen.get("created_at", "")
            elif hasattr(gen, "created_at"):
                created_at = getattr(gen, "created_at", "")
            else:
                created_at = ""
            data["verified"] = [
                {
                    "by": "human",
                    "at": created_at,
                    "note": "Verified from legacy record",
                    "action": "verify",
                }
            ]

        return data

    @property
    def source(self) -> KnowledgeSource:
        return (
            KnowledgeSource.USER_CONFIRMED
            if self.generated.by == GeneratedBy.HUMAN
            else KnowledgeSource.AUTO_INFERRED
        )

    @property
    def is_human_protected(self) -> bool:
        """Определяет, защищена ли карточка от деструктивных авто-мутаций (KS13 Вариант Б).

        Защищёнными являются:
        1. Карточки человека: generated.by == GeneratedBy.HUMAN (source == USER_CONFIRMED).
        2. Карточки после одобренного промоушена: наличие в verified записи с action='promote'.
        """
        if self.source == KnowledgeSource.USER_CONFIRMED:
            return True
        return any(v.action == "promote" for v in self.verified)

    @property
    def created_at(self) -> str:
        return self.generated.created_at


class CardRecordRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(..., min_length=3, description="Понятный заголовок карточки")
    category: str = Field(
        ...,
        description="Категория знаний (например, audio, ml, backend, frontend, database, platform)",
    )

    type: KnowledgeType
    source: KnowledgeSource = KnowledgeSource.AUTO_INFERRED
    status: KnowledgeStatus | None = Field(
        default=None,
        description="Статус карточки: 'draft' для карантина/черновика или 'active' для немедленной публикации",
    )
    agent: str | None = Field(default=None, description="Имя агента-автора (OKF v0.2)")
    context: str | None = Field(
        default=None,
        description="Контекст проблемы, архитектурный выбор или описание ситуации (альтернатива symptom/root_cause)",
    )
    symptom: str = Field(
        default="",
        validate_default=True,
        description="Краткое описание симптома или ошибки (Universal Anti-Spam Gate)",
    )
    root_cause: str = Field(
        default="",
        validate_default=True,
        description="Глубокий технический анализ первопричины сбоя (Universal Anti-Spam Gate)",
    )
    solution: str = Field(
        ...,
        min_length=50,
        description="Законченное техническое решение (Universal Anti-Spam Gate)",
    )
    rationale: str = Field(
        ...,
        min_length=30,
        description="Обоснование правильности решения / True Correctness Rule (Anti-Spam Gate)",
    )
    slug: str | None = Field(
        default=None,
        description="Семантический slug на английском (например, ffmpeg-sigttin)",
    )
    conditions: str | None = Field(
        default=None,
        description="Условия воспроизведения (платформа, флаги, версии)",
    )
    nuances: str | None = Field(
        default=None,
        description="Известные ограничения и подводные камни",
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="5–7 поисковых синонимов и симптомов",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Теги для классификации",
    )
    supersedes: str | None = Field(
        default=None,
        description="ID замещаемой карточки",
    )
    related_to: list[str] = Field(
        default_factory=list,
        description="Связанные карточки",
    )
    task_id: str | None = Field(
        default=None,
        description="Идентификатор задачи/сессии (для группировки и саморевью через review_knowledge)",
    )
    stale_after: str | None = Field(
        default=None,
        description="Дедлайн временного решения (OKF v0.2, YYYY-MM-DD)",
    )
    force: bool = Field(
        default=False,
        description="Принудительное создание в обход детектора дубликатов",
    )
    confirmed_force: bool = Field(
        default=False,
        description="Явное подтверждение создания дубликата при force=True (фаза 2)",
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_context_and_symptoms(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        ctx = data.get("context")
        sym = data.get("symptom")
        rc = data.get("root_cause")

        # При наличии context нормализуем symptom и root_cause для хранилища и FTS5
        if ctx is not None and isinstance(ctx, str):
            if not ctx.strip():
                data["context"] = None
            else:
                ctx_clean = ctx.strip()
                data["context"] = ctx_clean
                if not sym or not str(sym).strip():
                    if len(ctx_clean) <= 200:
                        data["symptom"] = ctx_clean
                    else:
                        truncated = ctx_clean[:200]
                        last_space = truncated.rfind(" ")
                        data["symptom"] = truncated[:last_space] if last_space > 40 else truncated
                if not rc or not str(rc).strip():
                    data["root_cause"] = ctx_clean
        return data

    @field_validator("context", mode="after")
    @classmethod
    def validate_context(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v_clean = _validate_non_empty_stripped(v)
        if len(v_clean) < 40:
            raise ValueError(
                "Поле 'context' должно содержать не менее 40 символов (Universal Anti-Spam Gate)"
            )
        return v_clean

    @field_validator("symptom", mode="after")
    @classmethod
    def validate_symptom(cls, v: str) -> str:
        v_clean = _validate_non_empty_stripped(v)
        if len(v_clean) < 10:
            raise ValueError(
                "Поле 'symptom' должно содержать не менее 10 символов (или укажите 'context' не менее 40 символов)"
            )
        return v_clean

    @field_validator("root_cause", mode="after")
    @classmethod
    def validate_root_cause(cls, v: str) -> str:
        v_clean = _validate_non_empty_stripped(v)
        if len(v_clean) < 40:
            raise ValueError(
                "Поле 'root_cause' должно содержать не менее 40 символов (или укажите 'context' не менее 40 символов)"
            )
        return v_clean

    @field_validator("title", "solution", "rationale", mode="before")
    @classmethod
    def validate_non_empty_stripped(cls, v: Any) -> Any:
        return _validate_non_empty_stripped(v)

    @field_validator("category", mode="before")
    @classmethod
    def validate_category(cls, v: Any) -> str:
        return _validate_category_slug(v)

    @field_validator("type", "source", mode="before")
    @classmethod
    def coerce_enum_strings(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("status", mode="before")
    @classmethod
    def validate_status_enum_only(cls, v: Any) -> Any:
        """K1: статус обязан быть валидным значением KnowledgeStatus.

        Smart-union `KnowledgeStatus | str | None` пропускал произвольные
        строки (например, 'вэри-бэд-статус'): карточка писалась на диск,
        но была нечитаема (get_knowledge → not_found, sync пропускал).
        Unknown-значения — reject, а не подмена default.
        """
        if v is None:
            return v
        if isinstance(v, KnowledgeStatus):
            return v
        if isinstance(v, str):
            normalized = v.strip().lower()
            if normalized in {s.value for s in KnowledgeStatus}:
                return normalized
        # Неизвестное значение: отдаём как есть — pydantic-union отвергнет
        # str-ветку и сгенерирует ValidationError с перечислением допустимых.
        return v

    @field_validator("stale_after", mode="before")
    @classmethod
    def coerce_request_dates(cls, v: Any) -> Any:
        if isinstance(v, (date, datetime)):
            return v.isoformat()
        return v


class CardStatusUpdateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    id: str
    action: Literal["suspect", "verify", "dispute", "append_note", "promote"] = Field(
        ..., description="'suspect' | 'verify' | 'dispute' | 'append_note' | 'promote'"
    )
    note: str | None = Field(
        default=None,
        min_length=1,
        description="Текст ошибки, комментарий или дополнение (для promote опционально)",
    )
    by: str | None = Field(
        default=None,
        description="Имя или тип автора верификации/обновления (по умолчанию 'human')",
    )
    source: KnowledgeSource = Field(
        default=KnowledgeSource.AUTO_INFERRED,
        description="Автор действия (для защиты user_confirmed карточек)",
    )

    @field_validator("action", mode="before")
    @classmethod
    def normalize_action(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("source", mode="before")
    @classmethod
    def coerce_source_string(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @model_validator(mode="after")
    def validate_note_required_for_non_promote(self) -> CardStatusUpdateRequest:
        # 'promote' may omit note: the use case records a default "Promoted from
        # draft to active" entry. Every other action still requires a meaningful note.
        if self.action != "promote" and not (self.note and self.note.strip()):
            raise ValueError(
                f"Поле 'note' обязательно для действия '{self.action}' "
                "(опционально только для 'promote')"
            )
        return self

    @field_validator("note", mode="before")
    @classmethod
    def validate_non_empty_note(cls, v: Any) -> Any:
        if isinstance(v, str):
            v_stripped = v.strip()
            if not v_stripped:
                raise ValueError("Заметка не может быть пустой")
            return v_stripped
        return v


class HypothesisRecordRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(..., min_length=3, description="Понятный заголовок гипотезы")
    category: str = Field(
        ...,
        description="Категория знаний (например, audio, ml, backend, frontend, database, platform)",
    )

    symptom: str = Field(
        ...,
        min_length=10,
        description="Контекст или наблюдаемый симптом",
    )
    hypothesis: str = Field(
        ...,
        min_length=15,
        description="Суть рабочей гипотезы (что предполагается причиной или решением)",
    )
    test_plan: str | None = Field(
        default=None,
        description="План проверки гипотезы",
    )
    task_id: str | None = Field(
        default=None,
        description="Идентификатор задачи или исследовательской сессии",
    )
    agent: str | None = Field(
        default=None,
        description="Имя агента-автора",
    )
    confidence: float = Field(
        default=0.20,
        ge=0.0,
        le=1.0,
        description="Начальный вес уверенности (по умолчанию 0.20)",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Теги для классификации",
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="Поисковые синонимы",
    )
    slug: str | None = Field(
        default=None,
        description="Семантический slug на английском (например, venv-uv-symlink)",
    )
    force: bool = Field(
        default=False,
        description="Принудительное создание в обход детектора дубликатов",
    )
    confirmed_force: bool = Field(
        default=False,
        description="Явное подтверждение обхода content gate при force=True (K7)",
    )

    @field_validator("title", "symptom", "hypothesis", mode="before")
    @classmethod
    def validate_non_empty_stripped(cls, v: Any) -> Any:
        return _validate_non_empty_stripped(v)

    @field_validator("category", mode="before")
    @classmethod
    def validate_category(cls, v: Any) -> str:
        return _validate_category_slug(v)

    @field_validator("confidence", mode="before")
    @classmethod
    def coerce_confidence(cls, v: Any) -> float:
        return _coerce_confidence(v, default=0.20)


class HypothesisResolution(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    id: str = Field(..., min_length=1, description="ID карточки-гипотезы")
    action: Literal["confirm", "refute", "drop", "duplicate"] = Field(
        ..., description="'confirm' | 'refute' | 'drop' | 'duplicate'"
    )
    solution: str | None = Field(
        default=None,
        description="Законченное техническое решение (обязательно при confirm, min 10 символов)",
    )
    rationale: str | None = Field(
        default=None,
        description="Обоснование правильности (опционально при confirm)",
    )
    refutation_reason: str | None = Field(
        default=None,
        description="Причина опровержения/тупика (обязательно при refute, min 15 символов)",
    )
    merge_into: str | None = Field(
        default=None,
        description="ID карточки для слияния аргумента опровержения в секцию нюансов",
    )
    duplicate_of: str | None = Field(
        default=None,
        description="ID канонической карточки-дубля (обязательно при action='duplicate')",
    )

    @field_validator("action", mode="before")
    @classmethod
    def normalize_action(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @model_validator(mode="after")
    def validate_action_fields(self) -> HypothesisResolution:
        if self.action == "refute" and (
            not self.refutation_reason or len(self.refutation_reason.strip()) < 15
        ):
            raise ValueError(
                "Для действия 'refute' обязательно поле 'refutation_reason' длиной не менее 15 символов"
            )
        if self.action == "confirm" and (not self.solution or len(self.solution.strip()) < 10):
            raise ValueError(
                "Для действия 'confirm' обязательно поле 'solution' длиной не менее 10 символов"
            )
        if self.action == "duplicate" and (not self.duplicate_of or not self.duplicate_of.strip()):
            raise ValueError("Для действия 'duplicate' обязательно поле 'duplicate_of'")
        return self


class HypothesesResolveRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    task_id: str | None = Field(
        default=None,
        description="Идентификатор задачи или исследовательской сессии",
    )
    resolutions: list[HypothesisResolution] = Field(
        ...,
        min_length=1,
        description="Список решений по гипотезам",
    )


def compute_card_badge(
    status: KnowledgeStatus | str,
    card_type: KnowledgeType | str | None = None,
) -> str:
    """Определяет понятный агенту бейдж карточки вместо сырого float confidence."""
    st_val = status.value if isinstance(status, KnowledgeStatus) else str(status).strip().lower()
    ty_val = (
        card_type.value
        if isinstance(card_type, KnowledgeType)
        else (str(card_type).strip().lower() if card_type is not None else "")
    )
    if st_val == KnowledgeStatus.VERIFIED.value:
        return "verified"
    if st_val == KnowledgeStatus.SUSPECT.value:
        return "suspect"
    if st_val == KnowledgeStatus.REFUTED.value or ty_val == KnowledgeType.ANTIPATTERN.value:
        return "refuted"
    if st_val == KnowledgeStatus.SUPERSEDED.value:
        return "superseded"
    if st_val == KnowledgeStatus.DISPUTED.value:
        return "disputed"
    if st_val == KnowledgeStatus.HYPOTHESIS.value:
        return "hypothesis"
    if st_val == KnowledgeStatus.DRAFT.value:
        return "draft"
    return "ok"


class KnowledgeSnippet(BaseModel):
    id: str
    title: str
    category: str = ""
    type: KnowledgeType
    status: KnowledgeStatus
    source: KnowledgeSource
    badge: str = "ok"
    confidence: float = Field(default=0.75, exclude=True)
    task_id: str | None = None
    snippet: str
    warning: str | None = None
    score: float | None = Field(
        default=None, description="Ранговый балл релевантности BM25 (ранг, не вероятность)"
    )


class CardDetail(BaseModel):
    id: str
    title: str
    category: str = ""
    type: KnowledgeType
    status: KnowledgeStatus
    source: KnowledgeSource
    badge: str = "ok"
    frontmatter: CardFrontmatter
    content: str
    supersedes: str | None = None
    superseded_by: str | None = None
    superseded_info: str | None = None
    see_also: list[str] = Field(default_factory=list)


class CardGetResponse(BaseModel):
    cards: list[CardDetail] = Field(default_factory=list)
    not_found: list[str] = Field(default_factory=list)
    warning: str | None = None


class CategoryOverviewItem(BaseModel):
    category: str = ""
    count: int
    sample_titles: list[str] = Field(default_factory=list)


class ProjectOverview(BaseModel):
    total_cards: int = 0
    categories: list[CategoryOverviewItem] = Field(default_factory=list)


class FacetedSearchResponse(BaseModel):
    query_results: list[KnowledgeSnippet] = Field(default_factory=list)
    results: list[KnowledgeSnippet] = Field(
        default_factory=list,
        description="DEPRECATED: переходный алиас query_results (удаление в v0.3). Используйте query_results или category_results.",
    )
    category_results: dict[str, list[KnowledgeSnippet]] = Field(default_factory=dict)
    antipatterns: list[KnowledgeSnippet] = Field(default_factory=list)
    hypotheses: list[KnowledgeSnippet] = Field(default_factory=list)
    overview: ProjectOverview | None = None
    hint: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _sync_faceted_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "query_results" in data and "results" not in data:
                data["results"] = data["query_results"]
            elif "results" in data and "query_results" not in data:
                data["query_results"] = data["results"]
        return data
