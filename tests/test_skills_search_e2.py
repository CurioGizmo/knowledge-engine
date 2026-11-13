"""Тесты E2 (review-fixes): улучшение поиска skills.

Пять дефектов из verdict_1/2/4 (разделы про ранжирование/поиск skills):
1. Флоу не попадают в результаты type='all' при поисковом запросе
   (server.py:548-553 — ветка FTS вызывается для type != "flow").
2. SQL LIMIT режет кандидатов ДО Python-реранка (storage.py:457-489)
   — релевантный документ за пределами LIMIT не может попасть в топ.
3. Fuzzy-набор не знает о project_stack: несовместимые кандидаты
   заполняют слоты выдачи и вымываются финальным фильтром,
   а совместимые (но ниже по fuzzy-скору) теряются.
4. intent_group не участвует в _score_flows (только пробрасывается
   в arbitration_options).
5. is_ambiguous (top−second ≤ 5) не различает «конкурирующие интенты»
   (разные intent_group — стоит спросить пользователя) и «варианты
   одного интента» (одинаковые intent_group — выбор внутри группы).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import pytest

from mcp_skills.models import SkillCard
from mcp_skills.server import _score_flows, search_skills
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer


def _make_storage(tmp_path: Path, cards: list[SkillCard]) -> SkillsStorage:
    """Индексирует карточки напрямую (без ФС-сканера) — герметично."""
    storage = SkillsStorage(tmp_path / "skills_fts.db")
    for card in cards:
        storage.index_skill(card, content=f"content of {card.name}")
    return storage


FLOW_MD = """---
title: Plan Drafting Flow
intent_group: plan-drafting
aliases: [составить план, планирование]
when_to_use: [нужен план реализации фичи]
---
# Plan Drafting
## 1. Draft [id: s1]
Instruction
"""

FLOW_MD_2 = """---
title: Plan Execution Flow
intent_group: plan-execution
aliases: [исполнить план, запуск плана]
when_to_use: [план готов, исполнить]
---
# Plan Execution
## 1. Execute [id: s1]
Instruction
"""


def _make_flow_project(tmp_path: Path) -> Path:
    """Каталог проекта с одним flow в .agents/flows (плюс pyproject для корня)."""
    root = tmp_path / "proj"
    (root / ".agents" / "flows").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "e2"\n', encoding="utf-8")
    return root


def _write_visibility_flow(
    root: Path,
    name: str,
    *,
    visibility: str = "public",
    entrypoint: bool = True,
    stage_text: str = "Shared review instructions",
    intent_group: str = "review",
) -> None:
    (root / ".agents" / "flows" / f"{name}.md").write_text(
        f"""---
title: Shared Review
description: Review changes
intent_group: {intent_group}
aliases: [review changes]
visibility: {visibility}
entrypoint: {str(entrypoint).lower()}
option_schema:
  depth:
    allowed: [quick, deep]
    default: quick
defaults:
  depth: quick
---
# Shared Review
## 1. Inspect [id: inspect]
{stage_text}
""",
        encoding="utf-8",
    )


def test_flow_discovery_excludes_internal_and_non_entrypoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Все маршруты discovery используют публичный фильтр до ранжирования и FTS."""
    root = _make_flow_project(tmp_path)
    _write_visibility_flow(
        root, "review-alpha", stage_text="stellar public inspection", intent_group="review-alpha"
    )
    _write_visibility_flow(root, "review-beta", intent_group="review-beta")
    _write_visibility_flow(
        root,
        "review-hidden",
        visibility="internal",
        entrypoint=False,
        stage_text="quasar secret inspection",
    )
    _write_visibility_flow(
        root, "review-helper", entrypoint=False, stage_text="quasar helper inspection"
    )
    isolated_skills = tmp_path / "isolated-skills"
    isolated_skills.mkdir()
    storage = SkillsStorage(tmp_path / "discovery.db")
    sync = SkillsSynchronizer(storage, [isolated_skills])
    sync.sync()
    from mcp_skills import server as skills_server

    indexed_names: list[set[str]] = []
    original_fts = skills_server._flow_fts_bonuses

    def capture_fts(flows: list[dict[str, Any]], query: str) -> dict[str, float]:
        indexed_names.append({flow["name"] for flow in flows})
        assert any("stellar" in flow["stage_text"] for flow in flows)
        return original_fts(flows, query)

    monkeypatch.setattr(skills_server, "_flow_fts_bonuses", capture_fts)

    def search(query: str = "", *, kind: str = "all") -> dict[str, Any]:
        return search_skills(
            query=query,
            type=kind,
            storage=storage,
            synchronizer=sync,
            project_root=root,
            skills_dir=isolated_skills,
        )

    overview = search()
    assert {"review-alpha", "review-beta"} <= {flow["name"] for flow in overview["flows"]}
    assert {"review-hidden", "review-helper"}.isdisjoint(flow["name"] for flow in overview["flows"])
    listing = search(kind="flow")
    assert {"review-alpha", "review-beta"} <= {flow["name"] for flow in listing["flows"]}
    assert {"review-hidden", "review-helper"}.isdisjoint(flow["name"] for flow in listing["flows"])
    assert all(flow["visibility"] == "public" and flow["entrypoint"] for flow in listing["flows"])
    assert listing["flows"][0]["option_schema"]["depth"]["allowed"] == ["quick", "deep"]
    assert listing["flows"][0]["defaults"] == {"depth": "quick"}

    ranked = search("review changes", kind="flow")
    assert {"review-alpha", "review-beta"} <= {flow["name"] for flow in ranked["flows"]}
    assert {"review-hidden", "review-helper"}.isdisjoint(flow["name"] for flow in ranked["flows"])
    assert ranked["is_ambiguous"] is True
    assert {"review-alpha", "review-beta"} <= {
        option["flow"] for option in ranked["arbitration_options"]
    }
    assert {"review-hidden", "review-helper"}.isdisjoint(
        option["flow"] for option in ranked["arbitration_options"]
    )

    combined = search("review changes")
    assert {"review-alpha", "review-beta"} <= {flow["name"] for flow in combined["flows"]}
    assert {"review-hidden", "review-helper"}.isdisjoint(flow["name"] for flow in combined["flows"])
    assert all("stage_text" not in flow for flow in combined["flows"])
    assert next(flow for flow in combined["flows"] if flow["name"] == "review-alpha")[
        "option_schema"
    ]["depth"]["allowed"] == ["quick", "deep"]

    # FTS использует текст этапов, но получает только публичные flow.
    assert indexed_names
    assert all({"review-hidden", "review-helper"}.isdisjoint(names) for names in indexed_names)
    assert search("quasar", kind="flow")["flows"] == []
    assert not search("quasar").get("flows")


# ---------- 1. Флоу в type='all' ----------


def test_search_all_includes_flows(tmp_path: Path) -> None:
    """E2-1: type='all' с запросом возвращает и скиллы, и релевантные флоу.

    До фикса: server.py:548-553 — ветка FTS скиллов выполнялась для любого
    type != "flow"; флоу появлялись только в type='flow' и zero-query
    overview (verdict_4, строка 86: mode="search", поля flows нет).
    """
    root = _make_flow_project(tmp_path)
    (root / ".agents" / "flows" / "plan-drafting-flow.md").write_text(FLOW_MD, encoding="utf-8")
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "writing-plans").mkdir()
    (skills_dir / "writing-plans" / "SKILL.md").write_text(
        "---\nname: writing-plans\ntitle: Writing Plans\ncategory: planning\n---\n"
        "# Writing Plans\nплан реализации фичи\n",
        encoding="utf-8",
    )
    storage = SkillsStorage(tmp_path / "skills_fts.db")
    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()

    res = search_skills(
        query="план реализации фичи",
        type="all",
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )

    assert res["mode"] == "search"
    # Скиллы по-прежнему ищутся
    assert any(r["name"] == "writing-plans" for r in res["results"])
    # Флоу попадают в ту же выдачу (ключ flows с релевантным флоу)
    assert "flows" in res, "type='all' должен возвращать релевантные флоу рядом со скиллами"
    flow_names = [f["name"] for f in res["flows"]]
    assert "plan-drafting-flow" in flow_names


def test_search_skill_type_unchanged(tmp_path: Path) -> None:
    """E2-1 (регресс-контур): type='skill' — только скиллы, ключ flows не добавляется."""
    root = _make_flow_project(tmp_path)
    (root / ".agents" / "flows" / "plan-drafting-flow.md").write_text(FLOW_MD, encoding="utf-8")
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "writing-plans").mkdir()
    (skills_dir / "writing-plans" / "SKILL.md").write_text(
        "---\nname: writing-plans\ntitle: Writing Plans\ncategory: planning\n---\n"
        "# Writing Plans\nплан\n",
        encoding="utf-8",
    )
    storage = SkillsStorage(tmp_path / "skills_fts.db")
    sync = SkillsSynchronizer(storage, [skills_dir])
    sync.sync()

    res = search_skills(
        query="план",
        type="skill",
        storage=storage,
        synchronizer=sync,
        project_root=root,
    )
    assert res["mode"] == "search"
    assert "flows" not in res
    assert res["results"]


# ---------- 2. Реранк ДО SQL LIMIT ----------


def test_rerank_pulls_relevant_skill_beyond_sql_limit(tmp_path: Path) -> None:
    """E2-2: релевантный скилл за пределами SQL LIMIT попадает в топ после реранка.

    Сценарий: many-match запрос, где целевой скилл по bm25 за пределами
    SQL LIMIT (limit=3), но реранк (matched_column_flags по колонкам
    name/title/description) поднимает его в топ — документ должен быть
    доступен реранку до отсечения.
    """
    # Шум: карточки, матчатся словом «план» в content (вес 0.15), bm25 ниже
    noise: list[SkillCard] = [
        SkillCard(
            name=f"noise-{i}",
            title=f"Noise {i}",
            description=f"заметка {i}",
            category="general",
        )
        for i in range(12)
    ]
    # Целевой скилл: «план» в NAME и title — должен победить по реранку
    target = SkillCard(
        name="release-plan",
        title="Release Plan",
        description="Составление плана релиза",
        category="planning",
    )

    storage = _make_storage(tmp_path, [*noise, target])
    # content содержит слово «план» у всех карточек — FTS-матч по нему
    with storage.get_connection() as conn:
        for i in range(12):
            conn.execute("DELETE FROM skills_index WHERE name = ?", (f"noise-{i}",))
            conn.execute(
                "INSERT INTO skills_index(name, category, title, description, stack, content, metadata)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    f"noise-{i}",
                    "general",
                    f"Noise {i}",
                    f"заметка {i}",
                    "",
                    f"план шум {i}",
                    _meta_json(f"noise-{i}"),
                ),
            )
        conn.commit()

    res = storage.search_skills("план", limit=3)
    top_names = [c.name for c in res]
    assert "release-plan" in top_names, (
        f"Целевой скилл за пределами SQL LIMIT должен попадать в топ после реранка; got {top_names}"
    )


def _meta_json(name: str) -> str:
    import json

    card = SkillCard(name=name, title=name, description="", category="general")
    return json.dumps(card.model_dump(mode="json"), ensure_ascii=False)


# ---------- 3. Fuzzy-набор с учётом стека ----------


def test_fuzzy_respects_project_stack(tmp_path: Path) -> None:
    """E2-3: опечатка + project_stack — несовместимые fuzzy-кандидаты не вымывают выдачу.

    До фикса: fuzzy-кандидаты набираются без стека, занимают слоты выдачи,
    затем вымываются финальным фильтром → совместимый py-utils (jaccard 0.444
    над порогом) теряется, результат [] (воспроизведено в verdict_4:89).
    """
    cards = [
        SkillCard(
            name="py-utils",
            title="Python Utils",
            description="utils utilities",
            category="general",
            requires_stack=["python"],
        ),
        SkillCard(
            name="ml-utilites",
            title="LoRA Training",
            description="utilites tools",
            category="audio-ml",
            requires_stack={"none_of": ["python"]},
        ),
    ]
    storage = _make_storage(tmp_path, cards)

    # Опечатка: 'utilites' вместо 'utilities'; фильтрация по стеку python
    res = storage.search_skills("utilites", limit=1, project_stack=["python"])
    cards_res, _ = res if isinstance(res, tuple) else (res, False)
    names = [c.name for c in cards_res]
    assert "py-utils" in names, (
        "Совместимый fuzzy-кандидат должен попадать в выдачу при стеке python; "
        f"получено: {names} (нечувствительный к стеку fuzzy-набор вымыл результат)"
    )
    assert "ml-utilites" not in names


# ---------- 4. intent_group в _score_flows ----------


def test_score_flows_uses_intent_group() -> None:
    """E2-4: intent_group влияет на скоринг флоу.

    Два флоу с ОДИНАКОВЫМ текстовым скором (одинаковый title/description),
    но у одного intent_group совпадает со словом запроса → он должен
    ранжироваться выше. До фикса intent_group не участвовал в скоринге
    (verdict_4:90 — поле лишь пробрасывалось в arbitration_options).
    """
    base = {
        "title": "Debug helper",
        "description": "описание",
        "aliases": [],
        "when_to_use": [],
        "name": "a-flow",
    }
    f_plain = {**base, "intent_group": "other-intent"}
    f_group = {**base, "name": "b-flow", "intent_group": "debugging"}

    ordered, scored = _score_flows([f_plain, f_group], "debug")
    scores = {f["name"]: s for s, f in scored}
    assert scores["b-flow"] > scores["a-flow"], (
        f"Флоу с intent_group, совпадающим с запросом, должен скориться выше (got {scores})"
    )
    assert ordered[0]["name"] == "b-flow"


def test_search_flow_intent_group_breaks_ties() -> None:
    """E2-4 (интеграционно): одинаковый текстовый скор — порядок по intent_group."""
    flows = [
        {
            "name": "z-flow",
            "title": "Same",
            "description": "то же",
            "aliases": [],
            "when_to_use": [],
            "intent_group": "review",
        },
        {
            "name": "a-flow",
            "title": "Same",
            "description": "то же",
            "aliases": [],
            "when_to_use": [],
            "intent_group": "release",
        },
    ]
    ordered, scored = _score_flows(flows, "release flow same")
    assert ordered[0]["name"] == "a-flow"


# ---------- 5. Ревизия is_ambiguous ----------


def test_is_ambiguous_same_intent_group_not_ambiguous(tmp_path: Path) -> None:
    """E2-5: флоу одной intent-группы при малой разнице НЕ считаются неоднозначными.

    Два флоу с intent_group='plan-execution' и близкими скорами — это
    варианты одного интента (авто/ручной режим), выбор внутри группы:
    движок не должен требовать вопрос пользователю.
    До фикса: top−second ≤ 5 → is_ambiguous=True (verdict_4:91).
    """
    from mcp_skills.server import _search_flows

    root = _make_flow_project(tmp_path)
    # Два флоу ОДНОЙ группы plan-execution, скоры близкие
    (root / ".agents" / "flows" / "plan-execution-flow.md").write_text(
        FLOW_MD_2.replace("Plan Execution Flow", "Plan Execution Flow"), encoding="utf-8"
    )
    (root / ".agents" / "flows" / "plan-execution-auto-flow.md").write_text(
        FLOW_MD_2.replace("Plan Execution Flow", "Plan Execution Auto Flow"),
        encoding="utf-8",
    )

    res = _search_flows(root, "запуск плана")
    assert res.get("is_ambiguous") is not True, (
        "Флоу одной intent_group не должны считаться неоднозначными — "
        "это варианты одного интента, а не конкурирующие интенты"
    )


def test_is_ambiguous_different_intent_groups_still_ambiguous(tmp_path: Path) -> None:
    """E2-5: флоу РАЗНЫХ intent-групп с близкими скорами — арбитраж сохранён.

    Конкурирующие интенты (планирование vs исполнение) при близком счёте —
    законный случай спросить пользователя (рекомендация ревью №4 сохранена).
    """
    from mcp_skills.server import _search_flows

    root = _make_flow_project(tmp_path)
    (root / ".agents" / "flows" / "plan-drafting-flow.md").write_text(FLOW_MD, encoding="utf-8")
    (root / ".agents" / "flows" / "plan-execution-flow.md").write_text(FLOW_MD_2, encoding="utf-8")

    # Изоляция от кэша пользователя: без явного skills_dir list_available_flows
    # подтягивает ~/.cache/mcp-skills/repo/flows (symlink на живой каталог),
    # и top-2 фиксируется каталогными флоу, а не парой фикстур этого теста.
    isolated_flows = tmp_path / "empty-skills-dir"
    isolated_flows.mkdir()

    res = _search_flows(root, "план", skills_dir=isolated_flows)
    assert res.get("is_ambiguous") is True
    assert res.get("arbitration_options"), "Опции арбитража обязательны при is_ambiguous"


# ---------- 6. Отрицательная маршрутизация (exclude_aliases) ----------


def test_score_flows_exclude_aliases() -> None:
    """Флоу с exclude_aliases полностью исключается из выдачи при совпадении с запрещённым действием."""
    flow = {
        "name": "release-flow",
        "title": "Воркфлоу подготовки релиза (Release Flow)",
        "description": "Подготовка продуктового релиза: расчёт SemVer, changelog и тег.",
        "aliases": ["подготовка релиза", "подготовь релиз"],
        "when_to_use": ["подготовка релиза"],
        "exclude_aliases": ["опубликуй релиз", "publish release"],
    }

    # Прямой запрос на подготовку — скорится успешно
    ordered, scored = _score_flows([flow], "подготовь релиз")
    assert len(ordered) == 1
    assert scored[0][0] > 0

    # Запрос на публикацию — исключается (скор 0, нет в выдаче)
    ordered, scored = _score_flows([flow], "опубликуй релиз")
    assert len(ordered) == 0
    assert len(scored) == 0

    # Запрос с дополнительными словами («пожалуйста, опубликуй релиз») — также исключается
    ordered, scored = _score_flows([flow], "пожалуйста опубликуй релиз")
    assert len(ordered) == 0
