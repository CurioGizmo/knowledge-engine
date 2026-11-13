"""Адаптер для Cursor Rules (.cursor/rules/*.mdc)."""

from __future__ import annotations

import yaml

from mcp_skills.adapters.base import BaseAdapter
from mcp_skills.models import AgentProfile

__all__ = ["CursorAdapter"]


class CursorAdapter(BaseAdapter):
    """Адаптер для экспорта профиля агента и скиллов в формат правил Cursor (.mdc)."""

    @property
    def target_name(self) -> str:
        return "cursor"

    def export_agent(self, profile: AgentProfile, compiled_skills_content: str) -> str:
        frontmatter = {
            "description": profile.description,
            "globs": ["*"],
            "alwaysApply": False,
        }
        yaml_header = yaml.dump(frontmatter, allow_unicode=True, sort_keys=False).strip()

        parts = [f"# Agent: {profile.name} ({profile.role})"]
        effective_model = profile.model_name or profile.model
        if effective_model:
            parts.append(f"Model: {effective_model}")
        if profile.tools:
            parts.append(f"Tools: {', '.join(profile.tools)}")
        if profile.instructions:
            parts.extend(["", "## Instructions", profile.instructions])
        if compiled_skills_content.strip():
            parts.extend(["", compiled_skills_content.strip()])
        body = "\n".join(parts).strip()
        return f"---\n{yaml_header}\n---\n\n{body}\n"
