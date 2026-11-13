"""Шаг 2 плана: результат сборки без состояния self.last_* в компиляторе."""

import threading
from pathlib import Path
from typing import Any

from mcp_skills.slot_compiler import AssemblyResult, SlotCompiler
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer


def _make_fixture(tmp_path: Path) -> tuple[SkillsStorage, SlotCompiler]:
    """Каталог с одним скиллом с двумя слотами и блоками под два стека."""
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "multi-stack-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: multi-stack-skill
title: Multi Stack Skill
category: planning
available_slots: [ui, backend]
---
# Multi Stack Skill

<!-- SLOT: ui -->

<!-- SLOT: backend -->

## Verification
Follow checklist.
""",
        encoding="utf-8",
    )

    blocks = skill_dir / "blocks"
    (blocks / "ui").mkdir(parents=True)
    (blocks / "backend").mkdir(parents=True)
    (blocks / "ui" / "react.md").write_text(
        "### React UI Guidelines\nUse React 18 hooks.", encoding="utf-8"
    )
    (blocks / "backend" / "fastapi.md").write_text(
        "### FastAPI Backend\nUse Pydantic v2.", encoding="utf-8"
    )

    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()
    compiler = SlotCompiler(storage)
    return storage, compiler


def test_assemble_result_returns_full_result(tmp_path: Path) -> None:
    _storage, compiler = _make_fixture(tmp_path)

    res = compiler.assemble_result(
        name="multi-stack-skill",
        touches=["ui", "backend"],
        project_stack=["react", "fastapi"],
    )

    assert isinstance(res, AssemblyResult)
    assert res.card.name == "multi-stack-skill"
    assert "React UI Guidelines" in res.content
    assert "FastAPI Backend" in res.content
    assert res.resources == []
    assert res.manifest["requested_stack"] == ["react", "fastapi"]
    assert res.manifest["matched_technologies"] == ["react", "fastapi"]
    assert res.manifest["unmatched_technologies"] == []
    assert res.manifest["status"] == "complete"
    assert "missing_slots" not in res.manifest
    assert res.slots_filled == ["ui", "backend"]
    assert res.missing_slots == []
    assert res.effective_slots == ["ui", "backend"]


def test_assemble_result_second_call_does_not_mutate_first(tmp_path: Path) -> None:
    _storage, compiler = _make_fixture(tmp_path)

    first = compiler.assemble_result(
        name="multi-stack-skill",
        touches=["ui"],
        project_stack=["react"],
    )
    second = compiler.assemble_result(
        name="multi-stack-skill",
        touches=["backend"],
        project_stack=["fastapi"],
    )

    # Вторая сборка не меняет результат первой.
    assert "React UI Guidelines" in first.content
    assert "FastAPI Backend" not in first.content
    assert first.manifest["matched_technologies"] == ["react"]
    assert first.slots_filled == ["ui"]
    assert first.effective_slots == ["ui"]

    assert "FastAPI Backend" in second.content
    assert "React UI Guidelines" not in second.content
    assert second.manifest["matched_technologies"] == ["fastapi"]
    assert second.slots_filled == ["backend"]
    assert second.effective_slots == ["backend"]


def test_legacy_tuple_adapters(tmp_path: Path) -> None:
    _storage, compiler = _make_fixture(tmp_path)

    card, content = compiler.assemble_skill(
        name="multi-stack-skill",
        touches=["ui"],
        project_stack=["react"],
    )
    assert card.name == "multi-stack-skill"
    assert "React UI Guidelines" in content

    card2, content2, resources = compiler.assemble_skill_with_resources(
        name="multi-stack-skill",
        touches=["backend"],
        project_stack=["fastapi"],
    )
    assert card2.name == "multi-stack-skill"
    assert "FastAPI Backend" in content2
    assert "React UI Guidelines" not in content2
    assert resources == []


def test_compiler_has_no_last_state(tmp_path: Path) -> None:
    _storage, compiler = _make_fixture(tmp_path)
    compiler.assemble_result(
        name="multi-stack-skill",
        touches=["ui"],
        project_stack=["react"],
    )
    for attr in (
        "last_resources",
        "last_manifest",
        "last_slots_filled",
        "last_missing_slots",
        "last_effective_slots",
    ):
        assert not hasattr(compiler, attr)


def test_concurrent_assemblies_with_barrier(tmp_path: Path) -> None:
    _storage, compiler = _make_fixture(tmp_path)

    barrier = threading.Barrier(2)
    results: dict[str, AssemblyResult | None] = {"react": None, "fastapi": None}
    errors: list[BaseException] = []

    def _worker(tech: str, slot: str) -> None:
        try:
            barrier.wait()
            results[tech] = compiler.assemble_result(
                name="multi-stack-skill",
                touches=[slot],
                project_stack=[tech],
            )
        except BaseException as exc:  # noqa: BLE001 - фиксируем любые сбои потока
            errors.append(exc)

    t1 = threading.Thread(target=_worker, args=("react", "ui"))
    t2 = threading.Thread(target=_worker, args=("fastapi", "backend"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == []
    react_res = results["react"]
    fastapi_res = results["fastapi"]
    assert isinstance(react_res, AssemblyResult)
    assert isinstance(fastapi_res, AssemblyResult)

    # Каждый поток получает согласованный результат своей сборки.
    assert "React UI Guidelines" in react_res.content
    assert "FastAPI Backend" not in react_res.content
    assert react_res.manifest["matched_technologies"] == ["react"]
    assert react_res.slots_filled == ["ui"]

    assert "FastAPI Backend" in fastapi_res.content
    assert "React UI Guidelines" not in fastapi_res.content
    assert fastapi_res.manifest["matched_technologies"] == ["fastapi"]
    assert fastapi_res.slots_filled == ["backend"]

    # Коллекции каждой сборки независимы.
    assert react_res.effective_slots is not fastapi_res.effective_slots
    assert react_res.resources is not fastapi_res.resources
