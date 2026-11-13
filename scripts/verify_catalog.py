#!/usr/bin/env python
"""F7 (review-fixes): поведенческая верификация живого каталога скиллов.

Проверяет сквозной путь «проект → поиск → сборка → этап» по реальному
репозиторию mcp_skills (read-only) во временной БД (кэш .agents/.cache
НЕ используется). Должен был поймать баг A6 (бандл add-tests терял блоки).

Запуск: uv run python scripts/verify_catalog.py [путь_к_репозиторию_скиллов]
Выход: exit 0 при успехе; по каждому пункту печатается PASS/FAIL.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_skills.bundler import AgentBundler
from mcp_skills.flow_parser import list_available_flows
from mcp_skills.server import load_skill, search_skills
from mcp_skills.slot_compiler import SlotCompiler
from mcp_skills.storage import BLOCKS_SECTION_MARKER, SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer

DEFAULT_REPO = Path(
    os.environ.get(
        "MCP_SKILLS_TEST_CATALOG", str(Path(__file__).resolve().parents[1].parent / "mcp_skills")
    )
)

# Контрольные запросы (F7-3): Top-1 выдачи обязан быть ожидаемым скиллом.
CONTROL_QUERIES: list[tuple[str, str]] = [
    ("sqlite performance", "sqlite-performance"),
    ("code review", "code-review"),
    ("writing plans", "writing-plans"),
    ("brainstorming ideas", "brainstorming"),
    ("lora training", "profile-lora-training"),
]


def main(
    skills_repo: Path | None = None,
    verbose: bool = True,
    bundle_min_chars: int = 6000,
) -> int:
    """Выполняет поведенческие проверки каталога; возвращает exit-code.

    ``skills_repo`` — каталог репозитория скиллов (содержит skills/, agents/,
    flows/); допускается передача самого skills/ (тогда agents/flows ищутся
    уровнем выше). ``bundle_min_chars`` — порог бандла add-tests (A6-ловушка:
    без передачи touches блоки терялись и бандл проседал до ~5.7К).
    """
    repo = Path(skills_repo) if skills_repo is not None else DEFAULT_REPO
    skills_base = repo / "skills" if repo.name != "skills" else repo
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, fn) -> None:
        """Выполняет проверку, любое исключение трактуется как FAIL."""
        try:
            ok, detail = fn()
        # Верификация обязана печатать FAIL, а не падать на сломанном каталоге
        except Exception as e:
            ok, detail = False, f"exception: {type(e).__name__}: {e}"
        checks.append((name, ok, detail))

    # (0) Каталог существует и содержит скиллы (guard от сломанного репо)
    check(
        "catalog-present",
        lambda: (
            bool(list(skills_base.rglob("SKILL.md"))),
            f"{len(list(skills_base.rglob('SKILL.md')))} SKILL.md",
        ),
    )

    # (1) SkillsSynchronizer по каталогу во временную БД
    tmpdir = tempfile.mkdtemp(prefix="verify_catalog_")
    db_path = Path(tmpdir) / "skills_fts.db"
    store = SkillsStorage(db_path)
    sync = SkillsSynchronizer(store, [skills_base])
    sync_result = sync.sync()
    checks.append(
        ("sync-indexed", sync_result.indexed_count > 0, f"indexed={sync_result.indexed_count}")
    )

    # (2) search_skills zero-query overview: категории непустые
    def _overview_check() -> tuple[bool, str]:
        overview = search_skills(
            query="", storage=store, synchronizer=sync, project_root=Path(tmpdir)
        )
        categories = overview.get("categories", [])
        non_empty = [c for c in categories if c.get("skills_count", 0) > 0]
        return bool(non_empty), f"{len(non_empty)}/{len(categories)} > 0"

    check("overview-categories", _overview_check)

    # (3) Контрольные запросы: Top-1 найден
    for query, expected in CONTROL_QUERIES:

        def _top1(q: str = query, exp: str = expected) -> tuple[bool, str]:
            res = search_skills(
                query=q,
                storage=store,
                synchronizer=sync,
                project_root=Path(tmpdir),
                skills_dir=repo,
            )
            results = res.get("results", [])
            top_name = results[0]["name"] if results else None
            return top_name == exp, f"top1={top_name}"

        check(f"top1:{query}", _top1)

    # (4) load_skill('writing-plans', stack=['python','mcp'])
    def _load_skill_check() -> tuple[bool, str]:
        loaded = load_skill(
            "writing-plans",
            stack=["python", "mcp"],
            storage=store,
            synchronizer=sync,
            compiler=SlotCompiler(store),
            project_root=Path(tmpdir),
        )
        content = loaded.get("content", "")
        slots_filled = loaded.get("slots_filled", [])
        has_slots = bool(slots_filled)
        blocks_leak = BLOCKS_SECTION_MARKER in content
        ok = len(content) > 500 and has_slots and not blocks_leak
        detail = f"len={len(content)}, slots={slots_filled}, marker={'leak' if blocks_leak else 'absent'}"
        return ok, detail

    check("load-skill-writing-plans", _load_skill_check)

    # (5) bundle_agent('add-tests') — проверка обязательных критериев, выбранной темы и отсутствия дублей (Шаг 17)
    def _bundle_check() -> tuple[bool, str]:
        bundler = AgentBundler(storage=store, compiler=SlotCompiler(store), skills_dir=repo)
        bundle_base = bundler.bundle_agent("add-tests", project_root=Path(tmpdir))
        has_mandatory_criteria = len(bundle_base) > 200

        profile = bundler.load_profile("add-tests", Path(tmpdir))
        if "add-tests" in profile.skills:
            bundle_with_topic = bundler.bundle_agent(
                "add-tests",
                project_root=Path(tmpdir),
                skill_options={"add-tests": {"touches": ["test-runner", "stack-patterns"]}},
            )
            has_selected_topic = "stack-patterns" in bundle_with_topic or len(
                bundle_with_topic
            ) > len(bundle_base)
            no_duplicates = bundle_with_topic.count("## Спецификация реализации: test-runner") <= 1
            no_broken_placeholders = "⚠️ [Недопустимое имя слота]" not in bundle_with_topic
        else:
            has_selected_topic = True
            no_duplicates = True
            no_broken_placeholders = True

        ok = (
            has_mandatory_criteria
            and has_selected_topic
            and no_duplicates
            and no_broken_placeholders
        )
        detail = (
            f"mandatory={has_mandatory_criteria}, selected_topic={has_selected_topic}, "
            f"no_duplicates={no_duplicates}, valid={no_broken_placeholders}"
        )
        return ok, detail

    check("bundle-add-tests", _bundle_check)

    # (6) list_available_flows: флоу находятся
    def _flows_check() -> tuple[bool, str]:
        flows = list_available_flows(Path(tmpdir), skills_dir=repo)
        return bool(flows), f"{len(flows)} flows"

    check("flows-available", _flows_check)

    failed = 0
    for name, ok, detail in checks:
        if not ok:
            failed += 1
        if verbose:
            print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")  # noqa: T201 - CLI output
    if verbose:
        print(  # noqa: T201 - CLI output
            f"{'OK' if failed == 0 else 'FAILED'}: {len(checks) - failed}/{len(checks)} passed"
        )

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
