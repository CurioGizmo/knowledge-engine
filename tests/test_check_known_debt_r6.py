"""Тесты R6: check_known_debt — порог близости значимых токенов и перебор кандидатов.

Воспроизведение (docs/analysis/2026-10-19-search-measurement-report.md, раздел 4):
12/36 кейсов exp=new ложно классифицированы как known — первый BM25-хит по
общим словам симптома («не», «при», «повторном», file stem «server») подхватывает
чужую карточку. Требуется порог значимого покрытия и перебор кандидатов,
а не остановка на первом хите.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_audit.storage import AuditStorage
from mcp_audit.synchronizer import AuditSynchronizer


def _card(
    card_id: str,
    title: str,
    symptom: str,
    status: str = "planned",
    target_file: str | None = None,
) -> str:
    return (
        "---\n"
        f'id: "{card_id}"\n'
        f'title: "{title}"\n'
        "category: backend\n"
        f"status: {status}\n"
        "priority: p2\n"
        f"target_file: {target_file or 'null'}\n"
        "---\n\n"
        f"## Симптом\n{symptom}\n"
    )


@pytest.fixture
def stand(tmp_path: Path) -> AuditStorage:
    """Стенд: известный долг про FTS5-токенизатор + карточка про другой дефект того же файла."""
    a_dir = tmp_path / ".agents" / "audit" / "todo" / "backend"
    a_dir.mkdir(parents=True)
    (a_dir / "known-1.md").write_text(
        _card(
            "aud-known-0001",
            "Токенизатор FTS5 не индексирует 2-символьные акронимы",
            "Поиск по коротким акронимам ui db возвращает 0 результатов через trigram-токенизатор",
            target_file="src/store.py",
        ),
        encoding="utf-8",
    )
    (a_dir / "other-1.md").write_text(
        _card(
            "aud-other-0002",
            "Лишняя перезапись конфига при повторном запуске",
            "Конфиг перезаписывается при каждом повторном запуске инсталлера без изменений",
            target_file="src/store.py",
        ),
        encoding="utf-8",
    )
    db = tmp_path / "audit_fts.db"
    storage = AuditStorage(db_path=db)
    AuditSynchronizer(storage=storage, audit_dir=tmp_path / ".agents" / "audit").sync_all()
    return storage


def test_new_defect_not_matched_by_generic_words(stand: AuditStorage) -> None:
    """Новый дефект с общими словами не подхватывает чужую карточку.

    Симптом «инсталлер падает при запуске с ошибкой» содержит общие слова
    (при/с/запуске), но значимые токены (инсталлер, падает, ошибка) не
    покрывают карточку про FTS5-токенизатор — expected new.
    """
    resp = stand.check_known_debt(symptom="инсталлер падает при запуске с ошибкой")
    assert resp.found is False, (
        f"Ложный known: {resp.id} — {resp.message}; значимые токены не покрыты"
    )


def test_known_defect_matched_by_significant_overlap(stand: AuditStorage) -> None:
    """Симптом с реальным пересечением значимых слов находит known-долг."""
    resp = stand.check_known_debt(symptom="короткие акронимы не находятся через токенизатор")
    assert resp.found is True
    assert resp.id == "aud-known-0001"
    assert resp.is_known_debt is True


def test_completed_matched_card_is_regression(stand: AuditStorage, tmp_path: Path) -> None:
    """Карточка в статусе completed по совпавшему симптому → is_regression."""
    a_dir = tmp_path / ".agents" / "audit" / "done" / "backend"
    a_dir.mkdir(parents=True, exist_ok=True)
    (a_dir / "closed-1.md").write_text(
        _card(
            "aud-closed-0003",
            "Лишняя перезапись конфига при повторном запуске",
            "Конфиг перезаписывается при каждом повторном запуске инсталлера без изменений",
            status="completed",
        ),
        encoding="utf-8",
    )
    AuditSynchronizer(storage=stand, audit_dir=tmp_path / ".agents" / "audit").sync_all()
    resp = stand.check_known_debt(symptom="конфиг перезаписывается при повторном запуске")
    assert resp.found is True
    assert resp.is_regression is True, f"Ожидалась регрессия, получено: {resp.status}"


def test_different_defect_same_file_not_first_hit(stand: AuditStorage, tmp_path: Path) -> None:
    """Разные дефекты одного файла: перебор кандидатов, а не первый BM25-хит.

    Ключевой дефект R6: карточка known-1 (FTS5) и новая карточка про тот же
    файл с другим симптомом. check_known_debt по чужому симптому НЕ должен
    матчиться на known-1 только потому, что она первая в BM25.
    """
    resp = stand.check_known_debt(
        symptom="инсталлер падает",
        file_path="src/store.py",
    )
    # Карточек про инсталлер с падением нет — new, а не ложный known
    assert resp.found is False, f"Ложный known по общему слову: {resp.id}: {resp.message}"
    assert "привязаны к другим файлам" not in resp.message


def test_file_scope_message_requires_similar_candidate_in_other_file(
    stand: AuditStorage,
    tmp_path: Path,
) -> None:
    """File-scope refusal is emitted only for a sufficiently similar candidate."""
    a_dir = tmp_path / ".agents" / "audit" / "todo" / "backend"
    (a_dir / "other-file.md").write_text(
        _card(
            "aud-other-file-0003",
            "Инсталлер падает при запуске",
            "Инсталлер падает при запуске с ошибкой конфигурации",
            target_file="src/installer.py",
        ),
        encoding="utf-8",
    )
    AuditSynchronizer(storage=stand, audit_dir=tmp_path / ".agents" / "audit").sync_all()

    resp = stand.check_known_debt(
        symptom="инсталлер падает при запуске с ошибкой конфигурации",
        file_path="src/store.py",
    )

    assert resp.found is False
    assert "привязаны к другим файлам" in resp.message


class TestCandidateOverlapEdgeCases:
    """Юнит-покрытие edge-кейсов _candidate_overlap/_candidate_meets_overlap (ревью c191a52)."""

    def test_empty_significant_returns_zero(self) -> None:
        # significant пуст → overlap 0.0 (не None): сохраняет старую семантику отказа
        assert AuditStorage._candidate_overlap({"title": "x"}, set()) == 0.0

    def test_empty_card_tokens_returns_none(self) -> None:
        # все поля карточки пустые → card_tokens пуст → None («непохожая» карточка)
        assert (
            AuditStorage._candidate_overlap(
                {"title": "", "symptom": "", "solution": ""}, {"alpha", "beta"}
            )
            is None
        )

    def test_none_fields_treated_as_empty(self) -> None:
        # None-поля не ломают сбор текста (str(None or ""))
        assert (
            AuditStorage._candidate_overlap(
                {"title": None, "symptom": None, "solution": None}, {"alpha"}
            )
            is None
        )

    def test_meets_overlap_thresholds(self) -> None:
        candidate = {"title": "alpha beta", "symptom": "", "solution": ""}
        significant = {"alpha", "beta", "gamma"}
        # overlap = 2/3 ≈ 0.667
        assert AuditStorage._candidate_meets_overlap(candidate, significant, 0.5) is True
        assert AuditStorage._candidate_meets_overlap(candidate, significant, 0.7) is False

    def test_subtoken_splitting_counts(self) -> None:
        # составной термин карточки (anti-spam) разбивается на под-токены
        candidate = {"title": "record_debt anti-spam", "symptom": "", "solution": ""}
        significant = {"record", "debt", "spam"}
        assert AuditStorage._candidate_meets_overlap(candidate, significant, 1.0) is True

    def test_root_cause_and_symbol_included_in_overlap(self) -> None:
        candidate = {
            "title": "SQL defect",
            "symptom": "slow query",
            "root_cause": "копипаста запросов",
            "solution": "helper",
            "symbol": "ItemStore",
        }
        assert (
            AuditStorage._candidate_meets_overlap(
                candidate, {"копипаста", "itemstore", "helper"}, 1.0
            )
            is True
        )


def test_a03_duplicate_sql_methods_paraphrase_recall(tmp_path: Path) -> None:
    """A03/S5: естественные перефразировки дублирования методов чтения находят посевной долг."""
    a_dir = tmp_path / ".agents" / "audit" / "todo" / "backend"
    a_dir.mkdir(parents=True)
    (a_dir / "b76d.md").write_text(
        "---\n"
        'id: "aud-backend-b76d"\n'
        'title: "Дублирующиеся SQL-запросы"\n'
        "category: backend\n"
        "status: planned\n"
        "priority: important\n"
        "target_file: store.py\n"
        "---\n\n"
        "## Симптом\n"
        "В store.py три одинаковых SQL-запроса дублируются в методах get_by_name, find_prefix и search_any\n\n"
        "## Первопричина\n"
        "Копипаста логики SQL-запросов.\n\n"
        "## Решение\n"
        "Вынести общий helper для выполнения SQL-запросов.\n",
        encoding="utf-8",
    )
    db = tmp_path / "audit_fts.db"
    storage = AuditStorage(db_path=db)
    AuditSynchronizer(storage=storage, audit_dir=tmp_path / ".agents" / "audit").sync_all()

    # Формулировка агента A03-r3 с именами методов и хелпером
    resp_r3 = storage.check_known_debt(
        symptom="Дублирование кода в трёх search-методах, нет общего helper",
        file_path="store.py",
        symbol="ItemStore",
    )
    assert resp_r3.found is True, resp_r3.message
    assert resp_r3.id == "aud-backend-b76d"
    assert resp_r3.is_known_debt is True

    # Формулировка диагностического хода
    resp_diag = storage.check_known_debt(
        symptom="Дублирование кода: get_by_name/find_prefix/search_any — три почти идентичных тела методов",
        file_path="store.py",
    )
    assert resp_diag.found is True, resp_diag.message
    assert resp_diag.id == "aud-backend-b76d"

    # Отрицательный контроль: тот же симптом в другом файле
    resp_other = storage.check_known_debt(
        symptom="Дублирование кода в трёх search-методах, нет общего helper",
        file_path="helpers.py",
    )
    assert resp_other.found is False
    assert "привязаны к другим файлам" in resp_other.message
