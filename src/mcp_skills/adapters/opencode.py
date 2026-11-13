"""Адаптер для OpenCode (.opencode/agents/)."""

from __future__ import annotations

import logging
from typing import Any

import yaml

from mcp_skills.adapters.base import BaseAdapter
from mcp_skills.errors import UnsupportedReadonlyProfileError
from mcp_skills.models import AgentProfile

__all__ = ["OpenCodeAdapter", "UnsupportedReadonlyProfileError"]

logger = logging.getLogger("mcp_skills.adapters.opencode")

# Провёренные read-only инструменты: начальный консервативный список. Расширение
# допускается только после проверки в целевой версии OpenCode (живой smoke, см.
# план задачи R2) — по поведению в клиенте, а не по «безопасно звучащему» имени.
# Префиксы (get_/check_/verify_/…) сами по себе НЕ считаются безопасными:
# имя проверяется целиком. Инвариант: пересечение с write-классами пусто
# (test_opencode_proven_safe_tools_invariant).
_PROVEN_SAFE_TOOLS: frozenset[str] = frozenset(
    {
        "read",
        "glob",
        "grep",
        "list",
        "search_knowledge",
        "search_skills",
        "search_debt",
        "search_artifacts",
        "get_knowledge",
        "get_help",
    }
)


def _reject_strict_readonly(profile: AgentProfile) -> None:
    """R2 (ревизия 4): строго read-only профиль отклоняется при экспорте.

    Wildcard-запрет ``"*": false`` и точечные запреты profile.tools НЕ
    гарантируют запрет неизвестных инструментов: семантика приоритета
    правил в конкретной версии OpenCode не подтверждена живой проверкой.
    Экспортировать «вероятно read-only» артефакт с предупреждением
    недостаточно — неизвестные инструменты остались бы доступными.
    Поэтому write=False без явного подтверждения владельца
    (readonly_acknowledged=True) — неподдерживаемая конфигурация.
    """
    if not profile.permissions.write and not profile.readonly_acknowledged:
        msg = (
            f"Строгий read-only профиль '{profile.name}' не поддерживается для "
            "экспорта в OpenCode: запрет неизвестных инструментов через "
            "tools: {'*': false} не гарантирован семантикой клиента "
            "(приоритет wildcard-правил не подтверждён живой проверкой). "
            "Включите permissions.write=True либо подтвердите фактическую "
            "проверку read-only прав в целевой версии OpenCode "
            "(readonly_acknowledged: true в профиле)."
        )
        raise UnsupportedReadonlyProfileError(msg)


def _build_tools_dict(profile: AgentProfile) -> dict[str, bool]:
    """Собирает словарь tools для frontmatter OpenCode (deny-by-default).

    Контракт (R2 отчёта приёмки 2026-10-18, ревизия 4; семантика OpenCode:
    legacy tools deprecated, true/false ≡ {"*": "allow"/"deny"}, wildcards
    поддерживаются, последнее совпадающее правило выигрывает):

    - write=False без readonly_acknowledged → экспорт отклонён ДО сборки
      словаря (см. _reject_strict_readonly): предупреждения недостаточно.
    - При write=True инструменты profile.tools экспортируются как True; при
      subagents=False имя ``task`` дополнительно получает явный False — запрет
      делегирования не зависит от приоритета полей в версии клиента.
    - При write=False (подтверждённом) первым идёт ``"*": False`` — общий
      запрет ВСЕХ инструментов, включая неизвестные. Отсутствие ключа не
      используется как запрет: неизвестное покрывается wildcard-записью.
    - После wildcard — точечные ``True`` только для имён из
      ``_PROVEN_SAFE_TOOLS`` (проверенные read-only; порядок важен: по
      документации OpenCode последнее совпадающее правило приоритетнее).
    - Всё из profile.tools вне allowlist получает явное ``False`` (в т.ч.
      namespaced mcp_*_*): без blanket-предположений о префиксах.
    """
    tools_dict: dict[str, bool] = {}
    if not profile.permissions.write:
        # deny-by-default: "*" первым; согласно документации OpenCode
        # "the last matching rule takes precedence", поэтому точечные
        # разрешения добавляются ПОСЛЕ и переопределяют wildcard.
        tools_dict["*"] = False
        for t in profile.tools:
            if t in _PROVEN_SAFE_TOOLS:
                # Проверенный read-only инструмент: явное разрешение,
                # переопределяющее wildcard (идёт после "*": False).
                tools_dict[t] = True
            else:
                # Всё остальное (включая namespaced mcp_*_* и неизвестные
                # имена) — явный запрет, не «отсутствие ключа».
                tools_dict[t] = False
    elif profile.tools:
        # write=True: явных запретов нет — экспортируем как есть
        # (permission уже задаёт полные права).
        for t in profile.tools:
            tools_dict[t] = True
        if "task" in tools_dict and not profile.permissions.subagents:
            # Дублируем запрет делегирования в tools: не полагаемся на
            # приоритет permission["task"] над tools["task"] в конкретной
            # версии клиента (R2: оба поля согласованы).
            tools_dict["task"] = False

    return tools_dict


class OpenCodeAdapter(BaseAdapter):
    """Адаптер для экспорта профиля агента в Markdown с YAML frontmatter под OpenCode."""

    @property
    def target_name(self) -> str:
        return "opencode"

    def export_agent(self, profile: AgentProfile, compiled_skills_content: str) -> str:
        # R2 (ревизия 4, MINOR-3): строго read-only профиль отклоняется ДО
        # сборки frontmatter и каких-либо warning-логов — небезопасный
        # артефакт не формируется вовсе, диагностика остаётся единственной.
        _reject_strict_readonly(profile)

        frontmatter: dict[str, Any] = {
            "description": profile.description or f"{profile.role} agent for {profile.name}",
            "mode": "subagent",
        }

        # Обработка модели: model_name явный ID, inherit -> отсутствие model;
        # для pro/flash без явного маппинга наследуем модель и логируем warning
        if profile.model_name:
            frontmatter["model"] = profile.model_name
        elif profile.model in ("pro", "flash"):
            logger.warning(
                "Для профиля '%s' запрошена модель '%s', но сопоставление ID модели для OpenCode не настроено. "
                "Модель будет унаследована от хоста (поле 'model' опущено).",
                profile.name,
                profile.model,
            )

        # Конфигурация permission (в единственном числе согласно документации OpenCode)
        permission: dict[str, Any] = {}
        if not profile.permissions.write:
            permission["edit"] = "deny"
            permission["bash"] = "deny"
        else:
            permission["edit"] = "allow"
            permission["bash"] = "allow"

        # Право на делегирование (task): read-only агент субагентов
        # не запускает вовсе — субагент не наследует ограничения tools
        # экспортирующего агента и мог бы выполнять запись от его имени.
        # Контракт R2 п.5: write=False && subagents=True → явный deny + warning.
        if not profile.permissions.subagents:
            permission["task"] = "deny"
        elif not profile.permissions.write:
            permission["task"] = "deny"
            logger.warning(
                "Профиль '%s': subagents=True при write=False. Делегирование "
                "запрещено: субагент не наследует read-only ограничения tools "
                "родителя и мог бы выполнять запись от его имени. Если "
                "делегирование нужно — включите write=True или снимите "
                "ограничение subagents.",
                profile.name,
            )
        else:
            permission["task"] = "allow"

        frontmatter["permission"] = permission

        # Ограничения по инструментам: запрет инструментов записи
        tools_dict = _build_tools_dict(profile)
        if tools_dict:
            frontmatter["tools"] = tools_dict

        yaml_header = yaml.dump(frontmatter, allow_unicode=True, sort_keys=False).strip()

        system_prompt_parts: list[str] = []
        if profile.instructions:
            system_prompt_parts.append(profile.instructions.strip())
        if compiled_skills_content.strip():
            system_prompt_parts.append(compiled_skills_content.strip())

        body = "\n\n---\n\n".join(system_prompt_parts).strip()
        return f"---\n{yaml_header}\n---\n\n# System Prompt\n\n{body}\n"
