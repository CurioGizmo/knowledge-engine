"""Тесты C6/B5: честность фильтрации стека — overview консистентен с drilldown/search.

Сценарий (воспроизведение verdict_7b, утверждение 1): каталог с карточками,
у части которых заполнен requires_stack. При project_stack=['python'] drilldown и
search их скрывают (_matches_project_stack), а overview до фикса считает все —
расхождение видно пользователю немедленно, т.к. MCP-обёртка всегда передаёт
project_stack="auto" (server.py:909).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from mcp_skills.server import create_server, search_skills
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer

# Эталонный каталог: 2 карточки только-аудио-ML (не матчат python), 3 совместимых.
# Воспроизводит симуляцию «после C1» из verdict_7b (5 карточек, 3 с requires_stack).
AUDIO_SKILL_MD = """---
name: whisper-engine
title: ONNX Whisper Engine
description: Whisper ASR engine
category: audio-ml
requires_stack:
  none_of: [python]
---
# Whisper Engine
"""

LORA_SKILL_MD = """---
name: profile-lora-training
title: Profile LoRA Training
description: LoRA training profiling
category: audio-ml
requires_stack:
  none_of: [python]
---
# LoRA Training
"""

PLANS_SKILL_MD = """---
name: python-design-patterns
title: Python Design Patterns
description: Idiomatic patterns
category: planning
requires_stack:
  none_of: [pytorch]
---
# Patterns
"""

AGENTS_SKILL_MD = """---
name: writing-agents
title: Writing Agents
description: Agent authoring
category: planning
requires_stack:
  none_of: [pytorch]
---
# Agents
"""

UV_SKILL_MD = """---
name: uv-usage
title: uv Usage
description: uv package manager
category: general
---
# uv
"""


def _make_catalog(tmp_path: Path) -> tuple[SkillsStorage, SkillsSynchronizer]:
    """Собирает эфемерный каталог скиллов через SkillsSynchronizer (как existing-тесты)."""
    db_path = tmp_path / "skills_fts.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)

    for name, md in (
        ("whisper-engine", AUDIO_SKILL_MD),
        ("profile-lora-training", LORA_SKILL_MD),
        ("python-design-patterns", PLANS_SKILL_MD),
        ("writing-agents", AGENTS_SKILL_MD),
        ("uv-usage", UV_SKILL_MD),
    ):
        skill_dir = skills_dir / name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(md, encoding="utf-8")

    sync = SkillsSynchronizer(storage=storage, skills_dirs=[skills_dir])
    sync.sync()
    return storage, sync


def test_get_category_counts_with_stack(tmp_path: Path) -> None:
    """C6-storage: при переданном стеке get_category_counts фильтрует карточки как search."""
    storage, sync = _make_catalog(tmp_path)

    # Без стека: полные счётчики (регресс на поведение по умолчанию)
    counts_plain = storage.get_category_counts()
    assert counts_plain == {"audio-ml": 2, "planning": 2, "general": 1}

    # Со стеком python: audio-ml исключён (requires_stack=[pytorch] не пересекается)
    counts_py = storage.get_category_counts(project_stack=["python"])
    assert counts_py == {"audio-ml": 0, "planning": 2, "general": 1}

    # Со стеком pytorch: только audio-ml непуст
    counts_ml = storage.get_category_counts(project_stack=["pytorch"])
    assert counts_ml == {"audio-ml": 2, "planning": 0, "general": 1}


def test_get_category_counts_cold_start_fallback_with_stack(tmp_path: Path) -> None:
    """C6-storage: cold-start fallback {c: 0} сохранён и для ветки со стеком."""
    storage = SkillsStorage(tmp_path / "empty.db")
    assert storage.get_category_counts(project_stack=["python"]) == {
        c: 0
        for c in (
            "planning",
            "audio-ml",
            "desktop",
            "frontend",
            "backend-db",
            "quality-review",
            "general",
        )
    }
    # И без стека — прежнее поведение (fallback для обеих веток)
    assert storage.get_category_counts() is not None


def test_overview_consistent_with_drilldown_and_search(tmp_path: Path) -> None:
    """C6-server: при стеке python overview не показывает категорию только-аудио-скиллов.

    До фикса: overview показывает audio-ml=2, drilldown по category='audio-ml'
    возвращает 0 — расхождение воспроизведено в verdict_7b (scratch/repro_e3).
    """
    storage, sync = _make_catalog(tmp_path)
    root = tmp_path

    # Overview со стеком python: категория audio-ml скрыта (count=0 → не показывается)
    resp_overview = search_skills(
        query="",
        project_stack=["python"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    assert resp_overview["mode"] == "overview"
    cats_py = {c["category"]: c["skills_count"] for c in resp_overview["categories"]}
    assert "audio-ml" not in cats_py
    assert cats_py == {"planning": 2, "general": 1}

    # Drilldown по той же категории со стеком python — консистентно: 0 скиллов
    resp_drill = search_skills(
        query="",
        category="audio-ml",
        project_stack=["python"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    assert resp_drill["mode"] == "category"
    assert resp_drill["count"] == 0
    assert resp_drill["results"] == []

    # Search по audio-тексту со стеком python тоже скрывает скиллы
    resp_search = search_skills(
        query="whisper",
        project_stack=["python"],
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    assert {r["name"] for r in resp_search["results"]} == set()

    # Контроль: без стека выдача не меняется (все категории и скиллы на месте)
    resp_overview_plain = search_skills(
        query="", storage=storage, synchronizer=sync, project_root=root
    )
    cats_plain = {c["category"]: c["skills_count"] for c in resp_overview_plain["categories"]}
    assert cats_plain == {"audio-ml": 2, "planning": 2, "general": 1}

    resp_drill_plain = search_skills(
        query="", category="audio-ml", storage=storage, synchronizer=sync, project_root=root
    )
    assert resp_drill_plain["count"] == 2


def test_overview_zero_categories_kept_without_stack(tmp_path: Path) -> None:
    """C6-server: без стека нулевые категории НЕ скрываются (cold-start {c:0} осмыслен)."""
    storage = SkillsStorage(tmp_path / "empty.db")
    resp = search_skills(
        query="",
        storage=storage,
        project_root=tmp_path,
    )
    assert resp["mode"] == "overview"
    # Все стандартные категории присутствуют со счётчиком 0 — ничего не скрыто
    assert {c["category"] for c in resp["categories"]} == {
        "planning",
        "audio-ml",
        "desktop",
        "frontend",
        "backend-db",
        "quality-review",
        "general",
    }
    assert all(c["skills_count"] == 0 for c in resp["categories"])


def test_overview_stack_filter_payload(tmp_path: Path) -> None:
    """C6-server: overview со стеком помечает применённый фильтр (как drilldown/search)."""
    storage, sync = _make_catalog(tmp_path)

    resp = search_skills(
        query="",
        project_stack=["python", "mcp"],
        storage=storage,
        synchronizer=sync,
        project_root=tmp_path,
    )
    sf = resp.get("stack_filter")
    assert sf is not None
    assert sf["applied"] is True
    assert sf["project_stack"] == ["python", "mcp"]


def test_mcp_tool_overview_auto_stack_consistency(tmp_path: Path) -> None:
    """C6 (E2E через MCP-обёртку): search_skills-тул с project_stack='auto'.

    MCP-обёртка всегда передаёт project_stack='auto' — overview обязан
    применять тот же фильтр, что drilldown (консистентность из verdict_7b).
    Проект-манифест pyproject.toml => resolved_stack содержит 'python'.
    """
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "e2e"\n', encoding="utf-8")

    server = create_server(project_root=tmp_path)
    res = asyncio.run(_call_search_tool(server, {}))
    data = json.loads(res.content[0].text)

    assert data["mode"] == "overview"
    # resolved_stack = ['python', 'mcp'] — auto-режим добавляет ось mcp
    assert data["stack_filter"]["applied"] is True
    assert "python" in data["stack_filter"]["project_stack"]


async def _call_search_tool(server: Any, args: dict[str, Any]) -> Any:
    return await server.call_tool("search_skills", args)


def test_search_skills_docstring_stack_honesty(tmp_path: Path) -> None:
    """B5: docstring search_skills-тула честно описывает условия скрытия карточек.

    До фикса: безусловное «карточки ... скрываются автоматически под стек проекта»
    при пустом requires_stack у 25/25 карточек — обещание не выполняется.
    После C1 честная формулировка: скрытие только для карточек с указанным
    requires_stack/activation; без requires_stack карточка универсальна.
    """
    server = create_server(project_root=tmp_path)
    tools = asyncio.run(server.list_tools())
    tool = next(t for t in tools if t.name == "search_skills")
    doc = tool.description or ""

    # Старое безусловное обещание удалено...
    assert "скрываются автоматически" not in doc
    # ...заменено на честные условия скрытия
    assert "requires_stack" in doc
    assert "activation" in doc
    assert "universal" in doc.lower() or "универсальной" in doc
