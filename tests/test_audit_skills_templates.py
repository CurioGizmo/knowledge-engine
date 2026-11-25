"""Тесты синхронизации шаблонов mcp_skills с парсерами mcp_audit (шаг 1 AUDIT-IMPROVEMENTS-2026-11)."""

from pathlib import Path
import pytest
from mcp_audit.parser import (
    parse_audit_markdown,
    parse_plan_markdown,
    parse_review_markdown,
)
from tests.conftest import get_companion_skills_dir


def _get_templates_dir() -> Path:
    companion = get_companion_skills_dir()
    if companion is None:
        pytest.skip("mcp_skills companion repository not found")
    tdir = companion / "templates"
    if not tdir.is_dir():
        pytest.skip(f"templates directory not found in {companion}")
    return tdir


def test_standard_plan_template_parses():
    templates_dir = _get_templates_dir()
    template_path = templates_dir / "standard-plan.template.md"
    if not template_path.exists():
        pytest.skip(f"Файл {template_path} отсутствует")
    content = template_path.read_text(encoding="utf-8")
    plan = parse_plan_markdown(content, file_path=template_path)
    assert plan.task_id is not None
    assert plan.title is not None
    assert plan.category is not None
    assert len(plan.steps) > 0


def test_master_plan_template_parses():
    templates_dir = _get_templates_dir()
    template_path = templates_dir / "master-plan.template.md"
    if not template_path.exists():
        pytest.skip(f"Файл {template_path} отсутствует")
    content = template_path.read_text(encoding="utf-8")
    plan = parse_plan_markdown(content, file_path=template_path)
    assert plan.task_id is not None
    assert plan.title is not None
    assert len(plan.steps) > 0


def test_review_log_template_parses():
    templates_dir = _get_templates_dir()
    template_path = templates_dir / "review-log.template.md"
    if not template_path.exists():
        pytest.skip(f"Файл {template_path} отсутствует")
    content = template_path.read_text(encoding="utf-8")
    review = parse_review_markdown(content, file_path=template_path)
    assert review.id is not None
    assert review.category is not None
    assert review.round is not None
    assert len(review.comments) > 0


def test_audit_card_template_parses():
    templates_dir = _get_templates_dir()
    template_path = templates_dir / "audit-card.template.md"
    if not template_path.exists():
        pytest.skip(f"Файл {template_path} отсутствует")
    content = template_path.read_text(encoding="utf-8")
    card = parse_audit_markdown(content, file_path=template_path)
    assert card.id is not None
    assert card.category is not None
    assert card.priority is not None
