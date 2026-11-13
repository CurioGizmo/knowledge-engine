"""Тесты Шага 5 (docs/skills-correctness-and-search-plan.md): LRU-кэш ProjectContext.

Группы:
- L1  — 33 проекта -> размер кэша ровно 32, самый старый вытеснен;
- L2  — cache hit перемещает запись в конец LRU (не вытесняется);
- L3  — смена сигнатуры (env / манифесты / маркеры) заменяет запись без
        раздувания размера кэша для проекта;
- L4  — потокобезопасный доступ при конкурентных вызовах.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from mcp_skills import project_context as pc_module

_CACHE_MAX = pc_module._CACHE_MAX


@pytest.fixture(autouse=True)
def _isolated_context_cache():
    """Каждый тест стартует с чистым LRU-кэшем ProjectContext."""
    pc_module._CONTEXT_CACHE.clear()
    yield
    pc_module._CONTEXT_CACHE.clear()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    """Детерминированное окружение: MCP_ACTIVE_SERVERS не задан."""
    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)


def _make_project(base: Path, idx: int) -> Path:
    """tmp-проект с уникальным pyproject.toml (уникальная сигнатура)."""
    root = base / f"proj-{idx:03d}"
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "proj-{idx}"\nversion = "0.{idx}.0"\n', encoding="utf-8"
    )
    return root


def _cache_keys() -> list[str]:
    return list(pc_module._CONTEXT_CACHE.keys())


# ---------- L1. Лимит 32, вытеснение самого старого ----------


def test_l1_eviction_after_33_projects(tmp_path: Path) -> None:
    """L1: 33 проекта -> в кэше ровно 32 записи, самый старый вытеснен."""
    roots = [_make_project(tmp_path, i) for i in range(33)]
    for root in roots:
        pc_module.get_project_context(root)

    assert len(pc_module._CONTEXT_CACHE) == _CACHE_MAX == 32
    keys = _cache_keys()
    # Вытеснен самый старый (proj-000): его ключа в кэше нет.
    assert str(roots[0].resolve()) not in keys
    # Все остальные (включая самый свежий proj-032) — присутствуют.
    for root in roots[1:]:
        assert str(root.resolve()) in keys
    # Порядок LRU: следующий за вытесненным — наименее недавно использованный.
    assert keys[0] == str(roots[1].resolve())


def test_l1_project_root_keeps_unresolved_path(tmp_path: Path) -> None:
    """L1b: ProjectContext.root хранит исходный root (без resolve)."""
    root = _make_project(tmp_path, 0)
    ctx = pc_module.get_project_context(root)
    assert ctx.root == root
    assert ctx.root == tmp_path / "proj-000"


# ---------- L2. Cache hit перемещает запись в конец ----------


def test_l2_hit_refreshes_lru_position(tmp_path: Path) -> None:
    """L2: обращение к старой записи перемещает её в конец — она не
    вытесняется следующим добавлением, вытесняется следующая по LRU."""
    roots = [_make_project(tmp_path, i) for i in range(33)]
    for root in roots[:32]:
        pc_module.get_project_context(root)
    assert len(pc_module._CONTEXT_CACHE) == 32

    # Cache hit по самой старой записи (proj-000) — перемещается в конец.
    ctx_before = pc_module.get_project_context(roots[0])
    assert _cache_keys()[-1] == str(roots[0].resolve())

    # Новое добавление вытесняет НЕ proj-000, а proj-001 (новый LRU-голова).
    pc_module.get_project_context(roots[32])
    assert len(pc_module._CONTEXT_CACHE) == 32
    keys = _cache_keys()
    assert str(roots[0].resolve()) in keys
    assert str(roots[1].resolve()) not in keys
    assert keys[-1] == str(roots[32].resolve())
    # Повторный hit возвращает тот же объект (без пересчёта).
    assert pc_module.get_project_context(roots[0]) is ctx_before


# ---------- L3. Инвалидация по сигнатуре без раздувания кэша ----------


def test_l3_env_change_replaces_entry_in_place(tmp_path: Path) -> None:
    """L3a: смена MCP_ACTIVE_SERVERS заменяет запись по тому же ключу."""
    root = _make_project(tmp_path, 0)
    ctx_mcp_unknown = pc_module.get_project_context(root)
    assert ctx_mcp_unknown.mode is None

    os.environ["MCP_ACTIVE_SERVERS"] = "skills-engine"
    try:
        ctx_mcp = pc_module.get_project_context(root)
        assert ctx_mcp is not ctx_mcp_unknown
        assert ctx_mcp.mode == "mcp"
        # Кэш не разросся: один проект — одна запись.
        assert len(pc_module._CONTEXT_CACHE) == 1
        assert _cache_keys() == [str(root.resolve())]
    finally:
        del os.environ["MCP_ACTIVE_SERVERS"]


def test_l3_manifest_change_replaces_entry_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L3b: правка манифеста (pyproject.toml) пересчитывает стек на месте."""
    root = _make_project(tmp_path, 0)
    ctx_before = pc_module.get_project_context(root)
    assert "fastapi" not in ctx_before.stack

    manifest = root / "pyproject.toml"
    manifest.write_text(
        '[project]\nname = "proj-0"\ndependencies = ["fastapi>=0.100"]\n', encoding="utf-8"
    )
    # Явный сдвиг mtime: страховка от гранулярности ФС.
    st = manifest.stat()
    os.utime(manifest, ns=(st.st_atime_ns + 10**6, st.st_mtime_ns + 10**9))

    ctx_after = pc_module.get_project_context(root)
    assert ctx_after is not ctx_before
    assert "fastapi" in ctx_after.stack
    assert len(pc_module._CONTEXT_CACHE) == 1


def test_l3_mode_marker_change_replaces_entry_in_place(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """L3c: изменение MCP_ACTIVE_SERVERS меняет режим — запись заменяется."""
    monkeypatch.delenv("MCP_ACTIVE_SERVERS", raising=False)
    root = _make_project(tmp_path, 0)
    ctx_before = pc_module.get_project_context(root)
    assert ctx_before.mode is None

    monkeypatch.setenv("MCP_ACTIVE_SERVERS", "skills-engine")
    ctx_after = pc_module.get_project_context(root)
    assert ctx_after is not ctx_before
    assert ctx_after.mode == "mcp"
    assert len(pc_module._CONTEXT_CACHE) == 1


def test_l3_signature_unchanged_same_object(tmp_path: Path) -> None:
    """L3d: без изменений сигнатуры повторный вызов возвращает тот же объект."""
    root = _make_project(tmp_path, 0)
    ctx1 = pc_module.get_project_context(root)
    ctx2 = pc_module.get_project_context(root)
    assert ctx1 is ctx2


# ---------- L4. Потокобезопасность ----------


def test_l4_concurrent_access(tmp_path: Path) -> None:
    """L4: конкурентные вызовы из многих потоков — без исключений,
    согласованный контекст, кэш в рамках лимита."""
    n_projects = 8
    n_threads = 16
    roots = [_make_project(tmp_path, i) for i in range(n_projects)]

    errors: list[BaseException] = []
    results: dict[int, list[pc_module.ProjectContext]] = {i: [] for i in range(n_threads)}
    barrier = threading.Barrier(n_threads)

    def worker(tid: int) -> None:
        try:
            barrier.wait()
            for _ in range(25):
                root = roots[(tid * 7 + _) % n_projects]
                results[tid].append(pc_module.get_project_context(root))
        except BaseException as exc:  # noqa: BLE001 - собираем всё
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == []
    assert all(not t.is_alive() for t in threads)

    # Все контексты одного корня согласованы по содержимому.
    all_ctx = [ctx for ctxs in results.values() for ctx in ctxs]
    assert len(all_ctx) == n_threads * 25
    by_root: dict[str, set[tuple]] = {}
    for ctx in all_ctx:
        by_root.setdefault(str(ctx.root), set()).add((ctx.component, tuple(ctx.stack), ctx.mode))
    assert len(by_root) == n_projects
    for attrs in by_root.values():
        assert len(attrs) == 1, "конкурентные вызовы дали разные контексты для одного корня"

    # Кэш — в рамках лимита, по одному ключу на проект.
    assert len(pc_module._CONTEXT_CACHE) == n_projects
    assert set(_cache_keys()) == {str(r.resolve()) for r in roots}


def test_l4_concurrent_env_flip(tmp_path: Path) -> None:
    """L4b: гонка «env переключается пока идут вызовы» — без падений,
    контекст всегда соответствует одной из сигнатур (не смесь)."""
    root = _make_project(tmp_path, 0)
    stop = threading.Event()
    errors: list[BaseException] = []

    def flipper() -> None:
        try:
            while not stop.is_set():
                os.environ["MCP_ACTIVE_SERVERS"] = "skills-engine"
                time.sleep(0.001)
                os.environ["MCP_ACTIVE_SERVERS"] = ""
                time.sleep(0.001)
                del os.environ["MCP_ACTIVE_SERVERS"]
                time.sleep(0.001)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            os.environ.pop("MCP_ACTIVE_SERVERS", None)

    def caller() -> None:
        try:
            for _ in range(50):
                ctx = pc_module.get_project_context(root)
                # Допустимые состояния: unknown / mcp / offline.
                assert ctx.mode in (None, "mcp", "offline")
                assert ctx.root == root
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    try:
        flipper_t = threading.Thread(target=flipper)
        callers = [threading.Thread(target=caller) for _ in range(4)]
        flipper_t.start()
        for t in callers:
            t.start()
        for t in callers:
            t.join(timeout=60)
        stop.set()
        flipper_t.join(timeout=10)

        assert errors == []
        assert all(not t.is_alive() for t in callers)
        assert not flipper_t.is_alive()
        # Один проект — одна запись в кэше, сколько бы env ни менялся.
        assert len(pc_module._CONTEXT_CACHE) == 1
    finally:
        stop.set()
        os.environ.pop("MCP_ACTIVE_SERVERS", None)
