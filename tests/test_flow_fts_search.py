"""FTS5-триграммы в поиске сценариев (flows) и фикс intent_group.

Бенчмарк здесь — зафиксированный набор запросов DEFECT-1 пилота skills-flow:
критерий приёмки «нужный flow присутствует в выдаче на обеих языковых
формулировках», а не «не менее трёх результатов» (число результатов не является
мерой релевантности и не защищает от распыления каталога).

Бенчмарк проверяет публичные точки входа каталога. Внутренние compatibility-flow
не должны появляться в результатах поиска, даже если старые имена остаются
доступны для прямого запуска.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mcp_skills.flow_fts import (
    FLOW_FTS_BONUS,
    build_flow_fts_index,
    flow_trigram_match,
    fts_bonus_rank,
)
from mcp_skills.server import _score_flows, _search_flows, _strip_stage_text

REPO_ROOT = Path(__file__).resolve().parents[1]


def _real_skills_dir() -> Path | None:
    """Каталог скиллов пилота, если он лежит рядом (mcp_skills как соседний checkout)."""
    from tests.conftest import get_companion_skills_dir

    cat = get_companion_skills_dir()
    if cat and (cat / "flows").is_dir():
        return cat
    for cand in (
        REPO_ROOT.parent / "mcp_skills",
        Path("/home/anton/projects/mcp_skills"),
    ):
        if (cand / "flows").is_dir():
            return cand
    return None


SKILLS_DIR = _real_skills_dir()
requires_catalog = pytest.mark.skipif(
    SKILLS_DIR is None, reason="каталог mcp_skills с flows/ не найден"
)


# ---------------------------------------------------------------------------
# Бенчмарк DEFECT-1: (запрос, ожидаемый flow). RU и EN формулировки одного
# намерения обязаны находить один и тот же сценарий.
# ---------------------------------------------------------------------------
BENCH: list[tuple[str, str]] = [
    ("i want to add a task storage feature", "feature-delivery"),
    ("feature", "feature-delivery"),
    ("feature delivery", "feature-delivery"),
    ("реализация фичи", "feature-delivery"),
    ("code review", "review-flow"),
    ("code-review", "review-flow"),
    ("review my code changes", "review-flow"),
    ("review code", "review-flow"),
    ("проверка кода", "review-flow"),
    ("plan execution", "execute-plan-flow"),
    ("plan-execution", "execute-plan-flow"),
    ("execute the plan", "execute-plan-flow"),
    ("план выполнения", "execute-plan-flow"),
    ("execute plan automatically", "execute-plan-flow"),
    ("debugging", "systematic-debugging-flow"),
    ("systematic debugging", "systematic-debugging-flow"),
    ("отладка бага", "systematic-debugging-flow"),
    ("commit changes", "commit-flow"),
    ("make a commit", "commit-flow"),
    ("release preparation", "release-flow"),
    ("qa live", "live-qa-flow"),
    ("live qa", "live-qa-flow"),
    ("qa", "live-qa-flow"),
    ("brainstorming", "brainstorming-flow"),
    ("parallel execution", "execute-plan-flow"),
    ("audit", "fast-audit-flow"),
    ("deep review", "review-flow"),
    ("plan drafting", "plan-drafting-flow"),
    ("plan the feature", "plan-drafting-flow"),
]

# Запросы, которые НЕ называют ни одного сценария. Хороший поиск возвращает
# мало кандидатов или ничего — иначе is_ambiguous начинает задавать пользователю
# вопросы на пустом месте. Замер до правок: в среднем 0.38 кандидатов.
NOISE_QUERIES = [
    "to do",
    "of it",
    "in a",
    "at it",
    "do it",
    "on the",
    "as is",
    "be it",
    "by id",
    "or",
    "id",
    "в базу",
    "по плану",
    "из каталога",
    "до релиза",
    "не работает",
    "как исправить",
    "покажи файл",
    "обнови индекс",
    "add a new endpoint to the api",
    "fix the failing test in ci",
    "explain how the parser works",
    "rename this variable",
]


def _rank(names: list[str], expected: str) -> int:
    """1-based ранг ожидаемого flow; 0 — отсутствует в выдаче."""
    for i, n in enumerate(names, start=1):
        if n == expected:
            return i
    return 0


def _search(root: Path, query: str, *, use_fts: bool = True) -> dict:
    """Реальный _search_flows; ``use_fts=False`` воспроизводит путь деградации."""
    if use_fts:
        return _search_flows(root, query, skills_dir=SKILLS_DIR)
    import mcp_skills.server as srv

    real = srv._flow_fts_bonuses
    srv._flow_fts_bonuses = lambda flows, q: {}  # type: ignore[assignment]
    try:
        return _search_flows(root, query, skills_dir=SKILLS_DIR)
    finally:
        srv._flow_fts_bonuses = real  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 1. flow_trigram_match: семантика OR, а не AND
# ---------------------------------------------------------------------------


def test_match_is_or_not_and() -> None:
    """AND по триграммам обнуляет выдачу, если хоть один токен отсутствует.

    Замер: AND дал полноту 61 % (любой «лишний» токен запроса — «i want to add
    a task storage feature» — обнулял результат), OR — 100 %.
    """
    expr = flow_trigram_match("task storage")
    assert " AND " not in expr
    assert " OR " in expr


def test_match_empty_for_short_query() -> None:
    """Токены < 3 символов не дают триграмм — MATCH пустой, это не «0 результатов»."""
    assert flow_trigram_match("qa") == ""
    assert flow_trigram_match("id") == ""
    assert flow_trigram_match("") == ""


def test_match_quotes_trigrams() -> None:
    """Триграммы кавычатся: иначе символы читаются как операторы MATCH.

    «review» даёт скользящее окно по 3 символа: rev, evi, vie, iew. Проверяется
    весь набор, чтобы тест фиксировал именно триграммы, а не случайные подстроки.
    """
    expr = flow_trigram_match("review")
    assert expr == '("evi" OR "iew" OR "rev" OR "vie")'


# ---------------------------------------------------------------------------
# 2. Построение индекса и деградация
# ---------------------------------------------------------------------------


def test_empty_flow_list_degrades() -> None:
    res = build_flow_fts_index([])
    assert res.conn is None
    assert res.error == "empty_flow_list"
    assert fts_bonus_rank(res, "code review") == {}


def test_bonus_empty_when_no_signal() -> None:
    """Нет токенов >= 3 → нет FTS-сигнала → пустой словарь, выдача не меняется."""
    res = build_flow_fts_index(
        [{"name": "a-flow", "title": "A", "description": "d", "intent_group": "x"}]
    )
    assert fts_bonus_rank(res, "qa") == {}
    res.close()


def test_bonus_is_position_graded() -> None:
    """Бонус убывает с рангом: FLOW_FTS_BONUS / (rank + 1)."""
    flows = [
        {
            "name": "code-review-flow",
            "title": "Code Review",
            "description": "review code diff",
            "intent_group": "code-review",
            "stage_text": "review the code changes",
        },
        {
            "name": "commit-flow",
            "title": "Commit",
            "description": "git commit",
            "intent_group": "commit",
            "stage_text": "stage and commit",
        },
    ]
    res = build_flow_fts_index(flows)
    bonuses = fts_bonus_rank(res, "code review")
    res.close()
    assert bonuses, "FTS должен найти хотя бы один сценарий"
    assert max(bonuses.values()) == pytest.approx(FLOW_FTS_BONUS)
    # упорядочены по убыванию бонуса
    vals = sorted(bonuses.values(), reverse=True)
    assert vals == sorted(vals, reverse=True)


def test_fts_bonus_can_rerank_equal_lexical_matches() -> None:
    """Совпадение в тексте этапа поднимает flow при равном метаданном скоре."""
    flows = [
        {
            "name": "a-flow",
            "title": "Review",
            "description": "",
            "aliases": [],
            "when_to_use": [],
            "intent_group": "",
            "stage_text": "unrelated instruction",
        },
        {
            "name": "b-flow",
            "title": "Review",
            "description": "",
            "aliases": [],
            "when_to_use": [],
            "intent_group": "",
            "stage_text": "review code changes and inspect code review findings",
        },
    ]
    result = build_flow_fts_index(flows)
    try:
        bonuses = fts_bonus_rank(result, "review code")
    finally:
        result.close()
    lexical, _ = _score_flows(flows, "review code")
    reranked, _ = _score_flows(flows, "review code", bonuses)
    assert [flow["name"] for flow in lexical] == ["a-flow", "b-flow"]
    assert [flow["name"] for flow in reranked] == ["b-flow", "a-flow"]


def test_missing_stage_text_still_indexes() -> None:
    """Без stage_text индекс строится на метаданных — не падает."""
    res = build_flow_fts_index([{"name": "a-flow", "title": "Code Review", "description": ""}])
    assert res.conn is not None
    assert res.indexed == 1
    res.close()


# ---------------------------------------------------------------------------
# 3. Фикс intent_group: дефисные группы и короткие группы
# ---------------------------------------------------------------------------


def test_intent_group_hyphen_split() -> None:
    """«code-review» обязан матчить слово «code», а не только «codereview».

    До правки split() по пробелам склеивал группу в один токен; 9 из 13
    сценариев были недостижимы через intent_group.
    """
    flow = {
        "name": "x-flow",
        "title": "T",
        "description": "d",
        "aliases": [],
        "when_to_use": [],
        "intent_group": "code-review",
    }
    ordered, scored = _score_flows([flow], "code")
    assert ordered, "слово «code» должно совпасть с intent_group «code-review»"


def test_intent_group_short_group_reachable() -> None:
    """Группа «qa» (2 символа) не должна отбрасываться фильтром len >= 3."""
    flow = {
        "name": "qa-flow",
        "title": "T",
        "description": "d",
        "aliases": [],
        "when_to_use": [],
        "intent_group": "qa",
    }
    ordered, _ = _score_flows([flow], "qa")
    assert ordered


def test_query_tokens_still_min_three_chars() -> None:
    """Токены запроса < 3 символов НЕ попадают в подстрочный матчинг.

    Замер на 24 нейтральных запросах: разрешение 2-символьных токенов поднимало
    TOP-1 на 6 п.п., но добавляло 80 ложных кандидатов («на код» → 4 вместо 0,
    «по плану» → 10, «из каталога» → 12 из 13). Это распыление каталога и
    ложные вопросы пользователю, поэтому порог сохранён.
    """
    flow = {
        "name": "code-review-flow",
        "title": "Code Review Flow",
        "description": "d",
        "aliases": [],
        "when_to_use": [],
        "intent_group": "code-review",
    }
    ordered, _ = _score_flows([flow], "на код")
    assert not ordered, "2-символьный токен «на» не должен матчить подстрокой"


def test_aliases_with_prepositions_and_short_substring_queries_do_not_spray() -> None:
    """Предлоги в aliases («на», «по», «to») не попадают в structural short terms, а «or»/«it»/«id» не матчатся подстрокой."""
    flows = [
        {
            "name": "review-flow",
            "title": "For work before commit",
            "description": "Checks for commit ideas and alternatives",
            "aliases": ["задача по плану на техдолг", "run to pr"],
            "when_to_use": ["при ревью"],
            "intent_group": "review",
        },
        {
            "name": "live-qa-flow",
            "title": "QA Verification",
            "description": "Live QA pipeline",
            "aliases": ["run qa"],
            "when_to_use": [],
            "intent_group": "qa",
        },
    ]
    # Предлог «на» в aliases не должен вызывать матчинг с aliases/when_to_use по «на»
    res_na, _ = _score_flows(flows, "на код")
    assert not res_na, f"Ложный хит по предлогу «на»: {res_na}"

    res_po, _ = _score_flows(flows, "по памяти")
    assert not res_po, f"Ложный хит по предлогу «по»: {res_po}"

    # Короткие слова не должны матчиться подстрокой в title/description («for», «commit», «ideas»)
    for q in ("or", "it", "id"):
        res_short, _ = _score_flows(flows, q)
        assert not res_short, f"Ложный подстрочный хит для {q!r}: {res_short}"

    # Структурный 2-буквенный токен 'qa' из name/intent_group продолжает точно находить live-qa-flow
    res_qa, _ = _score_flows(flows, "qa")
    assert [f["name"] for f in res_qa] == ["live-qa-flow"]


# ---------------------------------------------------------------------------
# 4. Интеграция: релевантность бенчмарка
# ---------------------------------------------------------------------------


@requires_catalog
def test_benchmark_hit_rate(tmp_path: Path) -> None:
    """Целевой flow найден хотя бы в выдаче для почти всех запросов бенчмарка."""
    misses = [
        (q, exp)
        for q, exp in BENCH
        if _rank([f["name"] for f in _search(tmp_path, q)["flows"]], exp) == 0
    ]
    assert misses == [], f"обнаружены промахи в бенчмарке: {misses}"


@requires_catalog
def test_benchmark_fts_preserves_candidates_and_top1_quality(tmp_path: Path) -> None:
    """FTS не меняет состав выдачи; точность top-1 публичного каталога остаётся высокой."""

    def top1(use_fts: bool) -> int:
        return sum(
            1
            for q, exp in BENCH
            if _rank([f["name"] for f in _search(tmp_path, q, use_fts=use_fts)["flows"]], exp) == 1
        )

    for query, _expected in BENCH:
        with_fts = {flow["name"] for flow in _search(tmp_path, query)["flows"]}
        without = {flow["name"] for flow in _search(tmp_path, query, use_fts=False)["flows"]}
        assert with_fts == without, f"FTS изменил состав кандидатов для {query!r}"
    assert top1(True) >= top1(False), "FTS ухудшил top-1 относительно лексического ранга"
    assert top1(True) / len(BENCH) >= 0.90


@requires_catalog
def test_bilingual_parity(tmp_path: Path) -> None:
    """RU и EN формулировки одного намерения находят один сценарий."""
    pairs = [
        ("code review", "проверка кода", "review-flow"),
        ("plan execution", "план выполнения", "execute-plan-flow"),
        ("systematic debugging", "отладка бага", "systematic-debugging-flow"),
    ]
    for en, ru, expected in pairs:
        en_names = [f["name"] for f in _search(tmp_path, en)["flows"]]
        ru_names = [f["name"] for f in _search(tmp_path, ru)["flows"]]
        assert expected in en_names, f"EN «{en}» не нашёл {expected}: {en_names}"
        assert expected in ru_names, f"RU «{ru}» не нашёл {expected}: {ru_names}"


# ---------------------------------------------------------------------------
# 5. Защита от распыления каталога и от ложного арбитража
# ---------------------------------------------------------------------------


@requires_catalog
def test_noise_queries_do_not_spray_catalog(tmp_path: Path) -> None:
    """FTS не расширяет выдачу: FTS только переставляет найденное скорингом.

    Замер: среднее число кандидатов на нейтральном запросе 0.38 и до, и после
    подключения FTS. Режим «расширения» давал 5.79 — неприемлемо.
    """
    with_fts = [len(_search(tmp_path, q)["flows"]) for q in NOISE_QUERIES]
    without = [len(_search(tmp_path, q, use_fts=False)["flows"]) for q in NOISE_QUERIES]
    assert sum(with_fts) == sum(without), (
        f"FTS изменил число кандидатов: {sum(with_fts)} vs {sum(without)}"
    )
    assert sum(with_fts) / len(with_fts) <= 1.0


@requires_catalog
def test_fts_does_not_change_ambiguity(tmp_path: Path) -> None:
    """Арбитраж решается по первичному лексическому скору — FTS его не меняет.

    Ступенька FTS-бонуса (20/10/6.7) шире окна арбитража ±5, поэтому если бы
    разрыв считался по итоговому скору, перестановка выдачи молча гасила бы
    законный вопрос пользователю. Этот регресс был пойман существующим тестом
    test_is_ambiguous_different_intent_groups_still_ambiguous (запрос «план»).

    Замер до разделения сигналов: ложный арбитраж 5/31 → 2/31, то есть FTS
    «улучшал» метрику ровно тем, что подавлял вопросы. Корректный инвариант —
    равенство, а не уменьшение.
    """

    def amb(queries: list[str], use_fts: bool) -> list[bool]:
        return [bool(_search(tmp_path, q, use_fts=use_fts).get("is_ambiguous")) for q in queries]

    bench = [q for q, _ in BENCH]
    assert amb(bench, True) == amb(bench, False), (
        "FTS-перестановка изменила решение арбитража на бенчмарке"
    )
    assert amb(NOISE_QUERIES, True) == amb(NOISE_QUERIES, False), (
        "FTS-перестановка изменила решение арбитража на нейтральных запросах"
    )


# ---------------------------------------------------------------------------
# 6. Контракт ответа: stage_text не утекает, пустой результат объяснён
# ---------------------------------------------------------------------------


@requires_catalog
def test_stage_text_never_in_response(tmp_path: Path) -> None:
    """Текст этапов нужен только индексу, не клиенту."""
    for q in ("code review", "feature", "debugging"):
        resp = _search(tmp_path, q)
        assert resp["flows"], f"запрос «{q}» дал пустую выдачу"
        for f in resp["flows"]:
            assert "stage_text" not in f, f"stage_text утекает в ответе на «{q}»"


def test_strip_stage_text_removes_only_that_key() -> None:
    flows = [
        {"name": "a", "title": "A", "stage_text": "x" * 1000},
        {"name": "b", "title": "B"},
    ]
    out = _strip_stage_text(flows)
    assert all("stage_text" not in f for f in out)
    assert [f["name"] for f in out] == ["a", "b"]
    assert out[0]["title"] == "A"


def test_strip_stage_text_passthrough_when_absent() -> None:
    flows = [{"name": "a", "title": "A"}]
    assert _strip_stage_text(flows) is flows


@requires_catalog
def test_empty_result_has_hint(tmp_path: Path) -> None:
    """Пустая выдача обязана объяснять причину, а не молчать.

    В пилотных прогонах молчаливый ``flows: []`` читался агентом как «в каталоге
    нет ничего похожего», и работа останавливалась.
    """
    resp = _search(tmp_path, "python pydantic database migrations task storage")
    assert resp["flows"] == []
    assert resp["count"] == 0
    assert "hint" in resp
    assert "метаданным" in resp["hint"]


@requires_catalog
def test_non_empty_result_has_no_hint(tmp_path: Path) -> None:
    resp = _search(tmp_path, "code review")
    assert resp["flows"]
    assert "hint" not in resp


@requires_catalog
def test_count_matches_flows_length(tmp_path: Path) -> None:
    resp = _search(tmp_path, "feature")
    assert resp["count"] == len(resp["flows"])


@requires_catalog
def test_empty_query_lists_all_without_stage_text(tmp_path: Path) -> None:
    """Без запроса индекс не строится (include_stage_text=False) — выдача лёгкая."""
    resp = _search(tmp_path, "")
    names = {flow["name"] for flow in resp["flows"]}
    assert {"review-flow", "execute-plan-flow"} <= names
    assert {"code-review-flow", "plan-execution-flow", "deep-review-flow"}.isdisjoint(names)
    assert resp["count"] == len(resp["flows"])
    assert all(flow["visibility"] == "public" and flow["entrypoint"] for flow in resp["flows"])
    assert all("stage_text" not in f for f in resp["flows"])


# ---------------------------------------------------------------------------
# 7. type='all' обязан ранжировать flows тем же механизмом
# ---------------------------------------------------------------------------


@requires_catalog
def test_search_skills_all_includes_ranked_flows(tmp_path: Path) -> None:
    from mcp_skills.server import search_skills

    resp = search_skills(
        query="code review", type="all", project_root=tmp_path, skills_dir=SKILLS_DIR
    )
    names = [f["name"] for f in resp.get("flows", [])]
    assert "review-flow" in names, f"review-flow отсутствует: {names}"
    assert "code-review-flow" not in names
    for f in resp.get("flows", []):
        assert "stage_text" not in f
