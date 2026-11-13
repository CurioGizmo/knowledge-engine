"""Парсер и сериализатор Markdown-карточек аудита и технического долга.

Поддерживает канонический формат с YAML frontmatter и эластичный парсинг устаревших
карточек в чистом Markdown без frontmatter (legacy transcrib format).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml

from mcp_audit.models import (
    AuditAutonomy,
    AuditComplexity,
    AuditIssue,
    AuditPriority,
    AuditStatus,
    PlanArtifact,
    PlanStatus,
    PlanStep,
    ReviewArtifact,
    ReviewComment,
    ReviewVerdict,
)
from mcp_core import markdown as core_md
from mcp_core.card_codec import (
    FrontmatterError,
    dump_frontmatter,
    rebuild_card,
    split_frontmatter,
)

__all__ = [
    "append_steps_to_plan_markdown",
    "mutate_plan_step_in_markdown",
    "parse_audit_markdown",
    "parse_plan_markdown",
    "parse_review_markdown",
    "serialize_audit_markdown",
    "serialize_plan_markdown",
    "serialize_review_markdown",
]

# Байт-копия регэкспа вынесена в mcp_core.markdown (M1); реэкспорт сохраняет
# имя для локальных вызовов parse_audit_markdown/_parse_legacy.
FRONTMATTER_SPLIT_REGEX = core_md.FRONTMATTER_SPLIT_REGEX

# Ключевые слова заголовков секций — общие для канонического и legacy парсинга.
_SYMPTOM_KEYWORDS = ("симптом", "symptom", "проблема", "problem")
_ROOT_CAUSE_KEYWORDS = ("первопричин", "root cause", "cause", "анализ")
_SOLUTION_KEYWORDS = ("решение", "solution", "финализ")

# Ключевые слова секции фактического решения (план C: resolution_note пишется
# отдельно от предложенного solution, чтобы не перезаписывать его).
ACTUAL_RESOLUTION_KEYWORDS = ("фактическое решение", "фактическ", "actual resolution", "actual")

_ACTUAL_RESOLUTION_HEADER = "## ✅ Фактическое решение (Actual resolution)"

logger = logging.getLogger("mcp_audit.parser")


def _parse_h2_sections(body: str) -> dict[str, str]:
    """Разбивает тело Markdown на словарь секций по заголовкам H2 с учётом code fence."""
    return core_md.split_sections(body, fence_aware=True)


def _find_matching_section(sections: dict[str, str], *patterns: str) -> str | None:
    """Ищет содержимое секции по совпадению ключевых слов в заголовке."""
    return core_md.find_section(sections, *patterns)


def _note_text_of_line(line: str) -> str:
    """Извлекает текст заметки из строки секции (без маркера списка и даты)."""
    stripped = line.strip().lstrip("-* ").strip()
    head, sep, tail = stripped.partition(": ")
    if sep and len(head) <= 12:  # префикс-дата вида 2026-10-18
        return tail.strip()
    return stripped


def extract_actual_resolution_lines(content: str) -> list[str]:
    """Возвращает сырые строки секции «Фактическое решение» (или пустой список)."""
    sections = _parse_h2_sections(content)
    existing = _find_matching_section(sections, *ACTUAL_RESOLUTION_KEYWORDS)
    if not existing:
        return []
    return [line for line in existing.splitlines() if line.strip()]


def set_actual_resolution_lines(content: str, lines: list[str]) -> str:
    """(Пере)создаёт секцию «Фактическое решение» ровно с данными строками.

    Существующая секция заменяется; остальное тело документа не затрагивается.
    """
    if not lines:
        return content
    src = content.splitlines()
    out: list[str] = []
    replaced = False
    i = 0
    while i < len(src):
        line = src[i]
        if (
            not replaced
            and line.startswith("## ")
            and any(kw in line.lower() for kw in ACTUAL_RESOLUTION_KEYWORDS)
        ):
            out.append(line)
            out.extend(lines)
            replaced = True
            i += 1
            # Пропускаем старое тело секции до следующего H2
            while i < len(src) and not src[i].startswith("## "):
                i += 1
            continue
        out.append(line)
        i += 1
    if not replaced:
        if out and out[-1].strip() != "":
            out.append("")
        out.append(_ACTUAL_RESOLUTION_HEADER)
        out.extend(lines)
    return "\n".join(out) + "\n"


def append_actual_resolution_to_markdown(
    content: str,
    note: str,
    today: str | None = None,
) -> str:
    """Добавляет фактическое решение долга в отдельную секцию карточки.

    Секция «Фактическое решение» не перезаписывает предложенное решение
    (frontmatter/секция «Предлагаемое решение»): заметка о факте закрытия
    пишется отдельно. Идемпотентность: если заметка с тем же текстом уже
    есть в секции, повторная запись не дублирует её; существующие строки
    секции сохраняются.
    """
    clean_note = note.strip()
    if not clean_note:
        return content
    existing = extract_actual_resolution_lines(content)
    if clean_note in {_note_text_of_line(line) for line in existing}:
        return content
    eff_today = today or datetime.now().strftime("%Y-%m-%d")
    note_line = f"- {eff_today}: {clean_note}"
    return set_actual_resolution_lines(content, [*existing, note_line])


def _parse_priority(val: Any) -> AuditPriority:
    if not val:
        return AuditPriority.IMPORTANT
    s = str(val).lower()
    if "crit" in s:
        return AuditPriority.CRITICAL
    if "sugg" in s or "оптим" in s:
        return AuditPriority.SUGGESTION
    return AuditPriority.IMPORTANT


def _parse_autonomy(val: Any) -> AuditAutonomy:
    if not val:
        return AuditAutonomy.HIGH
    s = str(val).lower()
    if "low" in s or "низк" in s:
        return AuditAutonomy.LOW
    if "med" in s or "сред" in s:
        return AuditAutonomy.MEDIUM
    return AuditAutonomy.HIGH


def _parse_complexity(val: Any) -> AuditComplexity:
    if not val:
        return AuditComplexity.M
    s = str(val).upper().strip()
    if "L" in s:
        return AuditComplexity.L
    if "H" in s:
        return AuditComplexity.H
    return AuditComplexity.M


def _parse_status(val: Any, default_status: AuditStatus = AuditStatus.PLANNED) -> AuditStatus:
    if not val:
        return default_status
    s = str(val).lower()
    # A10: невалидное значение не молчит — warning фиксирует подмену
    known_values = {st.value for st in AuditStatus}
    if s not in known_values and not any(
        marker in s
        for marker in (
            "[x]",
            "заверш",
            "complete",
            "closed",
            "решено",
            "закрыто",
            "[/]",
            "в работе",
            "in_progress",
            "progress",
            "obsolete",
            "устарел",
            "отменен",
        )
    ):
        logger.warning(
            "CARD_STATUS_UNKNOWN: значение status=%r не распознано — подставлен default=%s",
            val,
            default_status.value,
        )
    if (
        "[x]" in s
        or "заверш" in s
        or "complete" in s
        or "closed" in s
        or "решено" in s
        or "закрыто" in s
    ):
        return AuditStatus.COMPLETED
    if "[/]" in s or "в работе" in s or "in_progress" in s or "progress" in s:
        return AuditStatus.IN_PROGRESS
    if "obsolete" in s or "устарел" in s or "отменен" in s:
        return AuditStatus.OBSOLETE
    return AuditStatus.PLANNED


def parse_audit_markdown(content: str, file_path: Path | None = None) -> AuditIssue:
    """Парсит текст карточки аудита в структурированную модель AuditIssue.

    Разбор меты — единый core-кодек (Ф3, card_codec): FrontmatterError
    мапится в доменную ошибку аудита (v2_target: domain-error вместо
    v1-молчания ``meta = {}`` при невалидном YAML, кейс 13). Frontmatter
    у карточки аудита обязателен:

    - пустой файл (кейс 14) и сломанный fm-блок «за BOM начинается с
      ``---`` без валидного закрытия» (кейсы 11/15/17) — доменная ошибка,
      а не v1-молчание через legacy-ветку;
    - кейсы 04b/22 (EOF/CRLF сразу после закрывающего ``---``): сплиттер
      v1 их не матчит, core-кодек толерантнее — валидный блок, ok;
    - кейс 12 (открывающий маркер с хвостовым пробелом ``'--- \\n'``) —
      эквивалентен отсутствию блока (contract: legacy-ветка, как v1).
    """
    if not content.strip():
        # Кейс 14 (пустой файл): v1 молчал (legacy, id из stem), v2 — доменная
        # ошибка: пустая карточка не несёт ни меты, ни тела для legacy-разбора.
        raise ValueError("Карточка аудита пуста: ожидается YAML frontmatter-блок")

    match = FRONTMATTER_SPLIT_REGEX.match(content)
    if match:
        # Сплиттер сматчился → кодек (толерантное надмножество) тоже сматчится;
        # FrontmatterError (кейсы 08/13/20) → доменная ошибка аудита.
        try:
            meta = split_frontmatter(content)[0]
        except FrontmatterError as e:
            msg = f"Некорректный frontmatter карточки аудита: {e}"
            raise ValueError(msg) from e
        if meta is None:  # pragma: no cover — кодек надмножество сплиттера
            meta = {}
        return _parse_canonical(meta, match.group(2), file_path)

    no_bom = content.lstrip("\ufeff")
    if no_bom.startswith(("--- \n", "---\t")):
        # Кейс 12: сломанный открывающий маркер — валидного fm-блока нет,
        # но это не «сломанный fm» (contract-ячейка: legacy, как v1).
        return _parse_legacy(content, file_path)

    # Сплиттер жёстче кодека (не допускает EOF/CRLF-только после закрывающего
    # '---'): кейсы 04b/22 кодек валидирует как ok. Кейсы 11/15/17 (BOM,
    # незакрытый блок) кодек поднимает FrontmatterError → доменная ошибка.
    try:
        meta, body = split_frontmatter(content)
    except FrontmatterError as e:
        msg = f"Некорректный frontmatter карточки аудита: {e}"
        raise ValueError(msg) from e
    if meta is not None:
        return _parse_canonical(meta, body, file_path)

    return _parse_legacy(content, file_path)


def _parse_canonical(meta: dict[str, Any], body: str, file_path: Path | None) -> AuditIssue:
    """Разбирает карточку в каноническом формате с YAML frontmatter.

    Мета уже разобрана core-кодеком (parse_audit_markdown) и передаётся
    готовым dict; body — текст после закрывающего ``---``.
    """
    sections = _parse_h2_sections(body)
    stem = file_path.stem if file_path else "aud-unknown"
    issue_id = str(meta.get("id") or stem)
    category = str(
        meta.get("category")
        or (
            file_path.parent.name
            if file_path and file_path.parent.name not in ("todo", "done", "audit")
            else "general"
        )
    )
    title = str(meta.get("title") or issue_id)

    symptom = (
        meta.get("symptom") or _find_matching_section(sections, *_SYMPTOM_KEYWORDS) or body.strip()
    )
    root_cause = meta.get("root_cause") or _find_matching_section(sections, *_ROOT_CAUSE_KEYWORDS)
    solution = meta.get("solution") or _find_matching_section(sections, *_SOLUTION_KEYWORDS)

    blocked_by_raw = meta.get("blocked_by") or []
    if isinstance(blocked_by_raw, str):
        blocked_by_raw = [blocked_by_raw] if blocked_by_raw.strip() else []

    return AuditIssue(
        id=issue_id,
        title=title,
        category=category,
        priority=_parse_priority(meta.get("priority")),
        autonomy=_parse_autonomy(meta.get("autonomy")),
        complexity=_parse_complexity(meta.get("complexity")),
        status=_parse_status(meta.get("status")),
        blocked_by=[str(b) for b in blocked_by_raw],
        target_file=meta.get("target_file"),
        symbol=meta.get("symbol"),
        symptom=symptom,
        root_cause=root_cause,
        solution=solution,
        pr_url=meta.get("pr_url"),
        plan_path=meta.get("plan_path"),
        task_id=meta.get("task_id"),
        aliases=[str(a) for a in (meta.get("aliases") or []) if str(a).strip()],
        created_at=meta.get("created_at"),
        updated_at=meta.get("updated_at"),
    )


def _parse_legacy(content: str, file_path: Path | None) -> AuditIssue:
    """Разбирает карточку в устаревшем формате (чистый Markdown без frontmatter)."""
    sections = _parse_h2_sections(content)
    stem = file_path.stem if file_path else "aud-unknown"

    # Парсинг H1 заголовка: # ISS-21: Описание проблемы
    h1_match = re.search(
        r"^#\s+([A-Za-z0-9_.\-]+(?:\s*\([^)]+\))?):\s*(.+)$", content, re.MULTILINE
    )
    if h1_match:
        issue_id = h1_match.group(1).strip()
        # A17: legacy H1 вида «# ISS-21 (backend): ...» даёт id с пробелами и
        # скобками — нормализуем: часть в скобках отбрасывается, пробелы — в дефис
        issue_id = re.sub(r"\s*\([^)]*\)\s*", "", issue_id).strip()
        issue_id = issue_id.replace(" ", "-")
        title = h1_match.group(2).strip()
    else:
        issue_id = stem
        h1_simple = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        title = h1_simple.group(1).strip() if h1_simple else stem

    # Парсинг статуса
    default_status = (
        AuditStatus.COMPLETED if file_path and "done" in file_path.parts else AuditStatus.PLANNED
    )
    status_match = re.search(
        r"(?:>\s*)?\*\*(?:Статус|Status):\*\*\s*(?:\[(?P<box>[ x/])\])?\s*(?:\*\*)?(?P<text>[^\n()]+)",
        content,
        re.IGNORECASE,
    )
    if status_match:
        status_text = (status_match.group("box") or "") + " " + (status_match.group("text") or "")
        status = _parse_status(status_text, default_status=default_status)
    else:
        status = default_status

    # Секции
    symptom = _find_matching_section(sections, *_SYMPTOM_KEYWORDS) or content.strip()
    root_cause = _find_matching_section(sections, *_ROOT_CAUSE_KEYWORDS)
    solution = _find_matching_section(sections, *_SOLUTION_KEYWORDS)

    # Секция оценки (Метрики / Оценка задачи)
    assessment_text = _find_matching_section(sections, "оценка", "assessment", "метрик") or ""
    priority = AuditPriority.IMPORTANT
    autonomy = AuditAutonomy.HIGH
    complexity = AuditComplexity.M
    blocked_by: list[str] = []

    if assessment_text:
        p_match = re.search(r"(?:Важность|Priority):\s*([^\n]+)", assessment_text, re.IGNORECASE)
        if p_match:
            priority = _parse_priority(p_match.group(1))

        a_match = re.search(r"(?:Автономия|Autonomy):\s*([^\n]+)", assessment_text, re.IGNORECASE)
        if a_match:
            autonomy = _parse_autonomy(a_match.group(1))
            blocker_match = re.findall(r"(?:ISS|aud)-[A-Za-z0-9_.\-]+", a_match.group(1))
            if blocker_match:
                blocked_by.extend(blocker_match)

        c_match = re.search(r"(?:Сложность|Complexity):\s*([^\n]+)", assessment_text, re.IGNORECASE)
        if c_match:
            complexity = _parse_complexity(c_match.group(1))

    # Категория из директории или префикса ID
    category = "general"
    if file_path:
        for part in reversed(file_path.parts[:-1]):
            if part not in ("audit", "todo", "done", ".agents"):
                category = part
                break

    return AuditIssue(
        id=issue_id,
        title=title,
        category=category,
        priority=priority,
        autonomy=autonomy,
        complexity=complexity,
        status=status,
        blocked_by=blocked_by,
        symptom=symptom,
        root_cause=root_cause,
        solution=solution,
    )


def serialize_audit_markdown(issue: AuditIssue) -> str:
    """Сериализует AuditIssue в стандартизированный Markdown с YAML frontmatter."""
    meta: dict[str, Any] = {
        "id": issue.id,
        "title": issue.title,
        "category": issue.category,
        "priority": issue.priority.value,
        "autonomy": issue.autonomy.value,
        "complexity": issue.complexity.value,
        "status": issue.status.value,
    }
    if issue.blocked_by:
        meta["blocked_by"] = issue.blocked_by
    if issue.target_file:
        meta["target_file"] = issue.target_file
    if issue.symbol:
        meta["symbol"] = issue.symbol
    if issue.pr_url:
        meta["pr_url"] = issue.pr_url
    if issue.plan_path:
        meta["plan_path"] = issue.plan_path
    if issue.task_id:
        meta["task_id"] = issue.task_id
    if issue.aliases:
        meta["aliases"] = issue.aliases
    if issue.created_at:
        meta["created_at"] = issue.created_at
    if issue.updated_at:
        meta["updated_at"] = issue.updated_at

    fm_yaml = dump_frontmatter(meta).strip()

    body_parts = [
        "---",
        fm_yaml,
        "---",
        "",
        f"# {issue.id}: {issue.title}",
        "",
        "## ❌ Симптом",
        f"{issue.symptom.strip()}",
    ]

    if issue.root_cause:
        body_parts.extend(
            [
                "",
                "## 🔍 Техническая первопричина",
                f"{issue.root_cause.strip()}",
            ]
        )

    if issue.solution:
        body_parts.extend(
            [
                "",
                "## 🛠 Предлагаемое решение",
                f"{issue.solution.strip()}",
            ]
        )

    body_parts.append("")
    return "\n".join(body_parts)


# ---------------------------------------------------------------------------
# ПАРСИНГ И СЕРИАЛИЗАЦИЯ ПЛАНОВ РАЗРАБОТКИ (PlanArtifact)
# ---------------------------------------------------------------------------


def serialize_plan_markdown(plan: PlanArtifact) -> str:
    """Сериализует PlanArtifact в канонический Markdown с YAML frontmatter."""
    meta: dict[str, Any] = {
        "id": plan.id,
        "schema_version": plan.schema_version,
        "task_id": plan.task_id,
        "title": plan.title,
        "category": plan.category,
        "status": plan.status.value,
    }
    if plan.pr_url:
        meta["pr_url"] = plan.pr_url
    if plan.target_files:
        meta["target_files"] = plan.target_files
    if plan.related_debts:
        meta["related_debts"] = plan.related_debts
    if plan.created_at:
        meta["created_at"] = plan.created_at
    if plan.updated_at:
        meta["updated_at"] = plan.updated_at

    fm_yaml = dump_frontmatter(meta).strip()

    status_badges = {
        PlanStatus.DISCUSSED: "⚪ Обсуждено (`discussed`)",
        PlanStatus.APPROVED: "🔵 Утверждено (`approved`)",
        PlanStatus.IN_PROGRESS: "🟡 В процессе реализации (`in_progress`)",
        PlanStatus.DONE: "🟢 Сделано (`done`)",
        PlanStatus.CANCELLED: "🔴 Отменено (`cancelled`)",
    }
    badge = status_badges.get(plan.status, f"`{plan.status.value}`")

    lines = [
        "---",
        fm_yaml,
        "---",
        "",
        f"# План: {plan.task_id} {plan.title}",
        "",
        f"> **Статус:** {badge}",
    ]
    if plan.pr_url:
        pr_display = plan.pr_url
        pr_match = re.search(r"pull/(\d+)", plan.pr_url)
        if pr_match:
            pr_display = f"#{pr_match.group(1)} ({plan.pr_url})"
        lines.append(f"> 🔗 **Pull Request:** [{pr_display}]({plan.pr_url})")

    lines.extend(
        [
            f"> 📌 **Задача:** {plan.task_id}",
            "",
            "---",
            "",
            "## 🎯 Цель и критерии приёмки",
            plan.content.strip() if plan.content else "Цели и контекст задачи.",
            "",
            "## 📋 Этапы и шаги",
        ]
    )

    if plan.steps:
        for step in plan.steps:
            box = "x" if step.completed else " "
            lines.append(f"- [{box}] {step.index}. {step.title}")
            if step.note:
                lines.append(f"  > 📝 {step.note.strip()}")
    else:
        lines.append("- [ ] 1. Инициализация реализации")

    lines.append("")
    return "\n".join(lines)


def _section_has_checkbox(lines: list[str], start_idx: int, end_idx: int) -> bool:
    """Проверяет наличие чекбоксов в заданном диапазоне строк вне code fence."""
    checkbox_re = re.compile(r"^(\s*[-*]|\s*\d+\.)\s*\[([ xX])\]")
    active_fence: tuple[str, int] | None = None
    for line_num in range(start_idx + 1, end_idx):
        cl = lines[line_num]
        is_fence, active_fence = core_md.check_fence_line(cl, active_fence)
        if is_fence:
            continue
        if active_fence is None and checkbox_re.match(cl):
            return True
    return False


def _find_body_start_line(lines: list[str]) -> int:
    """Определяет индекс первой строки body (после завершения YAML frontmatter)."""
    if not lines or lines[0].strip() != "---":
        return 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return i + 1
    return 0


def _select_steps_section(lines: list[str]) -> tuple[int, int] | None:
    """Определяет диапазон строк (start_line, end_line) целевой секции шагов плана.

    Гарантирует единый источник истины для ЧТЕНИЯ и ЗАПИСИ (инвариант R2):
    1. Игнорирует строки YAML frontmatter (сканирует только body).
    2. Ищет границы H2-секций с учётом code fence по CommonMark.
       Секцией считается заголовок с 0-3 пробелами отступа: '^[ ]{0,3}## '.
    3. Приоритет точного канонического заголовка ('этапы и шаги', 'steps', 'план шагов', 'шаги реализации').
    4. Fallback: секции со словами 'этап', 'шаг', 'step'.
       - Если ровно одна секция — выбираем её.
       - Если несколько секций — выбираем первую секцию, содержащую хотя бы один чекбокс.
       - Иначе берём первую подходящую секцию.
    Возвращает (start_idx, end_idx) где start_idx указывает на строку заголовка,
    а end_idx — на строку следующего H2 или len(lines).
    """
    body_start = _find_body_start_line(lines)
    active_fence: tuple[str, int] | None = None
    h2_sections: list[tuple[int, str]] = []
    h2_regex = re.compile(r"^[ ]{0,3}## (.*)$")

    for i in range(body_start, len(lines)):
        line = lines[i]
        is_fence, active_fence = core_md.check_fence_line(line, active_fence)
        if is_fence:
            continue
        if active_fence is None:
            m = h2_regex.match(line)
            if m:
                h2_sections.append((i, m.group(1).strip()))

    if not h2_sections:
        return None

    # Поиск канонической секции
    target_idx: int | None = None
    for i, title in h2_sections:
        h_lower = title.lower()
        if "этапы и шаги" in h_lower or h_lower in (
            "steps",
            "план шагов",
            "шаги реализации",
        ):
            target_idx = i
            break

    # Fallback при отсутствии канонического заголовка
    if target_idx is None:
        matching = [
            i
            for i, title in h2_sections
            if any(k in title.lower() for k in ("этап", "шаг", "step"))
        ]
        if len(matching) == 1:
            target_idx = matching[0]
        elif len(matching) > 1:
            for cand_i in matching:
                next_cand_end = next(
                    (next_h2 for next_h2, _ in h2_sections if next_h2 > cand_i),
                    len(lines),
                )
                if _section_has_checkbox(lines, cand_i, next_cand_end):
                    target_idx = cand_i
                    break
            if target_idx is None:
                target_idx = matching[0]

    if target_idx is None:
        return None

    # Находим конец секции (следующий H2)
    next_h2_idx = next(
        (i for i, _ in h2_sections if i > target_idx),
        len(lines),
    )
    return (target_idx, next_h2_idx)


def _get_canonical_steps_section(sections: dict[str, str]) -> str:
    """Находит каноническую секцию шагов плана из словаря секций (для обратной совместимости)."""
    for sec_header, sec_text in sections.items():
        h_lower = sec_header.lower()
        if "этапы и шаги" in h_lower or h_lower.strip() in (
            "steps",
            "план шагов",
            "шаги реализации",
        ):
            return sec_text

    matching = [
        sec_text
        for sec_header, sec_text in sections.items()
        if any(k in sec_header.lower() for k in ("этап", "шаг", "step"))
    ]
    if len(matching) == 1:
        return matching[0]

    for sec_text in matching:
        if re.search(r"(?m)^(\s*[-*]|\s*\d+\.)\s*\[([ xX])\]", sec_text):
            return sec_text

    return matching[0] if matching else ""


def _extract_plan_steps(content_lines_or_sections: list[str] | dict[str, str]) -> list[PlanStep]:
    """Извлекает шаги плана с единым алгоритмом поиска секции и учётом code fence."""
    if isinstance(content_lines_or_sections, dict):
        steps_section = _get_canonical_steps_section(content_lines_or_sections)
        lines = steps_section.splitlines()
    else:
        span = _select_steps_section(content_lines_or_sections)
        if not span:
            return []
        start_idx, end_idx = span
        lines = content_lines_or_sections[start_idx + 1 : end_idx]

    steps: list[PlanStep] = []
    active_fence: tuple[str, int] | None = None
    step_idx = 0
    current_step: PlanStep | None = None

    for line in lines:
        is_fence, active_fence = core_md.check_fence_line(line, active_fence)
        if is_fence or active_fence is not None:
            continue

        s_line = line.strip()
        m = re.match(r"^[-*]\s*\[([ xX])\]\s*(.*)$", s_line) or re.match(
            r"^\d+\.\s*\[([ xX])\]\s*(.*)$", s_line
        )
        if m:
            step_idx += 1
            completed = m.group(1).lower() == "x"
            raw_title = m.group(2).strip()
            title_clean = re.sub(r"^\d+\.\s*", "", raw_title).strip() or raw_title
            current_step = PlanStep(
                index=step_idx,
                title=title_clean,
                completed=completed,
                note=None,
            )
            steps.append(current_step)
        elif current_step and s_line.startswith("> 📝"):
            current_step.note = s_line.replace("> 📝", "").strip()

    return steps


def parse_plan_markdown(content: str, file_path: Path | None = None) -> PlanArtifact:
    """Парсит Markdown-план в структурированную модель PlanArtifact.

    Строго валидирует YAML frontmatter (при ошибке выбрасывает PLAN_FILE_CORRUPTED).
    """
    match = FRONTMATTER_SPLIT_REGEX.match(content)
    if not match:
        msg = f"PLAN_FILE_CORRUPTED: Отсутствует корректный YAML frontmatter в плане {file_path or ''}"
        raise ValueError(msg)

    raw_yaml, body = match.group(1), match.group(2)
    try:
        meta = yaml.safe_load(raw_yaml)
    except Exception as err:
        msg = f"PLAN_FILE_CORRUPTED: Синтаксическая ошибка в YAML frontmatter: {err}"
        raise ValueError(msg) from err

    if not isinstance(meta, dict):
        msg = f"PLAN_FILE_CORRUPTED: Frontmatter должен быть словарем: {file_path or ''}"
        raise ValueError(msg)

    stem = file_path.stem if file_path else "plan-general-unknown"
    plan_id = str(meta.get("id") or stem)
    task_id = str(meta.get("task_id") or stem.replace("plan-", ""))
    title = str(meta.get("title") or task_id)
    category = str(meta.get("category") or "general")

    raw_status = str(meta.get("status") or "discussed").lower()
    try:
        status = PlanStatus(raw_status)
    except ValueError:
        status = PlanStatus.DISCUSSED

    target_files = meta.get("target_files") or []
    if isinstance(target_files, str):
        target_files = [target_files]

    related_debts = meta.get("related_debts") or []
    if isinstance(related_debts, str):
        related_debts = [related_debts]

    sections = _parse_h2_sections(body)
    steps = _extract_plan_steps(content.splitlines())

    # Описание / Цель
    goal_section = _find_matching_section(sections, "цель", "goal", "критери", "описан") or ""
    content_text = goal_section.strip() if goal_section else body.strip()

    raw_sv = meta.get("schema_version") or 1
    try:
        schema_version = int(raw_sv)
    except (ValueError, TypeError) as err:
        msg = f"PLAN_FILE_CORRUPTED: schema_version должен быть целым числом: {raw_sv}"
        raise ValueError(msg) from err

    return PlanArtifact(
        id=plan_id,
        schema_version=schema_version,
        task_id=task_id,
        title=title,
        category=category,
        status=status,
        pr_url=meta.get("pr_url"),
        target_files=[str(f) for f in target_files],
        related_debts=[str(d) for d in related_debts],
        steps=steps,
        content=content_text,
        created_at=meta.get("created_at"),
        updated_at=meta.get("updated_at"),
    )


def _find_plan_checkboxes(
    lines: list[str],
) -> tuple[list[tuple[int, int, str, str, bool]], list[int]]:
    """Находит все чекбоксы в секциях этапов/шагов плана.

    Возвращает (список (строка_idx, step_num, prefix, suffix, is_done), список_строк_чекбоксов).
    """
    span = _select_steps_section(lines)
    if not span:
        return [], []

    target_sec_idx, next_h2_idx = span
    active_fence: tuple[str, int] | None = None
    checkboxes: list[tuple[int, int, str, str, bool]] = []
    step_num = 0

    for i in range(target_sec_idx + 1, next_h2_idx):
        line = lines[i]
        is_fence, active_fence = core_md.check_fence_line(line, active_fence)
        if is_fence or active_fence is not None:
            continue

        m = re.match(r"^(\s*[-*]|\s*\d+\.)\s*\[([ xX])\](\s*.*)$", line)
        if m:
            step_num += 1
            is_done = m.group(2).lower() == "x"
            checkboxes.append((i, step_num, m.group(1), m.group(3), is_done))

    return checkboxes, [c[0] for c in checkboxes]


def _compute_new_plan_status(
    current_status_str: str,
    all_steps_done: bool,
    is_completed_target: bool,
) -> tuple[bool, PlanStatus | None]:
    """Вычисляет новое состояние плана на основе прогресса закрытия шагов."""
    if all_steps_done and current_status_str in (
        PlanStatus.IN_PROGRESS.value,
        PlanStatus.APPROVED.value,
        PlanStatus.DISCUSSED.value,
    ):
        return True, PlanStatus.DONE
    if not all_steps_done and current_status_str == PlanStatus.DONE.value:
        return True, PlanStatus.IN_PROGRESS
    if is_completed_target and current_status_str in (
        PlanStatus.APPROVED.value,
        PlanStatus.DISCUSSED.value,
    ):
        return True, PlanStatus.IN_PROGRESS
    return False, None


def mutate_plan_step_in_markdown(
    content: str,
    step_index: int,
    status: str = "done",
    note: str | None = None,
    today_date: str | None = None,
) -> tuple[str, bool, PlanStatus | None]:
    """Точечно и безопасно модифицирует шаг плана в Markdown без потери форматирования.

    Возвращает (обновленный Markdown, изменился ли статус плана, новый статус плана).
    """
    match = FRONTMATTER_SPLIT_REGEX.match(content)
    if not match:
        msg = "PLAN_FILE_CORRUPTED: Отсутствует YAML frontmatter в плане"
        raise ValueError(msg)

    raw_yaml = match.group(1)
    try:
        meta = yaml.safe_load(raw_yaml) or {}
    except Exception as err:
        msg = f"PLAN_FILE_CORRUPTED: Ошибка YAML frontmatter: {err}"
        raise ValueError(msg) from err

    clean_status = status.strip().lower()
    if clean_status not in ("done", "completed", "open", "undone"):
        msg = f"INVALID_STATUS: Недопустимый статус шага '{status}'. Ожидается 'done' или 'open'."
        raise ValueError(msg)

    is_completed_target = clean_status in ("done", "completed")
    eff_today = today_date or datetime.now().strftime("%Y-%m-%d")

    lines = content.splitlines()
    checkboxes, _ = _find_plan_checkboxes(lines)

    target_match = next((c for c in checkboxes if c[1] == step_index), None)
    if not target_match:
        msg = (
            f"STEP_NOT_FOUND: Шаг с номером {step_index} не найден в секции этапов плана. "
            f"Всего найдено шагов: {len(checkboxes)}."
        )
        raise ValueError(msg)

    target_line_idx, _, prefix, suffix, _ = target_match
    new_mark = "x" if is_completed_target else " "
    lines[target_line_idx] = f"{prefix} [{new_mark}]{suffix}"

    if note:
        sanitized_note = note.replace("\n", " ").replace("\r", "").strip()[:500]
        note_line = f"  > 📝 {eff_today}: {sanitized_note}"
        next_idx = target_line_idx + 1
        if next_idx < len(lines) and lines[next_idx].strip().startswith("> 📝"):
            lines[next_idx] = note_line
        else:
            lines.insert(next_idx, note_line)

    # Пересчёт прогресса
    updated_checkboxes, _ = _find_plan_checkboxes(lines)
    all_steps_done = bool(updated_checkboxes and all(c[4] for c in updated_checkboxes))

    current_status_str = str(meta.get("status") or "discussed").lower()
    status_changed, new_plan_status = _compute_new_plan_status(
        current_status_str, all_steps_done, is_completed_target
    )

    meta["updated_at"] = eff_today
    if status_changed and new_plan_status:
        meta["status"] = new_plan_status.value

    full_text = "\n".join(lines)
    # Ф3-хвост (rebuild_card): пересборка через core-кодек. Жёсткий guard на
    # FRONTMATTER_SPLIT_REGEX.match выше уже отсёк CRLF/сломанные планы —
    # здесь все валидные LF-файлы, поведение эквивалентно ручной сборке
    # (meta уже замутена; mutator возвращает её как есть).
    try:
        new_content = rebuild_card(full_text, lambda m: meta)
    except FrontmatterError:
        new_content = full_text
    return new_content, status_changed, new_plan_status


def append_steps_to_plan_markdown(
    content: str,
    new_steps: list[str],
) -> str:
    """Безопасно добавляет новые шаги в существующую секцию этапов Markdown-плана.

    Сохраняет все остальные секции документа (цели, риски, инварианты).
    """
    if not new_steps:
        return content

    lines = content.splitlines()
    checkboxes, _ = _find_plan_checkboxes(lines)
    next_idx = len(checkboxes) + 1

    # Формируем строки для добавления
    added_lines: list[str] = []
    for i, s_text in enumerate(new_steps):
        clean_text = s_text.strip()
        if clean_text.startswith(("- [ ]", "- [x]", "* [ ]", "* [x]")):
            clean_text = clean_text[5:].strip()
        added_lines.append(f"- [ ] {next_idx + i}. {clean_text}")

    if checkboxes:
        # Вставляем после последнего чекбокса
        last_checkbox_line = checkboxes[-1][0]
        insert_pos = last_checkbox_line + 1
        # Если за последним чекбоксом идёт заметка > 📝, вставляем после неё
        while insert_pos < len(lines) and lines[insert_pos].strip().startswith("> 📝"):
            insert_pos += 1
        for offset, added_line in enumerate(added_lines):
            lines.insert(insert_pos + offset, added_line)
    else:
        # Если секция пуста или не содержит чекбоксов
        steps_header_idx = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("## ") and any(
                k in stripped.lower() for k in ("этап", "шаг", "step")
            ):
                steps_header_idx = i
                break
        if steps_header_idx is not None:
            for offset, added_line in enumerate(added_lines):
                lines.insert(steps_header_idx + 1 + offset, added_line)
        else:
            lines.extend(["", "## 📋 Этапы и шаги", *added_lines])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ПАРСИНГ И СЕРИАЛИЗАЦИЯ РЕВЬЮ (ReviewArtifact)
# ---------------------------------------------------------------------------


def serialize_review_markdown(review: ReviewArtifact) -> str:
    """Сериализует ReviewArtifact в канонический Markdown с YAML frontmatter."""
    meta: dict[str, Any] = {
        "id": review.id,
        "category": review.category,
        "round": review.round,
        "verdict": review.verdict.value,
    }
    if review.task_id:
        meta["task_id"] = review.task_id
    if review.pr_url:
        meta["pr_url"] = review.pr_url
    if review.reviewer:
        meta["reviewer"] = review.reviewer
    if review.spawned_debts:
        meta["spawned_debts"] = review.spawned_debts
    if review.created_at:
        meta["created_at"] = review.created_at

    fm_yaml = dump_frontmatter(meta).strip()

    verdict_badges = {
        ReviewVerdict.APPROVED: "🟢 Approved",
        ReviewVerdict.CHANGES_REQUESTED: "🔴 Changes Requested",
        ReviewVerdict.COMMENTED: "💬 Commented",
    }
    v_badge = verdict_badges.get(review.verdict, f"`{review.verdict.value}`")

    target_name = review.task_id or review.pr_url or "Review"
    lines = [
        "---",
        fm_yaml,
        "---",
        "",
        f"# Code Review: {target_name} (Раунд {review.round})",
        "",
        f"> **Вердикт:** {v_badge}",
    ]
    if review.pr_url:
        lines.append(f"> 🔗 **Pull Request:** [{review.pr_url}]({review.pr_url})")
    if review.task_id:
        lines.append(f"> 📌 **Задача:** {review.task_id}")
    if review.reviewer:
        lines.append(f"> 👤 **Ревьюер:** {review.reviewer}")

    lines.extend(
        [
            "",
            "---",
            "",
            "## 📋 Резюме проверки",
            review.summary.strip() if review.summary else "Замечаний нет.",
            "",
            "## 🔍 Замечания и дефекты",
        ]
    )

    if review.comments:
        for c in review.comments:
            line_str = f":{c.line}" if c.line else ""
            lines.append(f"- **[{c.status.upper()}]** `{c.file_path}{line_str}`: {c.symptom}")
    else:
        lines.append("- Замечаний не обнаружено.")

    lines.append("")
    return "\n".join(lines)


def parse_review_markdown(content: str, file_path: Path | None = None) -> ReviewArtifact:
    """Парсит Markdown ревью в структурированную модель ReviewArtifact."""
    match = FRONTMATTER_SPLIT_REGEX.match(content)
    if not match:
        msg = f"REVIEW_FILE_CORRUPTED: Отсутствует YAML frontmatter в файле ревью {file_path or ''}"
        raise ValueError(msg)

    raw_yaml, body = match.group(1), match.group(2)
    try:
        meta = yaml.safe_load(raw_yaml) or {}
    except Exception as err:
        msg = f"REVIEW_FILE_CORRUPTED: Ошибка парсинга YAML frontmatter: {err}"
        raise ValueError(msg) from err

    stem = file_path.stem if file_path else "rev-general-unknown"
    rev_id = str(meta.get("id") or stem)
    task_id = meta.get("task_id")
    category = str(meta.get("category") or "general")
    round_num = int(meta.get("round") or 1)

    raw_verdict = str(meta.get("verdict") or "approved").lower()
    try:
        verdict = ReviewVerdict(raw_verdict)
    except ValueError:
        verdict = ReviewVerdict.COMMENTED

    sections = _parse_h2_sections(body)
    summary_sec = _find_matching_section(sections, "резюме", "summary", "вердикт") or ""

    # Извлекаем комментарии
    comments: list[ReviewComment] = []
    comments_sec = _find_matching_section(sections, "замечан", "дефект", "comment", "issue") or ""
    if comments_sec:
        for line in comments_sec.splitlines():
            s_line = line.strip()
            # Формат: - **[OPEN]** `src/auth.py:42`: Описание
            # A9: путь может содержать ':' (C:\src\auth.py) — матчим жадно до
            # ПОСЛЕДНЕЙ пары `:<digits>` в бэктиках (rpartition-семантика),
            # иначе путь обрезается и комментарий теряется
            m = re.match(
                r"^[-*]\s*\*\*\[(\w+)\]\*\*\s*`(.+?)(?::(\d+))?`\s*:\s*(.*)$",
                s_line,
            )
            if m:
                status_raw = m.group(1).lower()
                c_status: Literal["open", "resolved", "deferred"] = (
                    status_raw if status_raw in ("open", "resolved", "deferred") else "open"  # type: ignore[assignment]
                )
                file_p = m.group(2).strip()
                line_no = int(m.group(3)) if m.group(3) else None
                symp = m.group(4).strip()
                comments.append(
                    ReviewComment(file_path=file_p, line=line_no, symptom=symp, status=c_status)
                )

    spawned = meta.get("spawned_debts") or []
    if isinstance(spawned, str):
        spawned = [spawned]

    return ReviewArtifact(
        id=rev_id,
        task_id=str(task_id) if task_id else None,
        category=category,
        round=round_num,
        verdict=verdict,
        summary=summary_sec.strip() if summary_sec else "Ревью выполнено.",
        comments=comments,
        pr_url=meta.get("pr_url"),
        reviewer=meta.get("reviewer"),
        spawned_debts=[str(s) for s in spawned],
        created_at=meta.get("created_at"),
    )
