from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from mcp_core.paths import _find_default_project_root, get_agents_cache_dir
from mcp_core.sync import _atomic_write_text
from mcp_skills.bundler import validate_agent_key
from mcp_skills.models import AgentOverride
from mcp_skills.spam_gate import SpamGate

__all__ = [
    "GitSkillsManager",
    "SaveOverrideResult",
    "get_current_branch",
]


@dataclass
class SaveOverrideResult:
    success: bool
    path: str | None = None
    issues: list[str] = field(default_factory=list)
    message: str = ""


class GitSkillsManager:
    """Управление сохранением оверрайдов субагентов в локальный клон кэша."""

    def __init__(
        self,
        cache_dir: Path,
        spam_gate: SpamGate | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.spam_gate = spam_gate or SpamGate()

    def _validate_override_payload(
        self, name: str, project: str, target: str, content: str
    ) -> tuple[bool, list[str], str]:
        """Проверяет валидность контента оверрайда, схему и соответствие параметров."""
        gate_res = self.spam_gate.validate_content(name, content)
        if not gate_res.is_valid:
            return False, gate_res.issues, "Контент оверрайда отклонён фильтром безопасности"

        try:
            parsed_data = yaml.safe_load(content)
            if not isinstance(parsed_data, dict):
                return (
                    False,
                    ["Контент оверрайда должен быть YAML-словарём"],
                    "Некорректный формат оверрайда",
                )
            override_obj = AgentOverride.model_validate(parsed_data)
            if override_obj.base_agent and override_obj.base_agent != name:
                msg = f"Имя агента в оверрайде ('{override_obj.base_agent}') не совпадает с аргументом '{name}'"
                return False, [msg], "Несоответствие параметров оверрайда"
            if override_obj.target and override_obj.target != target:
                msg = f"Целевой рантайм в оверрайде ('{override_obj.target}') не совпадает с аргументом '{target}'"
                return False, [msg], "Несоответствие параметров оверрайда"
            if override_obj.project and override_obj.project != project:
                msg = f"Имя проекта в оверрайде ('{override_obj.project}') не совпадает с аргументом '{project}'"
                return False, [msg], "Несоответствие параметров оверрайда"
        except yaml.YAMLError as e:
            return False, [f"Ошибка парсинга YAML: {e}"], "Некорректный YAML контент"
        except Exception as e:
            return (
                False,
                [f"Ошибка валидации схемы AgentOverride: {e}"],
                "Контент не соответствует схеме AgentOverride",
            )

        return True, [], ""

    def _commit_override_file(self, override_file: Path, commit_message: str | None) -> list[str]:
        """Выполняет git add и опциональный git commit в локальном клоне кэша."""
        git_dir = self.cache_dir / ".git"
        if not git_dir.is_dir():
            return []

        git_issues: list[str] = []
        try:
            res_add = subprocess.run(
                ["git", "add", str(override_file)],
                cwd=str(self.cache_dir),
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if res_add.returncode != 0:
                git_issues.append(
                    f"Предупреждение git: не удалось проиндексировать файл ({res_add.stderr.strip()})"
                )
            elif commit_message:
                res_commit = subprocess.run(
                    ["git", "commit", "-m", commit_message],
                    cwd=str(self.cache_dir),
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                if res_commit.returncode != 0:
                    git_issues.append(
                        f"Предупреждение git: не удалось зафиксировать коммит ({res_commit.stderr.strip()})"
                    )
        except subprocess.TimeoutExpired:
            git_issues.append("Предупреждение git: операция превысила таймаут 30 секунд")
        except Exception as e:
            git_issues.append(f"Предупреждение git: ошибка выполнения команды ({e})")
        return git_issues

    def save_agent_override(
        self,
        name: str,
        project: str,
        target: str,
        content: str,
        commit_message: str | None = None,
    ) -> SaveOverrideResult:
        """Сохраняет оверрайд агента в центральный клон кэша."""
        # 1. Валидация ключа агента (name, target, project)
        try:
            validate_agent_key(name, target, project)
        except ValueError as e:
            return SaveOverrideResult(
                success=False,
                issues=[str(e)],
                message="Ошибка валидации параметров оверрайда",
            )

        # 2. Валидация контента и структуры схемы
        ok, val_issues, val_msg = self._validate_override_payload(name, project, target, content)
        if not ok:
            return SaveOverrideResult(
                success=False,
                issues=val_issues,
                message=val_msg,
            )

        # 3. Построение пути сохранения и defense-in-depth проверка на traversal
        override_dir = get_agents_cache_dir(self.cache_dir) / name / "overrides"
        override_file = override_dir / f"{name}_{project}_{target}.override.yaml"

        try:
            if not override_file.resolve().is_relative_to(self.cache_dir.resolve()):
                return SaveOverrideResult(
                    success=False,
                    issues=["Небезопасный путь к оверрайду (path traversal)"],
                    message="Сохранение отклонено фильтром безопасности",
                )
        except Exception as e:
            return SaveOverrideResult(
                success=False,
                issues=[f"Ошибка проверки пути: {e}"],
                message="Сохранение отклонено фильтром безопасности",
            )

        # 4. Атомарная запись через core-примитив (fsync + tmp + replace).
        # Intended-delta Ф1 (core-dedup v4): ранее hand-rolled версия БЕЗ fsync —
        # durability записи оверрайда повышена (tmp-формат и cleanup унифицированы).
        try:
            _atomic_write_text(override_file, content.strip() + "\n")
        except OSError as e:
            return SaveOverrideResult(
                success=False,
                issues=[f"Не удалось записать оверрайд: {e}"],
                message="Ошибка записи оверрайда в кэш",
            )

        # 5. Опциональный git commit / staging в кэше
        git_issues = self._commit_override_file(override_file, commit_message)

        return SaveOverrideResult(
            success=True,
            path=str(override_file),
            issues=git_issues,
            message=f"Оверрайд успешно сохранён ({override_file.name}).",
        )


def get_current_branch(project_root: Path | None = None) -> str | None:
    """Возвращает имя текущей Git-ветки или None (при ошибке/detached HEAD)."""
    root = project_root or _find_default_project_root()
    if not (root / ".git").exists():
        return None
    try:
        env = {**os.environ, "LC_ALL": "C"}
        res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=env,
        )
        if res.returncode == 0:
            branch = res.stdout.strip()
            if branch and branch != "HEAD":
                return branch
    except (subprocess.SubprocessError, OSError):
        pass
    return None
