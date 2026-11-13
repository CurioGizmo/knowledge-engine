"""Базовый абстрактный класс для мульти-рантайм адаптеров субагентов."""

from __future__ import annotations

from abc import ABC, abstractmethod

from mcp_skills.models import AgentProfile

__all__ = ["BaseAdapter"]


class BaseAdapter(ABC):
    """Базовый интерфейс адаптера целевого рантайма агента."""

    @property
    @abstractmethod
    def target_name(self) -> str:
        """Уникальный идентификатор целевого рантайма (например, 'antigravity', 'cursor')."""
        ...

    @abstractmethod
    def export_agent(self, profile: AgentProfile, compiled_skills_content: str) -> str:
        """Экспортирует профиль агента и скомпилированные скиллы в формат целевого рантайма."""
        ...
