"""Тесты для слотового компилятора скиллов."""

from pathlib import Path
import pytest

from mcp_skills.errors import CircularExtensionError, SkillNotFoundError
from mcp_skills.slot_compiler import SlotCompiler
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer


def test_slot_compiler_basic_assembly(tmp_path: Path) -> None:
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    # Создаем базовый скилл со слотами и блоками
    skill_dir = skills_dir / "writing-plans"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: writing-plans
title: Writing Plans
category: planning
available_slots: [ui, backend]
---
# Writing Plans Header

<!-- SLOT: ui -->

<!-- SLOT: backend -->

## Verification
Follow checklist.
""",
        encoding="utf-8",
    )

    # Создаем блоки
    blocks_dir = skill_dir / "blocks"
    (blocks_dir / "ui").mkdir(parents=True)
    (blocks_dir / "backend").mkdir(parents=True)

    (blocks_dir / "ui" / "react.md").write_text(
        "### React UI Guidelines\nUse React 18 hooks.", encoding="utf-8"
    )
    (blocks_dir / "backend" / "fastapi.md").write_text(
        "### FastAPI Backend\nUse Pydantic v2.", encoding="utf-8"
    )

    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()

    compiler = SlotCompiler(storage)

    # Ассемблируем под React и FastAPI
    card, compiled_md = compiler.assemble_skill(
        name="writing-plans",
        touches=["ui", "backend"],
        project_stack=["react", "fastapi"],
    )

    assert "React UI Guidelines" in compiled_md
    assert "FastAPI Backend" in compiled_md
    assert "Follow checklist" in compiled_md
    assert "<!-- SLOT:" not in compiled_md


def test_slot_compiler_backreferences_in_content(tmp_path: Path) -> None:
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    skill_dir = skills_dir / "windows-paths"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: windows-paths
title: Windows Paths
category: planning
available_slots: [config]
---
# Config Header
<!-- SLOT: config -->
""",
        encoding="utf-8",
    )

    blocks_dir = skill_dir / "blocks" / "config"
    blocks_dir.mkdir(parents=True)
    # Контент содержит \1, \g<name> и Windows-пути C:\Users\test
    (blocks_dir / "default.md").write_text(
        r"Path is C:\Users\app\1 and regex is \1 and \g<foo>", encoding="utf-8"
    )

    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()

    compiler = SlotCompiler(storage)
    _, compiled_md = compiler.assemble_skill(
        name="windows-paths",
        touches=["config"],
    )
    assert r"C:\Users\app\1" in compiled_md
    assert r"\1 and \g<foo>" in compiled_md


def test_slot_compiler_missing_block_fallback(tmp_path: Path) -> None:
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    skill_dir = skills_dir / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: test-skill
title: Test
category: general
available_slots: [database]
---
# Header
<!-- SLOT: database -->
""",
        encoding="utf-8",
    )

    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()

    compiler = SlotCompiler(storage)
    _, compiled_md = compiler.assemble_skill(
        name="test-skill",
        touches=["database"],
        project_stack=["postgres"],
    )

    # Warning-плейсхолдер фиксированный, без tech-hint: raw tech из стека
    # не подставляется в собранный контент (санитизация).
    assert "⚠️ [Спецификация отсутствует]" in compiled_md


def test_slot_compiler_project_overlay(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import mcp_skills.slot_compiler as sc_module

    sc_module._WARNED_LEGACY_OVERLAY = False  # сброс разового флага для теста

    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / "global_skills"
    skills_dir.mkdir()

    skill_dir = skills_dir / "base-rule"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: base-rule
title: Base Rule
category: quality-review
merge_strategy: append
---
# Base Rule Content
""",
        encoding="utf-8",
    )

    # Создаем проектный оверлей (legacy)
    project_root = tmp_path / "project"
    project_overlay_dir = project_root / ".agents" / "skills"
    project_overlay_dir.mkdir(parents=True)
    (project_overlay_dir / "base-rule.overlay.md").write_text(
        "## Project Specific Overlay\nStrict rules applied.",
        encoding="utf-8",
    )

    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()

    compiler = SlotCompiler(storage)
    with caplog.at_level("WARNING"):
        _, compiled_md = compiler.assemble_skill(
            name="base-rule",
            project_root=project_root,
        )

    assert "# Base Rule Content" in compiled_md
    assert "Project Specific Overlay" not in compiled_md
    assert "устаревшие оверлеи" in caplog.text


def test_slot_compiler_circular_extension(tmp_path: Path) -> None:
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    (skills_dir / "skill-a").mkdir()
    (skills_dir / "skill-a" / "SKILL.md").write_text(
        """---
name: skill-a
title: Skill A
category: general
extends: skill-b
---
# A
""",
        encoding="utf-8",
    )

    (skills_dir / "skill-b").mkdir()
    (skills_dir / "skill-b" / "SKILL.md").write_text(
        """---
name: skill-b
title: Skill B
category: general
extends: skill-a
---
# B
""",
        encoding="utf-8",
    )

    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()

    compiler = SlotCompiler(storage)
    with pytest.raises(CircularExtensionError):
        compiler.assemble_skill("skill-a")


def test_slot_compiler_max_depth_exceeded(tmp_path: Path) -> None:
    from mcp_skills.errors import MaxExtensionDepthError

    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    # Создаём цепочку наследования длины 7 (> MAX_EXTENSION_DEPTH = 5)
    for i in range(7):
        s_dir = skills_dir / f"skill-depth-{i}"
        s_dir.mkdir()
        extends_clause = f"extends: skill-depth-{i + 1}" if i < 6 else ""
        (s_dir / "SKILL.md").write_text(
            f"""---
name: skill-depth-{i}
title: Depth {i}
category: general
{extends_clause}
---
# Content {i}
""",
            encoding="utf-8",
        )

    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()

    compiler = SlotCompiler(storage)
    with pytest.raises(MaxExtensionDepthError):
        compiler.assemble_skill("skill-depth-0")


def test_slot_compiler_not_found(tmp_path: Path) -> None:
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    compiler = SlotCompiler(storage)
    with pytest.raises(SkillNotFoundError):
        compiler.assemble_skill("non-existent-skill")


def test_slot_compiler_path_traversal_protection(tmp_path: Path) -> None:
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    # Секретный файл вне директории блоков
    secret_file = tmp_path / "secret.md"
    secret_file.write_text("SUPER_SECRET_KEY=12345", encoding="utf-8")

    skill_dir = skills_dir / "safe-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: safe-skill
title: Safe Skill
category: general
---
# Safe Skill Body
""",
        encoding="utf-8",
    )

    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()

    compiler = SlotCompiler(storage)

    # Попытка path traversal через touches отвергается с ValueError
    with pytest.raises(ValueError, match="Неизвестные секции"):
        compiler.assemble_skill(
            name="safe-skill",
            touches=["../../secret", "../secret.md", "/etc/passwd"],
        )


def test_slot_compiler_merge_strategies(tmp_path: Path) -> None:
    """F3: Проверка merge_strategy (append, prepend, override, empty body) при extends."""
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    # Базовый родительский скилл
    parent_dir = skills_dir / "base-skill"
    parent_dir.mkdir()
    (parent_dir / "SKILL.md").write_text(
        """---
name: base-skill
title: Base Skill
category: general
---
# Base Body
Parent content.
""",
        encoding="utf-8",
    )

    # Потомок 1: append (дефолт)
    child_append = skills_dir / "child-append"
    child_append.mkdir()
    (child_append / "SKILL.md").write_text(
        """---
name: child-append
title: Child Append
category: general
extends: base-skill
merge_strategy: append
---
# Child Body
Appended content.
""",
        encoding="utf-8",
    )

    # Потомок 2: prepend
    child_prepend = skills_dir / "child-prepend"
    child_prepend.mkdir()
    (child_prepend / "SKILL.md").write_text(
        """---
name: child-prepend
title: Child Prepend
category: general
extends: base-skill
merge_strategy: prepend
---
# Child Body
Prepended content.
""",
        encoding="utf-8",
    )

    # Потомок 3: override
    child_override = skills_dir / "child-override"
    child_override.mkdir()
    (child_override / "SKILL.md").write_text(
        """---
name: child-override
title: Child Override
category: general
extends: base-skill
merge_strategy: override
---
# Override Body
Overridden content only.
""",
        encoding="utf-8",
    )

    # Потомок 4: пустое тело (fallback на родителя)
    child_empty = skills_dir / "child-empty"
    child_empty.mkdir()
    (child_empty / "SKILL.md").write_text(
        """---
name: child-empty
title: Child Empty
category: general
extends: base-skill
merge_strategy: append
---

""",
        encoding="utf-8",
    )

    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()

    compiler = SlotCompiler(storage)

    # 1. append
    _, md_append = compiler.assemble_skill("child-append")
    assert md_append.startswith("# Base Body")
    assert md_append.endswith("Appended content.")
    assert "Parent content.\n\n# Child Body" in md_append

    # 2. prepend
    _, md_prepend = compiler.assemble_skill("child-prepend")
    assert md_prepend.startswith("# Child Body")
    assert md_prepend.endswith("Parent content.")
    assert "Prepended content.\n\n# Base Body" in md_prepend

    # 3. override
    _, md_override = compiler.assemble_skill("child-override")
    assert "Base Body" not in md_override
    assert "Parent content" not in md_override
    assert md_override == "# Override Body\nOverridden content only."

    # 4. empty body
    _, md_empty = compiler.assemble_skill("child-empty")
    assert md_empty == "# Base Body\nParent content."


def test_slot_plus_multistack_concatenation(tmp_path: Path) -> None:
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    skill_dir = skills_dir / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: my-skill
title: My Skill
category: planning
available_slots: [invariants]
---
# Invariants Header

<!-- SLOT+: invariants -->

Footer.
""",
        encoding="utf-8",
    )

    blocks_dir = skill_dir / "blocks" / "invariants"
    blocks_dir.mkdir(parents=True)
    (blocks_dir / "sqlite.md").write_text("### SQLite Invariant\nUse WAL.", encoding="utf-8")
    (blocks_dir / "react.md").write_text("### React Invariant\nZero CLS.", encoding="utf-8")
    (blocks_dir / "default.md").write_text("### Default Invariant\nGeneric.", encoding="utf-8")

    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()

    compiler = SlotCompiler(storage)

    # 1. Проверяем склейку двух технологий в порядке передачи
    _, md = compiler.assemble_skill(
        name="my-skill",
        touches=["invariants"],
        project_stack=["sqlite", "react"],
    )
    assert "### SQLite Invariant\nUse WAL." in md
    assert "### React Invariant\nZero CLS." in md
    assert "### Default Invariant" not in md
    assert "<!-- SLOT" not in md
    assert md.index("SQLite Invariant") < md.index("React Invariant")


def test_slot_radio_single_match_intact(tmp_path: Path) -> None:
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    skill_dir = skills_dir / "radio-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: radio-skill
title: Radio Skill
category: planning
available_slots: [mode]
---
# Header

<!-- SLOT: mode -->

Footer.
""",
        encoding="utf-8",
    )

    blocks_dir = skill_dir / "blocks" / "mode"
    blocks_dir.mkdir(parents=True)
    (blocks_dir / "standard.md").write_text("Standard Mode", encoding="utf-8")
    (blocks_dir / "deep.md").write_text("Deep Mode", encoding="utf-8")

    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()

    compiler = SlotCompiler(storage)

    # При передаче нескольких стеков обычный SLOT берет ТОЛЬКО первое совпадение
    _, md = compiler.assemble_skill(
        name="radio-skill",
        touches=["mode"],
        project_stack=["deep", "standard"],
    )
    assert "Deep Mode" in md
    assert "Standard Mode" not in md


def test_shared_blocks_resolution_chain(tmp_path: Path) -> None:
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    catalog_root = tmp_path / "catalog"
    skills_dir = catalog_root / "skills"
    skills_dir.mkdir(parents=True)

    # Общие блоки каталога
    shared_blocks = catalog_root / "blocks" / "stack-invariants"
    shared_blocks.mkdir(parents=True)
    (shared_blocks / "postgres.md").write_text(
        "### Shared Postgres\nConnection pool.", encoding="utf-8"
    )

    skill_dir = skills_dir / "plan-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: plan-skill
title: Plan Skill
category: planning
available_slots: [stack-invariants]
---
# Plan

<!-- SLOT+: stack-invariants -->
""",
        encoding="utf-8",
    )

    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()

    compiler = SlotCompiler(storage, catalog_root=catalog_root)

    _, md = compiler.assemble_skill(
        name="plan-skill",
        touches=["stack-invariants"],
        project_stack=["postgres"],
    )
    assert "### Shared Postgres\nConnection pool." in md


def test_multistack_deduplication(tmp_path: Path) -> None:
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    skill_dir = skills_dir / "dedup-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: dedup-skill
title: Dedup Skill
category: planning
available_slots: [invariants]
---
<!-- SLOT+: invariants -->
""",
        encoding="utf-8",
    )

    blocks_dir = skill_dir / "blocks" / "invariants"
    blocks_dir.mkdir(parents=True)
    (blocks_dir / "sqlite.md").write_text("### SQLite Invariant", encoding="utf-8")

    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()

    compiler = SlotCompiler(storage)

    _, md = compiler.assemble_skill(
        name="dedup-skill",
        touches=["invariants"],
        project_stack=["SQLite", "sqlite", "SQLITE"],
    )
    assert md.count("### SQLite Invariant") == 1


def test_slot_plus_partial_match_no_default(tmp_path: Path) -> None:
    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    skill_dir = skills_dir / "partial-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: partial-skill
title: Partial Skill
category: planning
available_slots: [invariants]
---
<!-- SLOT+: invariants -->
""",
        encoding="utf-8",
    )

    blocks_dir = skill_dir / "blocks" / "invariants"
    blocks_dir.mkdir(parents=True)
    (blocks_dir / "sqlite.md").write_text("### SQLite Invariant", encoding="utf-8")
    (blocks_dir / "default.md").write_text("### Default Invariant", encoding="utf-8")

    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()

    compiler = SlotCompiler(storage)

    # Если совпал sqlite, а redis нет - default.md НЕ подмешивается!
    _, md = compiler.assemble_skill(
        name="partial-skill",
        touches=["invariants"],
        project_stack=["sqlite", "redis"],
    )
    assert "### SQLite Invariant" in md
    assert "### Default Invariant" not in md


def test_autodetect_catalog_root_with_skill_blocks(tmp_path: Path) -> None:
    """Проверяет, что _detect_catalog_root корректно поднимается к корню каталога,
    даже если у скилла есть собственная директория blocks/."""
    catalog_root = tmp_path / "catalog"
    catalog_root.mkdir()
    (catalog_root / "blocks" / "stack-invariants").mkdir(parents=True)
    (catalog_root / "blocks" / "stack-invariants" / "postgres.md").write_text(
        "### Shared Postgres", encoding="utf-8"
    )

    skills_dir = catalog_root / "skills"
    skills_dir.mkdir()

    skill_dir = skills_dir / "plan-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: plan-skill
title: Plan Skill
category: planning
available_slots: [stack-invariants]
---
<!-- SLOT+: stack-invariants -->
""",
        encoding="utf-8",
    )
    # У самого скилла тоже есть blocks/
    (skill_dir / "blocks" / "other-slot").mkdir(parents=True)
    (skill_dir / "blocks" / "other-slot" / "default.md").write_text("Other", encoding="utf-8")

    db_path = tmp_path / "skills.db"
    storage = SkillsStorage(db_path)
    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()

    # Создаём компилятор БЕЗ явного catalog_root
    compiler = SlotCompiler(storage)

    _, md = compiler.assemble_skill(
        name="plan-skill",
        touches=["stack-invariants"],
        project_stack=["postgres"],
    )
    assert "### Shared Postgres" in md
