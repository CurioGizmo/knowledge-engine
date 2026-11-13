"""Pool-level unit tests for the scoped lease API (Plan F, review wave 2).

Covers ``ProjectServicePool.scoped_get`` / ``release_scope``:
- lease coverage for the whole scope, repeated calls with the same root do not
  inflate the ref-count;
- scope nesting: an inner scope release never releases the outer scope's lease;
- exception safety: release_scope releases leases taken before the failure;
- bundle protection from eviction while a scope is active.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_core.service_pool import ProjectServicePool


class ClosableBundle:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _clean_scope_stack():
    from mcp_core import service_pool

    token = service_pool._scope_stack.set(None)
    yield
    service_pool._scope_stack.reset(token)


def test_scoped_get_no_active_scope_raises(tmp_path: Path) -> None:
    """Defect (MAJOR-2 root): scoped_get outside a tool-call scope must fail
    loudly instead of silently taking an unreleasable lease."""
    pool: ProjectServicePool[ClosableBundle] = ProjectServicePool(ClosableBundle, max_size=2)
    d0 = tmp_path / "p0"
    d0.mkdir()

    with pytest.raises(RuntimeError, match="requires an active tool-call scope"):
        pool.scoped_get(d0)
    # Pool untouched: no bundle created, no lease registered.
    assert pool._leases == {}
    assert d0.resolve() not in pool._pool


def test_scoped_get_idempotent_per_root(tmp_path: Path) -> None:
    """Repeating scoped_get with the same root within one scope must not
    inflate the lease ref-count (resolver + telemetry path)."""
    pool: ProjectServicePool[ClosableBundle] = ProjectServicePool(ClosableBundle, max_size=2)
    d0 = tmp_path / "p0"
    d0.mkdir()

    pool.start_scope()
    try:
        b1 = pool.scoped_get(d0)
        b2 = pool.scoped_get(d0)
        assert b1 is b2
        assert pool._leases == {d0.resolve(): 1}
    finally:
        pool.release_scope()

    assert pool._leases == {}


def test_release_scope_nested_outer_lease_kept(tmp_path: Path) -> None:
    """Nested scopes: releasing the inner scope releases only its own lease;
    the outer scope's lease stays active."""
    pool: ProjectServicePool[ClosableBundle] = ProjectServicePool(ClosableBundle, max_size=2)
    d0 = tmp_path / "p0"
    d0.mkdir()

    pool.start_scope()
    try:
        pool.scoped_get(d0)
        assert pool._leases == {d0.resolve(): 1}

        pool.start_scope()
        try:
            # Inner scope takes its own (ref-counted) lease.
            pool.scoped_get(d0)
            assert pool._leases == {d0.resolve(): 2}
        finally:
            pool.release_scope()

        # Only the inner lease is released; the outer one is kept.
        assert pool._leases == {d0.resolve(): 1}
    finally:
        pool.release_scope()

    assert pool._leases == {}


def test_scoped_get_unknown_root_isolated_per_scope(tmp_path: Path) -> None:
    """Two sibling scopes with different roots do not interfere."""
    pool: ProjectServicePool[ClosableBundle] = ProjectServicePool(ClosableBundle, max_size=4)
    d0 = tmp_path / "p0"
    d1 = tmp_path / "p1"
    d0.mkdir()
    d1.mkdir()

    pool.start_scope()
    try:
        pool.scoped_get(d0)
        pool.start_scope()
        try:
            pool.scoped_get(d1)
            assert pool._leases == {d0.resolve(): 1, d1.resolve(): 1}
        finally:
            pool.release_scope()
        assert pool._leases == {d0.resolve(): 1}
    finally:
        pool.release_scope()


def test_scoped_get_bundle_protected_from_eviction(tmp_path: Path) -> None:
    """While the scope is active, the bundle cannot be evicted/closed even
    when the pool overflows with other projects (capacity=1)."""
    pool: ProjectServicePool[ClosableBundle] = ProjectServicePool(ClosableBundle, max_size=1)
    d0 = tmp_path / "p0"
    d0.mkdir()

    pool.start_scope()
    try:
        b0 = pool.scoped_get(d0)
        for i in range(1, 5):
            di = tmp_path / f"p{i}"
            di.mkdir()
            pool.get(di)
        assert not b0.closed
        assert pool.scoped_get(d0) is b0
    finally:
        pool.release_scope()

    # After release, overflow can evict and close the bundle.
    for i in range(1, 5):
        pool.get(tmp_path / f"p{i}")
    assert b0.closed


def test_release_scope_on_exception(tmp_path: Path) -> None:
    """A failure inside the tool body must not leave a dangling lease —
    release_scope is the single finally-side cleanup point."""
    pool: ProjectServicePool[ClosableBundle] = ProjectServicePool(ClosableBundle, max_size=1)
    d0 = tmp_path / "p0"
    d0.mkdir()

    class ToolError(Exception):
        pass

    with pytest.raises(ToolError):
        pool.start_scope()
        try:
            pool.scoped_get(d0)
            raise ToolError("boom inside tool body")
        finally:
            pool.release_scope()

    assert pool._leases == {}
    # Subsequent eviction of d0 is allowed again.
    d1 = tmp_path / "p1"
    d1.mkdir()
    pool.get(d1)
    assert pool._pool[d1.resolve()].closed is False
    assert len(pool._pool) == 1
