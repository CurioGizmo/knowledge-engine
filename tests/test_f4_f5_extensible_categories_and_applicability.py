"""Тесты для F4 (расширяемые категории) и F5 (контракт применимости requires_stack)."""

from __future__ import annotations

from pathlib import Path
import yaml

from mcp_skills.categories import (
    get_categories_dict,
    get_category_keywords,
    is_valid_category,
    load_categories_data,
)
from mcp_skills.creator import create_skill
from mcp_skills.models import SkillCard
from mcp_skills.server import load_skill
from mcp_skills.storage import SkillsStorage, check_skill_applicability
from mcp_skills.synchronizer import SkillsSynchronizer


def test_f4_is_valid_category_slugs(tmp_path: Path) -> None:
    """F4: is_valid_category принимает категории из categories.yaml и базового реестра."""
    repo_dir = tmp_path / "skills-repo"
    docs_dir = repo_dir / "docs"
    docs_dir.mkdir(parents=True)
    cat_yaml = docs_dir / "categories.yaml"
    cat_yaml.write_text(
        """categories:
  devops:
    title: DevOps
    description: CI/CD and deployment
  cloud-infra:
    title: Cloud Infra
    description: Terraform and AWS
""",
        encoding="utf-8",
    )
    assert is_valid_category("devops", skills_repo_dir=repo_dir)
    assert is_valid_category("cloud-infra", skills_repo_dir=repo_dir)
    assert is_valid_category("backend-db", skills_repo_dir=repo_dir)  # стандартная

    # Недопустимые / незарегистрированные
    assert not is_valid_category("", skills_repo_dir=repo_dir)
    assert not is_valid_category("DevOps", skills_repo_dir=repo_dir)  # uppercase
    assert not is_valid_category("cloud infra", skills_repo_dir=repo_dir)  # пробел
    assert not is_valid_category("../../traversal", skills_repo_dir=repo_dir)
    assert not is_valid_category("unknown-unregistered", skills_repo_dir=repo_dir)


def test_f4_create_skill_appends_to_categories_yaml(tmp_path: Path) -> None:
    """F4: _append_categories дописывает новую категорию в docs/categories.yaml."""
    repo_dir = tmp_path / "skills-repo"
    repo_dir.mkdir(parents=True)
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    sync = SkillsSynchronizer(storage, [repo_dir / "skills"])

    from mcp_skills.creator import _append_categories

    # Добавляем категорию в categories.yaml через _append_categories
    added = _append_categories("devops", repo_dir, description="CI/CD and k8s")
    assert added is True

    # Проверяем, что теперь create_skill с категорией devops проходит
    res = create_skill(
        name="k8s-deploy",
        title="Kubernetes Deployment",
        description="Deploy apps on k8s",
        content="# K8s Guide\nDeploy properly.",
        category="devops",
        skills_repo_dir=repo_dir,
        storage=storage,
        synchronizer=sync,
    )
    assert res["status"] == "created"

    # Проверяем categories.yaml
    cat_yaml = repo_dir / "docs" / "categories.yaml"
    assert cat_yaml.is_file()
    data = yaml.safe_load(cat_yaml.read_text(encoding="utf-8"))
    assert "devops" in data["categories"]
    assert data["categories"]["devops"]["title"] == "Devops"

    # Проверяем загрузку категорий
    cats = load_categories_data(repo_dir)
    assert "devops" in cats
    keywords = get_category_keywords(repo_dir)
    assert "devops" in keywords


def test_f5_check_skill_applicability() -> None:
    """F5: check_skill_applicability поддерживает list, dict (all_of/any_of/none_of) и unknown."""
    # 1. Универсальный скилл -> applicable всегда
    universal = SkillCard(name="univ", title="Universal", category="general")
    assert check_skill_applicability(universal, None) == "applicable"
    assert check_skill_applicability(universal, []) == "applicable"
    assert check_skill_applicability(universal, ["python"]) == "applicable"

    # 2. Специфичный скилл при пустом стеке -> unknown
    python_skill = SkillCard(
        name="py-skill",
        title="Python",
        category="backend-db",
        requires_stack=["python"],
    )
    assert check_skill_applicability(python_skill, None) == "unknown"
    assert check_skill_applicability(python_skill, []) == "unknown"

    # 3. Список (any_of)
    assert check_skill_applicability(python_skill, ["python", "sqlite"]) == "applicable"
    assert check_skill_applicability(python_skill, ["ruby", "rails"]) == "unknown"

    # 4. all_of
    all_of_skill = SkillCard(
        name="full-skill",
        title="Full",
        category="backend-db",
        requires_stack={"all_of": ["python", "fastapi"]},
    )
    assert check_skill_applicability(all_of_skill, ["python", "fastapi", "docker"]) == "applicable"
    assert check_skill_applicability(all_of_skill, ["python", "sqlite"]) == "unknown"

    # 5. any_of + none_of
    complex_skill = SkillCard(
        name="web-skill",
        title="Web",
        category="frontend",
        requires_stack={"any_of": ["react", "vue"], "none_of": ["angular"]},
    )
    assert check_skill_applicability(complex_skill, ["react", "typescript"]) == "applicable"
    assert check_skill_applicability(complex_skill, ["react", "angular"]) == "incompatible"
    assert check_skill_applicability(complex_skill, ["svelte"]) == "unknown"

    # 6. activation
    mcp_skill = SkillCard(
        name="mcp-skill",
        title="MCP",
        category="general",
        activation="mcp",
    )
    assert check_skill_applicability(mcp_skill, None) == "unknown"
    assert check_skill_applicability(mcp_skill, ["mcp", "python"]) == "applicable"
    assert check_skill_applicability(mcp_skill, ["offline", "python"]) == "incompatible"


def test_f5_load_skill_returns_applicability(tmp_path: Path) -> None:
    """F5: load_skill возвращает поле 'applicability'."""
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    skill_dir = skills_dir / "sqlite-perf"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: sqlite-perf
title: SQLite Perf
category: backend-db
requires_stack: [sqlite]
---
# SQLite Perf
Guidelines.
""",
        encoding="utf-8",
    )

    incomp_dir = skills_dir / "incomp-perf"
    incomp_dir.mkdir()
    (incomp_dir / "SKILL.md").write_text(
        """---
name: incomp-perf
title: Incomp Perf
category: backend-db
requires_stack:
  none_of: [ruby]
---
# Incomp Perf
Guidelines.
""",
        encoding="utf-8",
    )

    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()

    # С совместимым стеком
    res_app = load_skill(
        name="sqlite-perf",
        stack=["python", "sqlite"],
        storage=storage,
        synchronizer=sync,
    )
    assert res_app["applicability"] == "applicable"

    # Со стеком без подтверждения позитивных требований -> unknown
    res_unknown = load_skill(
        name="sqlite-perf",
        stack=["ruby", "postgres"],
        storage=storage,
        synchronizer=sync,
    )
    assert res_unknown["applicability"] == "unknown"

    # С явно несовместимым стеком (none_of)
    res_incomp = load_skill(
        name="incomp-perf",
        stack=["ruby", "postgres"],
        storage=storage,
        synchronizer=sync,
    )
    assert res_incomp["applicability"] == "incompatible"
