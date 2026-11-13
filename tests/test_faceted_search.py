"""Тесты для многослойного фасетного поиска, динамической сводки тем (Project Overview) и скиллов агента."""

import json
from pathlib import Path
from typing import Any

import pytest

from mcp.types import CallToolResult, TextContent

from mcp_knowledge.installer import install_agent_rule, install_agent_skill, run_installer
from mcp_knowledge.models import (
    CardRecordRequest,
    FacetedSearchResponse,
    KnowledgeSource,
    KnowledgeType,
    ProjectOverview,
)
from mcp_knowledge.server import create_server
from mcp_knowledge.service import KnowledgeService
from mcp_knowledge.storage import KnowledgeStorage


def extract_data(res: CallToolResult) -> Any:
    if res.structured_content:
        return res.structured_content.get("result", res.structured_content)
    first_item = res.content[0]
    if isinstance(first_item, TextContent):
        return json.loads(first_item.text)
    return {}


@pytest.fixture
def env(tmp_path: Path):
    k_dir = tmp_path / "knowledge"
    c_db = tmp_path / "fts.db"
    storage = KnowledgeStorage(db_path=c_db, knowledge_dir=k_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=k_dir)
    server = create_server(service=service)
    return storage, service, server


@pytest.mark.asyncio
async def test_legacy_search_returns_list_backwards_compatible(env):
    """Вызов search_knowledge(query='...') в v0.2.0 возвращает единый dict с полями query_results и transitional results."""
    _, service, server = env

    service.record(
        CardRecordRequest(
            title="Зависание FFmpeg в фоне",
            category="audio",
            source=KnowledgeSource.USER_CONFIRMED,
            type=KnowledgeType.LESSON,
            symptom="FFmpeg зависает на 0% в фоне без терминала при чтении stdin",
            root_cause="Ядро Linux посылает сигнал SIGTTIN фоновому процессу, читающему stdin",
            solution="Обязательный флаг -nostdin и stdin=subprocess.DEVNULL при запуске процесса",
            rationale="Исключает блокировку фонового воркера сигналом терминала",
            slug="audio-sigttin-test",
        )
    )

    res = await server.call_tool("search_knowledge", {"query": "sigttin"})
    assert not res.is_error
    data = extract_data(res)
    assert isinstance(data, dict)
    assert len(data["query_results"]) == 1
    assert data["query_results"][0]["id"] == "audio-sigttin-test"
    assert "snippet" in data["query_results"][0]
    assert "category_results" in data

    # Проверяем вызов с фильтром по categories
    res_cat = await server.call_tool(
        "search_knowledge", {"query": "sigttin", "categories": ["audio"]}
    )
    assert not res_cat.is_error
    data_cat = extract_data(res_cat)
    assert isinstance(data_cat, dict)
    assert len(data_cat["query_results"]) == 1
    assert data_cat["query_results"][0]["id"] == "audio-sigttin-test"

    # Вызов с несовпадающим categories возвращает пустые результаты
    res_empty = await server.call_tool(
        "search_knowledge", {"query": "sigttin", "categories": ["frontend"]}
    )
    assert not res_empty.is_error
    data_empty = extract_data(res_empty)
    assert isinstance(data_empty, dict)
    assert len(data_empty["query_results"]) == 0


@pytest.mark.asyncio
async def test_empty_query_returns_overview_and_catalog(env):
    """Zero-query вызов search_knowledge() без аргументов возвращает обзор тем проекта."""
    _, service, server = env

    # 1. Сначала в пустой базе
    res_empty = await server.call_tool("search_knowledge", {})
    assert not res_empty.is_error
    data_empty = extract_data(res_empty)
    assert isinstance(data_empty, dict)
    assert "overview" in data_empty
    assert data_empty["overview"]["total_cards"] == 0
    assert "пуста" in data_empty["hint"].lower()

    # 2. Добавляем карточки в разных категориях
    service.record(
        CardRecordRequest(
            title="Оптимизация OCR binarization",
            category="ocr",
            source=KnowledgeSource.USER_CONFIRMED,
            type=KnowledgeType.LESSON,
            symptom="Низкая точность Tesseract на цветных сканах высокого разрешения",
            root_cause="Отсутствие адаптивной бинаризации Otsu перед подачей изображения",
            solution="Применение cv2.threshold с флагом cv2.THRESH_OTSU и grayscale",
            rationale="Устраняет паразитные цветовые шумы фона и артефакты сканера",
            slug="ocr-otsu-threshold",
        )
    )
    service.record(
        CardRecordRequest(
            title="Зависание Whisper VAD",
            category="transcription",
            source=KnowledgeSource.USER_CONFIRMED,
            type=KnowledgeType.LESSON,
            symptom="Бесконечные галлюцинации Whisper на длинных паузах в аудиопотоке",
            root_cause="Модель декодера зацикливается при отсутствии речевых токенов",
            solution="Интеграция Silero VAD для предварительной фильтрации тишины",
            rationale="Исключает подачу тишины в авторегрессионный декодер модели",
            slug="transcription-silero-vad",
        )
    )

    # 3. Вызов без параметров
    res = await server.call_tool("search_knowledge", {})
    assert not res.is_error
    data = extract_data(res)

    assert isinstance(data, dict)
    assert data["overview"]["total_cards"] == 2
    categories = {item["category"]: item["count"] for item in data["overview"]["categories"]}
    assert categories["ocr"] == 1
    assert categories["transcription"] == 1
    assert "Каталог базы знаний проекта" in data["hint"]


@pytest.mark.asyncio
async def test_multi_category_batch_retrieval(env):
    """Вызов search_knowledge(categories=[...]) возвращает карточки по нескольким темам за один запрос."""
    _, service, server = env

    service.record(
        CardRecordRequest(
            title="Утечка VRAM при инференсе",
            category="training",
            source=KnowledgeSource.USER_CONFIRMED,
            type=KnowledgeType.LESSON,
            symptom="CUDA out of memory при последовательном обучении батчей",
            root_cause="Сохранение графов вычислений в истории лоссов через loss, а не loss.item()",
            solution="Использование loss.item() для логирования метрик и torch.cuda.empty_cache()",
            rationale="Освобождает тензоры autograd из оперативной памяти GPU",
            slug="training-vram-leak",
        )
    )
    service.record(
        CardRecordRequest(
            title="Смещение таймкодов Whisper",
            category="transcription",
            source=KnowledgeSource.USER_CONFIRMED,
            type=KnowledgeType.HINT,
            symptom="Рассинхрон субтитров при нарезке длинных аудиофайлов",
            root_cause="Округление float секунд при слиянии аудиочанков с разным sample rate",
            solution="Использование целочисленных семплов audio samples вместо секунд",
            rationale="Гарантирует субмиллисекундную точность границ аудиофреймов",
            slug="transcription-timestamps",
        )
    )

    res = await server.call_tool("search_knowledge", {"categories": ["training", "transcription"]})
    assert not res.is_error
    data = extract_data(res)

    assert isinstance(data, dict)
    assert "category_results" in data
    assert "training" in data["category_results"]
    assert "transcription" in data["category_results"]
    assert len(data["category_results"]["training"]) == 1
    assert data["category_results"]["training"][0]["id"] == "training-vram-leak"
    assert len(data["category_results"]["transcription"]) == 1
    assert data["category_results"]["transcription"][0]["id"] == "transcription-timestamps"


@pytest.mark.asyncio
async def test_cross_facet_deduplication(env):
    """Карточка, подходящая под query и category, возвращается строго 1 раз (без дубликатов)."""
    _, service, server = env

    service.record(
        CardRecordRequest(
            title="Чек-лист ревью фоновых воркеров FFmpeg",
            category="audio",
            source=KnowledgeSource.USER_CONFIRMED,
            type=KnowledgeType.LESSON,
            symptom="Пропуск no-op моков при ревью тестов с FFmpeg подпроцессами",
            root_cause="Отсутствие валидации реального вызова stdin=DEVNULL в моках",
            solution="Строгая проверка флагов subprocess.Popen в тестах и на этапе ревью",
            rationale="Гарантирует изоляцию фоновых воркеров от терминала ОС",
            slug="audio-ffmpeg-review-check",
            tags=["review", "ffmpeg", "testing"],
        )
    )

    res = await server.call_tool(
        "search_knowledge",
        {
            "query": "ffmpeg",
            "categories": ["audio"],
        },
    )
    assert not res.is_error
    data = extract_data(res)

    # Карточка попала в query_results
    assert len(data["query_results"]) == 1
    assert data["query_results"][0]["id"] == "audio-ffmpeg-review-check"

    # Из-за сквозного seen_card_ids карточка не дублируется в category_results
    assert len(data["category_results"].get("audio", [])) == 0


@pytest.mark.asyncio
async def test_token_budget_clamping(env):
    """Проверка Token Budget Guard: клемпинг категорий до 5 и строгое ограничение суммарных сниппетов ровно до 12."""
    _, service, server = env

    # Создаем по 5 карточек в 3 категориях (суммарно 15 карточек)
    for cat in ["backend", "frontend", "database"]:
        for i in range(5):
            service.record(
                CardRecordRequest(
                    title=f"Тестовая карточка {cat} #{i}",
                    category=cat,
                    source=KnowledgeSource.USER_CONFIRMED,
                    type=KnowledgeType.LESSON,
                    symptom=f"Симптом тестовой проблемы в {cat} #{i} для проверки клемпинга",
                    root_cause=f"Первопричина тестовой проблемы в {cat} #{i} длиной более сорока символов",
                    solution=f"Решение тестовой проблемы в {cat} #{i} длиной более пятидесяти символов",
                    rationale=f"Обоснование правильности решения #{i} более тридцати",
                    slug=f"{cat}-card-{i}",
                    force=True,
                    confirmed_force=True,
                )
            )

    # Запрашиваем 3 категории с limit_per_section=5 (потенциально 15 карточек)
    res = await server.call_tool(
        "search_knowledge",
        {
            "categories": ["backend", "frontend", "database"],
            "limit_per_section": 5,
        },
    )
    assert not res.is_error
    data = extract_data(res)

    # Ровно 12 карточек суммарно (жесткий потолок 12)
    total_returned = sum(len(cards) for cards in data["category_results"].values())
    assert total_returned == 12
    assert "лимит выдачи (12 сниппетов)" in (data.get("hint") or "")


@pytest.mark.asyncio
async def test_overview_window_function_top3(env):
    """Проверка, что оконная функция в Overview отбирает ровно топ-3 авторитетных заголовка."""
    _, service, server = env

    for i in range(5):
        service.record(
            CardRecordRequest(
                title=f"Тема #{i} в категории backend",
                category="backend",
                source=KnowledgeSource.USER_CONFIRMED,
                type=KnowledgeType.LESSON,
                symptom=f"Симптом проблемы #{i} для проверки оконной функции Overview",
                root_cause=f"Первопричина проблемы #{i} длиной более сорока символов обязательно",
                solution=f"Решение проблемы #{i} длиной более пятидесяти символов обязательно",
                rationale=f"Обоснование правильности решения #{i} более тридцати символов",
                slug=f"backend-topic-{i}",
                force=True,
                confirmed_force=True,
            )
        )

    res = await server.call_tool("search_knowledge", {})
    data = extract_data(res)

    backend_overview = next(
        item for item in data["overview"]["categories"] if item["category"] == "backend"
    )
    assert backend_overview["count"] == 5
    assert len(backend_overview["sample_titles"]) == 3


def test_installer_skill_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Проверка генерации скилла .agents/skills/knowledge-memory/SKILL.md.

    P0-фикс (инцидент 2026-10-07): run_installer с default --antigravity=True писал
    мусорный --project-root в реальный ~/.gemini/**/mcp_config.json. Двойная изоляция:
    HOME-патч (страховка на случай смены дефолтов) + --no-antigravity (отключает запись).
    """
    # Двойная изоляция: HOME -> tmp_path и отключение antigravity-ветки
    monkeypatch.setenv("HOME", str(tmp_path))
    home_cfg = Path.home() / ".gemini" / "antigravity" / "mcp_config.json"
    cfg_before = home_cfg.read_text(encoding="utf-8") if home_cfg.exists() else None

    # 1. Генерация скилла
    skill_path = install_agent_skill(project_root=tmp_path)
    assert skill_path.exists()
    assert skill_path == tmp_path / ".agents" / "skills" / "knowledge-memory" / "SKILL.md"
    skill_content = skill_path.read_text(encoding="utf-8")
    assert "name: knowledge-memory" in skill_content
    assert "search_knowledge()" in skill_content
    assert "categories" in skill_content

    # 2. Проверка CLI инсталлера с флагом --skill (запись в конфиги Antigravity отключена)
    out_dir = tmp_path / "proj"
    run_installer(["--skill", "--no-antigravity", "--project-root", str(out_dir)])
    assert (out_dir / ".agents" / "skills" / "knowledge-memory" / "SKILL.md").exists()

    # 3. P0-регресс-инвариант: реальный конфиг Antigravity не изменился (canary
    # на будущие смены дефолтов --antigravity или ошибки изоляции HOME)
    cfg_after = home_cfg.read_text(encoding="utf-8") if home_cfg.exists() else None
    assert cfg_after == cfg_before
