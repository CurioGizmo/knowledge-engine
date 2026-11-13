"""Контракты общих параметров инструментов MCP."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Annotated, Any, TypeVar

from pydantic import Field

__all__ = [
    "PROJECT_PATH_OPTIONAL_PARAM",
    "PROJECT_PATH_PARAM",
    "PROJECT_PATH_REQUIRED_PARAM",
    "adapt_project_path_signature",
]

PROJECT_PATH_REQUIRED_PARAM = Annotated[
    str,
    Field(
        description=(
            "Absolute path to the active project root directory (e.g. from current workspace). "
            "May be omitted when the server is running with an anchored project; REQUIRED only "
            "in multi-tenant / multi-workspace environments."
        ),
    ),
]

PROJECT_PATH_OPTIONAL_PARAM = Annotated[
    str | None,
    Field(
        default=None,
        description=(
            "Absolute path to the active project root directory (e.g. from current workspace). "
            "May be omitted when the server is running with an anchored project; REQUIRED only "
            "in multi-tenant / multi-workspace environments."
        ),
    ),
]

PROJECT_PATH_PARAM = PROJECT_PATH_OPTIONAL_PARAM

_F = TypeVar("_F", bound=Callable[..., Any])


def adapt_project_path_signature(func: _F, multi_tenant: bool = False) -> _F:
    """Адаптирует сигнатуру инструмента под multi-tenant режим.

    - При multi_tenant=True: делает параметр `project_path` строго обязательным (без default, KEYWORD_ONLY).
    - При multi_tenant=False: оставляет исходную сигнатуру с default=None.
    Явно устанавливает `func.__signature__`, чтобы inspect.signature не следовал
    по цепочке `__wrapped__` к исходному default=None, при этом сохраняя
    `func.__globals__` для корректного разрешения строковых аннотаций типом Pydantic.
    """
    if not multi_tenant:
        return func

    try:
        sig = inspect.signature(func, eval_str=True)
    except NameError:
        sig = inspect.signature(func)
    if "project_path" not in sig.parameters:
        return func

    new_params: list[inspect.Parameter] = []
    for p in sig.parameters.values():
        if p.name == "project_path":
            new_params.append(
                p.replace(
                    kind=inspect.Parameter.KEYWORD_ONLY,
                    default=inspect.Parameter.empty,
                    annotation=PROJECT_PATH_REQUIRED_PARAM,
                )
            )
        else:
            new_params.append(p)

    new_sig = sig.replace(parameters=new_params)
    func.__signature__ = new_sig  # type: ignore[attr-defined]
    return func
