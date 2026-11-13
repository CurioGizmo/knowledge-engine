"""Модуль санитизации запросов и Query Expansion для SQLite FTS5.

Реэкспортирует реализацию из mcp_core для сохранения обратной совместимости.
"""

from mcp_core.sanitizer import (
    FTS5_RESERVED,
    LLM_QUERY_STOP_WORDS,
    SHORT_ACRONYMS,
    build_multi_field_fts_query,
    extract_short_acronym_tokens,
    sanitize_fts_query,
    stem_russian,
)

__all__ = [
    "FTS5_RESERVED",
    "LLM_QUERY_STOP_WORDS",
    "SHORT_ACRONYMS",
    "build_multi_field_fts_query",
    "extract_short_acronym_tokens",
    "sanitize_fts_query",
    "stem_russian",
]
