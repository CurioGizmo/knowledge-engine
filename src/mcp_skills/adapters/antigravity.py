"""Адаптер для Google Antigravity (AGY) SDK / define_subagent JSON format."""

from __future__ import annotations

import json

from mcp_skills.adapters.base import BaseAdapter
from mcp_skills.models import AgentProfile

__all__ = ["AntigravityAdapter"]


class AntigravityAdapter(BaseAdapter):
    """Адаптер для экспорта профиля агента в формат Google Antigravity / define_subagent payload."""

    @property
    def target_name(self) -> str:
        return "antigravity"

    def export_agent(self, profile: AgentProfile, compiled_skills_content: str) -> str:
        system_prompt_parts = [
            f"Role: {profile.role}",
            f"Description: {profile.description}",
        ]
        if profile.instructions:
            system_prompt_parts.extend(["", "## Instructions", profile.instructions])
        if compiled_skills_content.strip():
            system_prompt_parts.extend(
                [
                    "",
                    "## Assigned Skills & Invariants",
                    compiled_skills_content,
                ]
            )
        system_prompt = "\n".join(system_prompt_parts).strip()

        model = profile.model_name or profile.model
        payload = {
            "name": profile.name,
            "role": profile.role,
            "description": profile.description,
            "model": model,
            "system_prompt": system_prompt,
            "skills": profile.skills,
            "permissions": profile.permissions.model_dump(),
            "enable_write_tools": profile.permissions.write,
            "enable_mcp_tools": profile.permissions.mcp,
            "enable_subagent_tools": profile.permissions.subagents,
            "mode": profile.mode,
            "scope": profile.scope,
        }
        if profile.tools:
            payload["tools"] = profile.tools
        return json.dumps(payload, indent=2, ensure_ascii=False)
