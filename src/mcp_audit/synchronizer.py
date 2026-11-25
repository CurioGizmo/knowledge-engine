"""Синхронизатор между каталогами карточек (.agents/audit/todo и done) и SQLite FTS5 индексом."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]


import yaml

from mcp_audit.models import (
    AuditIssue,
    AuditStatus,
    PlanArtifact,
    PlanStatus,
    PlanStep,
    PlanVersionMetadata,
    ReviewArtifact,
    StepPlanResponse,
)
from mcp_audit.parser import (
    FRONTMATTER_SPLIT_REGEX,
    append_actual_resolution_to_markdown,
    append_steps_to_plan_markdown,
    compute_plan_definition_projection,
    compute_plan_definition_sha256,
    extract_actual_resolution_lines,
    mutate_plan_step_in_markdown,
    parse_audit_markdown,
    parse_plan_markdown,
    parse_review_markdown,
    replace_goal_section_in_markdown,
    replace_steps_in_markdown,
    serialize_audit_markdown,
    serialize_plan_markdown,
    serialize_review_markdown,
    set_actual_resolution_lines,
)
from mcp_audit.storage import AuditStorage
from mcp_core.card_codec import (
    FrontmatterError,
    dump_frontmatter,
    rebuild_card,
    split_frontmatter,
)
from mcp_core.sanitizer import normalize_category_slug, sanitize_task_id, title_to_slug
from mcp_core.sync import BaseSynchronizer, _atomic_write_text, move_card

__all__ = ["AuditSynchronizer"]

logger = logging.getLogger("mcp_audit.synchronizer")


_local_locks = threading.local()


@contextlib.contextmanager
def _advisory_lock(lock_path: Path):
    """Межпроцессный рекомендательный (advisory) лок для безопасных файловых мутаций.

    Гарантии:
    - На POSIX-платформах (при наличии fcntl) удерживается эксклюзивный flock на время всего блока with.
    - На Windows блокировка осуществляется через msvcrt.locking эксклюзивного региона файла
      с таймаутом ожидания до ~10 секунд (10 попыток с интервалом 1 с).
    - Файл лока открывается без усечения (os.O_CREAT | os.O_RDWR).
    - Файл лока не удаляется после снятия блокировки для предотвращения гонок дескрипторов.
    - Лок реентерабелен в рамках одного потока для одного канонического пути.
    - При отсутствии fcntl и msvcrt выполняется как no-op без взаимного исключения.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_key = str(lock_path.resolve())

    if not hasattr(_local_locks, "held"):
        _local_locks.held = {}

    if canonical_key in _local_locks.held:
        _local_locks.held[canonical_key]["count"] += 1
        try:
            yield
        finally:
            _local_locks.held[canonical_key]["count"] -= 1
            if _local_locks.held[canonical_key]["count"] == 0:
                del _local_locks.held[canonical_key]
        return

    if fcntl is None:
        msvcrt = _import_msvcrt()
        if msvcrt is None:
            # Neither fcntl nor msvcrt: documented no-op, no mutual exclusion guarantee.
            yield
            return
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o666)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            _local_locks.held[canonical_key] = {"count": 1, "fd": fd}
            try:
                yield
            finally:
                _local_locks.held.pop(canonical_key, None)
                with contextlib.suppress(OSError):
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)
        return

    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _local_locks.held[canonical_key] = {"count": 1, "fd": fd}
        try:
            yield
        finally:
            _local_locks.held.pop(canonical_key, None)
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _import_msvcrt():
    """Лениво импортирует msvcrt (Windows); None на других платформах.

    mypy знает msvcrt как платформенную stdlib-заглушку, поэтому без
    type-ignore; сама ImportError-ветка обрабатывает отсутствие на POSIX.
    """
    try:
        import msvcrt

        return msvcrt
    except ImportError:
        return None


def _normalize_step_title(t: str) -> str:
    """Нормализует заголовок шага для структурного сравнения."""
    cleaned = str(t).strip()
    if cleaned.startswith(("- [ ]", "- [x]", "- [X]", "* [ ]", "* [x]", "* [X]")):
        cleaned = cleaned[5:].strip()
    return re.sub(r"^\d+\.\s*", "", cleaned).strip()


def _get_step_title_str(s: Any) -> str:
    if isinstance(s, PlanStep):
        return s.title
    if isinstance(s, dict):
        return str(s.get("title") or "")
    return str(s)


def _steps_structure_matches(steps_a: list[Any], steps_b: list[Any]) -> bool:
    """Сравнивает названия и порядок шагов двух списков."""
    if len(steps_a) != len(steps_b):
        return False
    titles_a = [_normalize_step_title(_get_step_title_str(s)) for s in steps_a]
    titles_b = [_normalize_step_title(_get_step_title_str(s)) for s in steps_b]
    return titles_a == titles_b


def _plan_lock_path(sync: AuditSynchronizer, artifact_id: str, plan_file: Path | None) -> Path:
    """Stable per-artifact lock path (independent of active/completed directory)."""
    _ = plan_file  # file path не участвует: имя лока стабильно по artifact_id
    lock_dir = Path(sync.plans_dir) / ".locks"
    clean_id = sanitize_task_id(artifact_id) or artifact_id
    return lock_dir / f"{clean_id}.lock"


def _atomic_write_snapshot(target_path: Path, content: str) -> None:
    """Атомарно записывает неизменяемый снимок через temp+rename без overwrite."""
    if target_path.exists():
        raise FileExistsError(f"Snapshot already exists and is immutable: {target_path}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = target_path.with_name(f".{target_path.name}.tmp.{secrets.token_hex(4)}")
    try:
        temp_file.write_text(content, encoding="utf-8")
        if target_path.exists():
            raise FileExistsError(f"Snapshot already exists and is immutable: {target_path}")
        temp_file.replace(target_path)
    except Exception:
        temp_file.unlink(missing_ok=True)
        raise


def _verify_plan_version_history(version_dir: Path, plan_id: str, plan_version: int) -> None:
    """Проверяет непрерывность и целостность исторической цепочки снимков планов."""
    for v in range(1, plan_version + 1):
        v_path = version_dir / f"v{v:04d}.md"
        if not v_path.exists():
            raise ValueError(
                f"VERSION_HISTORY_CORRUPTED: снимок версии {v} отсутствует ({v_path}) "
                f"для плана '{plan_id}'"
            )
        try:
            raw_snap = v_path.read_text(encoding="utf-8")
            meta_dict, snap_body = split_frontmatter(raw_snap)
            if not meta_dict:
                raise ValueError("Отсутствуют метаданные")
            PlanVersionMetadata.model_validate(meta_dict)
            parse_plan_markdown(snap_body)
        except Exception as err:
            raise ValueError(
                f"VERSION_HISTORY_CORRUPTED: снимок версии {v} повреждён "
                f"для плана '{plan_id}': {err}"
            ) from err


def _resolve_restore_progress(
    current_plan: PlanArtifact,
    target_plan: PlanArtifact,
    version: int,
    reset_progress: bool,
    actual_etag: str,
) -> None:
    """Проверяет конфликты прогресса и применяет перенос или сброс отметок выполнения."""
    has_progress = any(s.completed or s.note for s in current_plan.steps)
    steps_match = _steps_structure_matches(target_plan.steps, current_plan.steps)

    if has_progress and not steps_match and not reset_progress:
        preview_curr = "\n".join(
            f"- [{'x' if s.completed else ' '}] {s.index}. {s.title}"
            + (f" (note: {s.note})" if s.note else "")
            for s in current_plan.steps
        )
        preview_target = "\n".join(f"- [ ] {s.index}. {s.title}" for s in target_plan.steps)
        raise ValueError(
            f"PROGRESS_CONFLICT: Структура шагов восстанавливаемой версии v{version} "
            f"отличается от текущей версии v{current_plan.plan_version}, в которой зафиксирован прогресс.\n"
            f"Текущие шаги с прогрессом:\n{preview_curr}\n"
            f"Восстанавливаемые шаги:\n{preview_target}\n"
            f"Актуальный ETag: '{actual_etag}'. "
            f"Повторите вызов с reset_progress=True и expected_state_etag='{actual_etag}'."
        )

    if steps_match and has_progress and not reset_progress:
        for i, s in enumerate(target_plan.steps):
            s.completed = current_plan.steps[i].completed
            s.note = current_plan.steps[i].note
    else:
        for s in target_plan.steps:
            s.completed = False
            s.note = None


class AuditSynchronizer(BaseSynchronizer):
    """Синхронизатор файлов задач аудита, планов, ревью и SQLite FTS5 кэша."""

    def __init__(
        self,
        storage: AuditStorage,
        audit_dir: Path,
        plans_dir: Path | None = None,
        reviews_dir: Path | None = None,
        plan_versions_dir: Path | None = None,
        create_dirs: bool = True,
    ) -> None:
        self.storage = storage
        self.audit_dir = Path(audit_dir)
        self.todo_dir = self.audit_dir / "todo"
        self.done_dir = self.audit_dir / "done"

        # Каталоги планов и ревью
        agents_dir = self.audit_dir.parent
        self.plans_dir = Path(plans_dir) if plans_dir else (agents_dir / "plans")
        self.plans_active_dir = self.plans_dir / "active"
        self.plans_completed_dir = self.plans_dir / "completed"
        self.plan_moves_dir = self.plans_dir / ".moves"

        self.reviews_dir = Path(reviews_dir) if reviews_dir else (agents_dir / "reviews")
        self.plan_versions_dir = (
            Path(plan_versions_dir) if plan_versions_dir else (agents_dir / "plan_versions")
        )

        self._sync_errors: dict[str, str] = {}

        if create_dirs:
            self.audit_dir.mkdir(parents=True, exist_ok=True)
            self.todo_dir.mkdir(parents=True, exist_ok=True)
            self.done_dir.mkdir(parents=True, exist_ok=True)
            self.plans_active_dir.mkdir(parents=True, exist_ok=True)
            self.plans_completed_dir.mkdir(parents=True, exist_ok=True)
            self.plan_moves_dir.mkdir(parents=True, exist_ok=True)
            self.reviews_dir.mkdir(parents=True, exist_ok=True)
            self.plan_versions_dir.mkdir(parents=True, exist_ok=True)

    def get_sync_error(self, task_id: str) -> str | None:
        """Возвращает последнюю ошибку синхронизации для задачи или None."""
        clean_task = sanitize_task_id(task_id)
        return self._sync_errors.get(clean_task)

    def _target_dir(self, status: AuditStatus, category: str) -> Path:
        """Каталог размещения карточки: done/ при COMPLETED, иначе todo/."""
        cat = category or "general"
        base = self.done_dir if status == AuditStatus.COMPLETED else self.todo_dir
        return base / cat

    def scan_card_files(self) -> list[tuple[Path, float, int, str]]:
        """Сканирует все .md карточки в todo/, done/ и корне .agents/audit/."""
        results: list[tuple[Path, float, int, str]] = []

        def _scan_dir(directory: Path, location_label: str, recursive: bool = True) -> None:
            if not directory.exists():
                return
            md_files = directory.rglob("*.md") if recursive else directory.glob("*.md")
            for p in md_files:
                if p.is_file() and not p.name.startswith(
                    (".", "audit-registry", "README", "BACKLOG")
                ):
                    try:
                        st = p.stat()
                        results.append((p, st.st_mtime, st.st_size, location_label))
                    except OSError:
                        pass

        _scan_dir(self.todo_dir, "todo")
        _scan_dir(self.done_dir, "done")

        # Сканируем корень .agents/audit/ для обратной совместимости с legacy файлами
        # (нерекурсивно: подкаталоги todo/ и done/ уже обработаны выше)
        _scan_dir(self.audit_dir, "root", recursive=False)

        return results

    def _index_file(self, md_file: Path, location: str) -> AuditIssue | None:
        """Парсит и индексирует один файл в SQLite."""
        try:
            content = md_file.read_text(encoding="utf-8")
            issue = parse_audit_markdown(content, file_path=md_file)
        except Exception as err:
            logger.warning("Ошибка парсинга карточки аудита %s: %s", md_file, err)
            return None

        stat = md_file.stat()
        self.storage.index_card(
            issue,
            file_path=str(md_file.resolve()),
            mtime=stat.st_mtime,
            size=stat.st_size,
            location=location,
        )

        return issue

    def sync(self) -> int:
        """Выполняет полную инкрементальную синхронизацию диска и базы."""
        disk_files = self.scan_card_files()
        disk_paths: set[str] = set()
        active_card_ids: set[str] = set()

        synced_files = self.storage.get_sync_state()

        indexed_count = 0
        for md_file, _mtime, _size, location in disk_files:
            file_str = str(md_file.resolve())
            disk_paths.add(file_str)

            # Проверяем, нужно ли индексировать
            needs_index = True
            if file_str in synced_files:
                s_mtime, s_size, card_id = synced_files[file_str]
                if not BaseSynchronizer.is_file_dirty(s_mtime, s_size, md_file):
                    needs_index = False
                    active_card_ids.add(card_id)

            if needs_index:
                issue = self._index_file(md_file, location)
                if issue:
                    active_card_ids.add(issue.id)
                    indexed_count += 1

        # Очистка удаленных файлов
        deleted_paths = set(synced_files.keys()) - disk_paths
        for d_path in deleted_paths:
            _, _, old_id = synced_files[d_path]
            # Удаляем из FTS только если карточка не переехала на другой путь
            if old_id not in active_card_ids:
                self.storage.drop_path(d_path, card_id=old_id)
            else:
                self.storage.drop_path(d_path)

        return indexed_count

    def find_card_file(self, issue_id: str) -> Path | None:
        """Находит физический файл карточки по её ID (сначала SQLite-индекс, затем скан диска)."""
        # Быстрый lookup по индексу file_sync; exists() отсекает стейл после ручных перемещений
        indexed_path = self.storage.find_card_path(issue_id)
        if indexed_path is not None:
            candidate = Path(indexed_path)
            if candidate.exists():
                return candidate

        for md_file, _, _, _ in self.scan_card_files():
            if md_file.stem == issue_id or md_file.name.startswith(f"{issue_id}-"):
                return md_file
            with contextlib.suppress(Exception):
                content = md_file.read_text(encoding="utf-8")
                issue = parse_audit_markdown(content, file_path=md_file)
                if issue.id == issue_id:
                    return md_file
        return None

    def save_and_index_card(self, issue: AuditIssue) -> Path:
        """Создает и сразу индексирует новую карточку аудита."""
        target_dir = self._target_dir(issue.status, issue.category)
        target_dir.mkdir(parents=True, exist_ok=True)

        clean_slug = title_to_slug(issue.title)
        file_name = f"{issue.id}-{clean_slug}.md"
        file_path = target_dir / file_name

        content = serialize_audit_markdown(issue)
        _atomic_write_text(file_path, content)

        location = "done" if target_dir.parent == self.done_dir else "todo"
        self._index_file(file_path, location)
        return file_path

    def update_issue_status(
        self,
        issue_id: str,
        new_status: AuditStatus,
        pr_url: str | None = None,
        resolution_note: str | None = None,
    ) -> tuple[AuditIssue, Path]:
        """Обновляет статус задачи, при необходимости перемещая файл между todo/ и done/."""
        # A3: RMW карточки долга под per-card advisory-локом (как у планов —
        # _stable_plan_lock). Сериализует конкурентные update_debt_status
        # (0/60 потерянных resolution-записей до фикса).
        with _advisory_lock(self._card_lock_path(issue_id)):
            return self._update_issue_status_locked(issue_id, new_status, pr_url, resolution_note)

    def _card_lock_path(self, issue_id: str) -> Path:
        """A3: стабильный путь лока карточки долга (по issue_id, вне todo/done)."""
        lock_dir = self.audit_dir / ".locks"
        clean_id = sanitize_task_id(issue_id) or issue_id
        return lock_dir / f"card-{clean_id}.lock"

    def _update_issue_status_locked(
        self,
        issue_id: str,
        new_status: AuditStatus,
        pr_url: str | None = None,
        resolution_note: str | None = None,
    ) -> tuple[AuditIssue, Path]:
        card_file = self.find_card_file(issue_id)
        if not card_file:
            msg = f"Карточка аудита с ID '{issue_id}' не найдена на диске"
            raise FileNotFoundError(msg)

        content = card_file.read_text(encoding="utf-8")
        issue = parse_audit_markdown(content, file_path=card_file)

        if issue.id != issue_id:
            msg = (
                f"Файл '{card_file.name}' разрешён для ID '{issue_id}', "
                f"но содержит карточку с ID '{issue.id}' — статус не изменён"
            )
            raise ValueError(msg)

        issue.status = new_status
        if pr_url:
            issue.pr_url = pr_url
        # A16: updated_at обновляется при любом изменении карточки, а не только
        # при resolution_note — иначе смена статуса не отражается в метаданных.
        issue.updated_at = datetime.now().strftime("%Y-%m-%d")

        # Существующие заметки секции сохраняются перед повторной сериализацией
        prior_resolution_lines = extract_actual_resolution_lines(content)

        # Перемещение между todo/ и done/ при необходимости
        target_dir = self._target_dir(new_status, issue.category)
        new_path = move_card(card_file, target_dir)

        # A1: точечная мутация меты через rebuild_card — тело, кастомные
        # секции и неизвестные ключи frontmatter не затрагиваются (полная
        # ресериализация serialize_audit_markdown теряла их).
        def _mutate(meta: dict[str, Any]) -> dict[str, Any]:
            meta["status"] = issue.status.value
            meta["updated_at"] = issue.updated_at
            if issue.pr_url:
                meta["pr_url"] = issue.pr_url
            return meta

        try:
            updated_content = rebuild_card(content, _mutate)
        except FrontmatterError:
            # Fallback для карточек без frontmatter-блока
            updated_content = serialize_audit_markdown(issue)
        if resolution_note or prior_resolution_lines:
            # Переносим историю заметок и дописываем новую (если она отлична).
            updated_content = (
                append_actual_resolution_to_markdown(updated_content, resolution_note)
                if resolution_note
                else updated_content
            )
            if prior_resolution_lines:
                kept = extract_actual_resolution_lines(updated_content)
                missing = [line for line in prior_resolution_lines if line not in kept]
                if missing:
                    updated_content = set_actual_resolution_lines(
                        updated_content, [*missing, *kept]
                    )
        _atomic_write_text(new_path, updated_content)

        location = "done" if target_dir.parent == self.done_dir else "todo"
        self._index_file(new_path, location)

        # Удаляем старый путь из file_sync если он изменился
        if str(card_file.resolve()) != str(new_path.resolve()):
            self.storage.drop_path(str(card_file.resolve()))

        return issue, new_path

    # =========================================================================
    # Синхронизация и управление файлами планов и ревью
    # =========================================================================

    def _plan_target_dir(self, status: PlanStatus, category: str) -> Path:
        """Каталог размещения плана: completed/ при DONE, иначе active/."""
        clean_cat = normalize_category_slug(category)
        base = self.plans_completed_dir if status == PlanStatus.DONE else self.plans_active_dir
        return base / clean_cat

    def _plan_version_dir(self, plan_id: str) -> Path:
        """Каталог неизменяемых снимков версий для плана."""
        clean_id = re.sub(r"[^\w\-.]", "_", plan_id)
        return self.plan_versions_dir / clean_id

    def _get_latest_snapshot_version(self, plan_id: str) -> int:
        """Возвращает максимальный номер существующего снимка версии (или 0)."""
        vdir = self._plan_version_dir(plan_id)
        if not vdir.exists():
            return 0
        versions: list[int] = []
        for p in vdir.glob("v*.md"):
            m = re.match(r"^v(\d+)\.md$", p.name)
            if m:
                versions.append(int(m.group(1)))
        return max(versions) if versions else 0

    def recover_orphan_snapshots(self, plan_id: str, current_version: int) -> list[Path]:
        """Очищает каталог снимков плана от незавершённых редакций (v > current_version).

        Переносит снимки v(N+1) в .agents/plan_versions/<plan-id>/.orphans/.
        Снимки v1..vN неизменяемы и никогда не удаляются.
        """
        version_dir = self._plan_version_dir(plan_id)
        if not version_dir.exists():
            return []
        orphans_dir = version_dir / ".orphans"
        quarantined: list[Path] = []
        for p in version_dir.glob("v*.md"):
            m = re.match(r"^v(\d+)\.md$", p.name)
            if not m:
                continue
            v = int(m.group(1))
            if v > current_version:
                orphans_dir.mkdir(parents=True, exist_ok=True)
                dest = orphans_dir / p.name
                p.replace(dest)
                logger.warning(
                    "ORPHAN_SNAPSHOT_QUARANTINED: Снимок %s (версия %d > текущей %d плана '%s') "
                    "перемещён в карантин %s (причина: прерванная запись)",
                    p.name,
                    v,
                    current_version,
                    plan_id,
                    dest,
                )
                quarantined.append(dest)
        return quarantined

    def recover_moves_for_task(self, task_id: str | None = None) -> list[Path]:
        """Выполняет recovery незавершённых переносов active/completed по маркерам (A9).

        Recovery маркера при lookup (PlanRepository.get_unique, synchronizer.find_plan_file):
        - Если target совпадает с target_sha256, а source совпадает со старым -> recovery удаляет source и маркер;
        - Если target совпадает с target_sha256, а source уже отсутствует -> recovery удаляет маркер;
        - Если target ещё нет, source цел и совпадает со старым -> отменяем маркер (удаляем маркер);
        - Любое иное состояние -> raise ValueError(MOVE_CONFLICT) и сохраняет файлы;
        """
        if not self.plan_moves_dir.exists():
            return []

        resolved: list[Path] = []
        clean_target_task = sanitize_task_id(task_id) if task_id else None

        for marker_path in sorted(self.plan_moves_dir.glob("*.json")):
            if marker_path.name.startswith("."):
                continue
            try:
                data = json.loads(marker_path.read_text(encoding="utf-8"))
            except Exception as err:
                logger.warning(
                    "MOVE_RECOVERY: Не удалось прочитать маркер %s: %s", marker_path, err
                )
                continue

            m_task = sanitize_task_id(str(data.get("task_id") or ""))
            if clean_target_task and m_task != clean_target_task:
                continue

            source = Path(data.get("source", ""))
            target = Path(data.get("target", ""))
            source_sha = data.get("source_sha256")
            target_sha = data.get("target_sha256")

            target_exists = target.is_file()
            source_exists = source.is_file()

            if target_exists:
                try:
                    cur_target_sha = hashlib.sha256(target.read_bytes()).hexdigest()
                except Exception as err:
                    raise ValueError(
                        f"MOVE_CONFLICT: не удалось прочитать target '{target}': {err}"
                    ) from err

                if cur_target_sha != target_sha:
                    raise ValueError(
                        f"MOVE_CONFLICT: target-файл '{target}' имеет несовпадающий sha256: "
                        f"ожидался '{target_sha}', получен '{cur_target_sha}'"
                    )

                if source_exists:
                    try:
                        cur_source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
                    except Exception as err:
                        raise ValueError(
                            f"MOVE_CONFLICT: не удалось прочитать source '{source}': {err}"
                        ) from err

                    if cur_source_sha != source_sha:
                        raise ValueError(
                            f"MOVE_CONFLICT: source-файл '{source}' имеет несовпадающий sha256: "
                            f"ожидался '{source_sha}', получен '{cur_source_sha}'"
                        )

                    source.unlink(missing_ok=True)
                    marker_path.unlink(missing_ok=True)
                    logger.info(
                        "MOVE_RECOVERY: Завершён перенос для плана '%s' (source удалён)", m_task
                    )
                    resolved.append(target)
                else:
                    marker_path.unlink(missing_ok=True)
                    logger.info(
                        "MOVE_RECOVERY: Завершён перенос для плана '%s' (маркер удалён)", m_task
                    )
                    resolved.append(target)
            else:
                if source_exists:
                    try:
                        cur_source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
                    except Exception as err:
                        raise ValueError(
                            f"MOVE_CONFLICT: не удалось прочитать source '{source}': {err}"
                        ) from err

                    if cur_source_sha != source_sha:
                        raise ValueError(
                            f"MOVE_CONFLICT: target отсутствует, но source-файл '{source}' изменён: "
                            f"ожидался '{source_sha}', получен '{cur_source_sha}'"
                        )

                    marker_path.unlink(missing_ok=True)
                    logger.info(
                        "MOVE_RECOVERY: Отменён незавершённый перенос для плана '%s'", m_task
                    )
                    resolved.append(source)
                else:
                    raise ValueError(
                        f"MOVE_CONFLICT: и source '{source}', и target '{target}' отсутствуют для маркера {marker_path.name}"
                    )

        return resolved

    def _publish_plan_file(
        self,
        plan: PlanArtifact,
        plan_file: Path,
        target_dir: Path,
        content: str,
    ) -> Path:
        """Публикует план, выполняя перенос с записью маркера при смене каталога (A9).

        Порядок перемещения:
        1) запись маркера;
        2) запись/публикация target файла через temp+rename;
        3) удаление source файла;
        4) удаление маркера.
        """
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / plan_file.name

        if target_path.resolve() == plan_file.resolve():
            _atomic_write_text(plan_file, content)
            return plan_file

        if target_path.exists():
            try:
                target_content = target_path.read_text(encoding="utf-8")
                target_plan = parse_plan_markdown(target_content, file_path=target_path)
                if target_plan.id != plan.id:
                    raise ValueError(
                        f"MOVE_CONFLICT: target-файл '{target_path}' уже существует и принадлежит плану '{target_plan.id}' (ожидался '{plan.id}')"
                    )
            except Exception as err:
                raise ValueError(
                    f"MOVE_CONFLICT: target-файл '{target_path}' уже существует: {err}"
                ) from err

        old_hash = hashlib.sha256(plan_file.read_bytes()).hexdigest()
        new_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        self.plan_moves_dir.mkdir(parents=True, exist_ok=True)
        clean_id = re.sub(r"[^\w\-.]", "_", plan.id)
        marker_file = self.plan_moves_dir / f"{clean_id}.json"
        marker_data = {
            "artifact_id": plan.id,
            "task_id": plan.task_id,
            "source": str(plan_file.resolve()),
            "target": str(target_path.resolve()),
            "source_sha256": old_hash,
            "target_sha256": new_hash,
        }
        # 1. Запись маркера
        _atomic_write_text(marker_file, json.dumps(marker_data, ensure_ascii=False, indent=2))

        # 2. Запись target файла через temp+rename
        _atomic_write_text(target_path, content)

        # 3. Удаление source файла
        plan_file.unlink(missing_ok=True)

        # 4. Удаление маркера
        marker_file.unlink(missing_ok=True)

        return target_path

    def _review_target_dir(self, category: str) -> Path:
        """Каталог размещения ревью-логов."""
        clean_cat = normalize_category_slug(category)
        return self.reviews_dir / clean_cat

    def scan_artifact_files(self) -> list[tuple[Path, int, int, str, str]]:
        """Сканирует .md файлы планов и ревью: (path, mtime_ns, size, location, artifact_type)."""
        results: list[tuple[Path, int, int, str, str]] = []

        def _scan(directory: Path, location_label: str, art_type: str) -> None:
            if not directory.exists():
                return
            for p in directory.rglob("*.md"):
                if p.is_file() and not p.name.startswith((".", "README")):
                    try:
                        st = p.stat()
                        results.append((p, st.st_mtime_ns, st.st_size, location_label, art_type))
                    except OSError:
                        pass

        _scan(self.plans_active_dir, "active", "plan")
        _scan(self.plans_completed_dir, "completed", "plan")
        _scan(self.reviews_dir, "reviews", "review")
        return results

    def _index_artifact_file(
        self,
        md_file: Path,
        location: str,
        artifact_type: str,
        parsed_artifact: PlanArtifact | ReviewArtifact | None = None,
        mtime_ns: int | None = None,
        size: int | None = None,
    ) -> tuple[bool, str | None]:
        """Парсит и индексирует один файл артефакта (план или ревью) в SQLite."""
        try:
            file_str = str(md_file.resolve())
            if mtime_ns is None or size is None:
                st = md_file.stat()
                mtime_ns = st.st_mtime_ns
                size = st.st_size

            if artifact_type == "plan":
                plan: PlanArtifact
                if isinstance(parsed_artifact, PlanArtifact):
                    plan = parsed_artifact
                else:
                    content = md_file.read_text(encoding="utf-8")
                    plan = parse_plan_markdown(content, file_path=md_file)
                self.storage.index_plan(
                    plan,
                    file_path=file_str,
                    mtime_ns=mtime_ns,
                    size=size,
                    location=location,
                )
                return True, plan.id
            elif artifact_type == "review":
                review: ReviewArtifact
                if isinstance(parsed_artifact, ReviewArtifact):
                    review = parsed_artifact
                else:
                    content = md_file.read_text(encoding="utf-8")
                    review = parse_review_markdown(content, file_path=md_file)
                self.storage.index_review(
                    review,
                    file_path=file_str,
                    mtime_ns=mtime_ns,
                    size=size,
                    location=location,
                )
                return True, review.id
        except Exception as err:
            logger.warning("Ошибка парсинга артефакта %s (%s): %s", md_file, artifact_type, err)
            return False, None
        return False, None

    def _extract_task_id_from_path(self, path: Path) -> str | None:
        """Извлекает task_id из файла или имени пути для логирования ошибки синхронизации."""
        with contextlib.suppress(Exception):
            content = path.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"(?m)^task_id:\s*['\"]?([A-Za-z0-9_.-]+)['\"]?", content)
            if m:
                return m.group(1).strip()
        file_str = str(path.resolve())
        with contextlib.suppress(Exception):
            stored_task_id = self.storage.get_task_id_for_file_path(file_str)
            if stored_task_id:
                return stored_task_id
        parts = path.stem.split("-")
        if len(parts) >= 3 and parts[0] == "plan":
            return parts[2]
        return None

    def _sync_plan_file(
        self,
        md_file: Path,
        location: str,
        mtime_ns: int,
        size: int,
        synced: dict[str, Any],
    ) -> str | None:
        """Синхронизирует один файл плана с проверкой проекции определения (A10)."""
        file_str = str(md_file.resolve())
        try:
            raw_bytes = md_file.read_bytes()
            raw_content = raw_bytes.decode("utf-8")
            plan = parse_plan_markdown(raw_content, file_path=md_file)
        except Exception as err:
            logger.warning("Ошибка чтения/парсинга плана %s: %s", md_file, err)
            task_guess = self._extract_task_id_from_path(md_file)
            if task_guess:
                self._sync_errors[task_guess] = f"Ошибка парсинга плана: {err}"
            return None

        clean_task = sanitize_task_id(plan.task_id)
        task_lock_path = Path(self.plans_dir) / ".locks" / f"task-{clean_task}.lock"
        with _advisory_lock(task_lock_path):
            self.recover_orphan_snapshots(plan.id, plan.plan_version)

            cur_projection = compute_plan_definition_projection(raw_content)
            cur_sha256 = compute_plan_definition_sha256(cur_projection)

            is_def_changed = (
                plan.definition_sha256 is not None and cur_sha256 != plan.definition_sha256
            )

            if is_def_changed:
                try:
                    version_dir = self._plan_version_dir(plan.id)
                    latest_snapshot = self._get_latest_snapshot_version(plan.id)
                    v1_path = version_dir / "v0001.md"
                    if latest_snapshot == 0 or not v1_path.exists():
                        meta_v1 = PlanVersionMetadata(
                            version=1,
                            parent_version=None,
                            created_at_utc=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            reason="initial creation",
                            source="manual",
                            restored_from=None,
                            definition_sha256=plan.definition_sha256 or cur_sha256,
                        )
                        fm_v1 = dump_frontmatter(meta_v1.model_dump()).strip()
                        snap_v1 = f"---\n{fm_v1}\n---\n{raw_content}"
                        _atomic_write_snapshot(v1_path, snap_v1)
                        latest_snapshot = 1

                    new_version = max(plan.plan_version, latest_snapshot) + 1

                    def _update_meta(m: dict[str, Any]) -> dict[str, Any]:
                        m["plan_version"] = new_version
                        m["definition_sha256"] = cur_sha256
                        return m

                    updated_content = rebuild_card(raw_content, _update_meta)

                    meta_vN = PlanVersionMetadata(
                        version=new_version,
                        parent_version=new_version - 1,
                        created_at_utc=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        reason="manual edit",
                        source="manual",
                        restored_from=None,
                        definition_sha256=cur_sha256,
                    )
                    fm_vN = dump_frontmatter(meta_vN.model_dump()).strip()
                    snap_vN = f"---\n{fm_vN}\n---\n{updated_content}"
                    vN_path = version_dir / f"v{new_version:04d}.md"

                    _atomic_write_snapshot(vN_path, snap_vN)
                    _atomic_write_text(md_file, updated_content)

                    self._sync_errors.pop(clean_task, None)
                    self._index_artifact_file(md_file, location, "plan")
                    return plan.id
                except Exception as err:
                    logger.warning(
                        "Ошибка версионирования ручной правки плана %s: %s", md_file, err
                    )
                    self._sync_errors[clean_task] = f"Ошибка версионирования ручной правки: {err}"
                    return None

            # Определение не менялось
            needs_index = True
            if file_str in synced:
                s_mtime_ns, s_size, _art_id, _ = synced[file_str]
                if s_mtime_ns == mtime_ns and s_size == size:
                    needs_index = False

            if needs_index:
                self._sync_errors.pop(clean_task, None)
                self._index_artifact_file(
                    md_file,
                    location,
                    "plan",
                    parsed_artifact=plan,
                    mtime_ns=mtime_ns,
                    size=size,
                )

            return plan.id

    def sync_artifacts(self) -> int:
        """Инкрементальная синхронизация планов и ревью с точным mtime_ns."""
        disk_files = self.scan_artifact_files()
        disk_paths: set[str] = set()
        active_ids: set[str] = set()

        synced = self.storage.get_artifacts_sync_state()
        indexed_count = 0

        for md_file, mtime_ns, size, location, art_type in disk_files:
            file_str = str(md_file.resolve())
            disk_paths.add(file_str)

            if art_type == "plan":
                plan_id = self._sync_plan_file(md_file, location, mtime_ns, size, synced)
                if plan_id:
                    indexed_count += 1
                    active_ids.add(plan_id)
            else:
                needs_index = True
                if file_str in synced:
                    s_mtime_ns, s_size, art_id, _s_type = synced[file_str]
                    if s_mtime_ns == mtime_ns and s_size == size:
                        needs_index = False
                        active_ids.add(art_id)

                if needs_index:
                    ok, art_id_new = self._index_artifact_file(md_file, location, art_type)
                    if ok and art_id_new:
                        indexed_count += 1
                        active_ids.add(art_id_new)

        # Очистка удалённых файлов артефактов
        deleted_paths = set(synced.keys()) - disk_paths
        for d_path in deleted_paths:
            _, _, old_id, _ = synced[d_path]
            if old_id not in active_ids:
                self.storage.drop_artifact_path(d_path, artifact_id=old_id)
            else:
                self.storage.drop_artifact_path(d_path)

        return indexed_count

    def sync_all(self) -> tuple[int, int]:
        """Синхронизирует аудит-долги и жизненные артефакты (планы/ревью)."""
        c1 = self.sync()
        c2 = self.sync_artifacts()
        return c1, c2

    def _plan_index_matches(self, clean_task: str, clean_cat: str | None) -> list[Path]:
        """A13: все индекс-хиты task_id (без LIMIT 1), отфильтрованные категорией."""
        index_matches: list[Path] = []
        for _artifact_id, file_path in self.storage.find_plans_by_task_id(clean_task):
            candidate = Path(file_path)
            if candidate.exists() and candidate not in index_matches:
                index_matches.append(candidate)
        if clean_cat:
            index_matches = [
                p
                for p in index_matches
                if p.parent.name == clean_cat or p.parent.parent.name == clean_cat
            ]
        return index_matches

    def _plan_disk_matches(self, clean_task: str, clean_cat: str | None) -> list[Path]:
        """A13: дисковые совпадения по шаблону имени; при пустом результате
        и отсутствии категории — контент-поиск (frontmatter.task_id)."""
        candidates: list[Path] = []
        for directory in (self.plans_active_dir, self.plans_completed_dir):
            if not directory.exists():
                continue
            search_dirs = [directory / clean_cat] if clean_cat else [directory]
            for search_dir in search_dirs:
                if not search_dir.exists():
                    continue
                candidates.extend(
                    p
                    for pattern in (f"*-{clean_task}.md", f"*-{clean_task}-*.md")
                    for p in search_dir.rglob(pattern)
                    if p.is_file() and p not in candidates
                )
        if not candidates and not clean_cat:
            self._append_content_matches(candidates, clean_task)

        # aud-backend-0f00: фильтруем кандидаты по точному frontmatter.task_id,
        # чтобы префиксный glob не захватывал задачи-суффиксы (например, task-1 vs task-1-v2)
        matches: list[Path] = []
        for p in candidates:
            try:
                content = p.read_text(encoding="utf-8")
                plan = parse_plan_markdown(content, file_path=p)
                if plan.task_id == clean_task:
                    matches.append(p)
            except Exception as err:
                logger.debug("Пропуск файла %s при проверке task_id: %s", p, err)
        return matches

    def _append_content_matches(self, matches: list[Path], clean_task: str) -> None:
        """Контент-поиск планов по frontmatter.task_id (дополняет matches)."""
        for directory in (self.plans_active_dir, self.plans_completed_dir):
            if not directory.exists():
                continue
            for p in directory.rglob("*.md"):
                if not p.is_file() or p in matches:
                    continue
                try:
                    content = p.read_text(encoding="utf-8")
                    plan = parse_plan_markdown(content, file_path=p)
                    if plan.task_id == clean_task:
                        matches.append(p)
                except Exception as err:
                    logger.debug("Пропуск файла %s: %s", p, err)

    def find_plan_file(self, task_id: str, category: str | None = None) -> Path | None:
        """Находит файл плана по task_id (опционально с фильтром категории).

        AUDIT-ARCH-2026-10 (ревью разворота, aud-backend-3712): тонкий
        делегат к ``PlanRepository.get_unique`` — единственному владельцу
        семантики неоднозначности (A13). Прежняя реализация была дословной
        копией и оставляла инвариант у двух владельцев.
        """
        from mcp_audit.repository import PlanRepository

        return PlanRepository(self).get_unique(task_id, category)

    def _find_plan_files_by_id(self, artifact_id: str) -> list[Path]:
        """Находит все файлы планов с данным artifact_id на диске (active/ и completed/).

        Сопоставление — ТОЛЬКО по точному совпадению ``id`` в YAML frontmatter.
        Имя файла (plan-<cat>-<task>-<slug>.md) содержит task_id и slug, поэтому
        подстрочный матч имени порождает коллизии соседних ID (task-1 матчит
        файл task-10 — потеря данных при save_plan, см. R1 отчёта приёмки
        2026-10-18). Frontmatter читается у каждого файла: планов единицы-десятки,
        точность дороже скорости.
        """
        found: list[Path] = []
        for directory in (self.plans_active_dir, self.plans_completed_dir):
            if not directory.exists():
                continue
            for p in directory.rglob("*.md"):
                if not p.is_file() or p.name.startswith("."):
                    continue
                with contextlib.suppress(Exception):
                    content = p.read_text(encoding="utf-8")
                    m = FRONTMATTER_SPLIT_REGEX.match(content)
                    if m:
                        meta = yaml.safe_load(m.group(1)) or {}
                        if str(meta.get("id") or "") == artifact_id:
                            found.append(p)
        return found

    def _plan_file_id_matches(self, path: Path, artifact_id: str) -> bool:
        """Точная проверка frontmatter id файла плана (R1, ревизия 4).

        Используется для валидации кандидатов из файлового индекса БД: индекс
        может устареть (правка frontmatter после последней синхронизации),
        поэтому canonical-путь принимается только при совпадении frontmatter
        id. Ошибки чтения/парсинга трактуются как «не совпадает» — безопаснее
        создать новый файл, чем перезаписать чужой план.
        """
        try:
            content = path.read_text(encoding="utf-8")
            m = FRONTMATTER_SPLIT_REGEX.match(content)
            if not m:
                return False
            meta = yaml.safe_load(m.group(1)) or {}
            return str(meta.get("id") or "") == artifact_id
        except (OSError, UnicodeError, yaml.YAMLError):
            logger.debug("Не удалось проверить frontmatter id в %s", path)
            return False

    def _resolve_plan_file_path(self, plan: PlanArtifact, target_dir: Path, filename: str) -> Path:
        """Разрешает путь файла плана по стабильному artifact_id (не по title-slug).

        Порядок:
        1. Существующие файлы этого artifact_id на диске (или в файловом индексе БД)
           — первый найденный используется как canonical путь, чтобы смена title
           не порождала второй файл.
        2. Иначе — новый файл со slug из текущего title; если файл с таким ИМЕНЕМ
           уже существует, но принадлежит другому id (подмена frontmatter), имя
           получает суффикс — чужой файл не перезаписывается (R1 MINOR-2).
        Дубликаты (несколько файлов одного ID) выявляются warning-логом и НЕ
        удаляются автоматически: конфликтующие данные разбираются вручную.
        """
        existing = self._find_plan_files_by_id(plan.id)
        if not existing:
            indexed = self.storage.find_artifact_path(plan.id)
            # Ревизия 4 (R1): стейл-индекс БД не доверяется вслепую —
            # файл должен ПОДТВЕРДИТЬ artifact_id своим frontmatter id.
            # Файл с подменённым/изменённым id (при прежнем имени файла)
            # не должен перезаписываться по старому ID: индекс мог
            # устареть до правки. Иначе — потеря данных чужого плана.
            if indexed is not None and Path(indexed).exists():
                if self._plan_file_id_matches(Path(indexed), plan.id):
                    existing = [Path(indexed)]
                else:
                    # MINOR-1: indexed-путь отклонён (frontmatter id не совпал
                    # или нечитаем) — диагностика warning-уровня, не debug:
                    # событие редкое и означает расхождение индекса и диска.
                    logger.warning(
                        "STALE_INDEX: indexed-путь %s отклонён: frontmatter id "
                        "не совпадает с artifact_id '%s' (план %s) — создан "
                        "новый файл",
                        indexed,
                        plan.id,
                        plan.task_id,
                    )

        if not existing:
            candidate = target_dir / filename
            if candidate.exists() and not self._plan_file_id_matches(candidate, plan.id):
                # R1 edge (MINOR-2): имя файла занято чужим файлом (подменённый
                # id при прежнем имени) — НЕ перезаписываем: суффикс имени.
                logger.warning(
                    "PLAN_PATH_COLLISION: файл %s существует, но его frontmatter "
                    "id не совпадает с artifact_id '%s' — запись в отдельный "
                    "файл (суффикс имени)",
                    candidate,
                    plan.id,
                )
                stem = candidate.stem
                suffix = candidate.suffix
                candidate = candidate.with_name(f"{stem}-{plan.id[-6:]}{suffix}")
            return candidate

        if len(existing) > 1:
            logger.warning(
                "DUPLICATE_PLAN_FILES: artifact_id '%s' найден в %d файлах: %s. "
                "Использую первый; дубликаты не удаляются автоматически — "
                "разберите конфликт вручную.",
                plan.id,
                len(existing),
                ", ".join(str(p) for p in existing),
            )

        canonical = existing[0]
        # Файл остаётся в своей категории/статусной директории при повторной записи:
        # перемещение между active/ и completed/ выполняет только update_plan/step_plan.
        return canonical

    def save_plan(self, plan: PlanArtifact, exist_ok: bool = False) -> Path:
        """Создает и индексирует новый план разработки (обновляет существующий по artifact_id).

        A2: молчаливая перезапись существующего плана по task_id запрещена —
        при обнаружении плана с тем же task_id (любой категории) рейзится
        ValueError (PLAN_EXISTS); для правок существует update_plan_details.
        ``exist_ok=True`` оставляет прежний upsert-контракт для внутренних
        вызовов (пересохранение того же артефакта).
        """
        target_dir = self._plan_target_dir(plan.status, plan.category)
        target_dir.mkdir(parents=True, exist_ok=True)

        clean_slug = title_to_slug(plan.title, fallback="")
        clean_cat = normalize_category_slug(plan.category)
        clean_task = sanitize_task_id(plan.task_id)

        # A2: блокировка по НОРМАЛИЗОВАННОМУ task_id (не artifact_id — планы
        # разных категорий имеют разные artifact_id и разные локи, а задача
        # task_id глобально уникальна). Проверка существования ОБЯЗАНА быть
        # внутри лока: иначе два параллельных record_plan оба увидят отсутствие
        # плана и оба выполнят запись (TOCTOU). A2-TOCTOU: task_id-лок
        # удерживается на ВСЮ запись (artifact-лок вложен внутрь) — между
        # первой проверкой и записью окно конкуренции не остаётся: два
        # конкурентных record_plan одного task_id в разных категориях
        # (разные artifact-локи) не могут оба пройти. Reject безусловен:
        # record_plan детерминированно порождает тот же artifact_id из
        # (category, task_id), поэтому совпадение id означает тот же task_id —
        # «перезапись того же артефакта» через record_plan и есть дефект A2
        # (затирает steps/details).
        task_lock_path = Path(self.plans_dir) / ".locks" / f"task-{clean_task or plan.task_id}.lock"
        with _advisory_lock(task_lock_path):
            if not exist_ok and clean_task:
                self._raise_if_plan_exists(clean_task)

            if clean_slug:
                filename = f"plan-{clean_cat}-{clean_task}-{clean_slug}.md"
            else:
                filename = f"plan-{clean_cat}-{clean_task}.md"

            # Стабильный artifact_id: файл один и тот же независимо от смены title.
            # Блокировка по ID сериализует конкурентные записи (POSIX/fcntl).
            lock_path = _plan_lock_path(self, plan.id, None)
            with _advisory_lock(lock_path):
                file_path = self._resolve_plan_file_path(plan, target_dir, filename)
                if not exist_ok and clean_task:
                    # Повторная проверка под artifact-локом: план мог быть создан
                    # параллельной записью между task-локом и этим моментом.
                    self._raise_if_plan_exists(clean_task)
                plan.plan_version = 1
                projection = compute_plan_definition_projection(plan)
                plan.definition_sha256 = compute_plan_definition_sha256(projection)
                content = serialize_plan_markdown(plan)

                # Запись неизменяемого снимка v0001.md
                version_dir = self._plan_version_dir(plan.id)
                v1_path = version_dir / "v0001.md"
                if not v1_path.exists():
                    meta_v1 = PlanVersionMetadata(
                        version=1,
                        parent_version=None,
                        created_at_utc=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        reason="initial creation",
                        source="mcp",
                        restored_from=None,
                        definition_sha256=plan.definition_sha256,
                    )
                    fm_v1 = dump_frontmatter(meta_v1.model_dump()).strip()
                    snap_content = f"---\n{fm_v1}\n---\n{content}"
                    _atomic_write_snapshot(v1_path, snap_content)

                _atomic_write_text(file_path, content)

                location = "completed" if plan.status == PlanStatus.DONE else "active"
                self._index_artifact_file(file_path, location, "plan")
        return file_path

    def _raise_if_plan_exists(self, clean_task: str) -> None:
        """A2: reject записи, если план с task_id уже существует (любая категория).

        AMBIGUOUS_PLAN (несколько планов task_id) — тоже существование:
        безусловный reject, не маскируется ошибкой неоднозначности.
        """
        try:
            existing = self.find_plan_file(clean_task)
        except ValueError as ambiguous:
            raise ValueError(
                f"PLAN_EXISTS: план для task_id '{clean_task}' уже существует "
                f"(неоднозначно: {ambiguous}) — используйте update_plan/step_plan "
                f"для правок, record_plan только создаёт новые планы"
            ) from ambiguous
        if existing is None:
            return
        raise ValueError(
            f"PLAN_EXISTS: план для task_id '{clean_task}' уже существует "
            f"({existing}) — используйте update_plan/step_plan для правок, "
            f"record_plan только создаёт новые планы"
        )

    @staticmethod
    def _apply_plan_updates(
        plan: PlanArtifact,
        title: str | None,
        status: PlanStatus | None,
        pr_url: str | None,
        target_files: list[str] | None,
        add_steps: list[str] | None,
        details: str | None = None,
        replace_steps: list[str] | None = None,
        reset_progress: bool = False,
    ) -> None:
        """Применяет изменения полей к объекту PlanArtifact."""
        if title:
            plan.title = title
        if pr_url:
            plan.pr_url = pr_url
        if target_files is not None:
            plan.target_files = target_files
        if status is not None:
            plan.status = status
        if details is not None:
            plan.content = details
        if replace_steps is not None:
            new_steps: list[PlanStep] = []
            for i, step_text in enumerate(replace_steps, 1):
                clean_title = _normalize_step_title(step_text)
                completed = False
                note = None
                if (
                    not reset_progress
                    and i <= len(plan.steps)
                    and _steps_structure_matches([step_text], [plan.steps[i - 1]])
                ):
                    completed = plan.steps[i - 1].completed
                    note = plan.steps[i - 1].note
                new_steps.append(
                    PlanStep(index=i, title=clean_title, completed=completed, note=note)
                )
            plan.steps = new_steps
        elif add_steps:
            start_idx = len(plan.steps) + 1
            for i, step_text in enumerate(add_steps):
                clean_text = step_text.strip()
                if clean_text.startswith(("- [ ]", "- [x]", "* [ ]", "* [x]")):
                    clean_text = clean_text[5:].strip()
                plan.steps.append(PlanStep(index=start_idx + i, title=clean_text, completed=False))
        plan.updated_at = datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def _render_updated_plan(
        plan: PlanArtifact,
        raw_content: str,
        add_steps: list[str] | None,
        details: str | None = None,
        replace_steps: list[str] | None = None,
    ) -> str:
        """Сериализует план, сохраняя исходные Markdown-секции."""
        content_to_use = raw_content
        if details is not None:
            content_to_use = replace_goal_section_in_markdown(content_to_use, details)
        if plan.title:
            content_to_use = re.sub(
                r"(?m)^#[ ]+(?:План|Plan):.*$",
                f"# План: {plan.task_id} {plan.title}",
                content_to_use,
            )
        status_badges = {
            PlanStatus.DISCUSSED: "⚪ Обсуждено (`discussed`)",
            PlanStatus.APPROVED: "🔵 Утверждено (`approved`)",
            PlanStatus.IN_PROGRESS: "🟡 В процессе реализации (`in_progress`)",
            PlanStatus.DONE: "🟢 Сделано (`done`)",
            PlanStatus.CANCELLED: "🔴 Отменено (`cancelled`)",
        }
        badge = status_badges.get(plan.status, f"`{plan.status.value}`")
        content_to_use = re.sub(
            r"(?m)^\s*>\s*(?:\*\*)?(?:Статус|Status)(?:\*\*)?:.*$",
            f"> **Статус:** {badge}",
            content_to_use,
        )
        if replace_steps is not None:
            content_to_use = replace_steps_in_markdown(content_to_use, replace_steps)
        elif add_steps:
            content_to_use = append_steps_to_plan_markdown(content_to_use, add_steps)

        meta: dict[str, Any] = {
            "id": plan.id,
            "schema_version": plan.schema_version,
            "task_id": plan.task_id,
            "title": plan.title,
            "category": plan.category,
            "status": plan.status.value,
            "plan_version": plan.plan_version,
        }
        if plan.definition_sha256:
            meta["definition_sha256"] = plan.definition_sha256
        if plan.pr_url:
            meta["pr_url"] = plan.pr_url
        if plan.target_files:
            meta["target_files"] = plan.target_files
        if plan.related_debts:
            meta["related_debts"] = plan.related_debts
        if plan.created_at:
            meta["created_at"] = plan.created_at
        meta["updated_at"] = plan.updated_at
        # Ф3-хвост (rebuild_card): пересборка через core-кодек; ручная
        # сборка `---\n{fm}\n---\n{parts[2]}` поглощена примитивом.
        try:
            return rebuild_card(content_to_use, lambda m: meta)
        except FrontmatterError:
            return serialize_plan_markdown(plan)

    def _stable_plan_lock(self, plan_file: Path) -> Path:
        """Lock path, stable by the plan's artifact_id (not by file name/directory)."""
        artifact_id = plan_file.stem
        with contextlib.suppress(Exception):
            content = plan_file.read_text(encoding="utf-8")
            m = FRONTMATTER_SPLIT_REGEX.match(content)
            if m:
                meta = yaml.safe_load(m.group(1)) or {}
                artifact_id = str(meta.get("id") or plan_file.stem)
        return _plan_lock_path(self, artifact_id, plan_file)

    def update_plan_details(
        self,
        task_id: str,
        title: str | None = None,
        status: PlanStatus | None = None,
        pr_url: str | None = None,
        target_files: list[str] | None = None,
        add_steps: list[str] | None = None,
        category: str | None = None,
        reason: str | None = None,
        details: str | None = None,
        replace_details: str | None = None,
        replace_steps: list[str] | None = None,
        expected_version: int | None = None,
        reopen: bool = False,
        reset_progress: bool = False,
    ) -> tuple[PlanArtifact, Path]:
        """Обновляет метаданные плана и перемещает файл при смене статуса.

        A13: category опционально уточняет выбор плана при нескольких
        планах одного task_id.
        """
        eff_details = replace_details if replace_details is not None else details

        clean_task = sanitize_task_id(task_id)
        task_lock_path = Path(self.plans_dir) / ".locks" / f"task-{clean_task}.lock"
        with _advisory_lock(task_lock_path):
            plan_file = self.find_plan_file(clean_task, category=category)
            if not plan_file:
                raise FileNotFoundError(f"Файл плана для задачи '{task_id}' не найден на диске")

            lock_path = self._stable_plan_lock(plan_file)
            with _advisory_lock(lock_path):
                raw_bytes = plan_file.read_bytes()
                content = raw_bytes.decode("utf-8")
                plan = parse_plan_markdown(content, file_path=plan_file)

                self.recover_orphan_snapshots(plan.id, plan.plan_version)

            if expected_version is not None and expected_version != plan.plan_version:
                raise ValueError(
                    f"VERSION_CONFLICT: ожидалась версия {expected_version}, "
                    f"актуальная версия плана {plan.plan_version}"
                )

            is_def_param = any(
                x is not None
                for x in (title, add_steps, target_files, replace_details, details, replace_steps)
            )
            if plan.status == PlanStatus.DONE and is_def_param and not reopen:
                raise ValueError(
                    f"PLAN_REOPEN_REQUIRED: план '{plan.task_id}' находится в статусе DONE. "
                    "Для правки определения требуется reopen=True."
                )

            if replace_steps is not None:
                has_progress = any(s.completed or s.note for s in plan.steps)
                if (
                    has_progress
                    and not _steps_structure_matches(replace_steps, plan.steps)
                    and not reset_progress
                ):
                    actual_etag = hashlib.sha256(raw_bytes).hexdigest()
                    preview_curr = "\n".join(
                        f"- [{'x' if s.completed else ' '}] {s.index}. {s.title}"
                        + (f" (note: {s.note})" if s.note else "")
                        for s in plan.steps
                    )
                    preview_target = "\n".join(
                        f"- [ ] {i}. {s}" for i, s in enumerate(replace_steps, 1)
                    )
                    raise ValueError(
                        f"PROGRESS_CONFLICT: Структура шагов в replace_steps отличается от текущего плана с зафиксированным прогрессом.\n"
                        f"Текущие шаги:\n{preview_curr}\n"
                        f"Новые шаги:\n{preview_target}\n"
                        f"Актуальный ETag: '{actual_etag}'. "
                        f"Повторите вызов с reset_progress=True и актуальным expected_state_etag."
                    )

            old_projection = compute_plan_definition_projection(content)
            old_sha256 = compute_plan_definition_sha256(old_projection)

            self._apply_plan_updates(
                plan,
                title,
                status,
                pr_url,
                target_files,
                add_steps,
                eff_details,
                replace_steps=replace_steps,
                reset_progress=reset_progress,
            )

            candidate_content = self._render_updated_plan(
                plan, content, add_steps, eff_details, replace_steps=replace_steps
            )
            candidate_projection = compute_plan_definition_projection(candidate_content)
            candidate_sha256 = compute_plan_definition_sha256(candidate_projection)

            is_definition_changed = candidate_sha256 != old_sha256

            if is_definition_changed:
                if status is None and plan.status in (
                    PlanStatus.APPROVED,
                    PlanStatus.IN_PROGRESS,
                    PlanStatus.DONE,
                ):
                    plan.status = PlanStatus.DISCUSSED
                    candidate_content = self._render_updated_plan(
                        plan, content, add_steps, eff_details, replace_steps=replace_steps
                    )

                version_dir = self._plan_version_dir(plan.id)
                latest_snapshot = self._get_latest_snapshot_version(plan.id)
                v1_path = version_dir / "v0001.md"
                if latest_snapshot == 0 or not v1_path.exists():
                    # Legacy-план или отсутствие базового снимка: сохраняем старое состояние как v1
                    meta_v1 = PlanVersionMetadata(
                        version=1,
                        parent_version=None,
                        created_at_utc=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        reason="initial creation",
                        source="manual",
                        restored_from=None,
                        definition_sha256=old_sha256,
                    )
                    fm_v1 = dump_frontmatter(meta_v1.model_dump()).strip()
                    snap_v1 = f"---\n{fm_v1}\n---\n{content}"
                    _atomic_write_snapshot(v1_path, snap_v1)
                    latest_snapshot = 1

                new_version = max(plan.plan_version, latest_snapshot) + 1
                plan.plan_version = new_version
                plan.definition_sha256 = candidate_sha256

                final_content = self._render_updated_plan(
                    plan, content, add_steps, eff_details, replace_steps=replace_steps
                )

                meta_vN = PlanVersionMetadata(
                    version=new_version,
                    parent_version=new_version - 1,
                    created_at_utc=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    reason=reason or "updated via MCP",
                    source="mcp",
                    restored_from=None,
                    definition_sha256=candidate_sha256,
                )
                vN_path = version_dir / f"v{new_version:04d}.md"
                fm_vN = dump_frontmatter(meta_vN.model_dump()).strip()
                snap_vN = f"---\n{fm_vN}\n---\n{final_content}"
                _atomic_write_snapshot(vN_path, snap_vN)
            else:
                if plan.definition_sha256 is None:
                    plan.definition_sha256 = old_sha256
                final_content = candidate_content

            # Перемещение при смене статуса на/с DONE
            target_dir = self._plan_target_dir(plan.status, plan.category)
            new_path = self._publish_plan_file(plan, plan_file, target_dir, final_content)

            location = "completed" if plan.status == PlanStatus.DONE else "active"
            self._index_artifact_file(new_path, location, "plan")

            if str(plan_file.resolve()) != str(new_path.resolve()):
                self.storage.drop_artifact_path(str(plan_file.resolve()))

            return plan, new_path

    def restore_plan_version(
        self,
        task_id: str,
        version: int,
        expected_version: int,
        expected_state_etag: str,
        reason: str,
        reset_progress: bool = False,
        reopen: bool = False,
        category: str | None = None,
    ) -> dict[str, Any]:
        """Безопасный откат редакции плана: восстановление как новая версия N+1.

        Объектом снимка является карточка плана (.agents/plans/...).
        """
        if not reason or not reason.strip():
            raise ValueError("reason must not be empty")

        clean_task = sanitize_task_id(task_id)
        clean_cat = normalize_category_slug(category) if category else None

        task_lock_path = Path(self.plans_dir) / ".locks" / f"task-{clean_task}.lock"
        with _advisory_lock(task_lock_path):
            from mcp_audit.repository import PlanRepository

            plan_file = PlanRepository(self).get_unique(clean_task, category=clean_cat)
            if plan_file is None:
                raise FileNotFoundError(f"Файл плана для задачи '{clean_task}' не найден на диске")

            raw_bytes = plan_file.read_bytes()
            actual_etag = hashlib.sha256(raw_bytes).hexdigest()
            if expected_state_etag != actual_etag:
                raise ValueError(
                    f"STATE_ETAG_CONFLICT: ожидался ETag '{expected_state_etag}', "
                    f"актуальный ETag '{actual_etag}'"
                )

            content = raw_bytes.decode("utf-8")
            plan = parse_plan_markdown(content, file_path=plan_file)

            self.recover_orphan_snapshots(plan.id, plan.plan_version)

            if expected_version != plan.plan_version:
                raise ValueError(
                    f"VERSION_CONFLICT: ожидалась версия {expected_version}, "
                    f"актуальная версия плана {plan.plan_version}"
                )

            if plan.status == PlanStatus.DONE and not reopen:
                raise ValueError(
                    f"PLAN_REOPEN_REQUIRED: план '{plan.task_id}' находится в статусе DONE. "
                    "Для восстановления требуется reopen=True."
                )

            version_dir = self._plan_version_dir(plan.id)
            _verify_plan_version_history(version_dir, plan.id, plan.plan_version)

            target_snap_path = version_dir / f"v{version:04d}.md"
            if not target_snap_path.exists() or version <= 0:
                raise ValueError(
                    f"PLAN_VERSION_NOT_FOUND: целевая версия {version} для плана '{plan.id}' не найдена"
                )

            raw_target_snap = target_snap_path.read_text(encoding="utf-8")
            target_meta_dict, target_snap_body = split_frontmatter(raw_target_snap)
            target_meta = PlanVersionMetadata.model_validate(target_meta_dict)
            target_plan = parse_plan_markdown(target_snap_body)

            _resolve_restore_progress(plan, target_plan, version, reset_progress, actual_etag)

            new_version = plan.plan_version + 1
            target_plan.plan_version = new_version
            target_plan.status = PlanStatus.DISCUSSED
            target_plan.updated_at = datetime.now().strftime("%Y-%m-%d")
            target_plan.definition_sha256 = target_meta.definition_sha256

            # Формирование контента восстанавливаемого плана
            def _update_restored_meta(m: dict[str, Any]) -> dict[str, Any]:
                m["plan_version"] = new_version
                m["status"] = PlanStatus.DISCUSSED.value
                m["definition_sha256"] = target_meta.definition_sha256
                m["updated_at"] = datetime.now().strftime("%Y-%m-%d")
                return m

            try:
                restored_content = rebuild_card(target_snap_body, _update_restored_meta)
            except Exception:
                restored_content = serialize_plan_markdown(target_plan)

            # Статус-бейдж
            restored_content = re.sub(
                r"(?m)^\s*>\s*(?:\*\*)?(?:Статус|Status)(?:\*\*)?:.*$",
                "> **Статус:** ⚪ Обсуждено (`discussed`)",
                restored_content,
            )

            # Форматирование шагов
            step_strings = []
            for s in target_plan.steps:
                box = "x" if s.completed else " "
                line = f"- [{box}] {s.index}. {s.title}"
                if s.note:
                    line += f"\n  > 📝 {s.note.strip()}"
                step_strings.append(line)
            restored_content = replace_steps_in_markdown(restored_content, step_strings)

            # Запись неизменяемого нового снимка v{new_version:04d}.md
            meta_new = PlanVersionMetadata(
                version=new_version,
                parent_version=plan.plan_version,
                created_at_utc=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                reason=reason.strip(),
                source="restore",
                restored_from=version,
                definition_sha256=target_meta.definition_sha256,
            )
            fm_new = dump_frontmatter(meta_new.model_dump()).strip()
            snap_new = f"---\n{fm_new}\n---\n{restored_content}"
            v_new_path = version_dir / f"v{new_version:04d}.md"
            _atomic_write_snapshot(v_new_path, snap_new)

            # Перемещение и обновление текущего файла (active)
            target_dir = self._plan_target_dir(PlanStatus.DISCUSSED, plan.category)
            new_path = self._publish_plan_file(plan, plan_file, target_dir, restored_content)

            location = "active"
            self._index_artifact_file(new_path, location, "plan")
            if str(plan_file.resolve()) != str(new_path.resolve()):
                self.storage.drop_artifact_path(str(plan_file.resolve()))

            return {
                "status": "restored",
                "task_id": plan.task_id,
                "plan_id": plan.id,
                "new_version": new_version,
                "restored_from": version,
                "plan_status": "discussed",
                "progress_reset": reset_progress,
                "object_type": "plan_card",
                "message": "Объектом снимка является карточка плана (.agents/plans/...)",
                "file_path": str(new_path),
            }

    def advance_plan_step(
        self,
        task_id: str,
        step: int,
        status: str = "done",
        note: str | None = None,
        allow_out_of_order: bool = False,
        category: str | None = None,
    ) -> StepPlanResponse:
        """Точечно закрывает или переключает чекбокс шага в плане с advisory flock.

        A13: category опционально уточняет выбор плана при нескольких
        планах одного task_id.
        """
        clean_task = sanitize_task_id(task_id)
        task_lock_path = Path(self.plans_dir) / ".locks" / f"task-{clean_task}.lock"
        with _advisory_lock(task_lock_path):
            plan_file = self.find_plan_file(clean_task, category=category)
            if not plan_file:
                raise FileNotFoundError(f"Файл плана для задачи '{task_id}' не найден на диске")

            lock_path = self._stable_plan_lock(plan_file)
            with _advisory_lock(lock_path):
                content = plan_file.read_text(encoding="utf-8")
                plan = parse_plan_markdown(content, file_path=plan_file)

                self.recover_orphan_snapshots(plan.id, plan.plan_version)

                # Проверка порядка выполнения
                if not allow_out_of_order and status == "done" and step > 1:
                    for prev in plan.steps:
                        if prev.index < step and not prev.completed:
                            raise ValueError(
                                f"Нельзя закрыть шаг {step} ('{plan.steps[step - 1].title if step <= len(plan.steps) else ''}') "
                                f"раньше незавершённого шага {prev.index} ('{prev.title}'). "
                                "Используйте allow_out_of_order=True для принудительного выполнения."
                            )

                mutated_content, status_changed, _new_status = mutate_plan_step_in_markdown(
                    content=content,
                    step_index=step,
                    status=status,
                    note=note,
                )
                updated_plan = parse_plan_markdown(mutated_content, file_path=plan_file)

                # Перемещение файла между active/ и completed/ при изменении статуса
                target_dir = self._plan_target_dir(updated_plan.status, updated_plan.category)
                new_path = self._publish_plan_file(
                    updated_plan, plan_file, target_dir, mutated_content
                )

            location = "completed" if updated_plan.status == PlanStatus.DONE else "active"
            self._index_artifact_file(new_path, location, "plan")

            if str(plan_file.resolve()) != str(new_path.resolve()):
                self.storage.drop_artifact_path(str(plan_file.resolve()))

            total_steps = len(updated_plan.steps)
            done_steps = sum(1 for s in updated_plan.steps if s.completed)
            progress_str = f"{done_steps}/{total_steps}"

            current_step_title = None
            remaining = []
            for s in updated_plan.steps:
                if not s.completed:
                    if current_step_title is None:
                        current_step_title = s.title
                    else:
                        remaining.append(s.title)

            return StepPlanResponse(
                status="ok",
                task_id=updated_plan.task_id,
                progress=progress_str,
                current_step=current_step_title,
                remaining_steps=remaining,
                status_changed=status_changed,
                new_plan_status=updated_plan.status if status_changed else None,
                file_path=str(new_path),
                plan_version=updated_plan.plan_version,
            )

    def save_review(self, review: ReviewArtifact) -> Path:
        """Сохраняет и индексирует артефакт код-ревью."""
        target_dir = self._review_target_dir(review.category)
        target_dir.mkdir(parents=True, exist_ok=True)

        clean_cat = normalize_category_slug(review.category)
        clean_task = sanitize_task_id(review.task_id) if review.task_id else None

        pr_num = None
        if review.pr_url:
            import re

            m = re.search(r"/pull/(\d+)", review.pr_url)
            if m:
                pr_num = m.group(1)

        filename_parts = ["rev", clean_cat]
        if clean_task:
            filename_parts.append(clean_task)
        if pr_num:
            filename_parts.append(f"pr{pr_num}")
        filename_parts.append(f"r{review.round}.md")
        filename = "-".join(filename_parts)

        file_path = target_dir / filename
        if file_path.exists():
            msg = f"REVIEW_ID_COLLISION: Файл ревью '{filename}' уже существует на диске ({file_path})."
            raise FileExistsError(msg)

        content = serialize_review_markdown(review)
        _atomic_write_text(file_path, content, overwrite=False)

        self._index_artifact_file(file_path, "reviews", "review")
        return file_path
