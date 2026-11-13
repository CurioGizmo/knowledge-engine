"""Тесты для модуля парсинга карточек скиллов и слотов."""

from pathlib import Path
import pytest

from mcp_skills.errors import InvalidSkillFormatError
from mcp_skills.parser import find_slots_in_skeleton, parse_skill_markdown


def test_find_slots_in_skeleton() -> None:
    skeleton = """# Planning Skeleton
Some header

<!-- SLOT: backend -->
Backend details

<!-- SLOT: ui -->
UI details

<!-- SLOT: database -->
"""
    slots = find_slots_in_skeleton(skeleton)
    assert slots == ["backend", "ui", "database"]


def test_find_slots_empty() -> None:
    skeleton = "# Plain skeleton without slot markers"
    assert find_slots_in_skeleton(skeleton) == []


def test_parse_skill_markdown_full(tmp_path: Path) -> None:
    md_content = """---
name: test-skill
title: Test Skill Title
description: A comprehensive test skill
category: backend-db
stack: [python, fastapi]
kind: skill
extends: base-skill
merge_strategy: prepend
categories: [api, test]
---
# Main Content Body

<!-- SLOT: storage -->
"""
    card, body = parse_skill_markdown(md_content, base_dir=tmp_path)
    assert card.name == "test-skill"
    assert card.title == "Test Skill Title"
    assert card.description == "A comprehensive test skill"
    assert card.category == "backend-db"
    assert card.stack == ["python", "fastapi"]
    assert card.kind == "skill"
    assert card.extends == "base-skill"
    assert card.merge_strategy == "prepend"
    assert card.categories == ["api", "test"]
    assert card.available_slots == ["storage"]
    assert "# Main Content Body" in body


def test_parse_skill_markdown_no_frontmatter(tmp_path: Path) -> None:
    # If no frontmatter, should raise InvalidSkillFormatError or construct fallback if name supplied
    with pytest.raises(InvalidSkillFormatError):
        parse_skill_markdown("Just some markdown content without frontmatter", base_dir=tmp_path)


def test_parse_skill_markdown_domain_fallback(tmp_path: Path) -> None:
    md_content = """---
name: legacy-skill
title: Legacy Skill
domain: audio-ml
domains: [audio, ml]
---
# Audio ML Content
"""
    card, body = parse_skill_markdown(md_content, base_dir=tmp_path)
    assert card.name == "legacy-skill"
    assert card.category == "audio-ml"
    assert card.categories == ["audio", "ml"]
