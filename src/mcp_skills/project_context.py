"""Единый ProjectContext: корень проекта, компонент, технологии и режим.

D6 плана review-fixes (docs/plans/2026-10-15-review-fixes-plan.md): раньше режим
(mcp/offline) определялся тремя разными способами — search_skills подставлял
``mode or "mcp"``, load_skill добавлял режим только при автодетекте, bundler
вообще его не определял. ProjectContext вычисляет контекст проекта ОДИН раз
(с кэшированием по сигнатуре окружения) и переиспользуется во всех трёх
точках, гарантируя одинаковый режим для одного корня.

Семантика дополнения явного стека (решение по вопросу №2, verdict_7c):
явный стек НЕ дополняется технологиями (язык/фреймворк — «явный выбор
уважается»), НО дополняется осью режима: если в стеке нет ни ``mcp``, ни
``offline`` — добавляется ``detect_mode(root)`` (если режим определён).
Ось режима ортогональна технологиям и нужна для выбора дуальных блоков
``blocks/<slot>/{mcp,offline}.md`` вместо default-зонда.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from mcp_core.paths import _find_default_project_root
from mcp_skills.stack_detector import detect_mode, detect_project_stack

__all__ = [
    "MODE_VALUES",
    "ProjectContext",
    "get_project_context",
    "supplement_mode_axis",
]

# Значения оси режима (ортогональны технологиям: python/fastapi/react/...).
MODE_VALUES: tuple[str, ...] = ("mcp", "offline")

# Манифесты, влияющие на detect_project_stack: входят в сигнатуру кэша,
# чтобы правка манифеста между вызовами подхватывалась без перезапуска.
_MANIFESTS: tuple[str, ...] = (
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "tauri.conf.json",
    "src-tauri/tauri.conf.json",
    "src-tauri/Cargo.toml",
)

# Маркеры режима из detect_mode: пересчитываются в сигнатуру кэша.
_MODE_MARKERS: tuple[str, ...] = (
    ".agents/audit",
    ".agents/knowledge",
    ".agents/.cache/knowledge_fts.db",
)

# Кэш контекстов: LRU (OrderedDict) с ограничением по числу проектов.
# Ключ — канонический root.resolve(); значение — (сигнатура, ProjectContext),
# где сигнатура = (_env_signature(), _fs_signature(root)). При смене сигнатуры
# запись заменяется; при cache hit — перемещается в конец (move_to_end);
# при превышении лимита вытесняется наименее недавно использованная запись
# (popitem(last=False)). Модульный (не lru_cache), чтобы тесты могли чистить
# его напрямую через _CONTEXT_CACHE.clear().
_CACHE_MAX = 32
_CONTEXT_CACHE: OrderedDict[str, tuple[tuple, ProjectContext]] = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _parse_active_servers() -> tuple[str, ...]:
    """Разбирает список активных MCP-серверов из MCP_ACTIVE_SERVERS."""
    val = os.environ.get("MCP_ACTIVE_SERVERS")
    if not val:
        return ()
    return tuple(s.strip() for s in val.split(",") if s.strip())


def _env_signature() -> tuple[str, ...]:
    """Сигнатура окружения: переменные, влияющие на контекст проекта и режим."""
    return (
        os.environ.get("MCP_ACTIVE_SERVERS", "\0unset"),
        os.environ.get("MCP_PROJECT_ROOT", "\0unset"),
        os.environ.get("PROJECT_PATH", "\0unset"),
        os.environ.get("SKILLS_REPO_DIR", "\0unset"),
    )


def _fs_signature(root: Path) -> tuple:
    """Сигнатура ФС: существование и mtime манифестов + маркеры режима."""
    parts: list[tuple] = []
    for rel in _MANIFESTS:
        p = root / rel
        try:
            stat = p.stat()
            parts.append((rel, True, stat.st_mtime_ns))
        except OSError:
            parts.append((rel, False))
    for rel in _MODE_MARKERS:
        p = root / rel
        try:
            p.stat()
            parts.append((rel, True))
        except OSError:
            parts.append((rel, False))
    try:
        for p in root.iterdir():
            if (
                p.is_file()
                and p.name not in ("skills.db", "knowledge.db")
                and (
                    p.suffix in (".sqlite", ".sqlite3")
                    or (p.suffix == ".db" and not p.name.endswith("_fts.db"))
                )
            ):
                parts.append((p.name, True, p.stat().st_mtime_ns))
    except OSError:
        pass
    return tuple(parts)


def _project_signature(root: Path) -> tuple:
    """Сигнатура проекта: env (MCP_ACTIVE_SERVERS и др.) + ФС (манифесты, маркеры,
    sqlite-файлы). Смена любой составляющей инвалидирует кэшированный контекст."""
    return (_env_signature(), _fs_signature(root))


@dataclass(frozen=True)
class ProjectContext:
    """Контекст проекта, вычисленный один раз для корня.

    Атрибуты:
        root: корень проекта (как передан, без resolve).
        component: каноническое имя компонента/проекта (git remote →
            манифесты → имя папки), источник для flow-движка и бандлера.
        stack: технологии проекта из автодетекта (sorted, без оси режима).
        stack_sources: {технология: источник обнаружения} для отладки
            и объяснимости (какой манифест дал значение).
        mode: 'mcp' / 'offline' / None — ось режима работы движка.
        active_servers: список активных MCP-серверов из MCP_ACTIVE_SERVERS.
    """

    root: Path
    component: str
    stack: list[str] = field(default_factory=list)
    stack_sources: dict[str, str] = field(default_factory=dict)
    mode: str | None = None
    active_servers: tuple[str, ...] = field(default_factory=tuple)

    def has_server(self, name: str) -> bool:
        """Проверяет доступность конкретного MCP-сервера по имени."""
        return name in self.active_servers

    def server_availability(self, name: str) -> str:
        """Доступность MCP-сервера: 'present' | 'absent' | 'unknown' (R7).

        'present' — MCP_ACTIVE_SERVERS задан и содержит имя; 'absent' —
        переменная задана, но имени нет (сервер точно не поднят); 'unknown' —
        переменная не задана (список серверов неизвестен: клиент не передал
        окружение). Глобальное отключение mcp-блоков по 'unknown' запрещено:
        интеграция, доступная хосту, должна собираться (per-card решение).
        """
        val = os.environ.get("MCP_ACTIVE_SERVERS")
        if not val:
            return "unknown"
        if name in self.active_servers:
            return "present"
        return "absent"


def _detect_stack_with_sources(root: Path) -> tuple[list[str], dict[str, str]]:
    """Технологии + источники: обёртка над detect_project_stack.

    ``detect_project_stack`` возвращает sorted-список значений; источник
    восстанавливается по файлу-манифесту (python/fastapi — pyproject.toml,
    node/react — package.json, rust — Cargo.toml, go — go.mod).
    """
    stack = detect_project_stack(root)
    sources: dict[str, str] = {}
    if (root / "pyproject.toml").is_file():
        sources["python"] = "pyproject.toml"
        # Значения из pyproject: fastapi/django/flask/pytorch/pydantic/... —
        # принадлежат python-манифесту по построению детектора.
        for tech in stack:
            sources.setdefault(tech, "pyproject.toml")
    if (root / "package.json").is_file():
        sources["node"] = "package.json"
        for tech in stack:
            if tech not in sources:
                sources[tech] = "package.json"
    if (root / "Cargo.toml").is_file() and "rust" in stack:
        sources["rust"] = "Cargo.toml"
    if (root / "go.mod").is_file() and "go" in stack:
        sources["go"] = "go.mod"
    return stack, sources


def get_project_context(project_root: Path | None = None) -> ProjectContext:
    """Возвращает (и кэширует) контекст проекта для корня.

    Кэш — LRU на ``_CACHE_MAX`` проектов (OrderedDict, ключ — канонический
    ``root.resolve()``). Значение кэша — ``(сигнатура, ProjectContext)``:
    сигнатура включает MCP_ACTIVE_SERVERS, mtime манифестов, маркеры режима
    и sqlite-файлы; при её смене контекст для проекта пересчитывается и
    заменяется на месте (без раздувания кэша). Cache hit перемещает запись
    в конец LRU; вытеснение — наименее недавно использованная запись.
    Доступ к кэшу защищён локом и потокобезопасен; при гонках устаревший
    контекст не перезаписывает более свежий (сравнение сигнатур под локом).
    ``ProjectContext.root`` хранит исходный переданный ``root`` без resolve.
    """
    root = project_root or _find_default_project_root()

    try:
        key = str(root.resolve())
    except OSError:
        # Неразрешимый путь (напр., удалённая tmp-директория) — ключ по
        # строковому представлению, чтобы не падать на cache-операциях.
        key = str(root)

    with _CACHE_LOCK:
        cached = _CONTEXT_CACHE.get(key)
        if cached is not None:
            signature, ctx = cached
            # Проверка под локом: контекст мог быть пересчитан другим потоком.
            current_sig = _project_signature(root)
            if signature == current_sig:
                _CONTEXT_CACHE.move_to_end(key)
                return ctx

    # Локальный импорт: избежать цикличности при моках в тестах.
    from mcp_skills.stack_detector import detect_canonical_project_name

    # Детекция вне лока: тяжёлые вызовы ФС не блокируют другие потоки.
    signature = _project_signature(root)
    stack, sources = _detect_stack_with_sources(root)
    ctx = ProjectContext(
        root=root,
        component=detect_canonical_project_name(root),
        stack=stack,
        stack_sources=sources,
        mode=detect_mode(root),
        active_servers=_parse_active_servers(),
    )

    with _CACHE_LOCK:
        # Повторная проверка под локом от гонок: пока шла детекция, другой
        # поток мог записать контекст с той же сигнатурой — не затираем его.
        cached = _CONTEXT_CACHE.get(key)
        if cached is not None and cached[0] == signature:
            _CONTEXT_CACHE.move_to_end(key)
            return cached[1]
        _CONTEXT_CACHE[key] = (signature, ctx)
        _CONTEXT_CACHE.move_to_end(key)
        while len(_CONTEXT_CACHE) > _CACHE_MAX:
            _CONTEXT_CACHE.popitem(last=False)
    return ctx


def supplement_mode_axis(
    stack: list[str],
    mode: str | None,
) -> tuple[list[str], list[str]]:
    """Дополняет явный стек осью режима, если её там нет.

    Возвращает ``(stack_with_mode, supplemented)``: технологии НЕ
    трогаются; если в ``stack`` уже есть ``mcp``/``offline`` (в любом
    регистре) — возвращается копия без изменений, ``supplemented = []``.
    Если режим неизвестен (``None``) — тоже без изменений.
    """
    if not stack:
        # Пустой стек = автодетект не нашёл технологий, но ось режима всё
        # равно нужна (бандлер: mode/mcp.md без технологий; search 'auto'
        # разрешает ['mcp'] тем же путём — storage.py:508-515).
        return ([mode], [mode]) if mode else (list(stack), [])
    lowered = {v.lower() for v in stack}
    if any(m in lowered for m in MODE_VALUES):
        return list(stack), []
    if mode is None:
        return list(stack), []
    return [*stack, mode], [mode]
