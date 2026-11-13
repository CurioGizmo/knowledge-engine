"""Deterministic API scenario checks; these do not exercise LLM activation or dialogue.

Search terms, expected skills and composition arguments are supplied by the test.
A fresh index is isolation of API state, not a fresh model session.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
import pytest

from mcp_skills.server import create_server, load_skill, search_skills
from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer

_env_skills = os.environ.get("MCP_SKILLS_TEST_CATALOG")
REPO_ROOT = (
    Path(_env_skills)
    if _env_skills and Path(_env_skills).is_dir()
    else (Path(__file__).resolve().parents[2] / "mcp_skills")
)

API_SCENARIOS = [
    {
        "id": "scenario_1_python_cli_review",
        "name": "Ревью Python CLI",
        "user_query": "Проведи архитектурное и качественное ревью Python CLI-утилиты для парсинга логов",
        "search_term": "code review",
        "expected_skill": "code-review",
        "load_stack": ["python"],
        "touches": None,
        "must_contain": ["specialized-architects.md", "Не навязывай Pydantic"],
        "must_not_contain": ["FastAPI", "SQLite", "tauri"],
    },
    {
        "id": "scenario_2_python_sqlite_review",
        "name": "Ревью Python+SQLite",
        "user_query": "Проведи ревью сервиса на Python со встроенной базой SQLite",
        "search_term": "sqlite",
        "expected_skill": "sqlite-performance",
        "load_stack": ["python", "sqlite"],
        "touches": None,
        "must_contain": ["busy_timeout"],
        "must_not_contain": ["tauri"],
    },
    {
        "id": "scenario_3_go_feature_plan",
        "name": "План Go-фичи",
        "user_query": "Спланируй разработку высоконагруженного сервиса на Go",
        "search_term": "writing plans",
        "expected_skill": "writing-plans",
        "load_stack": ["go"],
        "touches": None,
        "must_contain": ["План"],
        "must_not_contain": ["FastAPI"],
    },
    {
        "id": "scenario_4_ruby_feature_plan",
        "name": "План Ruby-фичи",
        "user_query": "Разработай план реализации модуля на Ruby on Rails",
        "search_term": "plans",
        "expected_skill": "writing-plans",
        "load_stack": ["ruby"],
        "touches": None,
        "must_contain": ["План"],
        "must_not_contain": ["blocks/mode/ruby.md"],
        "expected_unmatched": ["ruby"],
    },
    {
        "id": "scenario_5_react_ui",
        "name": "React UI",
        "user_query": "Спроектируй пользовательский интерфейс дашборда на React",
        "search_term": "ui ux",
        "expected_skill": "ui-ux-pro-max",
        "load_stack": ["react"],
        "touches": None,
        "must_contain": ["accessibility", "interfaces"],
        "must_not_contain": ["whisper"],
    },
    {
        "id": "scenario_6_tauri_ipc",
        "name": "Tauri IPC",
        "user_query": "Реализуй безопасный IPC-мост между React frontend и Rust бэкендом в десктопном приложении",
        "search_term": "tauri ipc",
        "expected_skill": "tauri-ipc-patterns",
        "load_stack": ["tauri", "react"],
        "touches": None,
        "must_contain": ["IPC", "invoke"],
        "must_not_contain": ["whisper"],
    },
    {
        "id": "scenario_7_whisper_vad",
        "name": "Whisper/VAD",
        "user_query": "Настрой модуль распознавания речи с локальной моделью Whisper и детекцией пауз",
        "search_term": "whisper",
        "expected_skill": "onnx-whisper-engine",
        "load_stack": None,
        "touches": ["vad-silero"],
        "must_contain": ["Silero"],
        "must_not_contain": ["webrtc"],
    },
    {
        "id": "scenario_8_new_stack_pre_install",
        "name": "Изменение технологии до её установки (SQLite)",
        "user_query": "Планируем внедрить SQLite для локального кэша, пакетов в проекте пока нет",
        "search_term": "sqlite",
        "expected_skill": "sqlite-performance",
        "load_stack": ["sqlite"],
        "touches": None,
        "must_contain": ["SQLite", "busy_timeout"],
        "must_not_contain": ["tauri"],
    },
]


def _run_api_case(scenario: dict[str, Any], session_index: int, tmp: Path) -> dict[str, Any]:
    """Проверяет заданные вызовы API с независимым индексом."""
    db_path = tmp / "skills_fts.db"
    store = SkillsStorage(db_path)
    skills_base = REPO_ROOT if REPO_ROOT.name == "skills" else REPO_ROOT / "skills"
    if not skills_base.is_dir():
        pytest.skip(f"Каталог скиллов {skills_base} не найден")
    sync = SkillsSynchronizer(store, [skills_base])
    sync.sync()

    # 2. Поиск компетенции (search_skills)
    search_res = search_skills(
        query=scenario["search_term"],
        storage=store,
        synchronizer=sync,
        project_root=tmp,
    )
    results = search_res.get("results", [])
    found_names = [r["name"] for r in results]
    assert scenario["expected_skill"] in found_names, (
        f"Ожидаемый скилл '{scenario['expected_skill']}' не найден среди {found_names} для запроса '{scenario['search_term']}'"
    )

    # 3. Загрузка компетенции (load_skill)
    load_kwargs: dict[str, Any] = {
        "name": scenario["expected_skill"],
        "storage": store,
        "synchronizer": sync,
        "project_root": tmp,
    }
    if scenario.get("load_stack") is not None:
        load_kwargs["stack"] = scenario["load_stack"]
    if scenario.get("touches") is not None:
        load_kwargs["touches"] = scenario["touches"]

    loaded = load_skill(**load_kwargs)
    content = loaded["content"]
    manifest = loaded.get("assembly", {})

    # 4. Проверка обязательных включений и исключений
    for item in scenario.get("must_contain", []):
        assert item.lower() in content.lower(), (
            f"Секция/требование '{item}' не найдено в собранном скилле"
        )

    for item in scenario.get("must_not_contain", []):
        assert item.lower() not in content.lower(), (
            f"Неприменимое требование '{item}' ошибочно включено в контент"
        )

    # 5. Проверка честного generic-fallback для неподдерживаемого стека
    expected_unmatched = scenario.get("expected_unmatched")
    if expected_unmatched:
        for u in expected_unmatched:
            assert u in manifest.get("unmatched_technologies", []), (
                f"Технология '{u}' должна быть в unmatched_technologies"
            )
            assert manifest.get("status") == "complete"

    return {
        "scenario_id": scenario["id"],
        "session_index": session_index,
        "skill": scenario["expected_skill"],
        "content_length": len(content),
        "tokens_est": len(content) // 4,
        "manifest_status": manifest.get("status", "complete"),
        "status": "PASS",
    }


@pytest.mark.parametrize("scenario", API_SCENARIOS, ids=[str(s["id"]) for s in API_SCENARIOS])
@pytest.mark.parametrize("session_index", [1, 2, 3])
def test_api_scenario_fresh_index(
    scenario: dict[str, Any], session_index: int, tmp_path: Path
) -> None:
    """Повторяет 8 детерминированных API-кейсов с 3 независимыми индексами."""
    result = _run_api_case(scenario, session_index, tmp_path)
    assert result["status"] == "PASS"
    assert result["content_length"] > 200
