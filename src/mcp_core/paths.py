"""Определение корня проекта и путей к директориям .agents/.

Вынесено в ядро, чтобы серверы и CLI-утилиты не тянули тяжёлые зависимости.
"""

import logging
import os
import urllib.parse
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

__all__ = [
    "BLOCKLIST_ROOTS",
    "PROJECT_ANCHOR_MARKERS",
    "InvalidProjectPathError",
    "ResolvedRoot",
    "RootNotAnchoredError",
    "SessionProjectMismatchError",
    "_find_default_project_root",
    "ensure_root_anchored",
    "get_agents_cache_dir",
    "get_agents_dir",
    "get_cache_dir",
    "get_flows_cache_dir",
    "get_flows_dir",
    "get_skills_cache_dir",
    "get_skills_dir",
    "is_anchored",
    "is_stale_tmp_root",
    "log_root_banner",
    "resolve_dynamic_project_root",
    "resolve_factory_root",
    "resolve_project_root",
    "resolve_skills_repo_dir",
    "safe_read_text",
    "sanitize_project_path",
]

PROJECT_ANCHOR_MARKERS: tuple[str, ...] = (
    ".git",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "AGENTS.md",
)

BLOCKLIST_ROOTS: set[Path] = {
    p.resolve()
    for p in (
        Path("/"),
        Path("/root"),
        Path("/sys"),
        Path("/proc"),
        Path("/dev"),
        Path("/run"),
        Path("/home"),
        Path("/tmp"),  # noqa: S108 — блок-лист системных путей, не создание temp-файлов
        Path("/etc"),
        Path("/usr"),
        Path("/var"),
        Path("/opt"),
        Path.home(),
    )
    if p.exists()
} | {Path("/")}


class InvalidProjectPathError(ValueError):
    """Невалидный путь проекта (не абсолютный, в блок-листе, не существует)."""


class SessionProjectMismatchError(ValueError):
    """Сессия flow привязана к другому корню проекта."""


class RootNotAnchoredError(ValueError):
    """Корень проекта не закреплён (нет MCP_PROJECT_ROOT и walk-up корневых маркеров).

    Поднимается пишущими инструментами MCP (fail-fast), когда резолв корня
    упал на cwd-fallback и явный write-target не передан: запись в такой
    корень с высокой вероятностью попадёт в мусорную директорию (например,
    cwd десинкнутого сервера или стейл-фикстуру тестов). Маппируется в
    error-envelope ``reason=ROOT_NOT_ANCHORED``.
    """


class ResolvedRoot(NamedTuple):
    """Результат резолва корня проекта вместе с источником (для banner-логов).

    ``source`` — один из:
    - ``env``: валидная директория из ``MCP_PROJECT_ROOT``;
    - ``anchor:<путь корневого маркера>``: walk-up нашёл маркер проекта;
    - ``llm:anchor:<путь>``: маркер найден по пути project_path от LLM;
    - ``explicit:<путь>``: явно переданный ``--project-root``, сам закреплён;
    - ``explicit-unanchored:<путь>``: явный ``--project-root`` без корневых маркеров
      (вероятно стейл-фикстура теста — guard записи остаётся активным);
    - ``llm:explicit-unanchored:<путь>``: project_path от LLM без корневых маркеров;
    - ``cwd-fallback:<путь>``: корневых маркеров нет, резолв упал на cwd.
    """

    root: Path
    source: str

    @property
    def anchored(self) -> bool:
        """True, если источник корня закреплён (env / walk-up корневой маркер / явный закреплённый root)."""
        return self.source.startswith(("env", "anchor:", "explicit:", "llm:anchor:"))


def ensure_root_anchored(write_ok: bool, resolved: ResolvedRoot | None = None) -> None:
    """Fail-fast guard пишущих инструментов: raise при незакреплённом корне.

    ``write_ok`` — флаг, вычисленный ОДИН раз на старте фабрики сервера
    (явный write-target передан ИЛИ корень закреплён). Пишущие обёртки тулов
    зовут guard до записи; при ``write_ok=False`` поднимается
    :class:`RootNotAnchoredError`, которую error-guard сервера мапит в
    envelope ``reason=ROOT_NOT_ANCHORED``.
    """
    if write_ok:
        return
    where = f" (корень={resolved.root})" if resolved is not None else ""
    raise RootNotAnchoredError(
        "Корень проекта не закреплён: MCP_PROJECT_ROOT не задан и корневых маркеров "
        "(AGENTS.md/pyproject.toml/.git) вверх по дереву от cwd не найдено"
        f"{where}. Запись отклонена, чтобы карточки не попали в случайный "
        "каталог (например, cwd десинкнутого сервера или стейл-фикстуру "
        "тестов). Задайте MCP_PROJECT_ROOT=<корень проекта> в окружении "
        "сервера или перезапустите его через инсталлер с --project-root. "
        "Явные write-target отключают guard: knowledge — --knowledge-dir, "
        "audit — --audit-dir. Инструменты create_skill и save_agent_override "
        "пишут в репозиторий скиллов, а не в проект, поэтому не требуют guard."
    )


def resolve_project_root(
    strict: bool = False,
    start_path: Path | None = None,
) -> ResolvedRoot:
    """Определяет корень проекта и возвращает его вместе с источником.

    Приоритет разрешения (тот же, что у :func:`_find_default_project_root`):
    1. ``MCP_PROJECT_ROOT`` (если директория существует) → ``source='env'``.
    2. Walk-up корневой маркер (AGENTS.md, pyproject.toml, .git) от ``start_path``
       (или cwd) → ``source='anchor:<путь корневого маркера>'``.
    3. Fallback: cwd → ``source='cwd-fallback:<путь>'`` (+ warning в лог).

    При ``strict=True`` и source='cwd-fallback' поднимает
    :class:`RootNotAnchoredError` с инструкцией — режим opt-in, существующие
    вызовы ``_find_default_project_root`` не затрагиваются.
    """
    if env_root := os.environ.get("MCP_PROJECT_ROOT"):
        p = Path(env_root).expanduser().resolve()
        if p.is_dir():
            return ResolvedRoot(root=p, source="env")
        logger.warning(
            "MCP_PROJECT_ROOT='%s' does not exist or is not a directory. Falling back to walk-up resolution.",
            env_root,
        )

    cwd = start_path or Path.cwd()
    for p in [cwd, *cwd.parents]:
        if (
            any((p / marker).exists() for marker in PROJECT_ANCHOR_MARKERS)
            and p.resolve() not in BLOCKLIST_ROOTS
        ):
            return ResolvedRoot(root=p, source=f"anchor:{p}")

    resolved = ResolvedRoot(root=cwd, source=f"cwd-fallback:{cwd}")
    if strict:
        ensure_root_anchored(False, resolved)
    logger.warning(
        "Корень проекта не закреплён (нет MCP_PROJECT_ROOT и корневых маркеров "
        "AGENTS.md/pyproject.toml/.git вверх по дереву), cwd=%s; запись может "
        "попасть не в тот проект. Задайте MCP_PROJECT_ROOT или запустите "
        "сервер с --project-root.",
        cwd,
    )
    return resolved


def sanitize_project_path(raw_path: str) -> Path:
    """Нормализует путь проекта от LLM с защитой от пробелов, кавычек, URI и относительных путей."""
    if not raw_path or not isinstance(raw_path, str):
        raise InvalidProjectPathError("Путь к проекту не может быть пустым")

    s = raw_path.strip().strip("'\"`").strip()
    if not s:
        raise InvalidProjectPathError("Путь к проекту не может быть пустым")

    parsed = urllib.parse.urlsplit(s)
    if parsed.scheme == "file":
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            raise InvalidProjectPathError(f"file:// URI с внешним хостом не поддерживается: {s}")
        s = urllib.parse.unquote(parsed.path)
    elif parsed.scheme == "workspace":
        s = urllib.parse.unquote(parsed.path or parsed.netloc)
    elif s.startswith("workspace://"):
        s = urllib.parse.unquote(s[12:])
    elif (
        parsed.scheme
        and len(parsed.scheme) > 1
        and not (len(parsed.scheme) == 1 and parsed.scheme.isalpha())
    ):
        raise InvalidProjectPathError(
            f"Неподдерживаемая схема URI для пути проекта: '{parsed.scheme}'"
        )
    else:
        # Для обычных файловых путей unquote НЕ применяется, чтобы не искажать
        # литеральные спецсимволы в именах каталогов (например /path/with%20literal)
        pass

    try:
        p = Path(s).expanduser()
        if not p.is_absolute():
            raise InvalidProjectPathError(
                f"Путь к проекту должен быть абсолютным, получено: '{raw_path}'"
            )

        resolved = p.resolve()
        if resolved in BLOCKLIST_ROOTS:
            raise InvalidProjectPathError(
                f"Путь к проекту не может указывать на системную директорию или домашнюю папку: {resolved}"
            )

        if not resolved.exists() or not resolved.is_dir():
            raise InvalidProjectPathError(
                f"Директория проекта не существует или не является каталогом: {resolved}"
            )
    except (OSError, ValueError) as e:
        if isinstance(e, InvalidProjectPathError):
            raise
        raise InvalidProjectPathError(f"Не удалось разрешить путь к проекту: {e}") from e

    return resolved


def resolve_dynamic_project_root(
    project_path: str | None = None,
    multi_tenant: bool = False,
    default_root: ResolvedRoot | None = None,
) -> ResolvedRoot:
    """Разрешает корень проекта для вызова тула с поддержкой multi-tenant и явного project_path.

    - В фиксированном режиме (multi_tenant=False): если передан default_root и передан
      project_path, отличный от default_root.root, переключение на чужой проект запрещено
      (поднимается InvalidProjectPathError).
    - При переданном project_path: санитизирует путь. Точный корень (sanitized) ВСЕГДА
      сохраняется (walk-up не подменяет root на предка!).
      Проверяется закреплённость: если среди (sanitized, *sanitized.parents) вне блок-листа
      найден корневой маркер, source=f"llm:anchor:{sanitized}" (anchored=True).
      Иначе source=f"llm:explicit-unanchored:{sanitized}" (anchored=False).
    - Если project_path не передан:
      - при multi_tenant=True поднимает RootNotAnchoredError с actionable-подсказкой;
      - при multi_tenant=False возвращает default_root (или resolve_project_root()).
    """
    if project_path is not None and project_path.strip():
        sanitized = sanitize_project_path(project_path)

        if not multi_tenant and default_root is not None:
            if sanitized != default_root.root.resolve():
                raise InvalidProjectPathError(
                    f"Сервер запущен в фиксированном режиме для '{default_root.root}'. "
                    f"Переключение на '{sanitized}' запрещено."
                )
            return default_root

        # Точный корень сохраняется, walk-up используется ТОЛЬКО для определения статуса anchored
        is_anchored = False
        for cand in (sanitized, *sanitized.parents):
            try:
                res_cand = cand.resolve()
            except OSError:
                continue
            if res_cand in BLOCKLIST_ROOTS:
                continue
            if any((cand / marker).exists() for marker in PROJECT_ANCHOR_MARKERS):
                is_anchored = True
                break

        if is_anchored:
            return ResolvedRoot(root=sanitized, source=f"llm:anchor:{sanitized}")
        return ResolvedRoot(root=sanitized, source=f"llm:explicit-unanchored:{sanitized}")

    if multi_tenant:
        raise RootNotAnchoredError(
            "Корень проекта не определён: сервер работает в multi-tenant режиме. "
            "Пожалуйста, передайте абсолютный путь к активному проекту в параметре "
            "project_path, например /path/to/project"
        )

    if default_root is not None:
        return default_root
    return resolve_project_root()


def resolve_factory_root(explicit_root: Path | None = None) -> ResolvedRoot:
    """Резолв корня для фабрик MCP-серверов (единая механика для всех трёх пакетов).

    - ``explicit_root`` передан (``--project-root``): источник ``explicit:<путь>``,
      если сам root (или его предки) содержит корневого маркера проекта, иначе
      ``explicit-unanchored:<путь>`` — production-инсталлер спавнит сервер с
      ``--project-root <репозиторий>``, где корневого маркера есть, а стейл-корень
      pytest-фикстуры (``/tmp/pytest-of-*``) корневых маркеров не содержит и guard записи
      остаётся активным.
    - ``explicit_root`` не задан: делегирование :func:`resolve_project_root`
      (env → walk-up → cwd-fallback).
    """
    if explicit_root is not None:
        root = Path(explicit_root).expanduser().resolve()
        if is_anchored(root):
            return ResolvedRoot(root=root, source=f"explicit:{root}")
        logger.warning(
            "Явный project_root='%s' не содержит корневых маркеров (AGENTS.md/pyproject.toml/.git) — "
            "вероятно, стейл-фикстура теста или мусорный каталог; запись пишущих "
            "инструментов будет отклонена с ROOT_NOT_ANCHORED.",
            root,
        )
        return ResolvedRoot(root=root, source=f"explicit-unanchored:{root}")
    return resolve_project_root()


def is_anchored(path: Path) -> bool:
    """True, если ``path`` (или любой его предок) содержит корневой маркер проекта.

    Корневые маркеры — те же, что у walk-up в :func:`resolve_project_root`:
    ``PROJECT_ANCHOR_MARKERS``. Проверяется также отсутствие в BLOCKLIST_ROOTS.
    """
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        return False
    for p in (resolved, *resolved.parents):
        if (
            any((p / marker).exists() for marker in PROJECT_ANCHOR_MARKERS)
            and p.resolve() not in BLOCKLIST_ROOTS
        ):
            return True
    return False


def _find_default_project_root(start_path: Path | None = None) -> Path:
    """Определяет корень проекта для размещения .agents/knowledge, .agents/audit и .agents/.cache.

    Приоритет разрешения:
    1. Переменная окружения MCP_PROJECT_ROOT (если директория существует).
       Если путь не существует или не является директорией — пишется warning в лог и выполняется fallback.
    2. Поиск корневого маркера (AGENTS.md, pyproject.toml, .git) вверх по дереву от start_path (или Path.cwd()).
    3. Fallback: текущая рабочая директория (cwd) — сопровождается warning в лог.
    """
    return resolve_project_root(start_path=start_path).root


def is_stale_tmp_root(root: Path) -> bool:
    """True, если резолвнутый корень лежит под /tmp — эвристика стейл-фикстуры.

    Явный ``--project-root`` под /tmp (особенно ``/tmp/pytest-of-*``) с высокой
    вероятностью означает, что сервер запущен на мусорной фикстуре тестов
    (живой инцидент 2026-10-07). Не блокирует запись — легитимные tmp-сценарии
    существуют — но фабрики серверов пишут warning-banner.
    """
    try:
        resolved = root.resolve()
    except OSError:
        return False
    return (
        resolved == Path("/tmp") or Path("/tmp") in resolved.parents  # noqa: S108 — эвристика обнаружения стейл-корня, не создание temp-файлов
    )


def log_root_banner(server_name: str, resolved: ResolvedRoot, write_ok: bool) -> None:
    """Однострочный banner-лог источника корня при старте сервера (диагностика десинка).

    Дополнительно пишет warning при незакреплённом корне (cwd-fallback) и при
    /tmp-эвристике (вероятно, стейл-фикстура теста — не блокирует запись).
    """
    logger.info(
        "[%s] root source=%s path=%s write_ok=%s",
        server_name,
        resolved.source,
        resolved.root,
        write_ok,
    )
    if not resolved.anchored:
        logger.warning(
            "[%s] корень не закреплён (source=%s): запись пишущих инструментов "
            "будет отклонена с ROOT_NOT_ANCHORED, пока не задан MCP_PROJECT_ROOT "
            "или явный --project-root на каталог с корневого маркерами проекта.",
            server_name,
            resolved.source,
        )
    if is_stale_tmp_root(resolved.root):
        logger.warning(
            "[%s] корень под /tmp (%s) — вероятно, стейл-фикстура теста или "
            "мусорный спавн сервера; результаты и записи могут быть неполными. "
            "Проверьте MCP_PROJECT_ROOT и аргументы запуска в mcp_config.json.",
            server_name,
            resolved.root,
        )


def get_cache_dir(project_root: Path | None = None) -> Path:
    """Возвращает путь к каталогу .agents/.cache/."""
    root = project_root or _find_default_project_root()
    return root / ".agents" / ".cache"


def get_skills_dir(project_root: Path | None = None) -> Path:
    """Возвращает путь к каталогу .agents/skills/."""
    root = project_root or _find_default_project_root()
    return root / ".agents" / "skills"


_WARNED_DEPRECATED_CACHE_ENV = False


def resolve_skills_repo_dir(cli_dir: Path | str | None = None) -> Path:
    """Определяет канонический путь к репозиторию скиллов.

    Приоритет:
    1. Явный CLI аргумент ``cli_dir`` (--skills-dir).
    2. Переменная окружения ``MCP_SKILLS_PATH``.
    3. Переменная окружения ``MCP_SKILLS_CACHE_DIR`` (deprecated алиас, разовый warning).
    4. Fallback по умолчанию: ``~/.cache/mcp-skills/repo``.
    """
    global _WARNED_DEPRECATED_CACHE_ENV
    if cli_dir is not None:
        return Path(cli_dir).expanduser().resolve()
    if env_path := os.environ.get("MCP_SKILLS_PATH"):
        return Path(env_path).expanduser().resolve()
    if env_cache := os.environ.get("MCP_SKILLS_CACHE_DIR"):
        if not _WARNED_DEPRECATED_CACHE_ENV:
            logger.warning(
                "Переменная окружения MCP_SKILLS_CACHE_DIR устарела. "
                "Используйте MCP_SKILLS_PATH или аргумент --skills-dir."
            )
            _WARNED_DEPRECATED_CACHE_ENV = True
        return Path(env_cache).expanduser().resolve()
    return Path.home() / ".cache" / "mcp-skills" / "repo"


def get_skills_cache_dir(cli_dir: Path | str | None = None) -> Path:
    """Возвращает путь к каталогу репозитория скиллов (делегирует resolve_skills_repo_dir)."""
    return resolve_skills_repo_dir(cli_dir)


def get_agents_dir(project_root: Path | None = None) -> Path:
    """Возвращает путь к каталогу .agents/agents/."""
    root = project_root or _find_default_project_root()
    return root / ".agents" / "agents"


def get_agents_cache_dir(cache_root: Path | None = None) -> Path:
    """Возвращает путь к каталогу агентов в кэше (~/.cache/mcp-skills/repo/agents или override)."""
    base = cache_root or get_skills_cache_dir()
    return base / "agents"


def get_flows_dir(project_root: Path | None = None) -> Path:
    """Возвращает путь к каталогу .agents/flows/."""
    root = project_root or _find_default_project_root()
    return root / ".agents" / "flows"


def get_flows_cache_dir(cache_root: Path | None = None) -> Path:
    """Возвращает путь к каталогу флоу в кэше (~/.cache/mcp-skills/repo/flows или override)."""
    base = cache_root or get_skills_cache_dir()
    return base / "flows"


def safe_read_text(root: Path, file_path: str | Path | None) -> str | None:
    """Безопасно читает текст файла внутри root с защитой от path traversal.

    Возвращает None, если file_path пуст, выходит за пределы корня, файл не существует
    или не читается.
    """
    if not file_path:
        return None
    try:
        candidate = Path(file_path)
        resolved_file = (
            (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        )
        resolved_file.relative_to(root.resolve())
        if resolved_file.is_file():
            return resolved_file.read_text(encoding="utf-8")
    except (ValueError, OSError):
        pass
    return None
