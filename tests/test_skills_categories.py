"""Тесты для реестра категорий и детектора стека проекта."""

from pathlib import Path

from mcp_skills.categories import CATEGORIES, get_categories_prompt, is_valid_category
from mcp_skills.stack_detector import detect_project_stack


def test_categories_registry() -> None:
    assert len(CATEGORIES) == 7
    assert "planning" in CATEGORIES
    assert "audio-ml" in CATEGORIES
    assert "frontend" in CATEGORIES
    assert "backend-db" in CATEGORIES

    assert is_valid_category("planning") is True
    assert is_valid_category("unknown-category") is False


def test_categories_prompt_token_efficiency() -> None:
    prompt = get_categories_prompt()
    assert "planning" in prompt
    assert "audio-ml" in prompt
    # Проверяем, что промпт ультра-компактный (до 600 символов / < 120 токенов)
    assert len(prompt) < 600


def test_stack_detector_python_pyproject(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
name = "my-app"
dependencies = [
    "fastapi>=0.100.0",
    "torch>=2.0.0",
    "pydantic"
]
""",
        encoding="utf-8",
    )
    stack = detect_project_stack(tmp_path)
    assert "python" in stack
    assert "fastapi" in stack
    assert "pytorch" in stack


def test_stack_detector_node_package_json(tmp_path: Path) -> None:
    pkg_json = tmp_path / "package.json"
    pkg_json.write_text(
        """
{
  "name": "ui",
  "dependencies": {
    "react": "^18.0.0",
    "next": "^14.0.0",
    "tailwindcss": "^3.0.0"
  }
}
""",
        encoding="utf-8",
    )
    stack = detect_project_stack(tmp_path)
    assert "node" in stack
    assert "react" in stack
    assert "nextjs" in stack
    assert "tailwind" in stack


def test_dynamic_categories_with_storage(tmp_path: Path) -> None:
    """Проверяет динамическое извлечение категорий из SkillsStorage."""
    from mcp_skills.models import SkillCard
    from mcp_skills.storage import SkillsStorage

    storage = SkillsStorage(tmp_path / "skills.db")
    card = SkillCard(
        name="custom-algo",
        title="Custom Algorithm",
        category="bio-informatics",
        description="DNA sequence processing",
    )
    storage.index_skill(card, content="# DNA sequence processing")

    assert is_valid_category("bio-informatics", storage=storage) is True
    assert is_valid_category("unregistered", storage=storage) is False

    prompt = get_categories_prompt(storage=storage)
    assert "bio-informatics" in prompt
    assert "Пользовательская категория" in prompt
