"""F7 (review-fixes): тесты scripts/verify_catalog.py на минимальном фикс-каталоге.

main() скрипта принимает путь к каталогу (tmp-копия), поэтому тесты не зависят
от живого репозитория и не меняют его. Сломанный каталог → FAIL (exit 1).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scripts.verify_catalog import main  # noqa: E402

import scripts.verify_catalog as vc  # noqa: E402

FRONTMATTER = """---
name: {name}
title: {title}
category: {category}
description: "{description}"
aliases: {aliases}{extra}
---
"""

BODY = """# {title}

{body}

## Обязательные правила

- Правило 1
- Правило 2

## Примеры

```python
def example():
    return 42
```
"""

TEST_SKILLS = [
    {
        "name": "sqlite-performance",
        "title": "SQLite Performance",
        "category": "backend-db",
        "description": "Оптимизация SQLite: индексы, WAL, EXPLAIN QUERY PLAN.",
        "aliases": '["sqlite", "wal", "индексы"]',
        "body": "Анализ медленных запросов SQLite и настройка индексов.",
    },
    {
        "name": "code-review",
        "title": "Code Review",
        "category": "quality-review",
        "description": "Строгий аудит кода: Scope Cascade, критерии, форматы вердиктов.",
        "aliases": '["review", "ревью"]',
        "body": "Процесс независимого ревью изменений кода.",
    },
    {
        "name": "writing-plans",
        "title": "Writing Plans",
        "category": "planning",
        "description": "Проектирование планов: декомпозиция, контрольные точки, TDD.",
        "aliases": '["план", "plans", "архитектура"]',
        "body": "Методология трёхуровневого планирования.",
    },
    {
        "name": "brainstorming",
        "title": "Brainstorming",
        "category": "planning",
        "description": "Структурированный брейншторминг идей перед планированием.",
        "aliases": '["ideas", "идеи"]',
        "body": "Техники генерации и отбора идей.",
    },
]

WRITING_PLANS_SLOTS: dict[str, dict[str, str]] = {
    "mode": {
        "default": "## Режим\n\nВыбор режима работы по умолчанию. " + "подробности " * 60,
    },
    "integration": {
        "python": "## Интеграция (python)\n\nPython-специфика интеграции. " + "детали " * 80,
        "mcp": "## Интеграция (mcp)\n\nИнтеграция через MCP-инструменты. " + "детали " * 80,
    },
}

AGENTS_ADD_TESTS = """name: add-tests
role: Test Engineer
description: Добавляет тесты к изменённому коду.
skills:
  - writing-plans
  - code-review
"""

FLOW_FILE = """---
name: test-flow
title: Test Flow
description: Контрольный флоу верификации.
aliases: [test]
---

# Test Flow

## Stage 1: do-work

- Описание этапа

- Выполнить работу

- Проверить результат
"""

FIXTURE_CONTROL_QUERIES = [
    ("sqlite performance", "sqlite-performance"),
    ("code review", "code-review"),
    ("writing plans", "writing-plans"),
    ("brainstorming ideas", "brainstorming"),
]


def _build_catalog(tmp_path: Path) -> Path:
    """Собирает минимальный фикс-каталог: 4 скилла, слоты, агент, флоу."""
    repo = tmp_path / "catalog"
    skills_base = repo / "skills"
    for meta in TEST_SKILLS:
        skill_dir = skills_base / meta["category"] / meta["name"]
        skill_dir.mkdir(parents=True)
        extra = ""
        if meta["name"] == "writing-plans":
            extra = "\navailable_slots:\n  - mode\n  - integration"
            meta = {**meta, "name": meta["name"]}  # copy для чтения ниже
        fm = FRONTMATTER.format(extra=extra, **meta)
        body = BODY.format(**meta)
        content = fm + "\n" + body
        if meta["name"] == "writing-plans":
            content += "\n\n## Слоты\n\n<!-- SLOT: mode -->\n<!-- SLOT: integration -->\n"
            for slot, variants in WRITING_PLANS_SLOTS.items():
                block_dir = skill_dir / "blocks" / slot
                block_dir.mkdir(parents=True)
                for tech, block_text in variants.items():
                    (block_dir / f"{tech}.md").write_text(block_text, encoding="utf-8")
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

    # Агент add-tests (профиль для bundle_agent)
    agents_dir = repo / "agents" / "add-tests"
    agents_dir.mkdir(parents=True)
    (agents_dir / "AGENT.yaml").write_text(AGENTS_ADD_TESTS, encoding="utf-8")

    # Флоу для list_available_flows (через skills_dir → get_flows_cache_dir)
    flows_dir = repo / "flows"
    flows_dir.mkdir(parents=True)
    (flows_dir / "test-flow.md").write_text(FLOW_FILE, encoding="utf-8")
    return repo


def test_verify_catalog_passes_on_valid_fixture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() на валидном фикс-каталоге возвращает exit-code 0."""
    repo = _build_catalog(tmp_path)
    original_queries = vc.CONTROL_QUERIES
    try:
        # Фикс-каталог содержит только 4 скилла: контрольные запросы — его подмножество
        vc.CONTROL_QUERIES = list(FIXTURE_CONTROL_QUERIES)
        # Порог бандла адаптирован к компактной фикстуре (живой каталог: 6000)
        exit_code = vc.main(repo, verbose=True, bundle_min_chars=500)
    finally:
        vc.CONTROL_QUERIES = original_queries
    out = capsys.readouterr().out
    assert "PASS" in out
    assert exit_code == 0, f"exit_code={exit_code}, output:\n{out}"


def test_verify_catalog_fails_on_broken_catalog(tmp_path: Path) -> None:
    """main() на сломанном каталоге (пустой) возвращает exit-code 1."""
    broken = tmp_path / "broken"
    broken.mkdir()
    exit_code = main(broken)
    assert exit_code == 1
