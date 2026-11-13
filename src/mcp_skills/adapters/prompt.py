"""Адаптер для выгрузки универсального системного промпта (Persona Mask)."""

from __future__ import annotations

from mcp_skills.adapters.base import BaseAdapter
from mcp_skills.models import AgentProfile

__all__ = ["PromptAdapter"]


class PromptAdapter(BaseAdapter):
    """Адаптер для формирования чистого текстового системного промпта."""

    @property
    def target_name(self) -> str:
        return "prompt"

    def export_agent(self, profile: AgentProfile, compiled_skills_content: str) -> str:
        effective_model = profile.model_name or profile.model
        parts = [
            f"# Persona: {profile.name}",
            f"Role: {profile.role}",
            f"Description: {profile.description}",
        ]
        if effective_model:
            parts.append(f"Model: {effective_model}")
        if profile.tools:
            parts.append(f"Allowed Tools: {', '.join(profile.tools)}")
        if profile.instructions:
            parts.extend(["", "## Instructions", profile.instructions])
        if compiled_skills_content.strip():
            parts.extend(["", "## Guidelines & Skills", compiled_skills_content.strip()])
        return "\n\n".join(part for part in parts if part).strip() + "\n"
