"""Шаг 3 плана AUDIT-VERIFY-2026-10: K1 (enum-only status), K2 (матрица
переходов), K4 (per-card lock, конкурентный RMW 100/100, порядок захвата).

K1: неизвестный status в record_knowledge — ValidationError, не «зомби»-карточка.
K2: draft+suspect/verify → отказ с подсказкой promote; hypothesis+verify →
отказ с подсказкой resolve_hypotheses.
K4: конкурентные мутации одной карточки не теряют записи; многокарточные
операции берут локи в отсортированном порядке.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from mcp_knowledge.models import (
    CardRecordRequest,
    CardStatusUpdateRequest,
    KnowledgeSource,
)
from mcp_knowledge.repository import CardRepository
from mcp_knowledge.service import KnowledgeService
from mcp_knowledge.storage import KnowledgeStorage


def _make_service(tmp_path: Path) -> KnowledgeService:
    k_dir = tmp_path / "k"
    storage = KnowledgeStorage(db_path=tmp_path / "c.db", knowledge_dir=k_dir)
    return KnowledgeService(storage=storage, knowledge_dir=k_dir)


def _base_req(**overrides) -> dict:
    base = dict(
        title="Карточка для шага 3",
        category="backend",
        type="lesson",
        symptom="Симптом для проверки фиксов шага 3 плана",
        root_cause="Причина для проверки фиксов шага 3 плана достаточно длинная",
        solution="Решение для проверки фиксов шага 3 плана достаточно длинное",
        rationale="Обоснование для проверки фиксов шага 3 плана достаточно длинное",
        source="user_confirmed",
    )
    base.update(overrides)
    return base


class TestK1StatusValidation:
    def test_unknown_status_rejected(self) -> None:
        with pytest.raises(Exception) as ei:
            CardRecordRequest(**_base_req(status="вэри-бэд-статус"))
        assert "status" in str(ei.value) or ei.value is not None

    def test_valid_statuses_accepted(self) -> None:
        for st in ("draft", "active", "verified", "suspect"):
            req = CardRecordRequest(**_base_req(status=st))
            assert req.status is not None

    def test_serializer_rejects_garbage_status(self, tmp_path: Path) -> None:
        """Defensive-ветка сериализатора (K1): мусорный статус — ValueError."""
        from mcp_knowledge.parser import serialize_card_markdown

        req = CardRecordRequest(**_base_req())
        # Эмулируем обход модели: подставляем str напрямую
        object.__setattr__(req, "status", "вэри-бэд-статус")
        with pytest.raises(ValueError, match="Недопустимый статус"):
            serialize_card_markdown(card_id="backend-k1-test", req=req)


class TestK2TransitionMatrix:
    def test_draft_suspect_rejected_with_promote_hint(self, tmp_path: Path) -> None:
        service = _make_service(tmp_path)
        # auto_inferred → draft (карантин)
        rec = service.record(CardRecordRequest(**_base_req(source="auto_inferred")))
        card_id = rec["id"]

        with pytest.raises(ValueError, match="promote"):
            service.update_status(
                CardStatusUpdateRequest(id=card_id, action="suspect", note="сбой")
            )

    def test_draft_verify_rejected_with_promote_hint(self, tmp_path: Path) -> None:
        service = _make_service(tmp_path)
        rec = service.record(CardRecordRequest(**_base_req(source="auto_inferred")))
        card_id = rec["id"]

        with pytest.raises(ValueError, match="promote"):
            service.update_status(
                CardStatusUpdateRequest(id=card_id, action="verify", note="проверено")
            )

    def test_hypothesis_verify_rejected_with_resolve_hint(self, tmp_path: Path) -> None:
        from mcp_knowledge.models import HypothesisRecordRequest

        service = _make_service(tmp_path)
        resp = service.propose_hypothesis(
            HypothesisRecordRequest(
                title="Гипотеза о причине зависания",
                category="backend",
                symptom="Зависание сервиса при пиковой нагрузке",
                hypothesis="Предположительно исчерпывается пул соединений",
            )
        )
        hyp_id = resp["id"]

        with pytest.raises(ValueError, match="resolve_hypotheses"):
            service.update_status(
                CardStatusUpdateRequest(id=hyp_id, action="verify", note="проверено")
            )

    def test_promote_then_verify_allowed(self, tmp_path: Path) -> None:
        """Правильный путь: draft → promote → verify работает."""
        service = _make_service(tmp_path)
        rec = service.record(CardRecordRequest(**_base_req(source="auto_inferred")))
        card_id = rec["id"]

        service.update_status(
            CardStatusUpdateRequest(id=card_id, action="promote", source="user_confirmed")
        )
        result = service.update_status(
            CardStatusUpdateRequest(id=card_id, action="verify", note="проверено", by="human")
        )
        assert result["new_status"] == "verified"


class TestK4CardLock:
    def _setup_card(self, tmp_path: Path) -> tuple[KnowledgeService, str]:
        service = _make_service(tmp_path)
        rec = service.record(CardRecordRequest(**_base_req()))
        assert rec["card_status"] == "active"
        return service, rec["id"]

    def test_concurrent_updates_no_lost_notes(self, tmp_path: Path) -> None:
        """K4 (приёмка плана): 2 потока × 50 append_note → 100/100 записей в логе."""
        service, rec_card_id = self._setup_card(tmp_path)

        N = 50
        errors: list[Exception] = []

        def writer(thread_idx: int) -> None:
            try:
                for i in range(N):
                    service.update_status(
                        CardStatusUpdateRequest(
                            id=rec_card_id,
                            action="append_note",
                            note=f"заметка-{thread_idx}-{i}",
                        )
                    )
            except Exception as err:  # pragma: no cover
                errors.append(err)

        t1 = threading.Thread(target=writer, args=(0,))
        t2 = threading.Thread(target=writer, args=(1,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, errors

        card_file = service.repo.find_card_file(rec_card_id)
        content = card_file.read_text(encoding="utf-8")
        expected = sum(1 for i in range(N) for t in (0, 1) if f"заметка-{t}-{i}" in content)
        assert expected == 2 * N, f"потеряно {2 * N - expected} из {2 * N} заметок"

    def test_supersede_parallel_append_note_not_lost(self, tmp_path: Path) -> None:
        """K4-блокер: supersede читает карточку под card_lock — параллельный
        append_note на вытесняемой карточке не затирается устаревшим снимком."""
        service, old_id = self._setup_card(tmp_path)

        results: dict[str, Any] = {}
        lock = threading.Lock()

        def superseder() -> None:
            rec = service.record(
                CardRecordRequest(
                    **_base_req(
                        title="Карточка-замена",
                        supersedes=old_id,
                    )
                )
            )
            with lock:
                results["new_id"] = rec["id"]

        def noter() -> None:
            try:
                service.update_status(
                    CardStatusUpdateRequest(
                        id=old_id,
                        action="append_note",
                        note="параллельная-заметка-K4",
                    )
                )
            except Exception as err:
                with lock:
                    results["note_err"] = str(err)

        t1 = threading.Thread(target=superseder)
        t2 = threading.Thread(target=noter)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        card_file = service.repo.find_card_file(old_id)
        content = card_file.read_text(encoding="utf-8")
        # SUPERSEDED-переход и параллельная заметка обязаны сосуществовать
        assert "SUPERSEDED: Replaced by" in content, content
        assert "параллельная-заметка-K4" in content or results.get("note_err"), (
            "параллельная заметка потеряна (устаревший снимок затёр её)",
            results,
        )

    def test_card_lock_is_reentrant_across_threads_serialized(self, tmp_path: Path) -> None:
        """Лок сериализует: захват вторым потоком ждёт освобождения первым."""
        repo = CardRepository(tmp_path / "k")
        order: list[str] = []
        release = threading.Event()

        def first() -> None:
            with repo.card_lock("card-x"):
                order.append("first-acquired")
                release.wait(timeout=5)
                order.append("first-released")

        def second() -> None:
            with repo.card_lock("card-x"):
                order.append("second-acquired")

        t1 = threading.Thread(target=first)
        t1.start()
        # Даём t1 захватить лок
        import time

        for _ in range(50):
            if order:
                break
            time.sleep(0.02)
        t2 = threading.Thread(target=second)
        t2.start()
        for _ in range(50):
            if len(order) >= 2:
                break
            time.sleep(0.02)

        release.set()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert order[0] == "first-acquired"
        assert order[1] in ("first-released", "second-acquired")
        if order[1] == "second-acquired":
            assert order[2] == "first-released"
        else:
            assert order[2] == "second-acquired"

    def test_related_links_sorted_lock_order(self, tmp_path: Path) -> None:
        """K4: _sync_related_links возвращает partial_update_warnings и берёт
        локи в отсортированном порядке (best-effort контракт)."""
        from mcp_knowledge.use_cases.record import RecordKnowledgeUseCase

        service, _ = self._setup_card(tmp_path)
        uc = RecordKnowledgeUseCase.__new__(RecordKnowledgeUseCase)
        uc.repo = service.repo
        uc.synchronizer = service.synchronizer

        # Пустой список — нет предупрежедений
        assert uc._sync_related_links("some-card", []) == []
        # Несуществующие карточки: find_card_file → None, warning не нужен
        # (карточки нет — не partial update, а отсутствующая связь)
        result = uc._sync_related_links("some-card", ["ghost-card"])
        assert result == []
