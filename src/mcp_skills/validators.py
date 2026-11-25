"""Валидаторы этапов выполнения сценариев (Quality Gates) для MCP Skills Engine."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp_core.paths import safe_read_text
from mcp_skills.errors import GateValidationError
from mcp_skills.models import FlowSession, FlowStage
from mcp_skills.storage import SkillsStorage

logger = logging.getLogger("mcp_skills.validators")

__all__ = [
    "VALIDATOR_REGISTRY",
    "ValidationFunc",
    "ValidationResult",
    "validate_plan",
    "validate_qa_report",
    "validate_review",
]


@dataclass
class ValidationResult:
    """Результат валидации этапа."""

    passed: bool
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _looks_like_path(s: str) -> bool:
    """Эвристика «строка-путь» для deliverable/плана (F2, verdict_6 T1, AUDIT-FIX-P0).

    Относительный или абсолютный путь с разделителями каталогов
    ('docs/plans/x.md', '/abs/path.md', '~/plan.md') или расширением
    файла — путь; многострочный текст плана или обычная фраза без
    расширения — текст.
    """
    s = s.strip()
    if not s or "\n" in s:
        return False
    if s.startswith(("/", "~")):
        return True
    if "/" in s or "\\" in s:
        return True
    return Path(s).suffix.lower() in {".md", ".markdown", ".txt", ".rst", ".yaml", ".yml", ".json"}


def _plan_deliverable_source(deliverable: Any) -> tuple[str | None, str | None]:
    """Разбирает deliverable на (текст плана, путь к файлу) — F2.

    Возвращает первый применимый источник: текст (``content`` или строка
    без маркеров пути) либо путь (``file_path`` или строка-путь).
    ``content`` приоритетнее ``file_path``.
    """
    if isinstance(deliverable, dict):
        content = deliverable.get("content")
        if isinstance(content, str):
            return content, None
        file_path = deliverable.get("file_path")
        if isinstance(file_path, str):
            return None, file_path
        return None, None
    if isinstance(deliverable, str):
        stripped = deliverable.strip()
        if _looks_like_path(stripped):
            return None, stripped
        return (stripped or None), None
    return None, None


def validate_plan(
    session: FlowSession,
    stage: FlowStage,
    deliverable: Any,
    project_root: Path,
    storage: SkillsStorage | None = None,
) -> ValidationResult:
    """Валидатор этапа проверки наличия плана реализации (validate_plan).

    F2 — однозначные типы артефакта (verdict_6 П.8):
    - ``deliverable``-строка, похожая на путь, — ПУТЬ: файл читается,
      отсутствие файла — блокер с явной причиной (раньше несуществующий
      путь молча принимался как «текст плана»);
    - ``deliverable={'content': str}`` — ТЕКСТ плана (приоритетнее file_path);
    - ``deliverable={'file_path': str}`` — путь;
    - ``stage.meta.target_file``: валидный файл-артефакт из deliverable
      снимает требование к целевому файлу (T4 — target_file больше не
      глушит корректный file_path), но текст плана — не снимает:
      объявленный target_file обязан существовать и не быть пустым.
    """
    blockers: list[str] = []
    plan_text: str | None = None
    artifact_file_found = False

    def _read_file(rel: str, label: str, *, is_target: bool = False) -> None:
        nonlocal plan_text, artifact_file_found
        try:
            text = safe_read_text(project_root, rel)
        except ValueError as err:
            raise GateValidationError(f"Ошибка безопасности пути плана: {err}") from err
        if text is None:
            if is_target:
                blockers.append(f"Целевой файл плана '{rel}' не найден в проекте")
            else:
                blockers.append(f"Файл плана '{rel}' не найден в проекте ({label})")
            return
        if not text.strip():
            if is_target:
                blockers.append(f"Целевой файл плана '{rel}' пуст")
            else:
                blockers.append(f"Файл плана '{rel}' пуст")
            return
        if plan_text is None:
            plan_text = text
        artifact_file_found = True

    # 1. deliverable — первый применимый источник (текст или путь)
    text_src, path_src = _plan_deliverable_source(deliverable)
    if text_src is not None:
        plan_text = text_src
    elif path_src is not None:
        _read_file(path_src, "deliverable")

    # 2. target_file: валидный файл-артефакт спасает ненайденный целевой
    # файл (T4); текст плана — нет, объявленный target_file проверяется.
    if stage.meta.target_file and not artifact_file_found:
        _read_file(stage.meta.target_file, "target_file", is_target=True)

    if plan_text is not None and not plan_text.strip():
        return ValidationResult(passed=False, blockers=["План реализации не предоставлен или пуст"])

    if blockers:
        return ValidationResult(passed=False, blockers=blockers)

    if plan_text is None:
        return ValidationResult(passed=False, blockers=["План реализации не предоставлен или пуст"])

    return ValidationResult(passed=True, blockers=[], warnings=[])


def _extract_qa_report_text(
    deliverable: Any,
    target_file: str | None,
    project_root: Path,
) -> tuple[str | None, list[str]]:
    """Извлекает текст отчёта из deliverable или target_file."""
    blockers: list[str] = []
    report_text: str | None = None

    def _read_file(rel: str, label: str) -> None:
        nonlocal report_text
        try:
            text = safe_read_text(project_root, rel)
        except ValueError as err:
            raise GateValidationError(f"Ошибка безопасности пути отчёта: {err}") from err
        if text is None:
            blockers.append(f"Файл отчёта приёмки '{rel}' не найден в проекте ({label})")
            return
        if not text.strip():
            blockers.append(f"Файл отчёта приёмки '{rel}' пуст ({label})")
            return
        if report_text is None:
            report_text = text

    if isinstance(deliverable, dict):
        if deliverable.get("content"):
            report_text = str(deliverable["content"])
        elif deliverable.get("file_path"):
            _read_file(str(deliverable["file_path"]), "deliverable.file_path")
    elif isinstance(deliverable, str) and deliverable.strip():
        stripped = deliverable.strip()
        if _looks_like_path(stripped):
            _read_file(stripped, "deliverable")
        else:
            report_text = stripped

    if report_text is None and target_file:
        _read_file(target_file, "target_file")

    return report_text, blockers


def _parse_qa_report_table(
    report_text: str,
) -> tuple[list[list[str]], dict[str, int]]:
    """Парсит markdown-таблицу проверок и определяет индексы колонок."""
    lines = report_text.splitlines()
    table_rows: list[list[str]] = []
    header_indices: dict[str, int] = {}
    in_table = False

    for line in lines:
        s = line.strip()
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s[1:-1].split("|")]
            if all(re.match(r"^:?-+:?$", c) for c in cells):
                in_table = True
                continue
            if not in_table:
                for idx, cell in enumerate(cells):
                    cl = cell.lower()
                    # Порядок классификации важен (R4): evidence раньше id —
                    # иначе английская «Evidence» матчится подстрокой «id»
                    # и колонка доказательств теряется.
                    if any(
                        k in cl for k in ("доказательств", "evidence", "вывод", "артефакт", "лог")
                    ):
                        header_indices["evidence"] = idx
                    elif re.search(r"\b(?:id|action)\b|действи|сценари", cl):
                        header_indices["id"] = idx
                    elif any(k in cl for k in ("ожидани", "expected")):
                        header_indices["expected"] = idx
                    elif any(k in cl for k in ("факт", "actual", "наблюда")):
                        header_indices["actual"] = idx
                    elif any(
                        k in cl for k in ("результат", "статус", "result", "status", "вердикт")
                    ):
                        header_indices["result"] = idx
            else:
                table_rows.append(cells)
        else:
            in_table = False

    return table_rows, header_indices


# Валидные статусы строки таблицы проверок (R4: QA-gate не принимает
# произвольные статусы вроде BANANA/PENDING как «успешные»). N/A — ветка
# отдельно (обязательное обоснование неприменимости).
_VALID_ROW_STATUSES: tuple[str, ...] = (
    "PASS",
    "PASSED",
    "OK",
    "УСПЕШНО",
    "ПРОЙДЕН",
    "QA_PASSED",
)


def _validate_qa_table_rows(
    table_rows: list[list[str]],
    header_indices: dict[str, int],
    has_limitations: bool,
) -> tuple[bool, bool, bool, bool, bool, bool, bool]:
    """Проверяет строки таблицы приёмки.

    Возвращает (missing_exp_act, missing_evidence, has_failed, has_blocked,
    missing_na_reason, missing_status, unknown_status). missing_status=True,
    если у какой-либо строки пустая ячейка «Результат» или колонка
    отсутствует вовсе. unknown_status=True, если статус непуст, но не входит
    в _VALID_ROW_STATUSES и не распознан как N/A (R4: BANANA/PENDING —
    блокеры, а не «успех»).
    """
    missing_exp_act = False
    missing_evidence = False
    has_failed = False
    has_blocked = False
    missing_na_reason = False
    missing_status = False
    unknown_status = False

    for row in table_rows:
        exp_cell = (
            row[header_indices["expected"]]
            if "expected" in header_indices and len(row) > header_indices["expected"]
            else ""
        )
        act_cell = (
            row[header_indices["actual"]]
            if "actual" in header_indices and len(row) > header_indices["actual"]
            else ""
        )
        ev_cell = (
            row[header_indices["evidence"]]
            if "evidence" in header_indices and len(row) > header_indices["evidence"]
            else ""
        )
        res_cell = (
            row[header_indices["result"]]
            if "result" in header_indices and len(row) > header_indices["result"]
            else ""
        )

        if not exp_cell or not act_cell:
            missing_exp_act = True
        if not ev_cell:
            missing_evidence = True
        # Пустой или отсутствующий статус строки — блокер: приёмка требует
        # распознанный непустой результат каждой проверки.
        if not res_cell.strip():
            missing_status = True

        res_upper = res_cell.upper()
        if any(k in res_upper for k in ("FAIL", "FAILED", "QA_FAILED")):
            has_failed = True
        elif "BLOCK" in res_upper:
            has_blocked = True
        elif "N/A" in res_upper or "NOT APPLICABLE" in res_upper:
            clean_res = re.sub(r"N/?A", "", res_cell, flags=re.IGNORECASE).strip(" ():-")
            has_act_reason = any(
                k in act_cell.lower()
                for k in ("не применимо", "n/a", "пропуск", "стенд", "ci", "отсутств")
            )
            if not clean_res and not has_act_reason and not has_limitations:
                missing_na_reason = True
        elif res_upper.strip() and not any(k == res_upper.strip() for k in _VALID_ROW_STATUSES):
            # Непустой статус вне whitelist — неизвестный вердикт проверки
            # (BANANA, PENDING, …) не может считаться успехом (R4).
            unknown_status = True

    return (
        missing_exp_act,
        missing_evidence,
        has_failed,
        has_blocked,
        missing_na_reason,
        missing_status,
        unknown_status,
    )


_NEGATED_FAIL_RE = re.compile(
    r"(?:\b0\s+(?:tests?\s+)?(?:fail(?:ed)?|failures?)\b"
    r"|\b(?:fail(?:ed)?|failures?)\s*[:=]\s*0\b"
    r"|\b(?:no|zero|without)\s+(?:tests?\s+)?(?:fail(?:ed)?|failures?)\b)",
    re.IGNORECASE,
)


def _has_unnegated_failure(report_text: str) -> bool:
    """Checks for FAIL/FAILED/QA_FAILED excluding negated zero-failure summaries."""
    cleaned = _NEGATED_FAIL_RE.sub(" ", report_text)
    return bool(re.search(r"\b(?:FAIL|FAILED|QA_FAILED)\b", cleaned, re.IGNORECASE))


def _validate_qa_freeform_text(
    report_text: str,
    has_limitations: bool,
) -> tuple[bool, bool, bool, bool, bool, bool, bool, bool]:
    """Проверяет неструктурированный (списковый) текст отчёта приёмки.

    Возвращает (has_scenarios, missing_exp_act, missing_evidence, has_failed,
    has_blocked, missing_na_reason, missing_status, unknown_status).
    unknown_status=True, если в тексте есть маркер строки-сценария
    (TC-N/Сценарий/Проверка) или строка «Результат:», но статус не входит в
    распознаваемые (PASS/PASSED/OK/УСПЕШНО/ПРОЙДЕН/QA_PASSED, N/A, FAIL,
    BLOCKED) — закрывает обход через freeform-формат (R4).
    """
    scenario_matches = re.findall(
        r"(?:###|##|-)\s*(?:TC-\d+|Сценарий|Проверка)", report_text, re.IGNORECASE
    )
    has_scenarios = bool(scenario_matches)

    has_exp = bool(re.search(r"(?:ожида(?:ние|лось)|expected)", report_text, re.IGNORECASE))
    has_act = bool(re.search(r"(?:факт|наблюда(?:лось|ем)|actual)", report_text, re.IGNORECASE))
    missing_exp_act = not (has_exp and has_act)

    has_ev = bool(
        re.search(
            r"(?:доказательств|evidence|артефакт|лог|log|\.log|\.txt|\.json|\[.*?\]\(.*?\)|```)",
            report_text,
            re.IGNORECASE,
        )
    )
    missing_evidence = not has_ev

    has_failed = _has_unnegated_failure(report_text)
    has_blocked = bool(re.search(r"\b(?:BLOCKED|ЗАБЛОКИРОВАН)\b", report_text, re.IGNORECASE))

    missing_na_reason = False
    if (
        re.search(r"\bN/A\b", report_text, re.IGNORECASE)
        and not has_limitations
        and not re.search(r"N/A\s*[:(.]+?[)\n]", report_text, re.IGNORECASE)
    ):
        missing_na_reason = True

    # unknown_status: каждая строка-сценарий с явным полем «Результат»/«Статус»
    # обязана нести распознаваемый вердикт. Общий QA_PASSED в вердикте отчёта
    # не оправдывает строку с невалидным статусом (R4, mixed-случай).
    unknown_status = False
    missing_status = False
    status_lines = re.findall(
        r"(?:результат|статус|result|status)\s*[:*]+\s*(.+?)(?:\n|$)", report_text, re.IGNORECASE
    )
    if status_lines and not has_failed and not has_blocked:
        for status_line in status_lines:
            status_value = status_line.strip().strip("«»\"'").upper()
            recognized = (
                any(
                    k in status_value
                    for k in ("FAIL", "FAILED", "QA_FAILED", "BLOCKED", "ЗАБЛОКИРОВАН")
                )
                or "N/A" in status_value
                or any(k in status_value for k in _VALID_ROW_STATUSES)
            )
            if not status_value:
                missing_status = True
            elif not recognized:
                unknown_status = True

    return (
        has_scenarios,
        missing_exp_act,
        missing_evidence,
        has_failed,
        has_blocked,
        missing_na_reason,
        missing_status,
        unknown_status,
    )


def _check_qa_report_session(report_text: str, session: FlowSession) -> list[str]:
    """Проверяет соответствие session_id в тексте отчёта текущей сессии.

    Точное совпадение только с текущей session.id: task_id сам по себе не
    отличает повторные запуски (шаг 7, план строка 92). Отсутствие session_id —
    ошибка, а не разрешение принять старый отчёт.
    """
    blockers: list[str] = []
    # \** допускает markdown-выделение («**Сессия:** <id>»)
    session_matches = re.findall(
        r"(?:сессия|session_id|session)\s*[:=]\s*\**\s*([a-zA-Z0-9_\-]+)",
        report_text,
        re.IGNORECASE,
    )
    if not session_matches:
        blockers.append(
            "QA report is not bound to the current session: "
            f"specify session_id of the current session ('{session.id}')"
        )
        return blockers
    for sid in session_matches:
        if sid != session.id:
            blockers.append(
                f"Отчёт привязан к сессии '{sid}', ожидалась текущая сессия '{session.id}'"
            )
            break
    return blockers


def _check_qa_report_verdict(report_text: str) -> tuple[bool, bool]:
    """Проверяет явное указание вердикта в тексте отчёта."""
    has_failed = False
    has_blocked = False
    verdict_match = re.search(
        r"(?:вердикт|verdict)\s*[:*]+\s*([A-Za-z0-9_]+)", report_text, re.IGNORECASE
    )
    if verdict_match:
        verdict_val = verdict_match.group(1).upper()
        if any(k in verdict_val for k in ("FAIL", "FAILED", "QA_FAILED")):
            has_failed = True
        elif "BLOCK" in verdict_val:
            has_blocked = True
    return has_failed, has_blocked


def _collect_qa_blockers(
    has_scenarios: bool,
    missing_expected_actual: bool,
    missing_evidence: bool,
    has_failed: bool,
    has_blocked: bool,
    missing_na_reason: bool,
    missing_status: bool = False,
    unknown_status: bool = False,
) -> list[str]:
    """Формирует список блокирующих несоответствий отчёта приёмки."""
    blockers: list[str] = []
    if not has_scenarios:
        blockers.append("Отчёт не содержит перечня проверок или сценариев")
    if missing_expected_actual:
        blockers.append("В отчёте отсутствуют ожидаемые или фактические результаты проверок")
    if missing_evidence:
        blockers.append(
            "В отчёте отсутствуют ссылки на доказательства (evidence: логи, артефакты или вывод команд)"
        )
    if has_failed:
        blockers.append(
            "Отчёт содержит непройденные проверки (FAILED): успешное завершение недопустимо"
        )
    if has_blocked:
        blockers.append(
            "Отчёт содержит заблокированные проверки (BLOCKED): успешное завершение недопустимо"
        )
    if missing_na_reason:
        blockers.append("Проверка со статусом N/A не содержит обоснования неприменимости")
    if missing_status:
        blockers.append(
            "Строка таблицы проверок без статуса результата: колонка «Результат» обязательна и не может быть пустой"
        )
    if unknown_status:
        blockers.append(
            "unknown_status: строка проверки содержит нераспознанный статус результата "
            "(BANANA/PENDING и т.п.): допускаются PASS/PASSED/OK/УСПЕШНО/ПРОЙДЕН/QA_PASSED, "
            "N/A с обоснованием, FAIL/FAILED, BLOCKED"
        )
    return blockers


def validate_qa_report(
    session: FlowSession,
    stage: FlowStage,
    deliverable: Any,
    project_root: Path,
    skills_storage: SkillsStorage | None = None,
) -> ValidationResult:
    """Валидатор этапа проверки отчёта живой приёмки (validate_qa_report).

    Проверяет:
    1. Наличие и непустоту отчёта (в deliverable или целевом файле).
    2. Привязку артефакта к сессии (сохранение в context_data; проверка session_id при наличии).
    3. Перечень проверок/сценариев (минимум один сценарий).
    4. Ожидаемый и фактический результат для каждой проверки.
    5. Ссылки на evidence (доказательства: файлы, логи, скриншоты, команды).
    6. Соответствие итога результатам (без непройденных/заблокированных, N/A с обоснованием).
    """
    blockers: list[str] = []
    warnings: list[str] = []

    report_text, extract_blockers = _extract_qa_report_text(
        deliverable, stage.meta.target_file, project_root
    )
    blockers.extend(extract_blockers)
    if blockers:
        return ValidationResult(passed=False, blockers=blockers)

    if report_text is None or not report_text.strip():
        return ValidationResult(
            passed=False, blockers=["Отчёт приёмки (deliverable) не предоставлен или пуст"]
        )

    # 2. Привязка к текущей сессии.
    blockers.extend(_check_qa_report_session(report_text, session))

    # 3. Парсинг сценариев / проверок
    table_rows, header_indices = _parse_qa_report_table(report_text)
    has_limitations = bool(
        re.search(r"##\s*(?:ограничения|limitations)", report_text, re.IGNORECASE)
    )

    if table_rows and ("result" in header_indices or "id" in header_indices):
        has_scenarios = True
        (
            missing_expected_actual,
            missing_evidence,
            has_failed,
            has_blocked,
            missing_na_reason,
            missing_status,
            unknown_status,
        ) = _validate_qa_table_rows(table_rows, header_indices, has_limitations)
    else:
        (
            has_scenarios,
            missing_expected_actual,
            missing_evidence,
            has_failed,
            has_blocked,
            missing_na_reason,
            missing_status,
            unknown_status,
        ) = _validate_qa_freeform_text(report_text, has_limitations)

    verdict_failed, verdict_blocked = _check_qa_report_verdict(report_text)
    has_failed = has_failed or verdict_failed
    has_blocked = has_blocked or verdict_blocked

    blockers.extend(
        _collect_qa_blockers(
            has_scenarios,
            missing_expected_actual,
            missing_evidence,
            has_failed,
            has_blocked,
            missing_na_reason,
            missing_status,
            unknown_status,
        )
    )

    if blockers:
        return ValidationResult(passed=False, blockers=blockers, warnings=warnings)

    return ValidationResult(passed=True, blockers=[], warnings=warnings)


def validate_review(
    session: FlowSession,
    stage: FlowStage,
    deliverable: Any,
    project_root: Path,
    skills_storage: SkillsStorage | None = None,
) -> ValidationResult:
    """Валидатор этапа независимого ревью (validate_review).

    Проверяет:
    1. Наличие deliverable со структурированными данными (dict или JSON-строка).
    2. Привязку к субагентам: при dispatch_agents=True требует указания
       списка запущенных субагентов (subagent_sessions или subagents) числом не менее min_agents.
    3. Наличие ссылки на запись ревью (review_id).
    """
    blockers: list[str] = []
    warnings: list[str] = []

    deliv_dict: dict[str, Any] | None = None
    if isinstance(deliverable, dict):
        deliv_dict = deliverable
    elif isinstance(deliverable, str):
        try:
            parsed = json.loads(deliverable)
            if isinstance(parsed, dict):
                deliv_dict = parsed
        except (json.JSONDecodeError, TypeError, ValueError) as err:
            logger.debug("Failed to parse deliverable as JSON: %s", err)

    if deliv_dict is None:
        blockers.append(
            "Для этапа независимого ревью требуется deliverable в формате словаря/JSON "
            "с обязательным полем 'review_id'."
        )
        return ValidationResult(passed=False, blockers=blockers, warnings=warnings)

    if stage.meta.dispatch_agents:
        raw_sessions = (
            deliv_dict.get("subagent_sessions")
            or deliv_dict.get("subagents")
            or deliv_dict.get("agents")
            or deliv_dict.get("subagent_session_id")
            or []
        )
        if isinstance(raw_sessions, str):
            sessions = [raw_sessions.strip()] if raw_sessions.strip() else []
        elif isinstance(raw_sessions, list):
            sessions = [str(s).strip() for s in raw_sessions if s is not None and str(s).strip()]
        else:
            sessions = []

        min_agents = 1
        if stage.meta.dispatch_policy and stage.meta.dispatch_policy.min_agents:
            min_agents = stage.meta.dispatch_policy.min_agents

        if len(sessions) < min_agents:
            blockers.append(
                f"Ревью требует запуска независимых субагентов (минимум: {min_agents}). "
                f"Передайте список их ID в deliverable['subagent_sessions'] (получено: {len(sessions)})."
            )

    review_id = deliv_dict.get("review_id")
    if not review_id or not str(review_id).strip():
        blockers.append(
            "Отсутствует идентификатор записи ревью ('review_id') в deliverable. "
            "Зафиксируйте раунд через record_review(task_id=..., ...) и передайте полученный ID."
        )

    return ValidationResult(passed=len(blockers) == 0, blockers=blockers, warnings=warnings)


ValidationFunc = Callable[
    [FlowSession, FlowStage, Any, Path, SkillsStorage | None], ValidationResult
]

# Реестр доступных валидаторов качества (VALIDATOR_REGISTRY).
# Валидаторы verify_catalog.py динамически запрашивают этот реестр.
VALIDATOR_REGISTRY: dict[str, ValidationFunc] = {
    "validate_plan": validate_plan,
    "validate_qa_report": validate_qa_report,
    "validate_report": validate_qa_report,
    "validate_review": validate_review,
}
