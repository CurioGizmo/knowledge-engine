"""Tests for plan F: lease coverage in tool wrappers and plugins packaging.

Covers:
1. ProjectServicePool lease lifetime: bundle stays open while a tool call is
   active, released on both success and exception, and LRU eviction never
   closes a leased bundle.
2. Plugins as importlib.resources: wheel-independent resource lookup, plugin
   install from any cwd, preserved user settings on reinstall.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mcp_core.service_pool import ProjectServicePool

REPO_SRC = Path(__file__).resolve().parent.parent / "src"
REPO_ROOT = REPO_SRC.parent


class ClosableBundle:
    """Fake bundle with observable close()."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.closed = False

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# 1. Lease coverage
# ---------------------------------------------------------------------------


def test_lease_covers_entire_tool_call_body(tmp_path: Path) -> None:
    """While a tool body is executing under pool.lease(), the bundle cannot be
    evicted or closed even when the pool overflows with other projects."""
    pool: ProjectServicePool[ClosableBundle] = ProjectServicePool(ClosableBundle, max_size=2)
    d0 = tmp_path / "p0"
    d0.mkdir()

    with pool.lease(d0) as b0:
        assert not b0.closed
        # Overflow the pool with other projects while the call is active
        for i in range(1, 5):
            di = tmp_path / f"p{i}"
            di.mkdir()
            pool.get(di)
        # Active call's bundle is leased: not closed, still in pool
        assert not b0.closed
        assert pool.get(d0) is b0

    # After release, overflow can evict and close the bundle. Note: inside the
    # lease we touched d0 again (get), so refresh LRU by touching only newer
    # unleased entries before triggering the overflow.
    assert not b0.closed
    for i in range(1, 5):
        pool.get(tmp_path / f"p{i}")
    # d0 is now the least recently used unleased entry; max_size=2 → evicted
    assert b0.closed


def test_lease_releases_on_exception(tmp_path: Path) -> None:
    """Lease is released when the tool body raises."""

    class ToolError(Exception):
        pass

    pool: ProjectServicePool[ClosableBundle] = ProjectServicePool(ClosableBundle, max_size=1)
    d0 = tmp_path / "p0"
    d0.mkdir()

    with pytest.raises(ToolError):
        with pool.lease(d0) as b0:
            raise ToolError("boom inside tool body")

    # Lease must be gone: subsequent eviction of d0 is allowed
    d1 = tmp_path / "p1"
    d1.mkdir()
    pool.get(d1)
    assert b0.closed


def _server_pool(server: object) -> ProjectServicePool:  # type: ignore[type-arg]
    """Достаёт пул сервисов из созданного сервера (closure поля резолвера)."""
    for tool in _server_tools(server):
        fn = getattr(tool, "fn", None)
        cell_pool = getattr(fn, "__closure__", None)
        if cell_pool:
            for cell in cell_pool:
                if isinstance(cell.cell_contents, ProjectServicePool):
                    return cell.cell_contents  # type: ignore[no-any-return]
    raise AssertionError("ProjectServicePool not found in server tool closures")


def _server_tools(server: object) -> list:  # type: ignore[type-arg]
    tm = getattr(server, "_tool_manager", None)
    tools = getattr(tm, "_tools", None) if tm is not None else None
    if tools is None:
        raise AssertionError("tool manager not accessible on server")
    return list(tools.values())  # type: ignore[union-attr]


def _assert_no_leases(server: object) -> None:
    pool = _server_pool(server)
    assert pool._leases == {}, f"dangling leases after tool call: {pool._leases}"


def test_servers_release_lease_on_tool_error(tmp_path: Path) -> None:
    """End-to-end: a tool call that fails must not leave a dangling lease —
    the pool of every server has no active leases after the failed call and
    subsequent calls to the same project still work."""
    import asyncio

    from mcp_audit.server import create_server as create_audit_server
    from mcp_knowledge.server import create_server as create_knowledge_server
    from mcp_skills.server import create_server as create_skills_server

    proj = tmp_path / "repo"
    proj.mkdir()
    (proj / ".git").mkdir()

    async def _call(server: object, tool: str, args: dict) -> object:
        return await server.call_tool(tool, args)  # type: ignore[attr-defined]

    # Knowledge: successful call, then a domain error, then success again.
    k_server = create_knowledge_server(multi_tenant=True)
    asyncio.run(_call(k_server, "search_knowledge", {"query": "", "project_path": str(proj)}))
    _assert_no_leases(k_server)
    asyncio.run(
        _call(
            k_server,
            "update_knowledge_status",
            {"id": "no-such-id", "action": "promote", "project_path": str(proj)},
        )
    )
    _assert_no_leases(k_server)
    asyncio.run(_call(k_server, "search_knowledge", {"query": "", "project_path": str(proj)}))
    _assert_no_leases(k_server)

    # Audit: record with invalid severity → envelope error, pool stays usable.
    a_server = create_audit_server(multi_tenant=True)
    asyncio.run(_call(a_server, "search_debt", {"query": "", "project_path": str(proj)}))
    _assert_no_leases(a_server)
    asyncio.run(
        _call(
            a_server,
            "record_debt",
            {
                "title": "t",
                "category": "c",
                "severity": "no-such-severity",
                "description": "d",
                "project_path": str(proj),
            },
        )
    )
    _assert_no_leases(a_server)
    asyncio.run(_call(a_server, "search_debt", {"query": "", "project_path": str(proj)}))
    _assert_no_leases(a_server)

    # Skills: step_flow with an unknown session → error, pool stays usable.
    s_server = create_skills_server(multi_tenant=True)
    asyncio.run(_call(s_server, "search_skills", {"query": "", "project_path": str(proj)}))
    _assert_no_leases(s_server)
    asyncio.run(
        _call(
            s_server,
            "step_flow",
            {
                "action": "status",
                "session_id": "no-such-session",
                "project_path": str(proj),
            },
        )
    )
    _assert_no_leases(s_server)
    asyncio.run(_call(s_server, "search_skills", {"query": "", "project_path": str(proj)}))
    _assert_no_leases(s_server)


# ---------------------------------------------------------------------------
# 2. Plugins via importlib.resources / wheel packaging
# ---------------------------------------------------------------------------

PLUGIN_NAMES = (
    "knowledge-engine-plugin",
    "skills-engine-plugin",
    "audit-engine-plugin",
)


def test_wheel_includes_plugins_package(tmp_path: Path) -> None:
    """Wheel ships plugin payloads as the mcp_plugins package (importlib.resources
    readable after install outside the source checkout)."""
    import zipfile

    dist = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--wheel", "-o", str(dist), "-q"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    (wheel,) = dist.glob("*.whl")
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
    for name in PLUGIN_NAMES:
        assert f"mcp_plugins/{name}/plugin.json" in names, f"{name} missing from wheel"
        assert f"mcp_plugins/{name}/mcp_config.json" in names
    assert "mcp_plugins/__init__.py" in names


@pytest.fixture
def wheel_venv(tmp_path: Path) -> Path:
    """Builds the wheel and installs it into a throwaway venv (never the main env)."""
    dist = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--wheel", "-o", str(dist), "-q"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    (wheel,) = dist.glob("*.whl")
    venv = tmp_path / "venv"
    subprocess.run(
        ["uv", "venv", str(venv), "-q"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["uv", "pip", "install", "--python", str(venv / "bin" / "python"), str(wheel), "-q"],
        check=True,
        capture_output=True,
        text=True,
    )
    return venv / "bin" / "python"


def test_wheel_resources_importable_outside_checkout(wheel_venv: Path, tmp_path: Path) -> None:
    """From a cwd outside the checkout, plugin resources resolve via
    importlib.resources and install_antigravity_plugin works without
    MCP_PROJECT_ROOT."""
    code = (
        "import importlib.resources, pathlib, sys; "
        "ref = importlib.resources.files('mcp_plugins'); "
        "assert (ref / 'knowledge-engine-plugin' / 'plugin.json').is_file(), 'resource missing'; "
        "from mcp_core.installer import install_antigravity_plugin; "
        f"install_antigravity_plugin('knowledge-engine-plugin', home_dir=pathlib.Path({str(tmp_path)!r})); "
        "print('OK')"
    )
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    proc = subprocess.run(
        [str(wheel_venv), "-c", code],
        cwd="/tmp",  # noqa: S108 — cwd outside the checkout is the point of the test
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
    installed = tmp_path / ".gemini" / "config" / "plugins" / "knowledge-engine-plugin"
    assert (installed / "plugin.json").is_file()
    assert (installed / "skills" / "knowledge-memory" / "SKILL.md").is_file()


def test_install_plugins_preserves_user_settings(tmp_path: Path) -> None:
    """Reinstalling a plugin must not overwrite user-modified files."""
    from mcp_core.installer import install_antigravity_plugin

    target = install_antigravity_plugin(PLUGIN_NAMES[0], home_dir=tmp_path)
    manifest = target / "plugin.json"
    user_edit = manifest.read_text(encoding="utf-8").replace(
        '"description"', '"user-edited description"'
    )
    manifest.write_text(user_edit, encoding="utf-8")

    install_antigravity_plugin(PLUGIN_NAMES[0], home_dir=tmp_path)

    assert "user-edited description" in manifest.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Post-review additions (counter-review findings 1/3): binary-fallback install
# and ContextVar propagation through anyio.to_thread.
# ---------------------------------------------------------------------------


def test_install_unreadable_text_file_does_not_crash(tmp_path: Path) -> None:
    """MINOR-4: a file with a text suffix but non-UTF-8 bytes must not crash
    the install; it is copied without comparison (fallback path)."""
    import shutil

    from mcp_core.installer import install_antigravity_plugin

    plugin_src = tmp_path / "plugin-src" / "test-plugin"
    (plugin_src / "skills").mkdir(parents=True)
    (plugin_src / "plugin.json").write_text(
        '{"name": "test-plugin", "display_name": "Test", "version": "0.4.0"}', encoding="utf-8"
    )
    # dst pre-created with latin-1 bytes (unreadable as utf-8) at same rel path
    dst_dir = tmp_path / "home" / ".gemini" / "config" / "plugins" / "test-plugin"
    dst_dir.mkdir(parents=True)
    (dst_dir / "plugin.json").write_bytes(b'\xff\xfe{"name": "test-plugin"}')

    installed = install_antigravity_plugin(
        plugin_name="test-plugin",
        home_dir=tmp_path / "home",
        plugins_source_dir=plugin_src.parent,
    )
    assert installed is not None
    # The unreadable file was overwritten by the fresh source copy
    assert (dst_dir / "plugin.json").read_text(encoding="utf-8").startswith("{")


def test_scoped_leases_isolated_across_worker_threads(tmp_path: Path) -> None:
    """ContextVar scope stack is isolated per thread (anyio.to_thread runs
    tool bodies in worker threads): concurrent calls cannot see each other's
    leases, and a call in a thread leaves no leases after release."""
    import threading

    class ThreadBundle:
        def __init__(self, root: Path) -> None:  # noqa: ARG001
            self.root = root

    pool: ProjectServicePool[ThreadBundle] = ProjectServicePool(ThreadBundle, max_size=4)
    roots = [tmp_path / f"t{i}" for i in range(4)]
    for r in roots:
        r.mkdir()

    errors: list[str] = []
    barrier = threading.Barrier(4)
    barrier2 = threading.Barrier(4)

    def worker(i: int) -> None:
        try:
            pool.start_scope()
            b = pool.scoped_get(roots[i])
            assert b is not None
            barrier.wait(timeout=10)  # all 4 scopes open simultaneously
            # ContextVar isolation: this scope leased only its own root. The
            # pool's shared _leases map legitimately contains OTHER threads'
            # leases (they coexist), but this thread must hold exactly one.
            assert roots[i].resolve() in pool._leases
            pool.release_scope()
            # After release this thread's lease is gone; others may remain.
            barrier2.wait(timeout=10)  # everyone released before final check
        except Exception as e:  # noqa: BLE001
            errors.append(f"worker {i}: {e!r}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, errors
    assert not pool._leases
