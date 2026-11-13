"""Тесты Шага 3 плана skills-correctness-and-search: метаданные секций и совпадения.

1. Полное название ресурса (спецсимволы, пробелы, кириллица) сохраняется в matched_resources.
2. Поиск по кириллическому слову, существующему только в title ресурса.
3. Многословный запрос 'fastapi auth' (OR-семантика) находит секцию 'fastapi supports auth'.
4. Trigram-запрос 'thor' находит карточку с 'authorization', но ресурс не попадает
   в matched_resources и не даёт local_match (намеренное различие алгоритмов).
5. Служебные имена ключей JSON не дают ложный local_match.
6. Похожие имена владельцев (auth/author) не смешиваются (точное сравнение owner).
7. _is_skill_dirty помечает скилл со старым payload '[resource=' и штатный sync
   переиндексирует его с новым форматом без изменения файлов на диске.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp_core.sanitizer import sanitize_fts_query
from mcp_skills.models import SkillCard
from mcp_skills.storage import (
    BLOCKS_SECTION_MARKER,
    SECTION_META_PREFIX,
    SkillsStorage,
    _section_header,
    _skill_hit_flags,
)
from mcp_skills.synchronizer import SkillsSynchronizer

_FULL_TITLE = "Мой справочник по API (v2.0) dev=fastapi"
_FULL_TITLE_BRACKETS = "Мой справочник по API (v2.0) [dev=fastapi]"

_SKILL_MD = f"""---
name: demo-skill
title: Demo Skill
description: Plain demo skill about unrelated things
category: planning
available_slots: [integration]
---
# Demo Skill

Skeleton body about weather and clouds.

<!-- SLOT: integration -->

[{_FULL_TITLE}](../../references/api-glossary.md)
"""


def _make_catalog(tmp_path: Path) -> tuple[SkillsStorage, SkillsSynchronizer, Path]:
    """Каталог: скилл + локальный блок + связанный ресурс-справочник."""
    catalog = tmp_path / "catalog"
    skills_dir = catalog / "skills"
    skill_dir = skills_dir / "demo-skill"
    (skill_dir / "blocks" / "integration").mkdir(parents=True)
    references = catalog / "references"
    references.mkdir(parents=True)

    (skill_dir / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    # Локальный блок: 'fastapi supports auth' существует только здесь.
    (skill_dir / "blocks" / "integration" / "default.md").write_text(
        "Local block mentions fastapi supports auth patterns.", encoding="utf-8"
    )
    # Ресурс-справочник: кириллическое слово — только в title ссылки, не в теле.
    (references / "api-glossary.md").write_text(
        "# API Glossary\n\nDefinitions of common terms zzz-rare-token-999.",
        encoding="utf-8",
    )

    storage = SkillsStorage(tmp_path / "skills.db")
    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()
    return storage, sync, catalog


def _first(storage: SkillsStorage, query: str, name: str) -> SkillCard | None:
    res = storage.search_skills(query)
    if isinstance(res, tuple):
        res = res[0]
    return next((c for c in res if c.name == name), None)


def _get_row(storage: SkillsStorage, name: str) -> Any:
    with storage.get_connection() as conn:
        return conn.execute(
            "SELECT rowid, name, title, description, stack, content, metadata "
            "FROM skills_index WHERE name = ?",
            (name,),
        ).fetchone()


def _hit_flags(storage: SkillsStorage, name: str, query: str) -> dict[str, bool]:
    """Вычисляет флаги совпадения (включая local_match) в живом соединении."""
    with storage.get_connection() as conn:
        row = conn.execute(
            "SELECT rowid, name, title, description, stack, content, metadata "
            "FROM skills_index WHERE name = ?",
            (name,),
        ).fetchone()
        assert row is not None
        return _skill_hit_flags(conn, row, query, sanitize_fts_query(query), name)


# ---------------------------------------------------------------------------
# 1-2. Полный title со спецсимволами и поиск по слову только из title
# ---------------------------------------------------------------------------


def test_full_title_with_specials_preserved_in_matched_resources(tmp_path: Path) -> None:
    storage, _sync, _catalog = _make_catalog(tmp_path)

    card = _first(storage, "glossary", "demo-skill")
    assert card is not None, "карточка не найдена по 'glossary'"
    assert card.matched_resources, "matched_resources пуст"
    res = next(m for m in card.matched_resources if "api-glossary" in m["resource_id"])
    assert res["title"] == _FULL_TITLE
    assert res["owner_skill"] == "demo-skill"


def test_full_title_with_brackets_roundtrips_through_json(tmp_path: Path) -> None:
    """Title с квадратными скобками: JSON-экранирование сохраняет его целиком.

    Markdown-ссылка с ']' в тексте не парсится существующим regex ресурсов —
    поэтому полный title с '[dev=fastapi]' проверяется на уровне payload/индекса.
    """
    storage = SkillsStorage(tmp_path / "skills.db")
    card = SkillCard(
        name="bracket-skill",
        title="Bracket Skill",
        description="Skill about brackets",
        category="general",
    )
    payload = (
        "# Bracket Skill\n\nBody about brackets.\n\n"
        + BLOCKS_SECTION_MARKER
        + "\n"
        + _section_header(
            {
                "type": "resource",
                "resource_id": "references/guide.md",
                "title": _FULL_TITLE_BRACKETS,
                "owner": "bracket-skill",
            }
        )
        + "\nglossary definitions here"
    )
    storage.index_skill(card, content=payload)

    found = _first(storage, "glossary", "bracket-skill")
    assert found is not None, "карточка не найдена по 'glossary'"
    res = next(m for m in found.matched_resources if "guide.md" in m["resource_id"])
    assert res["title"] == _FULL_TITLE_BRACKETS
    assert res["owner_skill"] == "bracket-skill"


def test_cyrillic_word_only_in_resource_title(tmp_path: Path) -> None:
    storage, _sync, _catalog = _make_catalog(tmp_path)

    # 'справочник' есть только в title ресурса (в теле — API Glossary на латинице).
    card = _first(storage, "справочник", "demo-skill")
    assert card is not None, "карточка не найдена по 'справочник'"
    matched_ids = [m["resource_id"] for m in card.matched_resources]
    assert any("api-glossary" in rid for rid in matched_ids), matched_ids
    res = next(m for m in card.matched_resources if "api-glossary" in m["resource_id"])
    assert res["title"] == _FULL_TITLE


def test_no_false_section_split_on_resource_marker_in_body(tmp_path: Path) -> None:
    """Вхождение '[resource=' в Markdown-теле секции не создаёт ложный сплит."""
    storage = SkillsStorage(tmp_path / "skills.db")
    card = SkillCard(name="split-skill", title="Split Skill", description="d", category="general")
    payload = (
        "# Split\n\nbody.\n\n"
        + BLOCKS_SECTION_MARKER
        + "\n"
        + _section_header(
            {"type": "resource", "resource_id": "references/one.md", "title": "One", "owner": "s"}
        )
        + "\nfirst section mentions marker token.\n\n"
        + "Inline example [resource=references/evil.md title=x] stays in first section.\n\n"
        + _section_header(
            {"type": "resource", "resource_id": "references/two.md", "title": "Two", "owner": "s"}
        )
        + "\nsecond section other words."
    )
    storage.index_skill(card, content=payload)

    found = _first(storage, "marker", "split-skill")
    assert found is not None
    # Совпала ровно первая секция; 'evil' не стал отдельной секцией.
    matched_ids = [m["resource_id"] for m in found.matched_resources]
    assert "references/one.md" in matched_ids
    assert "references/evil.md" not in matched_ids
    assert "references/two.md" not in matched_ids


# ---------------------------------------------------------------------------
# 3. OR-семантика многословного запроса
# ---------------------------------------------------------------------------


def test_multiterm_or_semantics_finds_local_block(tmp_path: Path) -> None:
    """'fastapi auth' находит секцию 'fastapi supports auth' (OR: одного терма достаточно)."""
    storage, _sync, _catalog = _make_catalog(tmp_path)

    res = storage.search_skills("fastapi auth")
    if isinstance(res, tuple):
        res = res[0]
    assert any(c.name == "demo-skill" for c in res), [c.name for c in res]
    card = _first(storage, "fastapi auth", "demo-skill")
    assert card is not None
    # Локальный блок (origin=local:default.md) не экспортируется в
    # matched_resources; ресурс с 'fastapi' в title матчится по OR-семантике.
    matched_ids = [m["resource_id"] for m in card.matched_resources]
    assert "local:default.md" not in matched_ids and "default.md" not in matched_ids
    assert any("api-glossary" in rid for rid in matched_ids), matched_ids
    # local_match за совпавшую локальную секцию 'fastapi supports auth' начисляется.
    flags = _hit_flags(storage, "demo-skill", "fastapi auth")
    assert flags["local_match"] is True


def test_multiterm_or_semantics_finds_resource_body(tmp_path: Path) -> None:
    """'glossary missingword' — один терм из тела ресурса, второго нет: OR находит."""
    storage, _sync, _catalog = _make_catalog(tmp_path)

    card = _first(storage, "glossary missingword", "demo-skill")
    assert card is not None, "карточка не найдена по 'glossary missingword'"
    matched_ids = [m["resource_id"] for m in card.matched_resources]
    assert any("api-glossary" in rid for rid in matched_ids), matched_ids


# ---------------------------------------------------------------------------
# 4. Trigram 'thor' vs 'authorization': карточка найдена, ресурс — нет
# ---------------------------------------------------------------------------

_THOR_SKILL_MD = """---
name: thor-skill
title: Thor Skill
description: Skill about authorization workflows
category: planning
---
# Thor Skill

Body mentions security workflows.

[Authz guide](../../references/authz.md)
"""


def _make_thor_catalog(tmp_path: Path) -> tuple[SkillsStorage, SkillsSynchronizer, Path]:
    catalog = tmp_path / "catalog"
    skills_dir = catalog / "skills"
    skill_dir = skills_dir / "thor-skill"
    references = catalog / "references"
    skill_dir.mkdir(parents=True)
    references.mkdir(parents=True)

    (skill_dir / "SKILL.md").write_text(_THOR_SKILL_MD, encoding="utf-8")
    # Ресурс: только слово 'authorization', никакого 'thor'.
    (references / "authz.md").write_text("# Rules\n\nauthorization tokens guide.", encoding="utf-8")

    storage = SkillsStorage(tmp_path / "skills.db")
    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()
    return storage, sync, catalog


def test_trigram_thor_finds_card_but_not_resource(tmp_path: Path) -> None:
    storage, _sync, _catalog = _make_thor_catalog(tmp_path)

    # Trigram-поиск карточек находит 'authorization' по 'thor' (подстрока триграмм)...
    res = storage.search_skills("thor")
    if isinstance(res, tuple):
        res = res[0]
    names = [c.name for c in res]
    assert "thor-skill" in names, f"trigram не нашёл карточку с 'authorization': {names}"

    card = _first(storage, "thor", "thor-skill")
    assert card is not None
    # ...но 'thor' не совпадает с началом никакого токена секции ('authorization',
    # 'tokens', 'guide', 'authz') -> ресурс НЕ в matched_resources...
    matched_ids = [m["resource_id"] for m in card.matched_resources]
    assert not any("authz" in rid for rid in matched_ids), matched_ids
    # ...и не даёт local_match (намеренное различие алгоритмов Шага 3).
    flags = _hit_flags(storage, "thor-skill", "thor")
    assert flags["local_match"] is False


# ---------------------------------------------------------------------------
# 5. Служебные ключи JSON не дают ложный local_match / matched
# ---------------------------------------------------------------------------


def test_service_json_keys_do_not_falsely_match(tmp_path: Path) -> None:
    storage, _sync, _catalog = _make_catalog(tmp_path)

    # FTS находит карточку: имена ключей есть в тексте JSON-заголовков content.
    # Но значащие поля секций ключей не содержат -> matched_resources пуст
    # и local_match не начисляется.
    for query in ("owner", "resource_id", "origin"):
        res = storage.search_skills(query)
        if isinstance(res, tuple):
            res = res[0]
        for c in res:
            assert c.matched_resources == [], (
                f"ложные matched по ключу '{query}': {c.matched_resources}"
            )
    flags = _hit_flags(storage, "demo-skill", "owner")
    assert flags["local_match"] is False


# ---------------------------------------------------------------------------
# 6. Похожие имена владельцев не смешиваются (точное сравнение owner)
# ---------------------------------------------------------------------------


def test_similar_owner_names_do_not_collide(tmp_path: Path) -> None:
    """owner='author' не даёт local_match скиллу 'auth' (подстрока 'owner=auth')."""
    storage = SkillsStorage(tmp_path / "skills.db")

    for name in ("auth", "author"):
        card = SkillCard(name=name, title=f"{name} skill", description="d", category="general")
        payload = (
            f"# {name}\n\nbody about widget handling.\n\n"
            + BLOCKS_SECTION_MARKER
            + "\n"
            + _section_header(
                {
                    "type": "resource",
                    "resource_id": f"references/{name}-guide.md",
                    "title": f"{name} Guide",
                    "owner": "author",
                }
            )
            + "\nwidget rules"
        )
        storage.index_skill(card, content=payload)

    # Секция совпала по 'widget', owner='author'. Для скилла 'auth' локальность
    # НЕ выполняется (точное 'author' != 'auth'); для 'author' — выполняется.
    flags_auth = _hit_flags(storage, "auth", "widget")
    assert flags_auth["local_match"] is False, "owner='author' ложно признан локальным для 'auth'"

    flags_author = _hit_flags(storage, "author", "widget")
    assert flags_author["local_match"] is True

    # Точный owner в ответе не урезается и не смешивается.
    card = _first(storage, "widget", "auth")
    assert card is not None
    matched = [m for m in card.matched_resources if "auth-guide" in m["resource_id"]]
    assert matched, card.matched_resources
    assert matched[0]["owner_skill"] == "author"
    assert matched[0]["title"] == "auth Guide"


# ---------------------------------------------------------------------------
# 7. Переиндексация старого формата через _is_skill_dirty
# ---------------------------------------------------------------------------


def _rewrite_payload_legacy(storage: SkillsStorage) -> None:
    """Симулирует старый (D1) payload секций в колонке content."""
    legacy = (
        "# Demo Skill\n\nSkeleton body about weather and clouds.\n\n"
        + BLOCKS_SECTION_MARKER
        + "\n[resource=references/api-glossary.md title="
        + _FULL_TITLE
        + " owner=demo-skill]\n"
        + "# API Glossary\n\nDefinitions of common terms zzz-rare-token-999.\n\n"
        + "[slot=integration variant=default origin=local:default.md]\n"
        + "Local block mentions fastapi supports auth patterns.\n"
    )
    with storage.get_connection() as conn:
        conn.execute("UPDATE skills_index SET content = ? WHERE name = 'demo-skill'", (legacy,))
        conn.commit()


def test_legacy_payload_forces_reindex_without_disk_changes(tmp_path: Path) -> None:
    storage, sync, catalog = _make_catalog(tmp_path)
    files_before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in catalog.rglob("*.md")}

    _rewrite_payload_legacy(storage)

    # Старый payload -> _is_skill_dirty обязан вернуть True.
    skill_dir = catalog / "skills" / "demo-skill"
    card_files = sync._collect_skill_files(skill_dir, catalog_root=catalog)
    assert sync._is_skill_dirty("demo-skill", card_files, skill_dir) is True

    # Штатный sync переиндексирует (файлы на диске не менялись).
    result = sync.sync()
    assert result.indexed_count == 1, result

    # Файлы на диске не изменились.
    files_after = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in catalog.rglob("*.md")}
    assert files_before == files_after

    # В content теперь новый формат: SECTION_META_PREFIX присутствует,
    # старых маркеров в блочной секции нет.
    with storage.get_connection() as conn:
        row = conn.execute("SELECT content FROM skills_index WHERE name = 'demo-skill'").fetchone()
    payload_part = row["content"].split(BLOCKS_SECTION_MARKER, 1)[-1]
    assert SECTION_META_PREFIX in payload_part
    assert "[resource=" not in payload_part
    assert "[slot=" not in payload_part

    # Повторный sync без изменений на диске — не переиндексирует (миграция
    # одноразовая, не повторяется на каждом sync/поиске).
    result2 = sync.sync()
    assert result2.indexed_count == 0, result2

    # Поиск по переиндексированной БД возвращает полное название ресурса.
    card = _first(storage, "glossary", "demo-skill")
    assert card is not None
    res = next(m for m in card.matched_resources if "api-glossary" in m["resource_id"])
    assert res["title"] == _FULL_TITLE


# ---------------------------------------------------------------------------
# Дополнительно: local_match через origin='local:'
# ---------------------------------------------------------------------------


def test_local_match_via_origin_local_prefix(tmp_path: Path) -> None:
    """origin='local:...' (даже чужой owner) даёт local_match при совпадении секции."""
    storage = SkillsStorage(tmp_path / "skills.db")
    card = SkillCard(name="local-skill", title="Local Skill", description="d", category="general")
    payload = (
        "# Local\n\nbody.\n\n"
        + BLOCKS_SECTION_MARKER
        + "\n"
        + _section_header(
            {"type": "slot", "slot": "core", "variant": "default", "origin": "local:default.md"}
        )
        + "\nwidget rules for local slot"
    )
    storage.index_skill(card, content=payload)

    flags = _hit_flags(storage, "local-skill", "widget")
    assert flags["local_match"] is True

    # Несовпавший запрос не даёт local_match даже для локальной секции.
    flags2 = _hit_flags(storage, "local-skill", "missing")
    assert flags2["local_match"] is False


# ---------------------------------------------------------------------------
# Русские словоформы: карточка и matched_resources согласованы
# ---------------------------------------------------------------------------


def test_russian_wordforms_consistent_card_and_resources(tmp_path: Path) -> None:
    """«глоссарий»/«глоссария» дают согласованный результат (стемминг через stem_russian)."""
    storage = SkillsStorage(tmp_path / "skills.db")
    card = SkillCard(name="ru-skill", title="Ru Skill", description="d", category="general")
    payload = (
        "# Ru\n\nbody.\n\n"
        + BLOCKS_SECTION_MARKER
        + "\n"
        + _section_header(
            {
                "type": "resource",
                "resource_id": "references/ru-guide.md",
                "title": "Глоссарий терминов",
                "owner": "ru-skill",
            }
        )
        + "\nтребования к глоссарию проекта"
    )
    storage.index_skill(card, content=payload)

    # stem_russian('глоссария') -> 'глоссар': совпадает с токеном 'глоссарий'/'глоссарию'.
    for query in ("глоссарий", "глоссария", "глоссарию"):
        found = _first(storage, query, "ru-skill")
        assert found is not None, f"карточка не найдена по '{query}'"
        matched_ids = [m["resource_id"] for m in found.matched_resources]
        assert any("ru-guide" in rid for rid in matched_ids), (query, matched_ids)


def test_skill_without_blocks_not_perpetually_dirty(tmp_path: Path) -> None:
    """S3-1: Скилл без blocks/ секции, но с текстом '[resource=' в теле, не должен вечно переиндексироваться."""
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    skill_dir = skills_root / "doc-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: doc-skill\ntitle: Doc Skill\ncategory: general\n---\n"
        "# Doc\n\nПример строки формата [resource=abc title=def] в документации.\n",
        encoding="utf-8",
    )

    storage = SkillsStorage(tmp_path / "skills.db")
    sync = SkillsSynchronizer(storage, [skills_root])

    # 1-й sync: индексирует 1 новый файл.
    res1 = sync.sync()
    assert res1.indexed_count == 1

    # 2-й sync: файлы на диске не менялись — должно быть 0 переиндексаций (не dirty).
    res2 = sync.sync()
    assert res2.indexed_count == 0
