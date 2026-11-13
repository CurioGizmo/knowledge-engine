"""Слотовый компилятор скиллов (Skeleton + Blocks + Overlays)."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp_skills.errors import (
    CircularExtensionError,
    MaxExtensionDepthError,
    MissingRequiredBlockError,
    SkillNotFoundError,
)
from mcp_skills.models import SkillCard
from mcp_skills.parser import find_slots_with_modes
from mcp_skills.resources import extract_resources_from_markdown
from mcp_skills.spam_gate import is_safe_name
from mcp_skills.storage import SkillsStorage

__all__ = ["AssemblyResult", "SlotCompiler"]

logger = logging.getLogger("mcp_skills.slot_compiler")


def _slot_required_servers(card: SkillCard) -> list[str]:
    """Имена MCP-серверов, необходимые скиллу (R7, ревизия 4).

    ``requires_servers`` — список имён («нужны ВСЕ перечисленные»); он
    применяется к mcp-кандидатам ЛЮБОГО слота (слотовой адресации нет —
    dict-форма отвергнута парсером). Пустой список — блок без серверных
    зависимостей (offline/default совместимы, проверка не применяется).
    """
    rs = card.requires_servers
    if isinstance(rs, dict):  # неинициализированные legacy-карточки: защита
        return []
    return [str(s).strip() for s in rs if str(s).strip()]


def _resolve_slot_with_servers(
    card: SkillCard,
    availability: Callable[[str], str],
) -> tuple[bool, str | None, str | None]:
    """Можно ли выбрать mcp-кандидата слота; причины для ответа.

    Возвращает (allowed, reason, warning):
    - allowed=False, reason не None → mcp-блок запрещён (absent-сервер),
      reason уходит в sections_meta.server_reason (R7: заменён на
      offline/default с объяснением).
    - allowed=True, warning не None → mcp-блок собран, но требует
      серверы в состоянии unknown (MCP_ACTIVE_SERVERS не задан):
      предупреждение ОБЯЗАНО попасть в ответ инструмента
      (sections_meta.server_warning, ревизия 4), не только в лог.
    """
    servers = _slot_required_servers(card)
    if not servers:
        return True, None, None
    absent = [s for s in servers if availability(s) == "absent"]
    if absent:
        return (
            False,
            (
                f"offline: требуемые MCP-серверы отсутствуют в MCP_ACTIVE_SERVERS: {', '.join(absent)}"
            ),
            None,
        )
    unknown = [s for s in servers if availability(s) == "unknown"]
    if unknown:
        warning = (
            f"unknown: MCP_ACTIVE_SERVERS не задан — доступность серверов "
            f"({', '.join(unknown)}) не подтверждена; mcp-блок собран, "
            "проверьте доступность серверов у хоста."
        )
        logger.warning("Скилл требует серверы %s: %s", ", ".join(unknown), warning)
        return True, None, warning
    return True, None, None


_WARNED_LEGACY_OVERLAY = False
MAX_EXTENSION_DEPTH = 5


def _get_server_availability(
    project_root: Path | None,
) -> Callable[[str], str]:
    """Коллбэк доступности серверов из ProjectContext (R7).

    project_root=None → всегда 'unknown' (окружение неизвестно; глобальное
    отключение интеграций запрещено — mcp-блок собирается с предупреждением).
    """
    if project_root is None:
        return lambda _name: "unknown"
    try:
        from mcp_skills.project_context import get_project_context

        ctx = get_project_context(project_root)
        return ctx.server_availability
    except Exception:
        logger.warning("Не удалось вычислить ProjectContext для %s; серверы unknown", project_root)
        return lambda _name: "unknown"


@dataclass
class AssemblyResult:
    """Полный результат одной сборки скилла без общего состояния компилятора."""

    card: SkillCard
    content: str
    resources: list[dict[str, Any]] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)
    slots_filled: list[str] = field(default_factory=list)
    missing_slots: list[str] = field(default_factory=list)
    effective_slots: list[str] = field(default_factory=list)


class SlotCompiler:
    """Сборщик скиллов из базового скелета, технологических блоков и проектных оверлеев."""

    def __init__(self, storage: SkillsStorage, catalog_root: Path | None = None) -> None:
        self.storage = storage
        self.catalog_root = catalog_root

    @staticmethod
    def _effective_sources(
        cards: list[SkillCard], bodies: list[str]
    ) -> list[tuple[SkillCard, str]]:
        """Сохраняет происхождение фрагментов и отбрасывает заменённые override-предки."""
        parts = [(cards[-1], bodies[-1])]
        for card, body in reversed(list(zip(cards[:-1], bodies[:-1], strict=True))):
            part = (card, body)
            if body.strip() and card.merge_strategy == "override":
                parts = [part]
            elif card.merge_strategy == "prepend":
                parts.insert(0, part)
            else:
                parts.append(part)
        return parts

    def collect_available_slots(self, name: str) -> list[str]:
        cards, bodies = self._resolve_inheritance_chain(name)
        return list(
            dict.fromkeys(
                slot
                for card, _ in self._effective_sources(cards, bodies)
                for slot in card.available_slots or []
            )
        )

    def optional_slots(self, name: str) -> set[str]:
        cards, bodies = self._resolve_inheritance_chain(name)
        return {
            slot
            for card, _ in self._effective_sources(cards, bodies)
            for slot in card.optional_slots or []
        }

    def _assemble_skeleton(
        self,
        cards: list[SkillCard],
        bodies: list[str],
        catalog_root: Path | None,
        name: str,
        project_root: Path | None,
    ) -> tuple[str, list[dict[str, Any]]]:
        parts: list[str] = []
        resources: list[dict[str, Any]] = []
        for card, body in self._effective_sources(cards, bodies):
            if catalog_root and card.base_dir:
                body, found = extract_resources_from_markdown(
                    body, card.base_dir / "SKILL.md", catalog_root, name, project_root
                )
                resources.extend(found)
            if body.strip():
                parts.append(body.strip())
        return "\n\n".join(parts), resources

    def _resolve_inheritance_chain(self, name: str) -> tuple[list[SkillCard], list[str]]:
        """Разрешает цепочку наследования extends (от потомка к предкам) с проверкой циклов и глубины."""
        cards: list[SkillCard] = []
        bodies: list[str] = []
        visited: set[str] = set()
        current: str | None = name
        depth = 0

        while current is not None:
            if current in visited:
                msg = f"Обнаружена циклическая зависимость для скилла '{current}'"
                raise CircularExtensionError(msg)
            if depth > MAX_EXTENSION_DEPTH:
                msg = f"Превышена максимальная глубина наследования ({MAX_EXTENSION_DEPTH}) для скилла '{current}'"
                raise MaxExtensionDepthError(msg)

            visited.add(current)
            res = self.storage.get_skill(current)
            if not res:
                msg = f"Скилл '{current}' не найден в хранилище"
                raise SkillNotFoundError(msg)

            card, body = res
            cards.append(card)
            bodies.append(body)
            current = card.extends
            depth += 1

        return cards, bodies

    def assemble_skill(
        self,
        name: str,
        stack: list[str] | None = None,
        touches: list[str] | None = None,
        project_stack: list[str] | None = None,
        project_root: Path | None = None,
    ) -> tuple[SkillCard, str]:
        """Собирает Markdown: touches — секции, project_stack — технологии."""
        res = self.assemble_result(
            name=name,
            stack=stack,
            touches=touches,
            project_stack=project_stack,
            project_root=project_root,
        )
        return res.card, res.content

    def assemble_skill_with_resources(
        self,
        name: str,
        stack: list[str] | None = None,
        touches: list[str] | None = None,
        project_stack: list[str] | None = None,
        project_root: Path | None = None,
    ) -> tuple[SkillCard, str, list[dict[str, Any]]]:
        """Собирает Markdown и возвращает список связанных ресурсов."""
        res = self.assemble_result(
            name=name,
            stack=stack,
            touches=touches,
            project_stack=project_stack,
            project_root=project_root,
        )
        return res.card, res.content, res.resources

    def assemble_result(
        self,
        name: str,
        stack: list[str] | None = None,
        touches: list[str] | None = None,
        project_stack: list[str] | None = None,
        project_root: Path | None = None,
    ) -> AssemblyResult:
        """Собирает скилл и возвращает полный результат сборки без общего состояния."""
        cards, bodies = self._resolve_inheritance_chain(name)
        target_card = cards[0]
        catalog_root = (
            self._detect_catalog_root(target_card.base_dir)
            if target_card.base_dir
            else self.catalog_root
        )

        assembled_body, skeleton_resources = self._assemble_skeleton(
            cards, bodies, catalog_root, name, project_root
        )

        # Сливаем доступные слоты
        available_slots = self.collect_available_slots(name)
        target_card.available_slots = available_slots

        # Определение эффективных секций touches
        raw_touches = touches if touches is not None else stack
        if raw_touches is None:
            opt = self.optional_slots(name)
            effective_slots = [s for s in available_slots if s not in opt]
        else:
            unknown_slots = [s for s in raw_touches if s not in available_slots]
            if unknown_slots:
                msg = f"Неизвестные секции для скилла '{name}': {unknown_slots}. Доступные слоты: {available_slots}"
                raise ValueError(msg)
            effective_slots = list(dict.fromkeys(raw_touches))

        collected_resources: list[dict[str, Any]] = []
        seen_res_ids: set[tuple[str, str | None]] = set()

        def _add_resources(res_list: list[dict[str, Any]]) -> None:
            for r in res_list:
                rid = r.get("resource_id")
                identity = (rid, r.get("anchor"))
                if rid and identity not in seen_res_ids:
                    seen_res_ids.add((rid, r.get("anchor")))
                    collected_resources.append(r)

        _add_resources(skeleton_resources)

        # 2. Подстановка блоков слотов один раз
        blocks_dirs = [
            c.base_dir / "blocks" for c in cards if c.base_dir and (c.base_dir / "blocks").is_dir()
        ]
        shared_blocks_dir = (catalog_root / "blocks") if catalog_root else None

        body, slot_resources, sections_meta, unique_stack = self._inject_slots_unified(
            body=assembled_body,
            touches=effective_slots,
            project_stack=project_stack or [],
            blocks_dirs=blocks_dirs,
            shared_blocks_dir=shared_blocks_dir,
            catalog_root=catalog_root,
            owner_skill=name,
            project_root=project_root,
            card=target_card,
            availability=_get_server_availability(project_root),
        )
        _add_resources(slot_resources)

        if project_root:
            self._check_legacy_overlay_warning(project_root)

        # Сборка манифеста: matched/unmatched технологии, секции и источники
        matched_techs_set: set[str] = set()
        for sm in sections_meta.values():
            for t in sm.get("matched_technologies", []):
                matched_techs_set.add(t)

        matched_technologies = [t for t in unique_stack if t in matched_techs_set]
        unmatched_technologies = [t for t in unique_stack if t not in matched_techs_set]

        sections = {}
        for slot, meta in sections_meta.items():
            entry: dict[str, Any] = {
                "status": meta["status"],
                "source": meta["source"],
                "matched_technologies": meta["matched_technologies"],
            }
            if meta.get("server_reason"):
                # R7: причина замены mcp-блока на offline (absent-сервер)
                entry["server_reason"] = meta["server_reason"]
            if meta.get("server_warning"):
                # R7 (ревизия 4): предупреждение unknown — в ответ инструмента
                entry["server_warning"] = meta["server_warning"]
            sections[slot] = entry

        missing_slots = [
            slot for slot, meta in sections_meta.items() if meta["status"] == "missing"
        ]
        slots_filled = [slot for slot in effective_slots if slot not in missing_slots]

        revision = (
            self.storage.get_dependency_revision()
            if hasattr(self.storage, "get_dependency_revision")
            else ""
        )
        selected_resources = [
            r["resource_id"] + ("#" + r["anchor"] if r.get("anchor") else "")
            for r in collected_resources
            if isinstance(r, dict) and "resource_id" in r
        ]

        manifest = {
            "requested_stack": list(unique_stack),
            "matched_technologies": matched_technologies,
            "unmatched_technologies": unmatched_technologies,
            "sections": sections,
            "selected_resources": selected_resources,
            "revision": revision,
            "status": "complete" if not missing_slots else "incomplete",
        }
        if missing_slots:
            manifest["missing_slots"] = missing_slots

        return AssemblyResult(
            card=target_card,
            content=body.strip(),
            resources=collected_resources,
            manifest=manifest,
            slots_filled=slots_filled,
            missing_slots=missing_slots,
            effective_slots=effective_slots,
        )

    def slot_variants(self, card: SkillCard, slots: list[str]) -> dict[str, list[str]]:
        """Варианты локальных и общих блоков с тем же поиском корня, что у сборки."""
        if card.base_dir is None:
            return {}
        root = self._detect_catalog_root(card.base_dir)
        parents = [card.base_dir / "blocks"]
        if root is not None:
            parents.append(root / "blocks")
        variants: dict[str, list[str]] = {}
        for slot in slots:
            if not is_safe_name(slot):
                continue
            values: set[str] = set()
            for parent in parents:
                directory = self._resolve_dir(parent, slot)
                if directory is None:
                    continue
                values.update(
                    path.stem.lower()
                    for path in directory.glob("*.md")
                    if path.stem.lower() != "default"
                    and is_safe_name(path.stem)
                    and self._read_block_file(directory, path.name) is not None
                )
            if values:
                variants[slot] = sorted(values)
        return variants

    def _detect_catalog_root(self, base_dir: Path | None) -> Path | None:
        """Ищет корень каталога скиллов вверх по дереву от папки скилла."""
        if self.catalog_root is not None:
            return self.catalog_root
        if base_dir:
            curr = base_dir.resolve()
            for parent in curr.parents:
                if (parent / "blocks").is_dir() and (parent / "skills").is_dir():
                    return parent
                if (parent / "verify_catalog.py").is_file():
                    return parent
                if (parent / "skills").is_dir() and (parent / "agents").is_dir():
                    return parent
        from mcp_core.paths import resolve_skills_repo_dir

        repo = resolve_skills_repo_dir()
        return repo if repo.name != "skills" else repo.parent

    def _inject_slots_unified(
        self,
        body: str,
        touches: list[str],
        project_stack: list[str],
        blocks_dirs: list[Path],
        shared_blocks_dir: Path | None = None,
        catalog_root: Path | None = None,
        owner_skill: str = "",
        project_root: Path | None = None,
        card: SkillCard | None = None,
        availability: Callable[[str], str] | None = None,
    ) -> tuple[str, list[dict[str, Any]], dict[str, dict[str, Any]], list[str]]:
        """Подставляет контент блоков в слоты скелета и собирает ресурсы.

        R7: card + availability — per-card проверка интеграционных блоков
        с requires_servers (mcp-кандидат слота при absent-сервере
        заменяется на offline/default с примечанием причины).
        """
        slot_modes = find_slots_with_modes(body)
        present_slots = list(slot_modes.keys())

        # Дедупликация project_stack без учета регистра с сохранением исходного порядка
        seen: set[str] = set()
        unique_stack: list[str] = []
        for item in project_stack or []:
            if isinstance(item, str):
                cleaned = item.strip().lower()
                if cleaned and cleaned not in seen:
                    seen.add(cleaned)
                    unique_stack.append(cleaned)

        all_collected_resources: list[dict[str, Any]] = []
        sections_meta: dict[str, dict[str, Any]] = {}

        for slot in touches:
            is_multi = slot_modes.get(slot, False)
            slot_content, slot_resources, slot_meta = self._resolve_slot_content_unified(
                blocks_dirs=blocks_dirs,
                shared_blocks_dir=shared_blocks_dir,
                slot=slot,
                project_stack=unique_stack,
                is_multi=is_multi,
                catalog_root=catalog_root,
                owner_skill=owner_skill,
                project_root=project_root,
                card=card,
                availability=availability,
            )
            sections_meta[slot] = slot_meta
            all_collected_resources.extend(slot_resources)
            marker_pattern = rf"<!--\s*SLOT\+?:\s*{re.escape(slot)}\s*-->"

            if slot in present_slots and re.search(marker_pattern, body):
                # Используем функцию-замену для предотвращения проблем с backreferences (\1, \g<...>)
                def _replace_marker(_m: re.Match[str], sc: str = slot_content) -> str:
                    return sc

                body = re.sub(marker_pattern, _replace_marker, body)
            else:
                # Если маркера в тексте нет, добавляем в конец документа
                body += f"\n\n## Спецификация реализации: {slot}\n\n{slot_content}\n"

        # Очищаем оставшиеся невостребованные маркеры слотов (как SLOT, так и SLOT+)
        body = re.sub(r"<!--\s*SLOT\+?:\s*[a-zA-Z0-9_-]+\s*-->\n?", "", body)
        return body, all_collected_resources, sections_meta, unique_stack

    def _inject_slots(
        self,
        body: str,
        touches: list[str],
        project_stack: list[str],
        base_dir: Path,
        catalog_root: Path | None = None,
        owner_skill: str = "",
    ) -> tuple[str, list[dict[str, Any]]]:
        """Подставляет контент блоков в слоты скелета (legacy single-dir wrapper)."""
        blocks_dirs = [base_dir / "blocks"] if (base_dir / "blocks").is_dir() else []
        shared = (catalog_root / "blocks") if catalog_root else None
        b, r, _, _ = self._inject_slots_unified(
            body=body,
            touches=touches,
            project_stack=project_stack,
            blocks_dirs=blocks_dirs,
            shared_blocks_dir=shared,
            catalog_root=catalog_root,
            owner_skill=owner_skill,
        )
        return b, r

    @staticmethod
    def _resolve_dir(parent_dir: Path | None, sub: str) -> Path | None:
        """Безопасно проверяет и возвращает вложенную директорию."""
        if not parent_dir:
            return None
        try:
            d = (parent_dir / sub).resolve()
            if d.is_relative_to(parent_dir.resolve()) and d.is_dir():
                return d
        except (OSError, ValueError):
            pass
        return None

    @staticmethod
    def _read_block_file(directory: Path | None, filename: str) -> str | None:
        """Читает контент файла блока, если он существует."""
        if not directory:
            return None
        try:
            target = (directory / filename).resolve()
            if target.is_relative_to(directory.resolve()) and target.is_file():
                return target.read_text(encoding="utf-8").strip()
        except (OSError, ValueError, UnicodeError):
            pass
        return None

    def _find_block_variant(
        self,
        blocks_dirs: list[Path],
        shared: Path | None,
        slot: str,
        filename: str,
        catalog_root: Path | None,
        owner: str,
        project_root: Path | None,
    ) -> tuple[str | None, list[dict[str, Any]], str | None]:
        candidates = [(base, "local") for base in blocks_dirs]
        if shared is not None:
            candidates.append((shared, "shared"))
        for base, kind in candidates:
            directory = self._resolve_dir(base, slot)
            raw = self._read_block_file(directory, filename)
            if raw is None or directory is None:
                continue
            resources: list[dict[str, Any]] = []
            if catalog_root:
                raw, resources = extract_resources_from_markdown(
                    raw, directory / filename, catalog_root, owner, project_root
                )
            return raw, resources, f"{kind}:{filename}"
        return None, [], None

    def _match_stack_blocks(
        self,
        blocks_dirs: list[Path],
        shared_blocks_dir: Path | None,
        slot: str,
        project_stack: list[str],
        is_multi: bool,
        catalog_root: Path | None,
        owner_skill: str,
        project_root: Path | None,
        card: SkillCard | None,
        availability: Callable[[str], str] | None,
    ) -> tuple[
        tuple[str, dict[str, Any]] | None,
        list[str],
        list[dict[str, Any]],
        list[str],
        list[str],
        str | None,
        str | None,
    ]:
        """Перебор стека технологий для слота (R7: серверный гейт mcp).

        Возвращает (first_result, matched_contents, collected_res,
        matched_sources, matched_techs, server_reason, server_warning):
        first_result не None — единичное совпадение (не multi-слот), готовый
        (content, meta); server_reason — причина пропуска mcp-кандидата
        (absent-серверы) для offline/default-фолбэка; server_warning —
        unknown-предупреждение (ревизия 4: в ответ инструмента).
        """
        matched_contents: list[str] = []
        collected_res: list[dict[str, Any]] = []
        matched_sources: list[str] = []
        matched_techs: list[str] = []
        server_reason: str | None = None
        server_warning: str | None = None

        for tech in project_stack:
            if not is_safe_name(tech):
                continue
            tech_clean = tech.lower()
            tech_filename = f"{tech_clean}.md"
            # R7: per-card серверный гейт mcp-кандидата
            gate_reason, gate_warning, skip_tech = self._server_gate_for_tech(
                tech_clean, card, availability
            )
            if gate_warning:
                server_warning = gate_warning
            if skip_tech:
                server_reason = gate_reason
                continue  # mcp-кандидат пропущен → offline/default ниже
            content, res, found_source = self._find_block_variant(
                blocks_dirs,
                shared_blocks_dir,
                slot,
                tech_filename,
                catalog_root,
                owner_skill,
                project_root,
            )

            if content:
                collected_res.extend(res)
                if found_source:
                    matched_sources.append(found_source)
                matched_techs.append(tech_clean)
                if not is_multi:
                    meta: dict[str, Any] = {
                        "status": "matched",
                        "source": found_source,
                        "matched_technologies": [tech_clean],
                    }
                    if server_warning:
                        meta["server_warning"] = server_warning
                    return (
                        (content, meta),
                        matched_contents,
                        collected_res,
                        matched_sources,
                        matched_techs,
                        server_reason,
                        server_warning,
                    )
                matched_contents.append(content)

        return (
            None,
            matched_contents,
            collected_res,
            matched_sources,
            matched_techs,
            server_reason,
            server_warning,
        )

    @staticmethod
    def _server_gate_for_tech(
        tech_clean: str,
        card: SkillCard | None,
        availability: Callable[[str], str] | None,
    ) -> tuple[str | None, str | None, bool]:
        """R7: серверный гейт mcp-кандидата слота.

        Возвращает (server_reason, server_warning, skip_tech):
        - skip_tech=True — mcp-кандидат отклонён (absent-серверы),
          server_reason несёт причину для offline/default-фолбэка;
        - server_warning — unknown-предупреждение (в ответ инструмента);
        - не-mcp технологии и карточки без requires_servers — без гейта.
        """
        if tech_clean != "mcp" or card is None:
            return None, None, False
        allowed, reason, warning = _resolve_slot_with_servers(
            card, availability or (lambda _n: "unknown")
        )
        if not allowed:
            return reason, None, True
        return None, warning, False

    def _resolve_slot_content_unified(
        self,
        blocks_dirs: list[Path],
        shared_blocks_dir: Path | None,
        slot: str,
        project_stack: list[str],
        is_multi: bool = False,
        catalog_root: Path | None = None,
        owner_skill: str = "",
        project_root: Path | None = None,
        card: SkillCard | None = None,
        availability: Callable[[str], str] | None = None,
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        """Ищет подходящий файл блока: сначала в blocks_dirs (потомок -> предок), затем в shared_blocks_dir.

        R7: mcp-кандидат слота проверяется по requires_servers карточки
        (per-card): absent-сервер → кандидат пропускается, берётся
        offline/default с примечанием причины; unknown → mcp-блок +
        предупреждение в лог (глобальное отключение запрещено).
        """
        if not is_safe_name(slot):
            return (
                "> ⚠️ [Недопустимое имя слота]",
                [],
                {"status": "missing", "source": None, "matched_technologies": []},
            )

        # R7: причина отказа от mcp-кандидата (уходит в slot_meta) и
        # предупреждение unknown — в ответ инструмента (ревизия 4).
        # Цикл по стеку вынесен в _match_stack_blocks (C901).
        (
            first_result,
            matched_contents,
            collected_res,
            matched_sources,
            matched_techs,
            server_reason,
            server_warning,
        ) = self._match_stack_blocks(
            blocks_dirs,
            shared_blocks_dir,
            slot,
            project_stack,
            is_multi,
            catalog_root,
            owner_skill,
            project_root,
            card,
            availability,
        )
        if first_result is not None:
            content, meta = first_result
            return content, collected_res, meta
        if matched_contents:
            multi_meta: dict[str, Any] = {
                "status": "matched",
                "source": ", ".join(matched_sources),
                "matched_technologies": matched_techs,
            }
            if server_warning:
                multi_meta["server_warning"] = server_warning
            return "\n\n".join(matched_contents), collected_res, multi_meta

        # R7: mcp-кандидат пропущен из-за отсутствия серверов — пробуем
        # offline-блок как замену (дуальные блоки mode/mcp.md|offline.md),
        # затем default-зонд.
        if server_reason:
            offline_content, res, source = self._find_block_variant(
                blocks_dirs,
                shared_blocks_dir,
                slot,
                "offline.md",
                catalog_root,
                owner_skill,
                project_root,
            )
            if offline_content:
                offline_meta: dict[str, Any] = {
                    "status": "matched",
                    "source": source,
                    "matched_technologies": ["offline"],
                    "server_reason": server_reason,
                }
                if server_warning:
                    offline_meta["server_warning"] = server_warning
                return offline_content, res, offline_meta

        default_content, res, source = self._find_block_variant(
            blocks_dirs,
            shared_blocks_dir,
            slot,
            "default.md",
            catalog_root,
            owner_skill,
            project_root,
        )
        if default_content:
            m: dict[str, Any] = {"status": "default", "source": source, "matched_technologies": []}
            if server_reason:
                # R7: offline/default выбран из-за отсутствия серверов —
                # причина фиксируется в примечании к секции.
                m["server_reason"] = server_reason
                default_content = f"{default_content}\n\n> ℹ️ [{server_reason}]"
            if server_warning:
                m["server_warning"] = server_warning
            return (
                default_content,
                res,
                m,
            )

        # Ревизия 4 (R7): если mcp-кандидат отклонён из-за absent-серверов и
        # НЕТ ни offline-, ни default-блока — это НЕ missing-заглушка, а
        # невозможность сборки обязательных инструкций: явная ошибка.
        if server_reason:
            msg = (
                f"Невозможно собрать слот '{slot}': mcp-блок требует серверы, "
                f"которые отсутствуют ({server_reason}), а блоки offline/default "
                f"не найдены. Установите сервер, задайте MCP_ACTIVE_SERVERS "
                f"или добавьте offline/default блок в каталог скиллов."
            )
            raise MissingRequiredBlockError(msg, missing_slots=[slot])

        return (
            "> ⚠️ [Спецификация отсутствует]",
            [],
            {
                "status": "missing",
                "source": None,
                "matched_technologies": [],
            },
        )

    def _resolve_slot_content(
        self,
        blocks_dir: Path,
        shared_blocks_dir: Path | None,
        slot: str,
        project_stack: list[str],
        is_multi: bool = False,
        catalog_root: Path | None = None,
        owner_skill: str = "",
    ) -> tuple[str, list[dict[str, Any]]]:
        """Legacy single-dir wrapper для _resolve_slot_content_unified."""
        content, res, _ = self._resolve_slot_content_unified(
            blocks_dirs=[blocks_dir],
            shared_blocks_dir=shared_blocks_dir,
            slot=slot,
            project_stack=project_stack,
            is_multi=is_multi,
            catalog_root=catalog_root,
            owner_skill=owner_skill,
        )
        return content, res

    def _check_legacy_overlay_warning(self, project_root: Path) -> None:
        """Однократное предупреждение при обнаружении устаревших оверлеев в проекте."""
        global _WARNED_LEGACY_OVERLAY
        if _WARNED_LEGACY_OVERLAY:
            return
        overlays_dir = project_root / ".agents" / "skills"
        if overlays_dir.is_dir() and any(overlays_dir.glob("*.overlay.md")):
            logger.warning(
                "Обнаружены устаревшие оверлеи в %s/*.overlay.md. "
                "Проектные оверлеи больше не применяются автоматически. "
                "Перенесите доработки в единый репозиторий скиллов.",
                overlays_dir,
            )
            _WARNED_LEGACY_OVERLAY = True
