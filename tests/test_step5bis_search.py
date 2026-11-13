"""Шаг 5бис плана AUDIT-VERIFY-2026-10: поисковые S-фиксы.

1. Морфоканал fuzzy (mcp_core/fuzzy.py): русские флексии длинных слов
   (зависание~зависания) матчатся при ослабленном edit-distance (lev<=3)
   и высоком Jaccard (>=0.55); ложные пары (cat_keyword~dog_keyword)
   отфильтрованы.
2. Морфо-гейт check_known_debt (mcp_audit/storage.py): overlap значимых
   токенов теперь по морфо-основам (stem_russian + англ. plural) —
   «зависание» в симптоме матчится «зависания» в карточке.
"""

from __future__ import annotations

from pathlib import Path

from mcp_core.fuzzy import jaccard_word_best
from mcp_audit.storage import _morpho_normalize, _significant_tokens


def test_fuzzy_morphology_channel_russian_flexions() -> None:
    """5бис-1: русские флексии длинных слов матчатся через fuzzy-канал."""
    assert jaccard_word_best("зависание", "зависания файла") >= 0.5
    assert jaccard_word_best("блокировку", "блокировки таблиц") >= 0.5


def test_fuzzy_morphology_channel_no_false_positives() -> None:
    """E-фаза регрессий: ложные пары не проходят даже с морфоканалом."""
    assert jaccard_word_best("cat_keyword", "dog_keyword") < 0.2857
    assert jaccard_word_best("added", "add") < 0.2857


def test_fuzzy_morphology_channel_preserves_typos() -> None:
    """Опечатки (отлдка~отладка) по-прежнему матчатся строгим гейтом."""
    assert jaccard_word_best("отлдка", "отладка") >= 0.2857


def test_morpho_normalize_russian_flexions() -> None:
    """5бис-2: морфо-нормализация сводит флексии к общей основе."""
    assert _morpho_normalize("зависание") == _morpho_normalize("зависания")
    assert _morpho_normalize("миграция") == _morpho_normalize("миграции")


def test_morpho_normalize_english_plurals() -> None:
    """A15: английские plural сводятся к единственной форме."""
    assert _morpho_normalize("errors") == _morpho_normalize("error")
    assert _morpho_normalize("processes") == _morpho_normalize("process")


def test_significant_tokens_unchanged_contract() -> None:
    """Контракт _significant_tokens не сломан морфо-дополнением."""
    tokens = _significant_tokens("метод > 50 строк в handler")
    assert "метод" in tokens
    assert "handler" in tokens
    # стоп-слова отфильтрованы
    assert "50" not in tokens  # длина < 4


def test_check_known_debt_morphology_e2e(tmp_path: Path) -> None:
    """5бис-2 (A03-класс): симптом в ед.ч. находит карточку во мн.ч."""
    from mcp_audit.synchronizer import AuditSynchronizer
    from mcp_audit.storage import AuditStorage

    audit_dir = tmp_path / ".agents" / "audit"
    storage = AuditStorage(tmp_path / "cache.db")
    sync = AuditSynchronizer(storage, audit_dir)

    card = """---
id: aud-backend-morpho1
title: Зависание процесса при повторном запуске
category: backend
priority: important
autonomy: high
complexity: M
status: completed
created_at: '2026-10-28'
---

# aud-backend-morpho1

## ❌ Симптом

Наблюдаются зависания процесса при повторном запуске тестов.

## 🔍 Техническая первопричина

Причина.

## 🛠 Предлагаемое решение

Решение.
"""
    todo = audit_dir / "todo" / "backend"
    todo.mkdir(parents=True, exist_ok=True)
    (todo / "aud-backend-morpho1.md").write_text(card, encoding="utf-8")
    sync.sync()

    resp = storage.check_known_debt("зависание процесса при повторном запуске")
    assert resp.found, resp
    assert resp.id == "aud-backend-morpho1"
