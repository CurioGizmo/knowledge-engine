"""Ф0 (core-dedup v4): golden-матрица frontmatter для 4 парсеров.

Двухслойные ожидания (см. docs/plans/2026-10-26-core-dedup-v4.md):
- contract-ячейки — поведение обязано сохраниться; сравнение СТРОГОЕ
  (полное равенство нормализованных записей);
- snapshot-v1-ячейки — заведомые баги: до Ф3 ассертится слой v1, после Ф3 —
  v2_target. Сравнение v2-слоя — СЕМАНТИЧЕСКОЕ (subset-правила ниже):
  * ``{"result": "ok"}`` — рантайм обязан вернуть ok (поля-идентификаторы
    не пинятся: их точные значения зависят от имплементации Ф3);
  * ``{"result": "raise", "type": "domain-error"}`` — доменная ошибка
    пакета (не сырой yaml.ParserError и не AttributeError-краш);
  * ``{"result": "raise", "type": "<Класс>"}`` — точный класс исключения.

Механика фазы: per-package константа PHASE3_APPLIED ниже. Коммит миграции
пакета (Ф3) флипает ровно одну строку здесь же. flow флипается ВМЕСТЕ со
skills (один коммит, один пакет mcp_skills). НЕ env-переменная (конфликт
conftest._isolate_env, недетерминизм CI).

Frontmatter-matrix.json после Ф0 заморожен: любой дифф фикстуры = откат.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

# --- механика выбора слоя: флипается в коммите Ф3 того же пакета ---
PHASE3_APPLIED = {
    "knowledge": True,  # Ф3: миграция на mcp_core.card_codec (коммит <ДАТА>) — флип вместе с реализацией
    "skills": True,  # Ф3 срез 2: skills+flow одним коммитом (один пакет mcp_skills)
    "audit": True,  # Ф3 срез 3: audit на core CardCodec (guard для кейсов 11/14/15/17)
    "flow": True,  # flow флипается вместе со skills (один пакет mcp_skills)
}

PACKAGES = ("knowledge", "skills", "audit", "flow")

# Род исключений, недопустимых как «domain-error» в v2_target-слое:
# - _RAW_CRASH_TYPES: утечка парсерных/внутренних исключений (Ф3 обязана
#   оборачивать их в доменные ошибки);
# - _PROGRAM_BUG_TYPES: программные сбои реализации (ревью Ф2-6) — RuntimeError
#   и прочие не могут легитимно значить «невалидный формат» (в отличие от
#   ValueError — родителя InvalidSkillFormatError/InvalidFlowFormatError).
_RAW_CRASH_TYPES = frozenset({"ParserError", "ScannerError", "AttributeError"})
_PROGRAM_BUG_TYPES = frozenset(
    {"RuntimeError", "TypeError", "KeyError", "NameError", "IndexError", "AttributeError"}
)


def _load_matrix() -> list[dict]:
    data = json.loads((FIXTURES / "frontmatter-matrix.json").read_text(encoding="utf-8"))
    assert isinstance(data, list) and data, "пустая матрица"
    return data


MATRIX = _load_matrix()
CASES_BY_ID = {c["id"]: c for c in MATRIX}


def _expected_cell(case: dict, package: str) -> dict:
    """Выбирает слой ожиданий по kind и фазе пакета."""
    cell = case["cells"][package]
    if cell["kind"] == "contract":
        return {k: v for k, v in cell.items() if k != "kind"}
    layer = "v2_target" if (package in PHASE3_APPLIED and PHASE3_APPLIED[package]) else "v1"
    return cell[layer]


def _matches_v2(actual: dict, target: dict) -> tuple[bool, str]:
    """Семантический матчинг v2-слоя (subset-правила, см. докстринг модуля).

    v1-слой и contract-слой сравниваются строго (==); v2_target — декларация
    намерения, а не точная запись: пиним род результата (ok / raise с классом
    или domain-error), не конкретные значения полей.
    """
    if target.get("result") == "ok":
        if actual.get("result") != "ok":
            return False, "ожидался ok"
        return True, ""
    # raise-цель
    if actual.get("result") != "raise":
        return False, f"ожидался raise, получен {actual.get('result')!r}"
    want_type = target.get("type")
    got_type = actual.get("type")
    if want_type == "domain-error":
        if got_type in _RAW_CRASH_TYPES:
            return False, (
                f"утечка/raw-краш ({got_type}) вместо доменной ошибки — "
                "Ф3 должна оборачивать (error-mapping)"
            )
        # Программные сбои не суть доменные ошибки (ревью Ф2-6): RuntimeError/
        # TypeError/KeyError после флипа Ф3 означают сломанный код парсера,
        # а не задокументированное поведение — пропускать их нельзя.
        if got_type in _PROGRAM_BUG_TYPES:
            return False, (
                f"программный сбой ({got_type}) замаскирован под domain-error — "
                "проверь код парсера: это не семантика формата, а дефект реализации"
            )
        return True, ""
    if want_type is not None and got_type != want_type:
        return False, f"ожидался класс {want_type}, получен {got_type}"
    return True, ""


# --- рантаймы 4 парсеров (нормализация к общей записи) ---
def _run_knowledge(raw: str) -> dict:
    from mcp_knowledge.parser import parse_card_markdown

    try:
        fm, sections = parse_card_markdown(raw)
        return {"result": "ok", "id": fm.id, "sections": sorted(sections)}
    except Exception as e:  # noqa: BLE001 — матрица пинит и род ошибок
        return {"result": "raise", "type": type(e).__name__, "msg_prefix": str(e)[:60]}


def _run_audit(raw: str) -> dict:
    from mcp_audit.parser import parse_audit_markdown

    try:
        issue = parse_audit_markdown(raw)
        return {"result": "ok", "id": issue.id, "symptom_head": (issue.symptom or "")[:60]}
    except Exception as e:  # noqa: BLE001
        return {"result": "raise", "type": type(e).__name__, "msg_prefix": str(e)[:60]}


def _run_skills(raw: str) -> dict:
    from mcp_skills.parser import parse_skill_markdown

    try:
        card, body = parse_skill_markdown(raw)
        return {"result": "ok", "name": card.name, "body_head": body[:60]}
    except Exception as e:  # noqa: BLE001
        return {"result": "raise", "type": type(e).__name__, "msg_prefix": str(e)[:60]}


def _run_flow(raw: str) -> dict:
    from mcp_skills.flow_parser import parse_flow_markdown

    try:
        flow = parse_flow_markdown(raw, "f")
        return {"result": "ok", "title": flow.title}
    except Exception as e:  # noqa: BLE001
        return {"result": "raise", "type": type(e).__name__, "msg_prefix": str(e)[:60]}


RUNNERS = {
    "knowledge": _run_knowledge,
    "audit": _run_audit,
    "skills": _run_skills,
    "flow": _run_flow,
}


def _build_raw(case: dict, package: str) -> str:
    """Восстанавливает вход кейса: shell + валидная мета пакета + flow-этапы.

    Генерация входов обязана совпадать с scratch/gen_frontmatter_matrix.py:
    меты и flow-этапы подставляются тем же правилом, иначе матрица разъедется.
    """
    meta = case["meta_variants"][package]
    raw = case["shell"].replace("{META}", meta)
    if (
        package == "flow"
        and "## Stage One" not in raw
        and case["id"]
        not in (
            "04b-eof-on-closing-dash",
            "22-crlf-eof",
            "14-empty-file",
            "15-only-dashes",
            "17-only-cr",
        )
    ):
        crlf = "\r" in raw
        suffix = (
            "\r\n\r\n## Stage One\r\n\r\nstep text" if crlf else "\n\n## Stage One\n\nstep text"
        )
        raw = raw + suffix
    return raw


def test_matrix_structure() -> None:
    """Страж фикстуры: contract без слоёв, snapshot — v1+v2_target, БЕЗ маскировки."""
    ids = [c["id"] for c in MATRIX]
    assert len(ids) == len(set(ids)), "дубли id кейсов"
    for case in MATRIX:
        assert case.get("shell") is not None, f"{case['id']}: нет shell"
        for pkg in PACKAGES:
            assert pkg in case["meta_variants"], f"{case['id']}: нет meta_variants[{pkg}]"
            assert pkg in case["cells"], f"{case['id']}: нет cells[{pkg}]"
            cell = case["cells"][pkg]
            kind = cell.get("kind")
            assert kind in ("contract", "snapshot-v1"), f"{case['id']}/{pkg}: kind={kind!r}"
            if kind == "contract":
                assert "v1" not in cell and "v2_target" not in cell
            else:
                assert "v1" in cell, f"{case['id']}/{pkg}: snapshot без v1"
                assert "v2_target" in cell, f"{case['id']}/{pkg}: snapshot без v2_target"
                # анти-маскировка: «баг пинится как целевой» запрещена.
                # Маскировка = v1 raise (баг frontmatter-сплиттера) И target
                # того же класса (изменение не планируется — тогда это contract).
                # Исключение: flow-кейсы без H2-этапов (04b/22) — raise
                # InvalidFlowFormatError там не frontmatter-баг, а контракт
                # flow-парсера (нет этапов → ошибка) — no-delta target легитимен.
                t = cell["v2_target"]
                v1 = cell["v1"]
                if v1.get("result") == "raise" and t.get("result") == "raise":
                    same_class = t.get("type") == v1.get("type")
                    flow_no_stages = (
                        pkg == "flow"
                        and v1.get("type") == "InvalidFlowFormatError"
                        and case["id"] in ("04b-eof-on-closing-dash", "22-crlf-eof")
                    )
                    if same_class and not flow_no_stages:
                        raise AssertionError(
                            f"{case['id']}/{pkg}: v2_target повторяет v1-класс "
                            f"{t.get('type')!r} — баг пинится как целевое поведение"
                        )


@pytest.mark.parametrize(
    "case_id,package",
    [(c["id"], p) for c in MATRIX for p in PACKAGES],
)
def test_matrix_cell(case_id: str, package: str) -> None:
    """Ячейка матрицы: фактический прогон парсера == ожидание выбранного слоя."""
    case = CASES_BY_ID[case_id]
    raw = _build_raw(case, package)
    expected = _expected_cell(case, package)
    actual = RUNNERS[package](raw)
    if case["cells"][package]["kind"] == "snapshot-v1" and PHASE3_APPLIED.get(package):
        ok, why = _matches_v2(actual, expected)
        assert ok, (
            f"{case_id}/{package}: v2_target не достигнут ({why}).\n"
            f"  actual:   {actual!r}\n  target: {expected!r}\n"
            f"  raw: {raw!r}"
        )
        return
    assert actual == expected, (
        f"{case_id}/{package}: расхождение с golden-слоём (v1/contract).\n"
        f"  actual:   {actual!r}\n  expected: {expected!r}\n"
        f"  raw: {raw!r}"
    )
