"""The recorder must preserve transport bytes, including errors and concurrent IDs."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_proxy_preserves_real_packets_and_writes_complete_trace(tmp_path: Path) -> None:
    server = tmp_path / "echo_server.py"
    server.write_text(
        "import sys\nfor line in sys.stdin.buffer:\n sys.stdout.buffer.write(line); sys.stdout.buffer.flush()\n"
    )
    trace = tmp_path / "trace.jsonl"
    packets = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "load_skill", "arguments": {"name": "пример"}},
        },
        {
            "jsonrpc": "2.0",
            "id": "2",
            "result": {"error": True, "reason": "ASSEMBLY_BUDGET_EXCEEDED"},
        },
    ]
    raw = b"".join((json.dumps(packet, ensure_ascii=False) + "\n").encode() for packet in packets)
    proxy = Path(__file__).resolve().parents[1] / "scripts" / "trace_mcp_stdio.py"
    result = subprocess.run(
        [sys.executable, str(proxy), "--output", str(trace), "--", sys.executable, str(server)],
        input=raw,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == raw
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    assert [row["seq"] for row in rows] == list(range(1, len(rows) + 1))
    assert rows[0]["event"] == "transport.start" and rows[-1]["event"] == "transport.end"
    for direction in ("client_to_server", "server_to_client"):
        assert [row["message"] for row in rows if row.get("direction") == direction] == packets
    # A rerun cannot silently append to, or overwrite, an earlier session.
    again = subprocess.run(
        [sys.executable, str(proxy), "--output", str(trace), "--", sys.executable, str(server)],
        input=b"",
        capture_output=True,
        timeout=10,
    )
    assert again.returncode != 0
    assert len(trace.read_text().splitlines()) == len(rows)


async def _exercise_real_mcp(tmp_path: Path) -> Path:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    root = Path(__file__).resolve().parents[1]
    catalog = tmp_path / "catalog"
    skill = catalog / "skills" / "example"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: example\ntitle: Example\n---\n# Required core\nPreserve the invariant.\n"
    )
    (tmp_path / "AGENTS.md").write_text("Local test project.")
    trace = tmp_path / "real-mcp.jsonl"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            str(root / "scripts" / "trace_mcp_stdio.py"),
            "--output",
            str(trace),
            "--",
            sys.executable,
            "-m",
            "mcp_skills.server",
            "--project-root",
            str(tmp_path),
            "--skills-dir",
            str(catalog),
            "--db-path",
            str(tmp_path / "index.db"),
        ],
        env={"PYTHONPATH": str(root / "src")},
    )
    async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
        await session.initialize()
        listed = await session.list_tools()
        assert {tool.name for tool in listed.tools} == {
            "search_skills",
            "load_skill",
            "create_skill",
            "get_help",
            "step_flow",
        }
        loaded = await session.call_tool("load_skill", {"name": "example"})
        assert "Preserve the invariant" in loaded.content[0].text
        rejected = await session.call_tool("load_skill", {"name": "missing"})
        assert json.loads(rejected.content[0].text)["reason"] == "SKILL_NOT_FOUND"
    return trace


def test_proxy_records_real_mcp_requests_and_domain_errors(tmp_path: Path) -> None:
    import asyncio

    trace = asyncio.run(_exercise_real_mcp(tmp_path))
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    requests = {
        row["message"]["id"]: row["message"]
        for row in rows
        if row.get("direction") == "client_to_server" and "id" in row["message"]
    }
    replies = {
        row["message"]["id"]: row["message"]
        for row in rows
        if row.get("direction") == "server_to_client" and "id" in row["message"]
    }
    calls = [request for request in requests.values() if request.get("method") == "tools/call"]
    assert len(calls) == 2
    assert all(request["id"] in replies for request in calls)
    missing = next(
        request for request in calls if request["params"]["arguments"]["name"] == "missing"
    )
    body = replies[missing["id"]]["result"]["content"][0]["text"]
    assert json.loads(body)["reason"] == "SKILL_NOT_FOUND"
    assert rows[-1]["event"] == "transport.end" and rows[-1]["exit_code"] == 0
