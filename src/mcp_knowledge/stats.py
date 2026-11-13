"""CLI инструмент для просмотра статистики использования инструментов (Adoption Telemetry).

Запуск:
    python -m mcp_knowledge.stats [--db /path/to/cache.db] [--json]
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from mcp_knowledge.paths import _find_default_project_root
from mcp_knowledge.storage import read_tool_call_stats


def print_stats_table(stats: list[dict[str, Any]]) -> None:
    if not stats:
        print("Телеметрия вызовов инструментов пуста.")
        return

    print("=== Статистика использования инструментов (Adoption Telemetry) ===\n")
    headers = ["Инструмент", "Вызовов", "Ошибок", "Ср. время (мс)", "Макс. время (мс)"]
    col_w = [25, 10, 10, 15, 16]

    header_str = " | ".join(h.ljust(w) for h, w in zip(headers, col_w, strict=True))
    divider = "-+-".join("-" * w for w in col_w)

    print(header_str)
    print(divider)

    total_calls = 0
    total_errors = 0

    for s in stats:
        tool_name = str(s.get("tool_name", "")).ljust(col_w[0])
        calls = str(s.get("total_calls", 0)).rjust(col_w[1])
        errors = str(s.get("error_calls", 0)).rjust(col_w[2])
        avg_lat = f"{float(s.get('avg_latency_ms', 0.0)):.2f}".rjust(col_w[3])
        max_lat = f"{float(s.get('max_latency_ms', 0.0)):.2f}".rjust(col_w[4])

        total_calls += int(s.get("total_calls", 0))
        total_errors += int(s.get("error_calls", 0))

        print(f"{tool_name} | {calls} | {errors} | {avg_lat} | {max_lat}")

    print(divider)
    print(f"Всего вызовов: {total_calls}, сбоев: {total_errors}\n")


def resolve_db_path(explicit_path: Path | None = None) -> Path:
    """Определяет путь к SQLite базе телеметрии: аргумент CLI или автопоиск."""
    if explicit_path is not None:
        if not explicit_path.exists():
            print(f"❌ Указанная база данных не найдена: {explicit_path}", file=sys.stderr)
            sys.exit(1)
        return explicit_path

    # Автопоиск: сначала в каталоге текущего проекта (.agents/.cache/knowledge_fts.db), затем глобальный кэш
    project_root = _find_default_project_root()
    candidates = [
        project_root / ".agents" / ".cache" / "knowledge_fts.db",
        Path.cwd() / ".agents" / ".cache" / "knowledge_fts.db",
        project_root / ".agents" / ".cache" / "knowledge_index.db",
        Path.cwd() / ".agents" / ".cache" / "knowledge_index.db",
        Path.home() / ".cache" / "mcp-knowledge-engine" / "knowledge_fts.db",
        Path.home() / ".cache" / "mcp-knowledge-engine" / "knowledge_index.db",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    searched_paths = "\n  - ".join(str(p) for p in candidates)
    print(
        f"❌ База данных телеметрии не найдена.\nПроверенные пути:\n  - {searched_paths}\n"
        "Укажите путь явно: python -m mcp_knowledge.stats --db /path/to/db",
        file=sys.stderr,
    )
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Adoption Telemetry reader")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Путь к SQLite базе кэша (по умолчанию .agents/.cache/knowledge_fts.db)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Вывести статистику в формате JSON",
    )
    args = parser.parse_args()

    db_path = resolve_db_path(args.db)

    try:
        stats = read_tool_call_stats(db_path)
    except Exception as e:
        print(f"❌ Ошибка чтения статистики из базы данных {db_path}: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(stats, indent=2, ensure_ascii=False))
    else:
        print_stats_table(stats)


if __name__ == "__main__":
    main()
