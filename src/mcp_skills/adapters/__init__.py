"""Пакет адаптеров мульти-рантайм интеграции субагентов и скиллов."""

from __future__ import annotations

from mcp_skills.adapters.antigravity import AntigravityAdapter
from mcp_skills.adapters.base import BaseAdapter
from mcp_skills.adapters.cursor import CursorAdapter
from mcp_skills.adapters.opencode import OpenCodeAdapter
from mcp_skills.adapters.prompt import PromptAdapter

__all__ = [
    "SUPPORTED_TARGETS",
    "AntigravityAdapter",
    "BaseAdapter",
    "CursorAdapter",
    "OpenCodeAdapter",
    "PromptAdapter",
    "get_adapter",
]

# target_name адаптера — единый источник правды для имени рантайма.
# target_name — абстрактный property, поэтому ключ строится через экземпляр.
_ADAPTERS: dict[str, type[BaseAdapter]] = {
    AntigravityAdapter().target_name: AntigravityAdapter,
    OpenCodeAdapter().target_name: OpenCodeAdapter,
    CursorAdapter().target_name: CursorAdapter,
    PromptAdapter().target_name: PromptAdapter,
}

# Единственный список поддерживаемых целевых рантаймов: cli.py (choices --target)
# и installer.py (choices инсталлятора) импортируют его отсюда, поэтому новый
# адаптер в _ADAPTERS расширяет все места автоматически. tuple(sorted(...)) даёт
# стабильный порядок для сообщений об ошибках и argparse-choices.
SUPPORTED_TARGETS: tuple[str, ...] = tuple(sorted(_ADAPTERS))


def get_adapter(target: str) -> BaseAdapter:
    """Возвращает экземпляр адаптера по имени целевого рантайма."""
    adapter_cls = _ADAPTERS.get(target.lower())
    if not adapter_cls:
        valid_targets = ", ".join(sorted(_ADAPTERS.keys()))
        msg = f"Неизвестный целевой рантайм '{target}'. Допустимые: {valid_targets}"
        raise ValueError(msg)
    return adapter_cls()
