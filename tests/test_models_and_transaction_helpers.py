"""Юнит-тесты общих module-level валидаторов models.py и хелпера _transaction synchronizer.py."""

import pytest

from mcp_knowledge.models import (
    CardRecordRequest,
    HypothesisRecordRequest,
    _coerce_confidence,
    _validate_category_slug,
    _validate_non_empty_stripped,
)
from mcp_knowledge.synchronizer import _transaction


# --- _validate_non_empty_stripped ---------------------------------------------


def test_validate_non_empty_stripped_strips_and_returns():
    assert _validate_non_empty_stripped("  текст  ") == "текст"


def test_validate_non_empty_stripped_rejects_blank():
    for blank in ("", "   ", "\t\n"):
        with pytest.raises(ValueError, match="Поле не может быть пустым"):
            _validate_non_empty_stripped(blank)


def test_validate_non_empty_stripped_passthrough_non_string():
    assert _validate_non_empty_stripped(123) == 123
    assert _validate_non_empty_stripped(None) is None


# --- _coerce_confidence --------------------------------------------------------


def test_coerce_confidence_none_returns_default():
    assert _coerce_confidence(None, default=0.75) == 0.75
    assert _coerce_confidence(None, default=0.20) == 0.20


def test_coerce_confidence_parses_numeric_strings():
    assert _coerce_confidence("0.5", default=0.75) == 0.5
    assert _coerce_confidence(1, default=0.75) == 1.0


def test_coerce_confidence_rejects_out_of_range():
    with pytest.raises(ValueError, match="Недопустимое значение confidence"):
        _coerce_confidence(1.5, default=0.75)
    with pytest.raises(ValueError, match="Недопустимое значение confidence"):
        _coerce_confidence(-0.1, default=0.75)


def test_coerce_confidence_rejects_non_numeric():
    with pytest.raises(ValueError, match="Недопустимое значение confidence"):
        _coerce_confidence("not-a-number", default=0.75)
    with pytest.raises(ValueError, match="Недопустимое значение confidence"):
        _coerce_confidence(object(), default=0.75)


# --- _validate_category_slug ---------------------------------------------------


def test_validate_category_slug_normalizes():
    assert _validate_category_slug("  ML  ") == "ml"
    assert _validate_category_slug("Data Base") == "data-base"


def test_validate_category_slug_rejects_invalid():
    with pytest.raises(ValueError, match="Недопустимое имя категории"):
        _validate_category_slug("!!!")


# --- Интеграция: те же валидаторы через pydantic-DTO ---------------------------


def test_card_request_uses_shared_validators():
    req = CardRecordRequest(
        title="  Заголовок карточки  ",
        symptom=" долго наблюдающийся симптом отказа ",
        root_cause="причина кроется в исчерпании пула соединений к базе данных",
        solution="решение состоит в увеличении пула и добавлении ретраев",
        rationale="обоснование: инцидент повторился трижды в продакшене",
        category="  Database ",
        type="lesson",
    )
    assert req.title == "Заголовок карточки"
    assert req.symptom == "долго наблюдающийся симптом отказа"
    assert req.category == "database"


def test_hypothesis_request_confidence_default():
    req = HypothesisRecordRequest(
        title="Рабочая гипотеза о таймаутах",
        symptom="периодические таймауты при чтении из базы данных",
        hypothesis="гипотетическая причина: неверная настройка пула соединений",
        category="ml",
    )
    assert req.confidence == 0.20


# --- _transaction --------------------------------------------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def execute(self, sql: str) -> None:
        self.executed.append(sql)


def test_transaction_commit_on_success():
    cursor = _FakeCursor()
    with _transaction(cursor):  # type: ignore[arg-type]
        pass
    assert cursor.executed == ["BEGIN IMMEDIATE;", "COMMIT;"]


def test_transaction_rollback_on_error():
    cursor = _FakeCursor()
    with pytest.raises(RuntimeError, match="fail"), _transaction(cursor):  # type: ignore[arg-type]
        raise RuntimeError("fail")
    assert cursor.executed == ["BEGIN IMMEDIATE;", "ROLLBACK;"]


def test_transaction_rollback_on_base_exception():
    cursor = _FakeCursor()
    with pytest.raises(KeyboardInterrupt), _transaction(cursor):  # type: ignore[arg-type]
        raise KeyboardInterrupt
    assert cursor.executed == ["BEGIN IMMEDIATE;", "ROLLBACK;"]
