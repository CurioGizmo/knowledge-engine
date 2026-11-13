"""MCP stdio сервер для MCP Skills Engine (`skills-engine`)."""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import re
import sys
import tempfile
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from mcp.server.mcpserver import MCPServer

from mcp_core.paths import (
    ResolvedRoot,
    SessionProjectMismatchError,
    _find_default_project_root,
    ensure_root_anchored,
    get_cache_dir,
    get_skills_cache_dir,
    get_skills_dir,
    is_anchored,
    log_root_banner,
    resolve_dynamic_project_root,
    resolve_factory_root,
    resolve_skills_repo_dir,
)
from mcp_core.service_pool import ProjectServicePool
from mcp_core.tool_contract import PROJECT_PATH_PARAM, adapt_project_path_signature
from mcp_skills.bundler import AgentBundler, create_default_override
from mcp_skills.creator import create_skill
from mcp_skills.error_envelope import _handle_tool_error as _handle_tool_error
from mcp_skills.error_envelope import _make_error_response as _make_error_response
from mcp_skills.error_envelope import _tool_error_guard as _tool_error_guard
from mcp_skills.errors import FlowNotFoundError, SkillNotFoundError
from mcp_skills.flow_engine import (
    abort_flow,
    advance_flow,
    get_flow_status,
    list_flows,
    resolve_flow_escalation,
    start_flow,
    step_flow,
)
from mcp_skills.flow_fts import build_flow_fts_index, fts_bonus_rank
from mcp_skills.flow_parser import list_available_flows
from mcp_skills.flow_storage import FlowStorage
from mcp_skills.git_manager import GitSkillsManager
from mcp_skills.help_docs import get_skills_help_text
from mcp_skills.installer import SKILLS_INSTRUCTIONS
from mcp_skills.models import SkillCard
from mcp_skills.project_context import (
    get_project_context,
    supplement_mode_axis,
)
from mcp_skills.resources import is_resource_associated_with_skill, read_resource_chunk
from mcp_skills.slot_compiler import SlotCompiler
from mcp_skills.spam_gate import is_kebab_name, is_safe_name
from mcp_skills.stack_detector import detect_canonical_project_name
from mcp_skills.storage import SkillsStorage, check_skill_applicability
from mcp_skills.synchronizer import SkillsSynchronizer
from mcp_skills.token_guard import TokenGuard

logger = logging.getLogger(__name__)

__all__ = [
    "SkillsServiceBundle",
    "abort_flow",
    "advance_flow",
    "create_server",
    "create_skill",
    "get_bundler",
    "get_compiler",
    "get_flow_status",
    "get_git_manager",
    "get_storage",
    "get_synchronizer",
    "install_agent_workflow",
    "list_flows",
    "load_agent",
    "load_skill",
    "resolve_flow_escalation",
    "run_server",
    "save_agent_override",
    "search_skills",
    "start_flow",
    "step_flow",
]


@dataclass(frozen=True)
class SkillsServiceBundle:
    root: Path
    storage: SkillsStorage
    synchronizer: SkillsSynchronizer
    compiler: SlotCompiler
    bundler: AgentBundler

    def close(self) -> None:
        self.storage.close()


_STORAGE: SkillsStorage | None = None
_SYNCHRONIZER: SkillsSynchronizer | None = None
_COMPILER: SlotCompiler | None = None
_GIT_MGR: GitSkillsManager | None = None
_BUNDLER: AgentBundler | None = None

# Реестр эфемерных баз SQLite для незакреплённых корней (авто-очистка при выходе)
_UNANCHORED_TEMP_DBS: set[Path] = set()


def cleanup_unanchored_temp_dbs() -> None:
    """Удаляет зарегистрированные эфемерные базы для незакреплённых корней."""
    for db_file in list(_UNANCHORED_TEMP_DBS):
        for path in (
            db_file,
            db_file.with_name(f"{db_file.name}-wal"),
            db_file.with_name(f"{db_file.name}-shm"),
        ):
            with contextlib.suppress(Exception):
                path.unlink(missing_ok=True)
    _UNANCHORED_TEMP_DBS.clear()


atexit.register(cleanup_unanchored_temp_dbs)

# Кэш значений стека (stack_values) per-storage. Ключ — (сигнатура file_sync,
# mtime stack-conventions.yaml). Синхронизатор обновляет file_sync при любых
# изменениях файлов скиллов → кэш автоматически инвалидируется, не требуя
# ФС-скана всех blocks/ на каждый zero-query search_skills.
_STACK_VALUES_CACHE: weakref.WeakKeyDictionary[SkillsStorage, tuple[str, dict[str, str]]] = (
    weakref.WeakKeyDictionary()
)


def _normalize_str_list(val: Any) -> list[str] | None:
    """Нормализует список строк: список/кортеж, одиночная CSV-строка или None.

    Защищает от MCP-хостов, сериализующих массивы аргументов в строки:
    ``"python,fastapi"`` и ``["python", "fastapi"]`` дают одинаковый результат.
    ``None`` или пустой ввод (``""``, ``" , "``, ``[]``) → ``None`` (не указан).
    Нестроковые элементы и неожидаемые типы → ``ValueError`` (fail-fast).
    """
    if val is None:
        return None
    if isinstance(val, str):
        items = [item.strip() for item in val.split(",") if item.strip()]
        return items or None
    if isinstance(val, (list, tuple)):
        normalized: list[str] = []
        for item in val:
            if not isinstance(item, str):
                msg = f"Ожидался список строк или CSV-строка, получен элемент типа {type(item).__name__!r}"
                raise ValueError(msg)
            if item.strip():
                normalized.append(item.strip())
        return normalized
    msg = f"Ожидался список строк, CSV-строка или None, получен {type(val).__name__!r}"
    raise ValueError(msg)


def _validate_stack_items(items: list[str]) -> list[str]:
    """Валидирует элементы стека технологий и возвращает их без дубликатов.

    Канон как в ``creator._validate_inputs``: kebab-case (``is_safe_name``)
    в нижнем регистре — path traversal, кириллица и CamelCase отклоняются.
    """
    validated: list[str] = []
    for item in items:
        if not is_kebab_name(item):
            msg = (
                f"Недопустимое имя технологии в стеке {item!r}: "
                "используйте kebab-case в нижнем регистре без спецсимволов."
            )
            raise ValueError(msg)
        validated.append(item)
    return list(dict.fromkeys(validated))


def get_storage(
    project_root: Path | None = None,
    db_path: Path | None = None,
) -> SkillsStorage:
    """Возвращает экземпляр SkillsStorage с поддержкой пользовательского пути."""
    global _STORAGE
    if db_path is not None:
        return SkillsStorage(db_path)
    if _STORAGE is None:
        root = project_root or _find_default_project_root()
        cache_dir = get_cache_dir(root)
        target_db = cache_dir / "skills_fts.db"
        _STORAGE = SkillsStorage(target_db)
    return _STORAGE


def get_synchronizer(
    project_root: Path | None = None,
    storage: SkillsStorage | None = None,
    skills_dir: Path | None = None,
) -> SkillsSynchronizer:
    """Возвращает экземпляр SkillsSynchronizer (без побочного sync).

    Синхронизацию выполняет вызывающая сторона (единый sync вместо двойного
    полного скана). Глобаль ``_SYNCHRONIZER`` инициализируется лениво, как и раньше.
    """
    global _SYNCHRONIZER
    repo_dir = resolve_skills_repo_dir(skills_dir)
    skills_base = repo_dir if repo_dir.name == "skills" else repo_dir / "skills"
    # D2: локальные скиллы проекта (.agents/skills) индексируются ПОСЛЕ репозитория —
    # при коллизии имён last-wins (локальный побеждает). Семантика зафиксирована
    # решением вопроса №4 (verdict_7c): last-wins + extends достаточно, .override.md
    # не вводится; extends наследует каркас только при пустом теле потомка
    # (slot_compiler), одноимённый локальный скилл = полная замена.
    dirs = [skills_base]
    if project_root is not None:
        local_dir = get_skills_dir(project_root)
        if local_dir.exists() and local_dir.resolve() not in {d.resolve() for d in dirs}:
            dirs.append(local_dir)
    if storage is not None or skills_dir is not None:
        store = storage or get_storage(project_root)
        return SkillsSynchronizer(store, dirs)
    if _SYNCHRONIZER is None:
        root = project_root or _find_default_project_root()
        store = get_storage(root)
        _SYNCHRONIZER = SkillsSynchronizer(store, dirs)
    return _SYNCHRONIZER


def get_compiler(
    project_root: Path | None = None,
    storage: SkillsStorage | None = None,
    catalog_root: Path | None = None,
) -> SlotCompiler:
    """Возвращает экземпляр SlotCompiler."""
    global _COMPILER
    if storage is not None or catalog_root is not None:
        cat_root = catalog_root or resolve_skills_repo_dir()
        store = storage if storage is not None else get_storage(project_root)
        return SlotCompiler(store, catalog_root=cat_root)
    if _COMPILER is None:
        store = get_storage(project_root)
        cat_root = resolve_skills_repo_dir()
        _COMPILER = SlotCompiler(store, catalog_root=cat_root)
    return _COMPILER


def get_git_manager(cache_dir: Path | None = None) -> GitSkillsManager:
    """Возвращает глобальный экземпляр GitSkillsManager."""
    global _GIT_MGR
    if cache_dir is not None:
        return GitSkillsManager(cache_dir=cache_dir)
    target_cache = get_skills_cache_dir()
    if _GIT_MGR is None or _GIT_MGR.cache_dir != target_cache:
        _GIT_MGR = GitSkillsManager(cache_dir=target_cache)
    return _GIT_MGR


def get_bundler(
    project_root: Path | None = None,
    storage: SkillsStorage | None = None,
    compiler: SlotCompiler | None = None,
    skills_dir: Path | None = None,
) -> AgentBundler:
    """Возвращает экземпляр AgentBundler.

    ``skills_dir`` — каталог репозитория скиллов (напр. --skills-dir сервера):
    карточки субагентов ищутся в ``<skills_dir>/agents`` в приоритете.
    """
    global _BUNDLER
    if storage is not None or compiler is not None or skills_dir is not None:
        store = storage or get_storage(project_root)
        comp = compiler or get_compiler(project_root, store)
        return AgentBundler(storage=store, compiler=comp, skills_dir=skills_dir)
    if _BUNDLER is None:
        store = get_storage(project_root)
        comp = get_compiler(project_root, store)
        _BUNDLER = AgentBundler(storage=store, compiler=comp)
    return _BUNDLER


def load_agent(
    name: str,
    target: str = "antigravity",
    project: str | None = None,
    project_root: Path | None = None,
    storage: SkillsStorage | None = None,
    compiler: SlotCompiler | None = None,
    bundler: AgentBundler | None = None,
    synchronizer: SkillsSynchronizer | None = None,
) -> dict[str, Any]:
    """Сборка и экспорт профиля субагента со скиллами под целевой рантайм."""
    root = project_root or _find_default_project_root()
    sync = synchronizer or get_synchronizer(root, storage)
    if synchronizer is None:
        # Синхронизируем только при самостоятельном создании (инжектированный синк управляется тестом/клиентом)
        sync.sync()
    canonical_project = project or detect_canonical_project_name(root)
    bnd = bundler or get_bundler(root, storage, compiler)
    existing_override = bnd.find_override(name, target, canonical_project, project_root=root)
    is_draft = existing_override is None
    draft_override: dict[str, Any] | None = None
    if is_draft:
        draft = create_default_override(name, target, canonical_project)
        draft_override = draft.model_dump(exclude_none=True)
    content = bnd.bundle_agent(
        name=name,
        target=target,
        project_root=root,
        project=canonical_project,
        override=existing_override,
    )
    return {
        "agent": name,
        "target": target,
        "project": canonical_project,
        "is_draft": is_draft,
        "draft_override": draft_override,
        "content": content,
    }


def install_agent_workflow(
    name: str,
    target: str = "antigravity",
    project: str | None = None,
    project_root: Path | None = None,
    storage: SkillsStorage | None = None,
    compiler: SlotCompiler | None = None,
    bundler: AgentBundler | None = None,
    synchronizer: SkillsSynchronizer | None = None,
) -> dict[str, Any]:
    """Экспортирует и сохраняет артефакты агента в рабочем пространстве проекта."""
    root = project_root or _find_default_project_root()
    sync = synchronizer or get_synchronizer(root, storage)
    if synchronizer is None:
        # Синхронизируем только при самостоятельном создании (инжектированный синк управляется тестом/клиентом)
        sync.sync()
    bnd = bundler or get_bundler(root, storage, compiler)
    return bnd.install_workflow(name=name, target=target, project_root=root, project=project)


def _word_overlap(w1: str, w2: str) -> bool:
    if not w1 or not w2:
        return False
    return (w1 == w2) or ((w1.startswith(w2) or w2.startswith(w1)) and min(len(w1), len(w2)) >= 3)


def _score_alias_matches(w: str, aliases_list: list[str]) -> int:
    """Оценка совпадения слова запроса с объявленными алиасами сценария.

    КАП: бонус не более одного раза на слово запроса — иначе скор растёт
    с числом алиасов, и арбитраж (top−second ≤ 5) становится функцией
    объёма каталога, а не семантики запроса (замер: «план» → 49/43,
    delta 6 = ровно один лишний «план»-алиас у execution против drafting).
    """
    alias_hit = False
    prefix_hit = False
    for alias in aliases_list:
        alias_words = [re.sub(r"[^\w]+", "", aw).lower() for aw in alias.split() if len(aw) >= 3]
        if w in alias_words:
            alias_hit = True
        elif any(_word_overlap(w, aw) for aw in alias_words):
            prefix_hit = True
    if alias_hit:
        return 6
    if prefix_hit:
        return 5
    return 0


def _score_one_flow(f: dict[str, Any], clean_query: str, q_words: list[str]) -> int:
    """Оценка одного флоу по запросу (E2-4: с учётом intent_group)."""
    title_text = f.get("title", "").lower()
    name_text = f.get("name", "").lower()
    desc_text = f.get("description", "").lower()
    aliases_raw = f.get("aliases", [])
    aliases_list = [a.lower() for a in aliases_raw if isinstance(a, str)]
    aliases_blob = " ".join(aliases_list)
    when_blob = " ".join(t.lower() for t in f.get("when_to_use", []) if isinstance(t, str))
    when_matched = False
    # E2-4 (verdict_4:90): intent_group — заявленная автором группа интентов.
    # Совпадение слова запроса с ней означает «этот флоу про то, что просят»:
    # сильнее совпадения по description, слабее точного совпадения по алиасу.
    group_text = str(f.get("intent_group") or "").lower()
    # Разбиваем по границам слов, а не только по пробелам: дефисные группы
    # («code-review», «plan-execution», «feature-delivery») иначе склеиваются в
    # один токен («codereview»), который не совпадает ни с одним словом запроса,
    # а короткие группы («qa») отбрасываются фильтром len >= 3 и становятся
    # недостижимыми. Замер на 13 сценариях: до правки через intent_group
    # матчились 4 токена запроса из 10, после — 10 из 10.
    group_words = [w for w in re.split(r"[^\w]+", group_text) if len(w) >= 2]

    # Отрицательная маршрутизация (exclude_aliases): если запрос совпадает
    # с явно исключённым действием (например «опубликуй релиз» для сценария,
    # который создаёт лишь локальный тег), сценарий исключается из выдачи.
    exclude_raw = f.get("exclude_aliases") or []
    exclude_list = [a.lower().strip() for a in exclude_raw if isinstance(a, str) and a.strip()]
    clean_lower = clean_query.lower()
    for ea in exclude_list:
        if ea in clean_lower:
            return 0
        ea_words = [re.sub(r"[^\w]+", "", w).lower() for w in ea.split() if len(w) >= 3]
        if ea_words and all(w in q_words for w in ea_words):
            return 0

    score = 0
    if clean_query.lower() in title_text or clean_query.lower() in name_text:
        score += 20
    elif clean_query.lower() in aliases_blob:
        score += 15
    elif clean_query.lower() in desc_text:
        score += 10

    # Прямое совпадение фразы запроса с intent_group (например «plan-execution»)
    if clean_query.lower() in group_text:
        score += 8

    for w in q_words:
        # Прямые совпадения слова
        if w in title_text:
            score += 5
        if w in name_text:
            score += 4
        if w in desc_text:
            score += 1
        if not when_matched and w in when_blob:
            when_matched = True
            score += 5
        # E2-4: слово запроса совпало с intent_group флоу
        if w in group_words:
            score += 3
        # Совпадение по объявленным алиасам сценария (Single Source of Truth).
        score += _score_alias_matches(w, aliases_list)
    return score


def _score_flows(
    flow_list: list[dict[str, Any]],
    clean_query: str,
    fts_bonuses: dict[str, float] | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[float, dict[str, Any]]]]:
    """Ранжирует сценарии по запросу; ``fts_bonuses`` уточняет порядок.

    ``fts_bonuses`` применяется ТОЛЬКО к сценариям, которые пословный скоринг
    уже признал релевантными (score > 0) — то есть переставляет порядок, но не
    расширяет выдачу. Замер на 31 релевантном и 24 «нейтральных» запросах:
    перестановка поднимает top-1 с 61 % до 84 % и снижает ложный арбитраж с
    5/31 до 2/31, при этом среднее число кандидатов на нейтральном запросе
    остаётся 0.38. Расширение выдачи силами FTS (OR-по-триграммам слишком
    мягкое) дало бы +1 случай на бенчмарке ценой 5.79 кандидатов на нейтральном
    запросе — это распыление каталога и ложные вопросы пользователю.
    """
    q_words = [re.sub(r"[^\w]+", "", w).lower() for w in clean_query.split() if len(w) >= 3]

    scored_flows: list[tuple[float, dict[str, Any]]] = []
    for f in flow_list:
        score: float = float(_score_one_flow(f, clean_query, q_words))
        if score > 0:
            if fts_bonuses:
                score += fts_bonuses.get(str(f.get("name") or ""), 0.0)
            scored_flows.append((score, f))

    scored_flows.sort(key=lambda x: x[0], reverse=True)
    return [f for _, f in scored_flows], scored_flows


def _flow_fts_bonuses(flow_list: list[dict[str, Any]], clean_query: str) -> dict[str, float]:
    """Позиционный бонус FTS5-триграмм по сценариям; ``{}`` если сигнала нет.

    Построение in-memory индекса стоит ~5 мс на 13 сценариях (замерено), поэтому
    постоянной таблицы и её миграций не нужно: арность существующих FTS-таблиц
    не меняется, а обнуления живых индексов при bump версии схемы не возникает.
    Любая ошибка построения/MATCH даёт пустой словарь — поиск деградирует на
    пословный скоринг, а не возвращает пустую выдачу.
    """
    if not clean_query or not flow_list:
        return {}
    result = build_flow_fts_index(flow_list)
    try:
        return fts_bonus_rank(result, clean_query)
    finally:
        result.close()


def _strip_stage_text(flows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Убирает ``stage_text`` из сценариев перед отправкой в ответ MCP.

    Текст этапов (~44 КБ на 13 сценариев) нужен только FTS-индексу; в выдаче
    инструмента он — расход токенов клиента без пользы.
    """
    if not any("stage_text" in f for f in flows):
        return flows
    return [{k: v for k, v in f.items() if k != "stage_text"} for f in flows]


def _search_flows(root: Path, clean_query: str, skills_dir: Path | None = None) -> dict[str, Any]:
    # ``include_stage_text`` нужен FTS-индексу: доменная лексика этапов не входит
    # в метаданные, и без неё индекс недобирает совпадений. Из ответа MCP текст
    # этапов вырезается (_strip_stage_text) — клиенту он не нужен.
    flow_list = list_available_flows(
        root, skills_dir=skills_dir, include_stage_text=bool(clean_query)
    )
    base_scored: list[tuple[float, dict[str, Any]]] = []
    if clean_query:
        # Два прогона скоринга — дёшево (~0.1 мс на 30 запросов, замерено) и
        # разделяет два разных сигнала:
        #   base_scored — первичное лексическое ранжирование, ТОЛЬКО для решения
        #                 об арбитраже;
        #   flow_list   — порядок выдачи, уточнённый FTS-триграммами.
        base_scored = _score_flows(flow_list, clean_query)[1]
        bonuses = _flow_fts_bonuses(flow_list, clean_query)
        flow_list = _score_flows(flow_list, clean_query, bonuses)[0]

    is_ambiguous = False
    recommendation: str | None = None
    arbitration_options: list[dict[str, Any]] = []

    if clean_query and len(flow_list) >= 2 and len(base_scored) >= 2:
        # Арбитраж решается по ПЕРВИЧНОМУ лексическому скору и по базовому
        # порядку топ-2, а не по итоговой (FTS-переставленной) выдаче. Иначе FTS
        # гасит законный вопрос пользователю двумя способами:
        #   1) ступенька бонуса (20/10/6.7) шире окна ±5 — разрыв растёт сам по себе;
        #   2) перестановка меняет то, КАКАЯ пара топ-2 сравнивается.
        # Оба эффекта были пойманы тестами: «план» (эффект 1) и
        # «plan the feature» (эффект 2 — feature-delivery/plan-execution-flow
        # delta 5 заменялась на feature-delivery/plan-execution-auto-flow).
        top_flow = base_scored[0][1]
        second_flow = base_scored[1][1]
        top_score = base_scored[0][0]
        second_score = base_scored[1][0]
        # E2-5 (verdict_4:91, ревизия порога): арбитраж нужен, когда конкурируют
        # РАЗНЫЕ интенты. Флоу ОДНОЙ явно заявленной группы (intent_group
        # непустой и одинаковый у топ-2) — варианты одного интента (авто/ручной
        # режим исполнения): движок вправе взять топ-1 без вопроса пользователю.
        # Отсутствие заявленной группы ничего не доказывает — сохраняем
        # legacy-поведение (арбитраж при близких скорах).
        top_group = str(top_flow.get("intent_group") or "")
        second_group = str(second_flow.get("intent_group") or "")
        same_group = bool(top_group) and top_group == second_group
        if top_score - second_score <= 5 and top_score > 0 and not same_group:
            is_ambiguous = True
            recommendation = (
                "Найдено несколько подходящих сценариев разработки. "
                "Не выбирайте сценарий самовольно! Запросите выбор у пользователя через "
                "нативный инструмент среды (ask_question в Antigravity, question в OpenCode, "
                "AskUserQuestion в Claude Code, либо вопрос в чат)."
            )
            arbitration_options = [
                {
                    "flow": f["name"],
                    "title": f.get("title", f["name"]),
                    "description": f.get("description", ""),
                    "intent_group": f.get("intent_group"),
                    "when_to_use": f.get("when_to_use", []),
                }
                for f in flow_list[:5]
            ]

    visible_flows = _strip_stage_text(flow_list)
    response: dict[str, Any] = {
        "mode": "flows",
        "count": len(visible_flows),
        "query": clean_query,
        "flows": visible_flows,
    }
    if is_ambiguous:
        response["is_ambiguous"] = True
        response["recommendation"] = recommendation
        response["arbitration_options"] = arbitration_options
    elif clean_query and not visible_flows:
        # Пустая выдача без объяснения читается как «в каталоге нет ничего
        # похожего» — в пилотных прогонах агент на этом останавливался.
        # Подсказка отделяет «нет такого сценария» от «нет таких слов в
        # метаданных»: второе лечится добавлением лексики в flow, а не сменой
        # механики поиска.
        response["hint"] = (
            "Ни один сценарий не совпал с запросом. Это не обязательно значит, что "
            "подходящего сценария нет: сопоставление идёт по метаданным flow "
            "(name/title/description/aliases/intent_group/when_to_use). Посмотрите "
            "полный список через search_skills() без query и выберите по смыслу."
        )
    return response


def _flow_brief(flow: dict[str, Any], max_len: int = 90) -> str:
    """Короткое описание flow для overview: первое предложение без markdown-шума.

    Описание в frontmatter начинается с длинной вводной («Сценарий регламентирует…»);
    для витрины каталога достаточно смысла, обрезанного до ~90 символов.
    """
    raw = str(flow.get("description") or flow.get("title") or "")
    raw = raw.split("---")[0].replace("\n", " ").strip()
    raw = re.sub(r"\*\*|\*", "", raw)  # markdown-акценты не несут смысла в JSON-витрине
    for sep in (". ", ".\n"):
        idx = raw.find(sep)
        if idx > 20:
            return raw[: idx + 1][:max_len]
    return raw[:max_len].rstrip() + ("…" if len(raw) > max_len else "")


STACK_CONVENTIONS_FILENAME = "docs/stack-conventions.yaml"


def _load_stack_conventions(skills_dir: Path | None) -> dict[str, str]:
    """Читает и валидирует семантику значений стека из docs/stack-conventions.yaml.

    Формат: словарь ``<value>: {description: ...}`` (краткий ``<value>: <str>``
    также допустим). Валидация: имя через ``is_safe_name`` (без path traversal),
    description непустой. Некорректные записи пропускаются с warning — сервер
    продолжает работать на валидной части словаря, fail-soft для контент-файла.
    """
    base = skills_dir or resolve_skills_repo_dir()
    path = base / STACK_CONVENTIONS_FILENAME
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        logger.warning("Не удалось распарсить %s: %s — используем generic-описания", path, e)
        return {}
    if not isinstance(data, dict):
        logger.warning("%s: ожидается словарь значений стека — используем generic-описания", path)
        return {}
    meanings: dict[str, str] = {}
    for raw_key, raw_val in data.items():
        key = str(raw_key).strip().lower()
        if not key or not is_safe_name(key):
            logger.warning("%s: недопустимое имя значения стека %r — пропущено", path, raw_key)
            continue
        desc = raw_val.get("description") if isinstance(raw_val, dict) else raw_val
        desc = str(desc or "").strip()
        if not desc:
            logger.warning("%s: у значения %r нет description — пропущено", path, key)
            continue
        meanings[key] = desc
    return meanings


def _collect_stack_values(store: SkillsStorage, skills_dir: Path | None = None) -> dict[str, str]:
    """Плоский словарь значений стека движка: значение → когда использовать.

    Источник значений — фактические файлы блоков ``blocks/<slot>/<tech>.md`` (union
    по всем скиллам), а НЕ тематические теги frontmatter ``stack`` (git, refactoring, ...
    — это таксономия для FTS-поиска через ``query``, в стек не передаются).
    Семантика — из docs/stack-conventions.yaml репозитория скиллов (контент, не код).
    Слоты из выдачи убраны: LLM передаёт только значение стека, движок сам применяет
    его в слотах, где есть соответствующий блок.

    Кэшируется per-storage: сигнатура из таблицы file_sync (обновляется
    синхронизатором при изменении любого файла скилла) + mtime файла конвенций.
    Общие блоки входят в сигнатуру по пути, mtime и размеру; локальные
    значения повторно не сканируются при совпадении сигнатуры.
    """
    conv_path = (skills_dir or resolve_skills_repo_dir()) / STACK_CONVENTIONS_FILENAME
    try:
        conv_sig = conv_path.stat().st_mtime_ns
    except OSError:
        conv_sig = 0

    # Дешёвая сигнатура состояния индекса: хэш ревизии file_sync.
    # skills_dir входит в ключ: разные каталоги скиллов при одном storage
    # дают разные значения стека (E2E-коллизия закэшированных ответов).
    files_sig = store.get_dependency_revision()
    shared_dir = (skills_dir or resolve_skills_repo_dir()) / "blocks"
    shared_files = sorted(shared_dir.glob("*/*.md"))
    shared_sig = []
    for path in shared_files:
        try:
            stat = path.stat()
            shared_sig.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            continue
    signature = f"{files_sig}:{conv_sig}:{skills_dir or ''}:{shared_sig}"

    cached = _STACK_VALUES_CACHE.get(store)
    if cached is not None and cached[0] == signature:
        return cached[1]

    values: set[str] = set()
    with store.get_connection() as conn:
        rows = conn.execute("SELECT metadata FROM skills_index").fetchall()
    for row in rows:
        card = SkillCard(**json.loads(row["metadata"]))
        if not card.base_dir:
            continue
        blocks_dir = card.base_dir / "blocks"
        if not blocks_dir.is_dir():
            continue
        for slot_dir in blocks_dir.iterdir():
            if not slot_dir.is_dir():
                continue
            for p in slot_dir.glob("*.md"):
                stem = p.stem.lower()
                if stem != "default" and is_safe_name(p.stem):
                    values.add(stem)
    values.update(
        p.stem.lower() for p in shared_files if p.stem.lower() != "default" and is_safe_name(p.stem)
    )
    meanings = _load_stack_conventions(skills_dir)
    result = {
        v: meanings.get(v, "локальная вариация блока для этого значения") for v in sorted(values)
    }
    _STACK_VALUES_CACHE[store] = (signature, result)
    return result


def _resolve_search_stack(
    project_stack: list[str] | str | None,
    root: Path,
) -> list[str] | None:
    """Разрешает параметр project_stack для search_skills согласно §2.4 п.2.

    D6: явный стек дополняется ТОЛЬКО осью режима (mcp/offline) — семантика
    едина с load_skill и bundle_agent через ProjectContext; технологии
    (язык/фреймворк) в явный стек не подмешиваются.
    """
    if project_stack is None or project_stack == "" or project_stack == []:
        return None
    ctx = get_project_context(root)
    if project_stack == "auto":
        if not ctx.stack and not ctx.mode:
            return None
        elements = set(ctx.stack)
        if ctx.mode:
            elements.add(ctx.mode)
        return sorted(elements)
    norm = _normalize_str_list(project_stack)
    if not norm:
        return None
    supplemented, _ = supplement_mode_axis(norm, ctx.mode)
    return supplemented


def _search_skill_category(
    store: SkillsStorage,
    category: str,
    resolved_stack: list[str] | None,
    stack_source: str,
    skills_dir: Path | None,
) -> dict[str, Any]:
    if resolved_stack is not None:
        drill_res = store.search_skills(
            query="", category=category, limit=100, project_stack=resolved_stack
        )
        cards, truncated = drill_res if isinstance(drill_res, tuple) else (drill_res, False)
        category_results = []
        for c in cards:
            app = check_skill_applicability(c, resolved_stack)
            item: dict[str, Any] = {
                "name": c.name,
                "title": c.title,
                "description": c.description,
                "applicability": app,
            }
            if app == "applicable":
                item["applicability_reason"] = "Применимо к стеку проекта"
            elif app == "unknown":
                item["applicability_reason"] = (
                    "Требуемые технологии не подтверждены манифестами проекта (unknown)"
                )
            elif app == "incompatible":
                item["applicability_reason"] = "Несовместимо со стеком проекта"
            category_results.append(item)
    else:
        drill_cards = store.search_skills(query="", category=category, limit=100)
        cards = drill_cards if isinstance(drill_cards, list) else drill_cards[0]
        truncated = False
        category_results = [
            {"name": c.name, "title": c.title, "description": c.description} for c in cards
        ]
    resp_category: dict[str, Any] = {
        "mode": "category",
        "category": category,
        "count": len(cards),
        "results": category_results,
        "stack_values": _collect_stack_values(store, skills_dir),
        "hint": (
            "Для полной инструкции вызовите load_skill(name='...'). "
            "Значения stack — общий словарь движка (stack_values выше)."
        ),
    }
    if resolved_stack is not None:
        resp_category["stack_filter"] = {
            "applied": True,
            "project_stack": resolved_stack,
            "source": stack_source,
            "truncated": truncated,
            "reason": (
                "Стек автодетектирован из манифестов проекта"
                if stack_source == "autodetect"
                else "Стек задан явно в параметрах поиска"
            ),
        }
    return resp_category


def _annotate_applicability(card: SkillCard, stack: list[str]) -> None:
    applicability = check_skill_applicability(card, stack)
    card.applicability = applicability  # type: ignore[assignment]
    reasons = {
        "applicable": "Применимо к стеку проекта",
        "unknown": "Требуемые технологии не подтверждены манифестами проекта (unknown)",
        "incompatible": "Несовместимо со стеком проекта",
    }
    card.applicability_reason = reasons[applicability]


def search_skills(
    query: str = "",
    type: Literal["all", "skill", "flow"] = "all",  # noqa: A002
    category: str | None = None,
    limit: int = 10,
    storage: SkillsStorage | None = None,
    synchronizer: SkillsSynchronizer | None = None,
    project_root: Path | None = None,
    skills_dir: Path | None = None,
    project_stack: list[str] | str | None = None,
) -> dict[str, Any]:
    """Полнотекстовый поиск по скиллам и сценариям с поддержкой общей сводки каталога (overview) при zero-query.

    Режимы (Progressive Disclosure — экономия токенов):
    - Zero-query (query='' и category=None): компактная сводка каталога — категории со
      счетчиками и имена flows (~150-300 токенов). Без описаний.
    - Drilldown (query='' и category=...): список скиллов категории тощими карточками
      {name, title, description} — LLM сам запрашивает нужную категорию после overview.
    - type='flow': возвращает список определений сценариев (flows).
    - type='skill' с поисковым запросом: выполняет FTS5 поиск по скиллам.
    - type='all' с поисковым запросом (E2): скиллы через FTS5 + релевантные флоу
      (ключ ``flows``, ранжирование _score_flows по intent_group/алиасам) в той же
      выдаче — флоу не теряются за пределами type='flow'.
    """
    root = project_root or _find_default_project_root()
    clean_query = query.strip()
    resolved_stack: list[str] | None = _resolve_search_stack(project_stack, root)
    stack_source = (
        "autodetect"
        if project_stack == "auto"
        else "explicit"
        if resolved_stack is not None
        else "unfiltered"
    )

    # 1. Поиск только по сценариям (flows)
    if type == "flow":
        return _search_flows(root, clean_query, skills_dir=skills_dir)

    store = storage or get_storage(root)

    # 2. Zero-query Overview (сводка каталога без описаний)
    if not clean_query and category is None and type in ("all", "skill"):
        # Фильтруем счётчики тем же стеком, что drilldown/search — иначе overview
        # разойдётся с выдачей категории (verdict_7b; MCP-обёртка всегда передаёт
        # project_stack="auto"). resolved_stack вычислен выше ДО ветки overview.
        category_counts = store.get_category_counts(
            project_stack=resolved_stack if resolved_stack is not None else None
        )
        # Нулевые категории скрываем ТОЛЬКО при переданном стеке: без стека
        # cold-start {c: 0} обязан показывать весь реестр категорий.
        if resolved_stack is not None:
            category_counts = {c: n for c, n in category_counts.items() if n > 0}
        flows_summary = [
            {"name": f["name"], "description": _flow_brief(f)}
            for f in list_available_flows(root, skills_dir=skills_dir)
        ]
        categories_payload = [
            {"category": cat, "skills_count": count} for cat, count in category_counts.items()
        ]
        overview: dict[str, Any] = {
            "mode": "overview",
            "categories": categories_payload,
            "flows": flows_summary,
            # Словарь стека общий для движка — union значений blocks/<slot>/<tech>.md
            # по всем скиллам репозитория, без привязки к конкретному скиллу.
            "stack_values": _collect_stack_values(store, skills_dir),
            "hint": (
                "Значения stack (stack_values) — оси сборки: передавайте в load_skill(stack=[...]) "
                "согласно описанию. Тематические теги (git, postgres, ...) — не стек: "
                "ищите скиллы по ним через search_skills(query='...'). "
                "Для списка скиллов категории: search_skills(category='...')."
            ),
        }
        if resolved_stack is not None:
            overview["stack_filter"] = {
                "applied": True,
                "project_stack": resolved_stack,
                "source": stack_source,
                "truncated": False,
                "reason": (
                    "Стек автодетектирован из манифестов проекта"
                    if stack_source == "autodetect"
                    else "Стек задан явно в параметрах поиска"
                ),
            }
        return overview

    # 3. Drilldown по категории: тощие карточки без полного SkillCard-дампа
    #    (base_dir, aliases, merge_strategy и пр. не нужны для выбора скилла).
    truncated: bool
    if not clean_query and category is not None:
        return _search_skill_category(store, category, resolved_stack, stack_source, skills_dir)

    # 4. Полнотекстовый поиск скиллов через FTS5
    sync = synchronizer or get_synchronizer(root, store)
    if synchronizer is None:
        sync.sync()
    effective_limit = max(1, min(limit, 100)) if limit > 0 else 10
    results: list[SkillCard]
    if resolved_stack is not None:
        search_res = store.search_skills(
            query=clean_query,
            category=category,
            limit=effective_limit,
            project_stack=resolved_stack,
        )
        results, truncated = search_res if isinstance(search_res, tuple) else (search_res, False)
        for result in results:
            _annotate_applicability(result, resolved_stack)
    else:
        plain_res = store.search_skills(query=clean_query, category=category, limit=effective_limit)
        results = plain_res if isinstance(plain_res, list) else plain_res[0]
        truncated = False
    response: dict[str, Any] = {
        "mode": "search",
        "count": len(results),
        "query": clean_query,
        "category": category,
        "results": [r.model_dump(mode="json", exclude_defaults=True) for r in results],
    }
    if resolved_stack is not None:
        response["stack_filter"] = {
            "applied": True,
            "project_stack": resolved_stack,
            "source": stack_source,
            "truncated": truncated,
            "reason": (
                "Стек автодетектирован из манифестов проекта"
                if stack_source == "autodetect"
                else "Стек задан явно в параметрах поиска"
            ),
        }
    # E2-1 (verdict_4:86): type='all' с запросом обязан показывать флоу рядом
    # со скиллами — до фикса FTS-ветка отдавала только скиллы, и fresh-агент
    # считал «в каталоге нет ничего похожего» (E2E: 6/8 промптов). Флоу
    # ранжируются тем же _score_flows; только релевантные (score > 0).
    if type == "all" and clean_query and category is None:
        flow_list = list_available_flows(root, skills_dir=skills_dir, include_stage_text=True)
        bonuses = _flow_fts_bonuses(flow_list, clean_query)
        matched_flows, _scored = _score_flows(flow_list, clean_query, bonuses)
        matched_flows = _strip_stage_text(matched_flows)
        if matched_flows:
            response["flows"] = matched_flows
            response["flows_count"] = len(matched_flows)
            response["hint"] = (
                "flows — релевантные сценарии разработки (ранжированы по запросу); "
                "подробности и арбитраж: search_skills(type='flow', query='...')."
            )
    if not results and not category:
        # Скиллы ищутся через FTS5 в skills_index; сценарии — через FTS5-триграммы
        # в _search_flows/_flow_fts_bonuses. Но оба индекса сопоставляют только те
        # слова, что есть в метаданных: запрос из терминов, отсутствующих в
        # описаниях скиллов, даст пустую выдачу при живом каталоге. Без подсказки
        # fresh-агент решает, что «в каталоге нет ничего похожего» (E2E-находка: 6/8).
        # E2-1: если флоу уже показаны выше — «ничего не найдено» ложь, hint другой.
        if "flows" in response:
            response["hint"] = (
                "Среди скиллов ничего не найдено, но есть подходящие сценарии "
                "(flows выше): вызовите search_skills(type='flow', query='...') "
                "для арбитража и деталей."
            )
        else:
            response["hint"] = (
                "Ничего не найдено среди скиллов. Флоу НЕ индексируются в FTS: "
                "для поиска сценариев вызовите search_skills(type='flow') "
                "(с query — поиск по алиасам с арбитражем) или search_skills() для overview."
            )
    return response


def load_skill(
    name: str,
    stack: list[str] | str | None = None,
    touches: list[str] | None = None,
    resource_id: str | None = None,
    cursor: str | None = None,
    storage: SkillsStorage | None = None,
    synchronizer: SkillsSynchronizer | None = None,
    compiler: SlotCompiler | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Динамическая сборка скилла под стек проекта с заполнением слотов или чтение ресурсов.

    Параметр ``stack`` принимает список технологий или CSV-строку
    (например ``"python,fastapi"``) — защита от MCP-хостов, сериализующих
    массивы аргументов в строки.
    Параметр ``resource_id`` позволяет читать Markdown-ресурс, связанный со скиллом
    (справочник, шаблон, пример). В этом режиме stack и touches запрещены.
    Параметр ``cursor`` передаётся для продолжения чтения большого ресурса.
    """
    root = project_root or _find_default_project_root()
    sync = synchronizer or get_synchronizer(root, storage)
    if synchronizer is None:
        # Синхронизируем только при самостоятельном создании (инжектированный синк управляется тестом/клиентом)
        sync.sync()
    store = storage or get_storage(root)
    comp = compiler or get_compiler(root, store)

    if resource_id is not None:
        if stack is not None or touches is not None:
            raise ValueError(
                "Неоднозначный запрос: в режиме чтения ресурса (resource_id) "
                "параметры stack и touches не принимаются. Для сборки скилла вызовите "
                "load_skill без resource_id, для чтения ресурса — без stack и touches."
            )
        card_entry = store.get_skill(name)
        if not card_entry:
            msg = f"Скилл '{name}' не найден в хранилище"
            raise SkillNotFoundError(msg)
        card, _ = card_entry
        cat_root = comp._detect_catalog_root(card.base_dir) if (card.base_dir and comp) else None
        if cat_root is None:
            cat_root = resolve_skills_repo_dir()
        if not is_resource_associated_with_skill(
            name, resource_id, cat_root, card.base_dir, storage=store, project_root=root
        ):
            raise PermissionError(f"Ресурс '{resource_id}' не связан со скиллом '{name}'")
        chunk = read_resource_chunk(resource_id, cat_root, cursor=cursor, project_root=root)
        return {
            "name": name,
            "resource_id": chunk["resource_id"],
            "anchor": chunk["anchor"],
            "content": chunk["content"],
            "revision": chunk["revision"],
            "next_cursor": chunk["next_cursor"],
            "total_chars": chunk["total_chars"],
            "offset": chunk["offset"],
            "was_truncated": chunk["was_truncated"],
        }

    # Нормализация и валидация стека: list | tuple | CSV-строка | None
    items = _normalize_str_list(stack)
    normalized_stack = _validate_stack_items(items) if items is not None else None

    stack_source: str | None = None
    stack_supplemented: list[str] = []
    ctx = get_project_context(root)
    if normalized_stack is None:
        if ctx.stack or ctx.mode:
            # Автодетект: технологии + режим, дедуп с сохранением порядка
            normalized_stack = list(
                dict.fromkeys([*ctx.stack, ctx.mode] if ctx.mode else ctx.stack)
            )
            stack_source = "autodetect"
    else:
        stack_source = "explicit"
        # D6 (решение №2, verdict_7c): явный стек дополняется ТОЛЬКО осью
        # режима — иначе в MCP-проекте load_skill(stack=['fastapi']) берёт
        # default-зонд вместо blocks/<slot>/mcp.md. Технологии НЕ дополняются.
        normalized_stack, stack_supplemented = supplement_mode_axis(normalized_stack, ctx.mode)

    # Fail-fast: карточка должна существовать до сборки. Двойной get_skill
    # (server + компилятор) — осознанное решение: серверу нужны available_slots.
    card_preview = store.get_skill(name)
    if not card_preview:
        msg = f"Скилл '{name}' не найден в хранилище"
        raise SkillNotFoundError(msg)

    # Сборка через SlotCompiler: единый контракт для touches, extends и слотов
    assembly = comp.assemble_result(
        name=name,
        touches=touches,
        project_stack=normalized_stack,
        project_root=root,
    )
    card, content = assembly.card, assembly.content
    resources = assembly.resources
    manifest = assembly.manifest
    missing_slots = assembly.missing_slots
    slots_filled = assembly.slots_filled
    available_slots = comp.collect_available_slots(name)

    guard = TokenGuard()
    bounded_content, was_truncated = guard.enforce_budget(content, raise_on_core_overflow=True)

    applicability = check_skill_applicability(card, normalized_stack)

    res = {
        "name": card.name,
        "title": card.title,
        "category": card.category,
        "applicability": applicability,
        "content": bounded_content,
        "was_truncated": was_truncated,
        "resources": resources,
        "slots_filled": slots_filled,
        # Ось стека общая для движка (язык / режим / фреймворк); конкретные
        # варианты берутся из ФС: blocks/<slot>/<tech>.md — авторинг per-skill не нужен.
        "slot_variants": comp.slot_variants(card, available_slots),
        "available_slots": available_slots,
        "optional_slots": sorted(comp.optional_slots(name)),
        # stack_applied: эффективный входной стек сборки (совместимость).
        # Не гарантирует наличие конкретного блока технологии: для точной проверки
        # см. manifest assembly (matched_technologies / unmatched_technologies).
        "stack_applied": normalized_stack or [],
        "stack_source": stack_source,
        # D6: какие оси режима добавлены к явному стеку автоматически
        # (пусто при автодетекте и когда режим передан явно).
        "stack_supplemented": stack_supplemented,
        "assembly": manifest,
        "status": "incomplete" if missing_slots else "complete",
    }
    if missing_slots:
        res["missing_slots"] = missing_slots
    return res


def _collect_available_slots(store: SkillsStorage, name: str) -> list[str]:
    """Собирает слоты скилла: предки (extends) + собственные через компилятор."""
    return SlotCompiler(store).collect_available_slots(name)


def save_agent_override(
    name: str,
    project: str,
    target: str,
    content: str,
    commit_message: str | None = None,
    git_manager: GitSkillsManager | None = None,
    cache_dir: Path | None = None,
) -> dict[str, Any]:
    """Сохраняет оверрайд агента в центральный клон кэша."""
    mgr = git_manager or get_git_manager(cache_dir=cache_dir)
    res = mgr.save_agent_override(
        name=name,
        project=project,
        target=target,
        content=content,
        commit_message=commit_message,
    )
    return {
        "success": res.success,
        "path": res.path,
        "issues": res.issues,
        "message": res.message,
    }


def create_server(
    project_root: Path | None = None,
    db_path: Path | None = None,
    skills_dir: Path | None = None,
    storage: SkillsStorage | None = None,
    synchronizer: SkillsSynchronizer | None = None,
    compiler: SlotCompiler | None = None,
    bundler: AgentBundler | None = None,
    multi_tenant: bool = False,
) -> MCPServer:
    """Создаёт и конфигурирует экземпляр MCPServer с инструментами управления скиллами.

    Root-guard (P1): ``write_ok`` (флаг разрешения записи write_ok) — передан
    явный ``project_root`` ИЛИ корень закреплён (env / walk-up / закреплённый
    ``--project-root``). Явный ``db_path`` разрешением записи НЕ считается: он пиннит
    только эфемерный FTS-кэш, а flows.db пишется под root.
    """
    default_resolved = resolve_factory_root(project_root) if not multi_tenant else None

    if storage is not None:
        def_root = default_resolved.root if default_resolved is not None else Path.cwd()
        sync = synchronizer or get_synchronizer(def_root, storage, skills_dir=skills_dir)
        comp = compiler or get_compiler(def_root, storage, catalog_root=skills_dir)
        bnd = bundler or get_bundler(def_root, storage, comp, skills_dir=skills_dir)
        default_bundle = SkillsServiceBundle(
            root=def_root,
            storage=storage,
            synchronizer=sync,
            compiler=comp,
            bundler=bnd,
        )
        pool: ProjectServicePool[SkillsServiceBundle] | None = None
        write_ok_default = True
    else:

        def _factory(r: Path) -> SkillsServiceBundle:
            is_project = is_anchored(r) or (r / ".agents").exists()
            if is_project:
                target_db = (
                    db_path
                    if (
                        default_resolved is not None
                        and r == default_resolved.root
                        and db_path is not None
                    )
                    else (get_cache_dir(r) / "skills_fts.db")
                )
                target_db.parent.mkdir(parents=True, exist_ok=True)
            else:
                import hashlib

                h = hashlib.sha256(str(r).encode()).hexdigest()[:12]
                target_db = Path(tempfile.gettempdir()) / f"mcp_skills_unanchored_{h}.db"
                _UNANCHORED_TEMP_DBS.add(target_db)

            store = SkillsStorage(target_db)
            sync = get_synchronizer(r, store, skills_dir=skills_dir)
            sync.sync()
            comp = get_compiler(r, store, catalog_root=skills_dir)
            bnd = get_bundler(r, store, comp, skills_dir=skills_dir)
            return SkillsServiceBundle(
                root=r,
                storage=store,
                synchronizer=sync,
                compiler=comp,
                bundler=bnd,
            )

        pool = ProjectServicePool(_factory)
        if not multi_tenant and default_resolved is not None:
            default_bundle = pool.get(default_resolved.root)
            write_ok_default = bool(project_root) or default_resolved.anchored
        else:
            default_bundle = None
            write_ok_default = False

    if default_resolved is not None:
        log_root_banner("skills-engine", default_resolved, write_ok_default)

    def resolver(project_path: str | None) -> tuple[SkillsServiceBundle, ResolvedRoot, bool]:
        res = resolve_dynamic_project_root(
            project_path=project_path,
            multi_tenant=multi_tenant,
            default_root=default_resolved,
        )
        if pool is not None:
            # Scoped lease: released in _tool_error_guard finally via release_scope.
            bundle = pool.scoped_get(res.root)
            effective_write_ok = res.anchored if project_path is not None else write_ok_default
        else:
            if default_bundle is None:
                raise RuntimeError("Default skills bundle is not initialized")
            bundle = default_bundle
            effective_write_ok = True
        return bundle, res, effective_write_ok

    server = MCPServer("skills-engine", instructions=SKILLS_INSTRUCTIONS)
    _register_skills_tools(
        server,
        resolver,
        default_bundle=default_bundle,
        skills_repo_dir=skills_dir,
        multi_tenant=multi_tenant,
        pool=pool,
    )
    return server


def _make_reg(server: MCPServer, multi_tenant: bool) -> Callable[..., Callable[[Any], Any]]:
    """Фабрика декоратора регистрации тула (Ф2 плана core-dedup v4).

    Поглощает локальную копию ``_reg`` из ``_register_catalog_tools``:
    адаптация project_path-сигнатуры под multi-tenant-режим + регистрация
    через ``server.tool(name=name)``.
    """

    def _reg(name: str, is_project: bool = True) -> Callable[[Any], Any]:
        def dec(fn: Any) -> Any:
            adapted = adapt_project_path_signature(fn, multi_tenant) if is_project else fn
            return server.tool(name=name)(adapted)

        return dec

    return _reg


def _register_catalog_tools(
    server: MCPServer,
    resolver: Callable[[str | None], tuple[SkillsServiceBundle, ResolvedRoot, bool]],
    skills_repo_dir: Path | None = None,
    multi_tenant: bool = False,
    pool: ProjectServicePool[SkillsServiceBundle] | None = None,
) -> None:
    _reg = _make_reg(server, multi_tenant)

    @_reg("search_skills")
    @_tool_error_guard("search_skills", pool=pool)
    def search_skills_tool(
        query: str = "",
        type: Literal["all", "skill", "flow"] = "all",  # noqa: A002
        category: str | None = None,
        limit: int = 10,
        project_stack: list[str] | str | None = "auto",
        project_path: PROJECT_PATH_PARAM = None,
    ) -> dict[str, Any]:
        """Full-text search across skills and flows with catalog overview support on zero-query.

        In multi-workspace or global mode, pass project_path="<project_root>" to all calls.

        project_stack parameter:
        - "auto" (default): auto-detects stack from project manifests.
        - None (null) or []: search without stack filtering.
        - explicit list/string: technology query context (e.g. ["sqlite"] or ["tauri"]).
          Unlike load_skill(stack=...), stack here provides search/ranking context
          rather than filling assembly slots.

        Cards with incompatible stacks are hidden only upon proven conflict:
        requires_stack none_of or conflicting activation mode (mcp/offline).
        If required stack is not confirmed by manifests (incomplete detector, stdlib sqlite3, or
        planning a new technology) — card remains visible with status unknown.
        If requires_stack is omitted — card is treated as universal and visible under any stack.

        Trigger (Progressive Disclosure):
        - Zero-query: compact catalog overview — categories with counts and flow names.
        - Drilldown: search_skills(category="planning", project_path="<project_root>") — lean cards {name, title, description} for category skills.
        - Search skill: search_skills(query="fastapi", category="backend", project_path="<project_root>")
        - Full catalog search (E2): search_skills(query="feature plan", project_path="<project_root>") — skills AND relevant
          flows in a single result set; type="skill" — skills only.
        - List flows: search_skills(type="flow", project_path="<project_root>")
        """
        bundle, resolved, write_ok = resolver(project_path)
        bundle.synchronizer.sync()
        result = search_skills(
            query=query,
            type=type,
            category=category,
            limit=limit,
            storage=bundle.storage,
            synchronizer=bundle.synchronizer,
            project_root=resolved.root,
            skills_dir=skills_repo_dir,
            project_stack=project_stack,
        )
        if isinstance(result, dict):
            result["project_root"] = str(resolved.root)
            result["root_source"] = resolved.source
            if not write_ok:
                result["root_warning"] = (
                    f"Корень проекта не закреплён (source={resolved.source}): результаты "
                    "могут быть пустыми/неполными, запись отклоняется ROOT_NOT_ANCHORED. "
                    "Проверьте MCP_PROJECT_ROOT / --project-root в mcp_config.json."
                )
        return result

    @_reg("load_skill")
    @_tool_error_guard("load_skill", pool=pool)
    def load_skill_tool(
        name: str,
        stack: list[str] | str | None = None,
        touches: list[str] | None = None,
        resource_id: str | None = None,
        cursor: str | None = None,
        project_path: PROJECT_PATH_PARAM = None,
    ) -> dict[str, Any]:
        """Dynamic skill assembly tailored to the project tech stack or delivery of linked resources.

        Trigger: Call for a skill selected via search_skills prior to writing code with it.
        Assembly example: load_skill(name="writing-plans", stack=["python", "mcp"], project_path="<project_root>").
        Resource reading example: load_skill(name="writing-plans", resource_id="templates/standard-plan.template.md#2.4", project_path="<project_root>").
        touches — section names, NOT technologies; without touches all required sections are assembled.
        optional_slots are included only when explicitly passed in touches; ensure required sections are also included.
        SLOT selects the first match; SLOT+ combines all matches; local blocks override shared blocks for the same technology.
        stack — list of strings (e.g. ["python", "fastapi", "sqlite"]) or CSV string ("python,fastapi,sqlite"), not an object with axis keys. Values are shared across skills: language, mcp/offline mode, framework.
        Each skill may have blocks for any axis — see slot_variants in the response for available values of a given skill.
        stack omitted — auto-detects language/framework from manifests and mode from MCP_ACTIVE_SERVERS / .agents markers; explicit stack is NEVER overridden or supplemented with technologies; mode axis (mcp/offline) is auto-added if omitted (see stack_supplemented); source indicated in stack_source.
        """
        bundle, resolved, write_ok = resolver(project_path)
        bundle.synchronizer.sync()
        result = load_skill(
            name=name,
            stack=stack,
            touches=touches,
            resource_id=resource_id,
            cursor=cursor,
            storage=bundle.storage,
            synchronizer=bundle.synchronizer,
            compiler=bundle.compiler,
            project_root=resolved.root,
        )
        if isinstance(result, dict):
            result["project_root"] = str(resolved.root)
            result["root_source"] = resolved.source
            if not write_ok:
                result["root_warning"] = (
                    f"Корень проекта не закреплён (source={resolved.source}): сборка "
                    "может не учитывать локальные скиллы проекта. "
                    "Проверьте MCP_PROJECT_ROOT / --project-root в mcp_config.json."
                )
        return result


def _register_create_skill_tool(
    server: MCPServer,
    skills_repo_dir: Path | None = None,
) -> None:
    @server.tool(name="create_skill")
    @_tool_error_guard("create_skill")
    def create_skill_tool(
        name: str,
        title: str,
        description: str,
        content: str,
        category: str = "general",
        stack: list[str] | str | None = None,
        aliases: list[str] | str | None = None,
        available_slots: list[str] | str | None = None,
        stack_descriptions: dict[str, str] | None = None,
        blocks: dict[str, dict[str, str]] | None = None,
        overwrite: bool = False,
        optional_slots: list[str] | str | None = None,
    ) -> dict[str, Any]:
        """Create or update a modular skill in the repository (scaffold + blocks + stack conventions).

        Trigger: Call to add or update a skill (new practices, stack standards).
        Skill structure (Progressive Disclosure):
        - content — scaffold: invariants and general instructions with ALL <!-- SLOT: <slot> --> markers at points of variation; scaffold must NOT contain stack specifics.
        - blocks — {slot: {value: markdown}}: files blocks/<slot>/<value>.md placed at marker positions upon load_skill(stack=[value]). value 'default' is fallback without stack. Use kebab-case names.
        - available_slots — slot names from scaffold markers (for indexing).
        - optional_slots — optional sections loaded only via load_skill(touches=...).
        - stack — assembly axis values for the skill; new values are automatically recorded in docs/stack-conventions.yaml.
        - stack_descriptions — {value: description when to apply}; pass for new axes, otherwise added to conventions without semantics.
        - aliases — when creating, generate concise search phrases from skill purpose in English and user language (typically 4–8 total; English only for English users) and pass in the same call. Use actions, symptoms, and technical synonyms reflecting skill content; avoid generic terms, duplicates, or listing the entire stack. Preserve standard technology naming. When updating, preserve valid existing aliases. Limit: 30 aliases, up to 100 characters each.
        After creation, continue with main task; do not invoke search_skills merely to verify aliases. Search quality is verified separately in MCP/catalog tests.
        Example: create_skill(name="fastapi-di", title="FastAPI DI", description="...", content="...<!-- SLOT: core -->...", category="backend", stack="python,fastapi", stack_descriptions={"fastapi": "Project framework"}, blocks={"core": {"default": "...", "fastapi": "..."}})
        Dual mode (mcp/offline) — if skill functions via both MCP tools and file conventions: mode/integration slot with mcp.md, offline.md, and default.md (probe).
        Update: pass overwrite=True; existing axis semantics in conventions are preserved, new blocks appended.
        Note: writes to the skills repository (Single Source of Truth), not the target project.
        """
        if not is_kebab_name(name):
            raise ValueError(
                f"Недопустимое имя скилла '{name}': используйте kebab-case без спецсимволов и path traversal."
            )

        canonical_repo = resolve_skills_repo_dir(skills_repo_dir)
        target_db = get_cache_dir(canonical_repo) / "skills_fts.db"
        store = SkillsStorage(target_db)
        sync = get_synchronizer(canonical_repo, store, skills_dir=canonical_repo)
        return create_skill(
            name=name,
            title=title,
            description=description,
            content=content,
            category=category,
            stack=_normalize_str_list(stack),
            aliases=_normalize_str_list(aliases),
            available_slots=_normalize_str_list(available_slots),
            optional_slots=_normalize_str_list(optional_slots),
            stack_descriptions=stack_descriptions,
            blocks=blocks,
            overwrite=overwrite,
            skills_repo_dir=canonical_repo,
            storage=store,
            synchronizer=sync,
        )


def _register_get_help_tool(server: MCPServer) -> None:
    @server.tool(name="get_help")
    def get_help_tool(tool_name: str | None = "overview") -> str:
        """Detailed help, protocols, and tutorials for skills-engine tools.

        Trigger: Call when uncertain about skill structure, stack values, or authoring guidelines.
        Example: get_help(tool_name="create_skill") — authoring tutorial; get_help() — overview.
        """
        try:
            return get_skills_help_text(tool_name)
        except Exception as e:
            logger.exception("Ошибка при получении справки по '%s': %s", tool_name, e)
            return f"⚠️ Ошибка при получении справки по '{tool_name}': {e}"


def _register_step_flow_tool(
    server: MCPServer,
    resolver: Callable[[str | None], tuple[SkillsServiceBundle, ResolvedRoot, bool]],
    skills_repo_dir: Path | None = None,
    multi_tenant: bool = False,
    pool: ProjectServicePool[SkillsServiceBundle] | None = None,
) -> None:
    @_tool_error_guard("step_flow", pool=pool)
    def step_flow_tool(
        action: Literal["advance", "status", "retry", "abort", "escalation_resolve"] = "advance",
        flow: str | None = None,
        flow_options: dict[str, Any] | None = None,
        deliverable: Any = None,
        verdict: Literal["approved", "needs_work"] | None = None,
        session_id: str | None = None,
        expected_stage_id: str | None = None,
        transition: str | None = None,
        resolution: Literal["override_approved", "retry", "abort"] | None = None,
        notes: str = "",
        human_confirmed: bool = False,
        target: Literal["antigravity", "cursor", "opencode", "prompt"] | None = None,
        project_path: PROJECT_PATH_PARAM = None,
    ) -> dict[str, Any]:
        """Unified facade for lifecycle management and state machine execution of development flows (Flow Runner).

        Trigger: Start or continue a development flow, code review, or plan execution.
        Actions (Decision Table):
        - action='status': inspect status and current instructions of the active branch/session (read-only safe).
        - action='advance': start session (if flow provided) or advance to next stage validating Quality Gates/verdict.
        - action='retry': retry the current stage of the active session.
        - action='abort': terminate session early.
        - action='escalation_resolve': clear Circuit Breaker lock (requires resolution: override_approved|retry|abort).
        In multi-workspace or global mode, always pass project_path="<project_root>".
        After start, pass session_id from response; for advance — expected_stage_id from current stage.id.
        flow_options — configuration for new session, e.g. autonomy and scheduling for execute-plan-flow.
        transition — name of allowed transition for current stage when action='advance';
        use to explicitly choose path or repeat stage within session.
        human_confirmed=True — explicit human confirmation for stages requiring require_human_confirm (D4).
        Fail-fast: mutating actions require anchored project root.
        """
        bundle, resolved, write_ok = resolver(project_path)
        if action != "status":
            ensure_root_anchored(write_ok, resolved)

        flow_db_path = get_cache_dir(resolved.root) / "flows.db"
        if not flow_db_path.exists() and action == "status":
            if session_id is not None:
                raise FlowNotFoundError(f"Сессия '{session_id}' не найдена")
            res: dict[str, Any] = {
                "status": "no_active_session",
                "message": "Активных сессий флоу для данного проекта/ветки не найдено.",
                "project_root": str(resolved.root),
                "root_source": resolved.source,
                "required_inputs": [],
                "next_call": None,
            }
            if not write_ok:
                res["root_warning"] = (
                    f"Корень проекта не закреплён (source={resolved.source}): "
                    "запись отклоняется ROOT_NOT_ANCHORED."
                )
            return res

        if session_id is not None and flow_db_path.exists():
            flow_store = FlowStorage(project_root=resolved.root)
            session = flow_store.get_session(session_id)
            if (
                session is not None
                and Path(session.project_root).resolve() != resolved.root.resolve()
            ):
                raise SessionProjectMismatchError(
                    f"Сессия '{session_id}' принадлежит проекту '{session.project_root}', "
                    f"но вызов адресован '{resolved.root}'."
                )

        result = step_flow(
            action=action,
            flow=flow,
            flow_options=flow_options,
            deliverable=deliverable,
            verdict=verdict,
            session_id=session_id,
            expected_stage_id=expected_stage_id,
            transition=transition,
            resolution=resolution,
            notes=notes,
            human_confirmed=human_confirmed,
            target=target,
            project_root=resolved.root,
            bundler=bundle.bundler,
            skills_storage=bundle.storage,
            skills_dir=skills_repo_dir,
        )
        if isinstance(result, dict):
            result["project_root"] = str(resolved.root)
            result["root_source"] = resolved.source
            if not write_ok:
                result["root_warning"] = (
                    f"Корень проекта не закреплён (source={resolved.source}): "
                    "запись отклоняется ROOT_NOT_ANCHORED."
                )
        return result

    adapted = adapt_project_path_signature(step_flow_tool, multi_tenant)
    server.tool(name="step_flow")(adapted)


def _register_workflow_tools(
    server: MCPServer,
    resolver: Callable[[str | None], tuple[SkillsServiceBundle, ResolvedRoot, bool]],
    default_bundle: SkillsServiceBundle | None,
    skills_repo_dir: Path | None = None,
    multi_tenant: bool = False,
    pool: ProjectServicePool[SkillsServiceBundle] | None = None,
) -> None:
    _register_create_skill_tool(server, skills_repo_dir)
    _register_get_help_tool(server)
    _register_step_flow_tool(server, resolver, skills_repo_dir, multi_tenant, pool)


def _register_skills_tools(
    server: MCPServer,
    resolver: Callable[[str | None], tuple[SkillsServiceBundle, ResolvedRoot, bool]],
    default_bundle: SkillsServiceBundle | None,
    skills_repo_dir: Path | None = None,
    multi_tenant: bool = False,
    pool: ProjectServicePool[SkillsServiceBundle] | None = None,
) -> None:
    _register_catalog_tools(server, resolver, skills_repo_dir, multi_tenant=multi_tenant, pool=pool)
    _register_workflow_tools(
        server, resolver, default_bundle, skills_repo_dir, multi_tenant=multi_tenant, pool=pool
    )


def run_server() -> None:
    """Точка входа запуска MCP stdio сервера скиллов."""
    if len(sys.argv) > 1 and sys.argv[1] == "install":
        from mcp_skills.installer import run_installer

        run_installer(sys.argv[2:])
        return

    import argparse

    parser = argparse.ArgumentParser(description="MCP Skills Engine stdio server")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Путь к корню целевого проекта",
    )
    parser.add_argument(
        "--skills-dir",
        type=Path,
        default=None,
        help="Путь к репозиторию скиллов (override MCP_SKILLS_PATH)",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Путь к файлу кэша SQLite FTS5",
    )
    parser.add_argument(
        "--multi-tenant",
        action="store_true",
        default=False,
        help="Мультипроектный режим: требует обязательной передачи project_path в инструменты",
    )
    args = parser.parse_args()

    server = create_server(
        project_root=args.project_root,
        db_path=args.db_path,
        skills_dir=args.skills_dir,
        multi_tenant=args.multi_tenant,
    )
    server.run(transport="stdio")


if __name__ == "__main__":
    run_server()
