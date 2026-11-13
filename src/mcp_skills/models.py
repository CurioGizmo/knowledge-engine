"""Pydantic DTO модели для пакета mcp_skills."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from mcp_skills.spam_gate import is_kebab_name, is_safe_name


def _coerce_alias_list(v: object) -> list[str]:
    """Приводит строковые скаляры, списки и кортежи к нормализованному list[str]."""
    if v is None:
        return []
    if isinstance(v, str):
        return (
            [x.strip() for x in v.split(",") if x.strip()]
            if "," in v
            else [v.strip()]
            if v.strip()
            else []
        )
    if isinstance(v, (list, tuple, set)):
        res: list[str] = []
        for item in v:
            if isinstance(item, str):
                s = item.strip()
                if s and s not in res:
                    res.append(s)
        return res
    return []


class SkillCard(BaseModel):
    name: str
    title: str
    description: str = ""
    categories: list[str] = Field(default_factory=list)
    category: str = "general"
    stack: list[str] = Field(default_factory=list)
    kind: Literal["skill", "extension"] = "skill"
    extends: str | None = None
    merge_strategy: Literal["append", "prepend", "override"] = "append"
    available_slots: list[str] = Field(default_factory=list)
    optional_slots: list[str] = Field(default_factory=list)
    subagents: list[str] = Field(default_factory=list)
    scope: Literal["global", "project"] = "global"
    activation: str | None = None
    # Канонические lowercase-теги детектора стека (см. stack_detector.py)
    requires_stack: list[str] | dict[str, list[str]] = Field(default_factory=list)
    # Зависимость интеграционных блоков от MCP-серверов (R7, ревизия 4):
    # ЕДИНСТВЕННАЯ форма — список имён серверов. Список означает «нужны
    # ВСЕ перечисленные». Применяется к mcp-кандидатам любого слота при
    # server_availability: present/absent/unknown. Серверные зависимости
    # хранятся отдельно от технологических осей stack; ось «integration
    # server» не вводится. Блоки offline/default без поля совместимы.
    requires_servers: list[str] = Field(default_factory=list)
    base_dir: Path | None = None
    score: float | None = Field(
        default=None, description="Ранговый балл релевантности BM25 (ранг, не вероятность)"
    )
    aliases: list[str] = Field(default_factory=list)
    applicability: Literal["applicable", "unknown", "incompatible"] | None = None
    applicability_reason: str | None = None
    matched_resources: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("optional_slots")
    @classmethod
    def _validate_optional_slots(cls, value: list[str]) -> list[str]:
        if any(not is_kebab_name(slot) for slot in value):
            raise ValueError("optional_slots must contain safe lowercase slot names")
        return list(dict.fromkeys(value))

    @field_validator("activation", mode="before")
    @classmethod
    def _coerce_activation(cls, v: object) -> str | None:
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip().lower()
            return s if s else None
        return None

    @field_validator("requires_stack", mode="before")
    @classmethod
    def _coerce_requires_stack(cls, v: object) -> list[str] | dict[str, list[str]]:
        if isinstance(v, dict):
            res_dict: dict[str, list[str]] = {}
            for k, val in v.items():
                items = _coerce_alias_list(val)
                res_dict[str(k).lower()] = [item.strip().lower() for item in items if item.strip()]
            return res_dict
        raw_list = _coerce_alias_list(v)
        res: list[str] = []
        for item in raw_list:
            lowered = item.strip().lower()
            if lowered and lowered not in res:
                res.append(lowered)
        return res

    @field_validator("aliases", mode="before")
    @classmethod
    def _coerce_aliases(cls, v: object) -> list[str]:
        return _coerce_alias_list(v)

    @model_validator(mode="after")
    def _sync_categories(self) -> SkillCard:
        if not self.category:
            self.category = "general"
        if not self.categories:
            self.categories = [self.category]
        return self


class AgentPermissions(BaseModel):
    write: bool = False
    mcp: bool = True
    subagents: bool = False


class SkillOptionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    touches: list[str] | None = None
    resources: list[str] | None = None

    @field_validator("touches", "resources", mode="before")
    @classmethod
    def _coerce_string_lists(cls, v: object) -> list[str] | None:
        if v is None:
            return None
        return _coerce_alias_list(v)


class AgentProfile(BaseModel):
    name: str
    role: str = ""
    description: str = ""
    model: Literal["pro", "flash", "inherit"] = "pro"
    skills: list[str] = Field(default_factory=list)
    permissions: AgentPermissions = Field(default_factory=AgentPermissions)
    mode: Literal["sequential", "autonomous"] = "sequential"
    scope: Literal["global", "project"] = "global"
    instructions: str = ""
    tools: list[str] = Field(default_factory=list)
    model_name: str | None = None
    skill_options: dict[str, SkillOptionItem] = Field(default_factory=dict)
    # R2 (ревизия 4): строго read-only профиль (write=False) отклоняется
    # при экспорте в OpenCode — wildcard-запрет не гарантирует запрет
    # неизвестных инструментов в неподтверждённой версии клиента. Флаг
    # снимает отклонение: владелец профиля подтвердил фактическую проверку
    # прав read-only в целевой версии OpenCode (живой smoke). Не используется
    # для включения записи: write контролируется только permissions.
    readonly_acknowledged: bool = False

    @model_validator(mode="after")
    def _ensure_role_and_desc(self) -> AgentProfile:
        if not self.role:
            self.role = f"{self.name.replace('-', ' ').title()} Specialist"
        if not self.description:
            self.description = f"{self.role} agent for {self.name}"
        return self


class AgentOverride(BaseModel):
    base_agent: str = Field(default="", validation_alias=AliasChoices("base_agent", "name"))
    project: str | None = None
    target: str | None = None
    model: Literal["pro", "flash", "inherit"] | None = None
    model_name: str | None = None
    tools: list[str] | None = None
    permissions: AgentPermissions | None = None
    extra_skills: list[str] = Field(default_factory=list)
    skills: list[str] | None = None
    instructions_suffix: str = ""
    mode: Literal["sequential", "autonomous"] | None = None
    skill_options: dict[str, SkillOptionItem] | None = None


class FlowStatus(StrEnum):
    ACTIVE = "active"
    BLOCKED_ESCALATE = "blocked_escalate"
    COMPLETED = "completed"
    ABORTED = "aborted"


class FlowVerdict(StrEnum):
    APPROVED = "approved"
    NEEDS_WORK = "needs_work"


class FlowVisibility(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"


def _is_json_scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _same_scalar(left: object, right: object) -> bool:
    """Compare option scalars without conflating bool with int."""
    return type(left) is type(right) and left == right


def _validate_option_mapping(value: dict[str, Any], field_name: str) -> dict[str, Any]:
    for key, item in value.items():
        if not is_safe_name(key):
            raise ValueError(f"Недопустимое имя option '{key}' в {field_name}")
        if not _is_json_scalar(item):
            raise ValueError(f"Значение option '{key}' в {field_name} должно быть JSON scalar")
    return value


class FlowOptionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: list[Any] = Field(min_length=1)
    default: Any

    @field_validator("allowed")
    @classmethod
    def _validate_allowed(cls, values: list[Any]) -> list[Any]:
        if any(not _is_json_scalar(item) for item in values):
            raise ValueError("allowed должен содержать только JSON scalar значения")
        deduplicated: list[Any] = []
        for item in values:
            if not any(_same_scalar(item, present) for present in deduplicated):
                deduplicated.append(item)
        return deduplicated

    @field_validator("default")
    @classmethod
    def _validate_default_scalar(cls, value: Any) -> Any:
        if not _is_json_scalar(value):
            raise ValueError("default должен быть JSON scalar значением")
        return value

    @model_validator(mode="after")
    def _default_must_be_allowed(self) -> FlowOptionSpec:
        if not any(_same_scalar(self.default, item) for item in self.allowed):
            raise ValueError("default должен входить в allowed")
        return self


class FlowOptionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    when: dict[str, Any] = Field(default_factory=dict)
    defaults: dict[str, Any] = Field(default_factory=dict)

    @field_validator("when", "defaults")
    @classmethod
    def _validate_mappings(cls, value: dict[str, Any], info: Any) -> dict[str, Any]:
        return _validate_option_mapping(value, info.field_name)

    @model_validator(mode="after")
    def _rule_must_have_condition_and_effect(self) -> FlowOptionRule:
        if not self.when:
            raise ValueError("option rule должен содержать непустой when")
        if not self.defaults:
            raise ValueError("option rule должен содержать непустой defaults")
        return self


class FlowDispatchPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_agents: int = Field(default=1, ge=1, le=4)
    max_agents: int = Field(default=1, ge=1, le=4)
    parallel: Literal["never", "when_safe"] = "when_safe"
    selection: Literal["risk_and_touched_domains", "dag_and_write_sets"]

    @model_validator(mode="after")
    def _validate_range(self) -> FlowDispatchPolicy:
        if self.min_agents > self.max_agents:
            raise ValueError("min_agents не может превышать max_agents")
        return self


class FlowTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str
    max_uses: int = Field(default=100, ge=1)
    when_options: dict[str, Any] = Field(default_factory=dict)
    allow_needs_work: bool = False

    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        if not is_safe_name(value):
            raise ValueError(f"Недопустимый target перехода '{value}'")
        return value

    @field_validator("when_options")
    @classmethod
    def _validate_when_options(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_option_mapping(value, "when_options")


class FlowStageMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str | None = None
    gate: str | None = None
    target_file: str | None = None
    loop_max: int = 3
    dispatch_agents: bool = False
    require_verdict: bool = False
    require_human_confirm: bool = False
    deliverable_must_exist: bool = False
    # F1: имена скиллов/тулов (НЕ контент) — компиляция в инструкцию этапа
    # выполняется движком через SlotCompiler (flow_engine._compile_stage_skills).
    skills: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    skill_options: dict[str, SkillOptionItem] = Field(default_factory=dict)
    transitions: dict[str, FlowTransition] = Field(default_factory=dict)
    transition_required: bool = False
    dispatch_policy: FlowDispatchPolicy | None = None

    @field_validator("skills", "required_tools", mode="before")
    @classmethod
    def _coerce_stage_name_lists(cls, v: object) -> list[str]:
        """Строка/список → list[str] (как aliases), без дедупликации потерь.

        Имена с разделителями путей (path traversal) отвергаются на этапе
        парсинга флоу (flow_parser._parse_stage_section), не в модели —
        здесь коэрцинг к списку для YAML-скаляров.
        """
        return _coerce_alias_list(v)

    @field_validator("skills", "required_tools")
    @classmethod
    def _validate_stage_name_lists(cls, v: list[str]) -> list[str]:
        for name in v:
            if not is_safe_name(name):
                msg = f"Недопустимое имя '{name}' в списке skills/required_tools"
                raise ValueError(msg)
        return v

    @field_validator("loop_max")
    @classmethod
    def validate_loop_max(cls, v: int) -> int:
        if v <= 0:
            msg = "Параметр loop_max должен быть строго больше 0"
            raise ValueError(msg)
        return v

    @field_validator("transitions")
    @classmethod
    def _validate_transition_labels(
        cls, value: dict[str, FlowTransition]
    ) -> dict[str, FlowTransition]:
        for label in value:
            if not is_safe_name(label):
                raise ValueError(f"Недопустимое имя перехода '{label}'")
        return value

    @model_validator(mode="after")
    def _required_transition_needs_choices(self) -> FlowStageMeta:
        if self.transition_required and not self.transitions:
            raise ValueError("transition_required требует непустой transitions")
        return self


class FlowStage(BaseModel):
    source_file: Path | None = Field(default=None, exclude=True)
    id: str
    title: str
    instruction: str
    meta: FlowStageMeta = Field(default_factory=FlowStageMeta)
    order: int = 0


class FlowDefinition(BaseModel):
    name: str
    title: str = ""
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    intent_group: str | None = None
    when_to_use: list[str] = Field(default_factory=list)
    stages: dict[str, FlowStage] = Field(default_factory=dict)
    stage_order: list[str] = Field(default_factory=list)
    # F1: мостики «куда идти дальше» — включаются в completed-ответ step_flow
    next_flows: list[str] = Field(default_factory=list)
    exclude_aliases: list[str] = Field(default_factory=list)
    visibility: FlowVisibility = FlowVisibility.PUBLIC
    entrypoint: bool = True
    deprecated: bool = False
    superseded_by: str | None = None
    option_schema: dict[str, FlowOptionSpec] = Field(default_factory=dict)
    defaults: dict[str, Any] = Field(default_factory=dict)
    option_rules: list[FlowOptionRule] = Field(default_factory=list)
    dispatch_policy: FlowDispatchPolicy | None = None

    @field_validator("aliases", mode="before")
    @classmethod
    def _coerce_aliases(cls, v: object) -> list[str]:
        return _coerce_alias_list(v)

    @field_validator("when_to_use", mode="before")
    @classmethod
    def _coerce_when_to_use(cls, v: object) -> list[str]:
        return _coerce_alias_list(v)

    @field_validator("next_flows", mode="before")
    @classmethod
    def _coerce_next_flows(cls, v: object) -> list[str]:
        return _coerce_alias_list(v)

    @field_validator("exclude_aliases", mode="before")
    @classmethod
    def _coerce_exclude_aliases(cls, v: object) -> list[str]:
        return _coerce_alias_list(v)

    @field_validator("option_schema")
    @classmethod
    def _validate_option_schema_names(
        cls, value: dict[str, FlowOptionSpec]
    ) -> dict[str, FlowOptionSpec]:
        for key in value:
            if not is_safe_name(key):
                raise ValueError(f"Недопустимое имя option '{key}' в option_schema")
        return value

    @field_validator("defaults")
    @classmethod
    def _validate_defaults(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_option_mapping(value, "defaults")

    @model_validator(mode="after")
    def _validate_flow_contract(self, info: ValidationInfo) -> FlowDefinition:
        if not is_safe_name(self.name):
            raise ValueError(f"Недопустимое имя flow '{self.name}'")
        if self.visibility is FlowVisibility.INTERNAL and self.entrypoint:
            raise ValueError("internal flow не может быть entrypoint")
        if self.superseded_by is not None:
            if not is_safe_name(self.superseded_by):
                raise ValueError(f"Недопустимое имя superseded_by '{self.superseded_by}'")
            if self.superseded_by == self.name:
                raise ValueError("flow не может supersede сам себя")
        if self.option_schema:
            known = set(self.option_schema)
            unknown_defaults = set(self.defaults) - known
            if unknown_defaults:
                raise ValueError(
                    f"defaults содержит неизвестные options: {sorted(unknown_defaults)}"
                )
            for rule in self.option_rules:
                unknown = (set(rule.when) | set(rule.defaults)) - known
                if unknown:
                    raise ValueError(f"option rule содержит неизвестные options: {sorted(unknown)}")
                for key, item in {**rule.when, **rule.defaults}.items():
                    spec = self.option_schema[key]
                    if not any(_same_scalar(item, allowed) for allowed in spec.allowed):
                        raise ValueError(
                            f"option rule задаёт недопустимое значение {item!r} для '{key}'"
                        )
            for key, item in self.defaults.items():
                spec = self.option_schema[key]
                if not any(_same_scalar(item, allowed) for allowed in spec.allowed):
                    raise ValueError(f"defaults задаёт недопустимое значение {item!r} для '{key}'")
        if not (info.context or {}).get("partial_flow", False):
            self._validate_transition_options()
        return self

    def _validate_transition_options(self) -> None:
        for stage in self.stages.values():
            for label, transition in stage.meta.transitions.items():
                for key, item in transition.when_options.items():
                    spec = self.option_schema.get(key)
                    if spec is None:
                        raise ValueError(
                            f"transition '{stage.id}:{label}' содержит неизвестный option '{key}'"
                        )
                    if not any(_same_scalar(item, allowed) for allowed in spec.allowed):
                        raise ValueError(
                            f"transition '{stage.id}:{label}' задаёт недопустимое "
                            f"значение {item!r} для '{key}'"
                        )


class FlowSession(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    task_id: str | None = None
    project_root: str
    git_branch: str | None = None
    flow_name: str
    current_stage_id: str
    iteration_count: int = 1
    status: FlowStatus = FlowStatus.ACTIVE
    version: int = 1
    context_data: dict[str, Any] = Field(default_factory=dict)
    created_at: float
    updated_at: float
