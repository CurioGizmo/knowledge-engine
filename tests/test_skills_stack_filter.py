"""Тесты для фильтрации search_skills по стеку проекта (activation / requires_stack)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import mcp_skills.cli
from mcp_skills.models import SkillCard
from mcp_skills.parser import parse_skill_markdown
from mcp_skills.server import _resolve_search_stack, search_skills
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer


@pytest.fixture(autouse=True)
def isolated_default_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Исключить случайную синхронизацию установленного пользовательского каталога.
    monkeypatch.setenv("MCP_SKILLS_PATH", str(tmp_path / "empty-catalog"))


# 1. Парсинг и нормализация регистра
def test_parsing_and_case_normalization() -> None:
    # frontmatter с activation: MCP, requires_stack: [Python, FastAPI]
    raw_md = """---
name: test-skill
title: Test Skill
activation: MCP
requires_stack: [Python, FastAPI]
---
# Content
"""
    card, _ = parse_skill_markdown(raw_md)
    assert card.activation == "mcp"
    assert card.requires_stack == ["python", "fastapi"]

    # YAML-скаляр requires_stack: python
    raw_scalar = """---
name: test-scalar
title: Test Scalar
requires_stack: Python
---
# Content
"""
    card_scalar, _ = parse_skill_markdown(raw_scalar)
    assert card_scalar.requires_stack == ["python"]

    # CSV-строка
    card_csv = SkillCard(name="c", title="C", requires_stack="Python, FastAPI")  # type: ignore[arg-type]
    assert card_csv.requires_stack == ["python", "fastapi"]

    # activation с пробелами -> None
    card_blank = SkillCard(name="b", title="B", activation="   ")
    assert card_blank.activation is None

    # activation None -> None
    card_none = SkillCard(name="n", title="N", activation=None)
    assert card_none.activation is None


# 2. Фильтр включён, стек-совпадение
def test_storage_filter_stack_match(tmp_path: Path) -> None:
    store = SkillsStorage(tmp_path / "test.db")
    card = SkillCard(
        name="py-skill",
        title="Py Skill",
        requires_stack=["python"],
    )
    store.index_skill(card, content="python content")

    # Совпадение
    res, truncated = store.search_skills("py-skill", project_stack=["python", "mcp"])  # type: ignore[misc]
    assert not truncated
    assert len(res) == 1
    assert res[0].name == "py-skill"

    # Несовпадение положительного стека: статус unknown (виден при прямом запросе)
    res_ruby, truncated_ruby = store.search_skills("py-skill", project_stack=["ruby"])  # type: ignore[misc]
    assert not truncated_ruby
    assert len(res_ruby) == 1
    assert res_ruby[0].name == "py-skill"
    assert res_ruby[0].applicability == "unknown"

    # Доказанное противоречие (none_of): скрыт
    card_inc = SkillCard(
        name="incompat-ruby-tool",
        title="Incompat Ruby Tool",
        requires_stack={"none_of": ["ruby"]},
    )
    store.index_skill(card_inc, content="incompatible content")
    res_inc, _ = store.search_skills("incompat-ruby-tool", project_stack=["ruby"])  # type: ignore[misc]
    assert len(res_inc) == 0


# 3. Universal карточка видна при любом стеке
def test_universal_card_visible_any_stack(tmp_path: Path) -> None:
    store = SkillsStorage(tmp_path / "test.db")
    card = SkillCard(name="universal-skill", title="Universal Skill")
    store.index_skill(card, content="universal content")

    # С project_stack=["python"]
    res1, _ = store.search_skills("universal-skill", project_stack=["python"])  # type: ignore[misc]
    assert len(res1) == 1

    # С project_stack=["ruby"]
    res2, _ = store.search_skills("universal-skill", project_stack=["ruby"])  # type: ignore[misc]
    assert len(res2) == 1

    # С project_stack=[]
    res3, _ = store.search_skills("universal-skill", project_stack=[])  # type: ignore[misc]
    assert len(res3) == 1


# 4. activation фильтрация
def test_activation_filter(tmp_path: Path) -> None:
    store = SkillsStorage(tmp_path / "test.db")
    c_mcp = SkillCard(name="c-mcp", title="MCP Skill", activation="mcp")
    c_offline = SkillCard(name="c-offline", title="Offline Skill", activation="offline")
    c_always = SkillCard(name="c-always", title="Always Skill", activation="always")
    c_none = SkillCard(name="c-none", title="None Skill", activation=None)

    for c in (c_mcp, c_offline, c_always, c_none):
        store.index_skill(c, content=f"content {c.name}")

    # Стек с mcp
    res_mcp, _ = store.search_skills(query="", project_stack=["python", "mcp"], limit=10)  # type: ignore[misc]
    names_mcp = {c.name for c in res_mcp}
    assert "c-mcp" in names_mcp
    assert "c-offline" not in names_mcp
    assert "c-always" in names_mcp
    assert "c-none" in names_mcp

    # Стек с offline
    res_off, _ = store.search_skills(query="", project_stack=["python", "offline"], limit=10)  # type: ignore[misc]
    names_off = {c.name for c in res_off}
    assert "c-mcp" not in names_off
    assert "c-offline" in names_off
    assert "c-always" in names_off
    assert "c-none" in names_off


# 5. Over-fetch / дожим
def test_over_fetch_and_cap(tmp_path: Path) -> None:
    store = SkillsStorage(tmp_path / "test.db")

    # 12 нерелевантных скиллов (ruby)
    for i in range(12):
        c = SkillCard(name=f"ruby-skill-{i:02d}", title=f"Ruby Tool {i}", requires_stack=["ruby"])
        store.index_skill(c, content=f"search keyword ruby {i}")

    # 2 релевантных скилла (python)
    for i in range(2):
        c = SkillCard(
            name=f"python-skill-{i:02d}", title=f"Python Tool {i}", requires_stack=["python"]
        )
        store.index_skill(c, content=f"search keyword python {i}")

    # limit=2: должны возвращаться ровно 2 релевантных
    res, truncated = store.search_skills(  # type: ignore[misc]
        query="search keyword", limit=2, project_stack=["python"]
    )
    assert not truncated
    assert len(res) == 2
    assert all("python-skill" in c.name for c in res)

    # Проверка cap: 100 несовместимых скиллов (none_of python), limit=2, без релевантных
    store_cap = SkillsStorage(tmp_path / "test_cap.db")
    for i in range(100):
        c = SkillCard(
            name=f"other-skill-{i:03d}",
            title=f"Other {i}",
            requires_stack={"none_of": ["python"]},
        )
        store_cap.index_skill(c, content="cap test")

    res_cap, truncated_cap = store_cap.search_skills(  # type: ignore[misc]
        query="cap test", limit=2, project_stack=["python"]
    )
    assert len(res_cap) == 0
    # При 100 несовместимых и limit=2: page_size = max(2*4, 16) = 16.
    # 4 страницы = 64 просмотренных строки < 100 -> остановка по cap!
    assert truncated_cap is True


# 6. Фильтр выключен: project_stack=None возвращает list[SkillCard], не кортеж
def test_filter_off_returns_list_not_tuple(tmp_path: Path) -> None:
    store = SkillsStorage(tmp_path / "test.db")
    store.index_skill(SkillCard(name="s1", title="S1", requires_stack=["ruby"]), content="common")
    store.index_skill(SkillCard(name="s2", title="S2", requires_stack=["python"]), content="common")

    res = store.search_skills(query="common", project_stack=None)
    assert isinstance(res, list)
    assert not isinstance(res, tuple)
    assert len(res) == 2


# 7. Stale-кэш (эмуляция старого формата)
def test_stale_cache_backward_compatibility(tmp_path: Path) -> None:
    store = SkillsStorage(tmp_path / "test.db")
    card = SkillCard(name="old-skill", title="Old Skill")
    store.index_skill(card, content="old content")

    # Прямая перезапись metadata JSON без activation и requires_stack
    old_meta = {
        "name": "old-skill",
        "title": "Old Skill",
        "description": "",
        "categories": ["general"],
        "category": "general",
        "stack": [],
        "kind": "skill",
    }
    with store.get_connection() as conn:
        conn.execute(
            "UPDATE skills_index SET metadata = ? WHERE name = ?",
            (json.dumps(old_meta), "old-skill"),
        )
        conn.commit()

    # Поиск со стеком ["ruby"]: карточка видна (дефолты universal)
    res, _ = store.search_skills(query="old", project_stack=["ruby"])  # type: ignore[misc]
    assert len(res) == 1
    assert res[0].name == "old-skill"
    assert res[0].requires_stack == []
    assert res[0].activation is None


# 8. Серверная обёртка: project_stack="auto" в проекте с pyproject.toml
def test_server_wrapper_auto_and_drilldown(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "my-proj"\n', encoding="utf-8")
    db_path = tmp_path / "skills.db"
    store = SkillsStorage(db_path)

    store.index_skill(
        SkillCard(name="py-tool", title="Py Tool", category="backend", requires_stack=["python"]),
        content="py tool backend",
    )
    store.index_skill(
        SkillCard(name="rb-tool", title="Rb Tool", category="backend", requires_stack=["ruby"]),
        content="rb tool backend",
    )

    # Search-режим
    resp = search_skills(
        query="tool",
        project_stack="auto",
        project_root=tmp_path,
        storage=store,
    )
    assert "stack_filter" in resp
    sf = resp["stack_filter"]
    assert sf["applied"] is True
    assert "python" in sf["project_stack"]
    assert "mcp" not in sf["project_stack"]
    assert sf["truncated"] is False
    assert resp["count"] == 2
    assert resp["results"][0]["name"] == "py-tool"
    assert resp["results"][0]["applicability"] == "applicable"
    assert resp["results"][1]["name"] == "rb-tool"
    assert resp["results"][1]["applicability"] == "unknown"

    # Drilldown-режим (category="backend", query="")
    resp_drilldown = search_skills(
        query="",
        category="backend",
        project_stack="auto",
        project_root=tmp_path,
        storage=store,
    )
    assert "stack_filter" in resp_drilldown
    sf_drill = resp_drilldown["stack_filter"]
    assert sf_drill["applied"] is True
    assert "python" in sf_drill["project_stack"]
    assert resp_drilldown["count"] == 2
    assert resp_drilldown["results"][0]["name"] == "py-tool"
    assert resp_drilldown["results"][1]["name"] == "rb-tool"


# 9. CSV-строка project_stack="python,fastapi"
def test_csv_project_stack_normalized(tmp_path: Path) -> None:
    db_path = tmp_path / "skills.db"
    store = SkillsStorage(db_path)
    store.index_skill(
        SkillCard(name="fastapi-tool", title="FastAPI Tool", requires_stack=["fastapi"]),
        content="fastapi tool",
    )

    resp = search_skills(
        query="fastapi",
        project_stack="python,fastapi",
        project_root=tmp_path,
        storage=store,
    )
    assert "stack_filter" in resp
    assert resp["stack_filter"]["project_stack"] == ["python", "fastapi"]
    assert resp["count"] == 1


# 10. Пустой detected_stack -> фильтр off
def test_empty_detected_stack_turns_filter_off(tmp_path: Path) -> None:
    # Директория без манифестов
    empty_repo = tmp_path / "empty_repo"
    empty_repo.mkdir()

    db_path = empty_repo / "skills.db"
    store = SkillsStorage(db_path)
    store.index_skill(
        SkillCard(name="py-bound", title="Py Bound", requires_stack=["python"]),
        content="py bound content",
    )

    resp = search_skills(
        query="bound",
        project_stack="auto",
        project_root=empty_repo,
        storage=store,
    )
    # Фильтр выключен, stack_filter отсутствует
    assert "stack_filter" not in resp
    # Скилл с requires_stack: [python] ВИДИМ!
    assert resp["count"] == 1
    assert resp["results"][0]["name"] == "py-bound"


# 11. Пограничные значения project_stack
def test_boundary_values_project_stack(tmp_path: Path) -> None:
    store = SkillsStorage(tmp_path / "test.db")
    store.index_skill(SkillCard(name="s1", title="S1"), content="s1 content")

    # "" -> фильтр off
    resp_empty_str = search_skills(
        query="s1", project_stack="", storage=store, project_root=tmp_path
    )
    assert "stack_filter" not in resp_empty_str

    # [] -> фильтр off
    resp_empty_list = search_skills(
        query="s1", project_stack=[], storage=store, project_root=tmp_path
    )
    assert "stack_filter" not in resp_empty_list

    # ["auto", "python"] -> литеральные теги, не auto sentinel
    resp_list_with_auto = search_skills(
        query="s1", project_stack=["auto", "python"], storage=store, project_root=tmp_path
    )
    assert "stack_filter" in resp_list_with_auto
    assert resp_list_with_auto["stack_filter"]["project_stack"] == ["auto", "python"]


# 12. exclude_defaults в выдаче (глобально, формат не зависит от фильтра)
def test_exclude_defaults_globally(tmp_path: Path) -> None:
    store = SkillsStorage(tmp_path / "test.db")
    universal_card = SkillCard(
        name="univ", title="Univ Title", description="Univ Desc", category="planning"
    )
    bound_card = SkillCard(
        name="bound",
        title="Bound Title",
        description="Bound Desc",
        category="planning",
        activation="mcp",
        requires_stack=["python"],
    )
    store.index_skill(universal_card, content="univ content")
    store.index_skill(bound_card, content="bound content")

    # Вызов с фильтром
    resp_filtered = search_skills(
        query="univ", project_stack=["python"], storage=store, project_root=tmp_path
    )
    r_univ = resp_filtered["results"][0]
    # Дефолтные поля отсутствуют
    assert "activation" not in r_univ
    assert "requires_stack" not in r_univ
    assert "kind" not in r_univ
    assert "extends" not in r_univ
    assert "merge_strategy" not in r_univ
    assert "scope" not in r_univ
    assert "subagents" not in r_univ
    assert "aliases" not in r_univ
    assert "available_slots" not in r_univ
    # Обязательные / недефолтные поля присутствуют
    assert r_univ["name"] == "univ"
    assert r_univ["title"] == "Univ Title"
    assert r_univ["category"] == "planning"
    assert r_univ["categories"] == ["planning"]
    assert "score" in r_univ

    # Вызов без фильтра (project_stack=None): формат выдачи тоже exclude_defaults
    resp_unfiltered = search_skills(
        query="univ", project_stack=None, storage=store, project_root=tmp_path
    )
    r_unf = resp_unfiltered["results"][0]
    assert "activation" not in r_unf
    assert "requires_stack" not in r_unf
    assert "kind" not in r_unf

    # Для bound_card поля со значениями присутствуют
    resp_bound = search_skills(
        query="bound", project_stack=["python", "mcp"], storage=store, project_root=tmp_path
    )
    r_b = resp_bound["results"][0]
    assert r_b["activation"] == "mcp"
    assert r_b["requires_stack"] == ["python"]


# 13. CLI передаёт project_stack="auto"
def test_cli_passes_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    called_args: dict[str, Any] = {}

    def spy_search_skills(**kwargs: Any) -> dict[str, Any]:
        called_args.update(kwargs)
        return {"mode": "overview", "categories": [], "flows": []}

    monkeypatch.setattr(mcp_skills.cli, "search_skills", spy_search_skills)

    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="?", default="")
    parser.add_argument("--search-type", default="all")
    parser.add_argument("--category", default=None)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--project-root", default=None)

    parsed = parser.parse_args(["test-query"])
    ret = mcp_skills.cli._handle_search(parsed)
    assert ret == 0
    assert called_args.get("project_stack") == "auto"


# 14. Регистр в пересечении (MAJOR-A)
def test_case_insensitivity_in_intersection(tmp_path: Path) -> None:
    store = SkillsStorage(tmp_path / "test.db")
    # Скилл с авторингом в верхнем/смешанном регистре
    card = SkillCard(name="python-upper", title="Upper", requires_stack=["Python"])
    store.index_skill(card, content="upper python")

    # Поиск с lowercase стеком
    res, _ = store.search_skills("upper", project_stack=["python"])  # type: ignore[misc]
    assert len(res) == 1
    assert res[0].name == "python-upper"


# 15. truncated-флаг (search и drilldown)
def test_truncated_flag_search_and_drilldown(tmp_path: Path) -> None:
    # 1. Search-часть: 500+ нерелевантных карточек, limit=2 -> truncated=True
    store = SkillsStorage(tmp_path / "test_trunc.db")
    for i in range(500):
        c = SkillCard(
            name=f"ruby-bulk-{i:03d}",
            title=f"Ruby Bulk {i}",
            category="backend",
            requires_stack=["ruby"],
        )
        store.index_skill(c, content="ruby bulk search item")

    # 1 релевантная карточка
    c_py = SkillCard(
        name="py-lone",
        title="Python Lone",
        category="backend",
        requires_stack=["python"],
    )
    store.index_skill(c_py, content="ruby bulk search item python")

    cards, is_trunc = store.search_skills(  # type: ignore[misc]
        query="ruby bulk search item",
        limit=2,
        project_stack=["python"],
    )
    assert is_trunc is True
    assert len(cards) <= 2

    # Через сервер search-режим
    resp = search_skills(
        query="ruby bulk search item",
        limit=2,
        project_stack=["python"],
        storage=store,
        project_root=tmp_path,
    )
    assert resp["stack_filter"]["truncated"] is True
    assert resp["count"] <= 2

    # 2. Drilldown-часть: category с >1600 несовместимых карточек, limit=100
    # Порог: 4 страницы * 400 = 1600. Создадим 1620 карточек.
    store_drill = SkillsStorage(tmp_path / "test_drill_trunc.db")
    for i in range(1620):
        c = SkillCard(
            name=f"drill-rb-{i:04d}",
            title=f"Drill {i}",
            category="bigcat",
            requires_stack={"none_of": ["python"]},
        )
        store_drill.index_skill(c, content="drill content")

    drill_cards, drill_trunc = store_drill.search_skills(  # type: ignore[misc]
        query="",
        category="bigcat",
        limit=100,
        project_stack=["python"],
    )
    assert drill_trunc is True
    assert len(drill_cards) == 0

    # Через сервер drilldown-режим
    resp_drill = search_skills(
        query="",
        category="bigcat",
        limit=100,
        project_stack=["python"],
        storage=store_drill,
        project_root=tmp_path,
    )
    assert resp_drill["stack_filter"]["truncated"] is True


def test_resolve_search_stack_mode_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """При явном MCP_ACTIVE_SERVERS='' режим 'offline', 'mcp' не форсируется."""
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["fastapi"]')
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "")

    resolved = _resolve_search_stack("auto", tmp_path)
    assert resolved is not None
    assert "offline" in resolved
    assert "mcp" not in resolved
    assert "fastapi" in resolved


def test_resolve_search_stack_mode_only_when_no_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """При отсутствии манифестов, но активном режиме MCP, фильтр активен с ['mcp']."""
    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "skills,knowledge")

    resolved = _resolve_search_stack("auto", tmp_path)
    assert resolved == ["mcp"]


def test_step_10_acceptance_stack_discoverability(tmp_path: Path) -> None:
    db_path = tmp_path / "skills.db"
    store = SkillsStorage(db_path)

    store.index_skill(
        SkillCard(
            name="sqlite-performance",
            title="High-Performance SQLite & Storage Engineering",
            category="backend-db",
            requires_stack=["sqlite"],
        ),
        content="sqlite performance WAL busy_timeout fts5",
    )
    store.index_skill(
        SkillCard(
            name="tauri-ipc-patterns",
            title="Tauri IPC Architecture & Patterns",
            category="desktop",
            requires_stack=["tauri"],
        ),
        content="tauri ipc async command stream events",
    )
    store.index_skill(
        SkillCard(
            name="onnx-whisper-engine",
            title="ONNX Whisper Inference Engine",
            category="audio-ml",
            requires_stack=["audio-ml"],
        ),
        content="whisper onnx runtime inference faster-whisper",
    )
    store.index_skill(
        SkillCard(
            name="systematic-debugging",
            title="Systematic Debugging",
            category="quality-review",
        ),
        content="systematic debugging root cause hypothesis",
    )

    # 1. Direct search "sqlite performance" in python-only project (no sqlite) -> found with unknown applicability
    py_proj = tmp_path / "py_proj"
    py_proj.mkdir()
    (py_proj / "pyproject.toml").write_text(
        '[project]\nname = "py-app"\ndependencies = ["fastapi"]\n', encoding="utf-8"
    )

    res_py = search_skills(
        query="sqlite performance", project_stack="auto", project_root=py_proj, storage=store
    )
    assert res_py["count"] >= 1
    found_sqlite = next(c for c in res_py["results"] if c["name"] == "sqlite-performance")
    assert found_sqlite["applicability"] == "unknown"
    assert "не подтверждены" in found_sqlite["applicability_reason"]

    # 2. In project with sqlite -> found with applicable
    (py_proj / "app.db").write_bytes(b"")
    res_py_sqlite = search_skills(
        query="sqlite performance", project_stack="auto", project_root=py_proj, storage=store
    )
    assert res_py_sqlite["count"] >= 1
    found_sqlite_app = next(
        c for c in res_py_sqlite["results"] if c["name"] == "sqlite-performance"
    )
    assert found_sqlite_app["applicability"] == "applicable"

    # 3. Tauri project with tauri.conf.json -> tauri-ipc-patterns is applicable
    tauri_proj = tmp_path / "tauri_proj"
    tauri_proj.mkdir()
    (tauri_proj / "tauri.conf.json").write_text("{}", encoding="utf-8")
    res_tauri = search_skills(
        query="tauri ipc", project_stack="auto", project_root=tauri_proj, storage=store
    )
    assert res_tauri["count"] >= 1
    found_tauri = next(c for c in res_tauri["results"] if c["name"] == "tauri-ipc-patterns")
    assert found_tauri["applicability"] == "applicable"

    # 4. Audio / whisper project with faster-whisper -> onnx-whisper-engine is applicable
    whisper_proj = tmp_path / "whisper_proj"
    whisper_proj.mkdir()
    (whisper_proj / "pyproject.toml").write_text(
        '[project]\ndependencies = ["faster-whisper>=1.0"]\n', encoding="utf-8"
    )
    res_whisper = search_skills(
        query="whisper", project_stack="auto", project_root=whisper_proj, storage=store
    )
    assert res_whisper["count"] >= 1
    found_whisper = next(c for c in res_whisper["results"] if c["name"] == "onnx-whisper-engine")
    assert found_whisper["applicability"] == "applicable"

    # 5. Empty project: overview doesn't have false 0 categories
    empty_proj = tmp_path / "empty_proj"
    empty_proj.mkdir()
    res_overview = search_skills(
        query="", category=None, project_stack="auto", project_root=empty_proj, storage=store
    )
    cat_counts = {c["category"]: c["skills_count"] for c in res_overview["categories"]}
    assert cat_counts["backend-db"] > 0
    assert cat_counts["desktop"] > 0
    assert cat_counts["audio-ml"] > 0
    assert cat_counts["quality-review"] > 0

    # 6. project_stack=[] disables filter completely
    res_disabled = search_skills(
        query="sqlite performance", project_stack=[], project_root=empty_proj, storage=store
    )
    assert "stack_filter" not in res_disabled


def test_step_10_all_catalog_skills_discoverable(tmp_path: Path) -> None:
    from tests.conftest import get_companion_skills_dir

    _catalog = get_companion_skills_dir()
    catalog_root = (
        _catalog
        if _catalog is not None
        else (Path(__file__).resolve().parents[1].parent / "mcp_skills")
    )
    if not (catalog_root / "skills").is_dir():
        pytest.skip("mcp_skills catalog not found at standard path")
    db_path = tmp_path / "skills.db"
    store = SkillsStorage(db_path)
    sync = SkillsSynchronizer(store, [catalog_root / "skills"])
    sync.sync()

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname="test"\n', encoding="utf-8")

    skill_paths = list((catalog_root / "skills").rglob("SKILL.md"))
    # Число скиллов в соседнем каталоге меняется независимо от этого репо —
    # пиннить точное значение нельзя (дефект теста: хардкод == 24 при 25 скиллах).
    # Проверяем, что каталог не пуст и КАЖДОЕ имя реально находится поиском.
    assert len(skill_paths) >= 1

    for p in skill_paths:
        name = p.parent.name
        res = search_skills(
            query=name,
            project_stack="auto",
            project_root=proj,
            storage=store,
            synchronizer=sync,
            limit=10,
        )
        found_names = [c["name"] for c in res["results"]]
        assert any(name in fn or fn in name for fn in found_names), (
            f"Skill {name} was not found by query '{name}'"
        )


def test_skills_search_score_precision_non_zero(tmp_path: Path) -> None:
    """SCORE (41e9): skills search сохраняет ненулевой score для малых значений BM25."""
    from mcp_skills.models import SkillCard

    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)

    long_content = (
        "микросекунда " + "обычное руководство по разработке программного обеспечения " * 300
    )
    card = SkillCard(
        name="micro-skill",
        title="Тест точности ранжирования скиллов",
        description="Описание скилла",
        category="backend",
        content=long_content,
    )
    storage.index_skill(card, content=long_content)

    res = storage.search_skills("микросекунда")
    assert len(res) == 1
    assert res[0].score is not None
    assert res[0].score > 0.0, f"Score не должен округляться в 0.0, получено {res[0].score}"
