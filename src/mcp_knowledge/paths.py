"""Определение корня проекта для размещения .agents/knowledge и .agents/.cache.

Реэкспортирует реализацию из mcp_core для сохранения обратной совместимости.
"""

from mcp_core.paths import _find_default_project_root

__all__ = ["_find_default_project_root"]
