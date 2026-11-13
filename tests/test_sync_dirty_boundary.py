"""Ф0 (core-dedup v4): пин границы dirty-предиката abs(mtime) == 1e-4.

Дрейф 2-vs-2 (см. план, Ф4): core `> 1e-4` (sync.py:66 — clean при ровно
1e-4) vs knowledge `>=` (synchronizer.py:163 — dirty) / `<`-clean
(synchronizer.py:253 — dirty при >=) и audit `<` (:188 — dirty при >=).
План выбирает clean при ровно tol — intended-delta для knowledge/audit.

Эти тесты пинят ТЕКУЩЕЕ поведение (pre-Ф4); при миграции Ф4 тест
test_boundary_is_exact_tolerance меняет ожидание в том же коммите.
"""

from __future__ import annotations

from pathlib import Path

from mcp_core.sync import BaseSynchronizer


def test_core_is_file_dirty_boundary_is_clean() -> None:
    """Core: abs == tol ровно → файл считается чистым (строго больше — dirty).

    Проверяется САМА ФУНКЦИЯ на реальном файле (ревью Ф2-4): мутация
    ``> 1e-4`` → ``>= 1e-4`` в sync.py:66 обязана краснить этот тест.
    Техника: st_mtime задаётся точно через ``os.utime`` (мантисса 0.8192 *
    2^-13 — представима без округления), ``stored_mtime = 0.0`` →
    ``abs(stat.st_mtime - 0.0) == 1e-4`` ровно — единственная точка,
    где ``>`` и ``>=`` расходятся.

    Float-нюанс: 1e-4 = 0.8192 * 2^-13 представима точно; при обычных
    mtime (~1.7e9, ulp ≈ 2.4e-7) дельта — всегда кратная ulp, и равенство
    ``delta == tol`` недостижимо — потому тест строит файл с управляемой
    mtime, а не полагается на случайные stat-значения.
    """
    import os
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as tmp:
        tmp.write("x")
        tmp_path = Path(tmp.name)
    try:
        # Дельта ровно tol: st_mtime = 1e-4 (точно, ниже точности обрезки ФС),
        # stored = 0.0. `> 1e-4` → False (clean); мутация `>= 1e-4` → True
        # (dirty) — тест краснеет. Это единственная точка расхождения `>`/`>=`.
        os.utime(tmp_path, (1e-4, 1e-4))
        stat = tmp_path.stat()
        assert stat.st_mtime == 1e-4  # utime-точность подтверждена (иначе тест лжет)
        assert BaseSynchronizer.is_file_dirty(0.0, stat.st_size, tmp_path) is False

        # Дельта существенно выше допуска (0.5 > 1e-4): dirty — инвариант
        # направления (не пинит точную границу, но ловит инверсию знака/логики).
        os.utime(tmp_path, (0.5, 0.5))
        stat = tmp_path.stat()
        assert stat.st_mtime == 0.5
        assert BaseSynchronizer.is_file_dirty(0.0, stat.st_size, tmp_path) is True

        # Дельта существенно ниже допуска: 0.5 vs stored = 0.5 (совпадение)
        # → clean; мутация порога в большую сторону (напр. 1.0) — красная.
        assert BaseSynchronizer.is_file_dirty(0.5, stat.st_size, tmp_path) is False
    finally:
        os.unlink(tmp_path)

    # Дрейф-документация (не функция, а заявленная граница): knowledge/audit
    # сегодня считают >= tol грязными, core — чистым при ровно tol.
    stored, current = 100.0, 100.0 + 1e-4
    assert abs(current - stored) > 1e-4  # фактический float-результат: True


def test_core_is_file_dirty_missing_file_is_dirty() -> None:
    """Отсутствующий файл (OSError) → dirty: переиндексация при потере файла."""
    assert BaseSynchronizer.is_file_dirty(0.0, 0, Path("/nonexistent")) is True


def test_core_is_file_dirty_size_change_is_dirty() -> None:
    """Изменение размера при том же mtime → dirty."""
    import os
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as tmp:
        tmp.write("x")
        tmp_path = Path(tmp.name)
    try:
        st = tmp_path.stat()
        # тот же mtime, другой размер
        assert BaseSynchronizer.is_file_dirty(st.st_mtime, st.st_size + 1, tmp_path) is True
        # идентичные mtime/размер → clean
        assert BaseSynchronizer.is_file_dirty(st.st_mtime, st.st_size, tmp_path) is False
    finally:
        os.unlink(tmp_path)
