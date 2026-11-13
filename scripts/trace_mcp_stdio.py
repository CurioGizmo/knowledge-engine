#!/usr/bin/env python3
"""Passively record a real MCP stdio connection; never synthesize tool calls.

Usage: python trace_mcp_stdio.py --output /tmp/run/mcp.jsonl -- SERVER ARGS...
The client's stdin and stdout remain the server's JSON-RPC transport.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, TextIO


class TraceWriter:
    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.lock = threading.Lock()
        self.seq = 0

    def record(self, event: str, **fields: object) -> None:
        with self.lock:
            self.seq += 1
            row = {
                "schema": "mcp-stdio-trace/v1",
                "seq": self.seq,
                "ts": datetime.now(UTC).isoformat(),
                "monotonic_ns": time.monotonic_ns(),
                "event": event,
                **fields,
            }
            self.stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.stream.flush()

    def packet(self, direction: str, line: bytes) -> None:
        fields: dict[str, object] = {
            "direction": direction,
            "wire_bytes": len(line),
            "wire_sha256": hashlib.sha256(line).hexdigest(),
        }
        try:
            fields["message"] = json.loads(line)
        except (UnicodeError, ValueError):
            # Preserve malformed output as evidence; never repair the transport.
            fields["invalid_json_hex"] = line.hex()
        self.record("packet", **fields)


def forward(source: BinaryIO, destination: BinaryIO, trace: TraceWriter, direction: str) -> None:
    for line in source:
        trace.packet(direction, line)
        destination.write(line)
        destination.flush()


def run(command: list[str], output: Path) -> int:
    # Exclusive creation prevents accidentally mixing independent sessions.
    with output.open("x", encoding="utf-8") as stream:
        trace = TraceWriter(stream)
        trace.record("transport.start", command=command)
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as child:  # noqa: S603
            assert child.stdin is not None and child.stdout is not None  # noqa: S101
            input_errors: list[str] = []

            def client_input() -> None:
                try:
                    forward(sys.stdin.buffer, child.stdin, trace, "client_to_server")
                    child.stdin.close()
                except (OSError, ValueError) as exc:
                    input_errors.append(str(exc))
                    child.terminate()

            worker = threading.Thread(target=client_input, daemon=True)
            worker.start()
            try:
                forward(child.stdout, sys.stdout.buffer, trace, "server_to_client")
                exit_code = child.wait()
            except BaseException:
                child.terminate()
                child.wait()
                raise
            trace.record("transport.end", exit_code=exit_code, input_errors=input_errors)
            return exit_code if not input_errors else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("Pass the real MCP server command after --")
    return run(command, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
