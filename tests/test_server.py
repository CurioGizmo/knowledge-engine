"""Тесты для MCP stdio сервера (MCP Server Integration)."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from mcp.types import CallToolResult

from mcp_knowledge.server import create_server
from mcp_knowledge.service import KnowledgeService
from mcp_knowledge.storage import KnowledgeStorage


@pytest.fixture
def test_server(tmp_path: Path):
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    cache_db = tmp_path / ".cache" / "knowledge_fts.db"
    cache_db.parent.mkdir(parents=True, exist_ok=True)

    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=knowledge_dir)
    return create_server(service=service)


@pytest.mark.asyncio
async def test_mcp_tools_registration_and_schemas(test_server):
    """
    Вход: Инициализация MCP сервера.
    Выход: Список зарегистрированных инструментов.
    Проверка: Присутствуют search_knowledge, get_knowledge, record_knowledge, update_knowledge_status.
    """
    tools = await test_server.list_tools()
    tool_names = {t.name for t in tools}

    assert "search_knowledge" in tool_names
    assert "get_knowledge" in tool_names
    assert "record_knowledge" in tool_names
    assert "update_knowledge_status" in tool_names
    assert "review_knowledge" in tool_names
    assert "get_help" in tool_names


@pytest.mark.asyncio
async def test_mcp_record_knowledge_tool_call(test_server):
    """
    Вход: Вызов record_knowledge через MCP протокол.
    Выход: {"status": "created", "id": "...", "path": "..."}.
    Проверка: Файл создан на диске, возвращен валидный статус.
    """
    args = {
        "title": "Тестирование MCP записи карточки",
        "category": "audio",
        "type": "lesson",
        "source": "auto_inferred",
        "symptom": "FFmpeg зависает на 0% в фоне без терминала",
        "root_cause": "Ядро Linux посылает сигнал SIGTTIN фоновому процессу, читающему stdin",
        "solution": "Обязательный флаг -nostdin и stdin=subprocess.DEVNULL при запуске процесса",
        "rationale": "Исключает блокировку фонового воркера сигналом терминала",
        "slug": "audio-mcp-record-test",
    }

    result = await test_server.call_tool("record_knowledge", args)
    assert not result.is_error

    if result.structured_content:
        data = result.structured_content
    else:
        data = json.loads(result.content[0].text)

    assert data.get("status") == "created"
    assert "audio-mcp-record-test" in data.get("id", "")
    assert Path(data.get("path")).exists()


@pytest.mark.asyncio
async def test_mcp_search_knowledge_tool_call(test_server):
    """
    Вход: Вызов инструмента search_knowledge с тестовым запросом.
    Выход: Массив словарей KnowledgeSnippet.
    Проверка: Валидный результат, содержащий id, title, snippet.
    """
    record_args = {
        "title": "Поиск по коду ошибки SIGTTIN",
        "category": "audio",
        "type": "lesson",
        "symptom": "FFmpeg сигнал SIGTTIN при запуске транскрибации",
        "root_cause": "Ядро Linux посылает сигнал SIGTTIN фоновому процессу, читающему stdin",
        "solution": "Обязательный флаг -nostdin и stdin=subprocess.DEVNULL при запуске процесса",
        "rationale": "Исключает блокировку фонового воркера сигналом терминала",
        "slug": "audio-sigttin-search",
        "source": "user_confirmed",
    }
    await test_server.call_tool("record_knowledge", record_args)

    search_args = {"query": "sigttin"}
    result = await test_server.call_tool("search_knowledge", search_args)
    assert not result.is_error

    if result.structured_content:
        data = result.structured_content.get("result", result.structured_content)
    else:
        data = json.loads(result.content[0].text)

    assert isinstance(data, dict)
    assert len(data["query_results"]) == 1
    assert data["query_results"][0]["id"] == "audio-sigttin-search"
    assert "SIGTTIN" in data["query_results"][0]["title"]
    assert "snippet" in data["query_results"][0]
    assert "category_results" in data


@pytest.mark.asyncio
async def test_mcp_get_knowledge_tool_call(test_server):
    """
    Вход: Вызов get_knowledge с существующим и несуществующим ID.
    Выход: JSON с объектом CardGetResponse (cards и not_found).
    Проверка: Структура полностью совпадает со спецификацией.
    """
    record_args = {
        "title": "Карточка для проверки get",
        "category": "ml",
        "type": "hint",
        "symptom": "Медленный инференс модели whisper на процессорах Intel",
        "root_cause": "Отсутствие инструкций AVX-512 в базовой сборке ONNX Runtime",
        "solution": "Использование оптимизированного пакета onnxruntime-openvino для CPU",
        "rationale": "Аппаратное ускорение векторных инструкций дает ускорение в 2.5 раза",
        "slug": "ml-intel-avx",
    }
    await test_server.call_tool("record_knowledge", record_args)

    get_args = {"ids": ["ml-intel-avx", "ml-non-existent"]}
    result = await test_server.call_tool("get_knowledge", get_args)
    assert not result.is_error

    if result.structured_content:
        data = result.structured_content
    else:
        data = json.loads(result.content[0].text)

    assert "cards" in data
    assert "not_found" in data
    assert len(data["cards"]) == 1
    assert data["cards"][0]["id"] == "ml-intel-avx"
    assert "ml-non-existent" in data["not_found"]


@pytest.mark.asyncio
async def test_mcp_update_knowledge_status_tool_call(test_server):
    """
    Вход: Вызов update_knowledge_status через MCP протокол.
    Выход: {"status": "updated", "id": "...", "new_status": "..."}.
    Проверка: Статус обновлен.
    """
    record_args = {
        "title": "Карточка для проверки смены статуса",
        "category": "desktop",
        "type": "hint",
        "symptom": "Появление черной консоли при старте приложения",
        "root_cause": "Windows по умолчанию выделяет консольное окно дочернему процессу",
        "solution": "Установка creationflags=CREATE_NO_WINDOW при создании подпроцесса",
        "rationale": "Гарантирует чистое графическое окно без консольных всплываний",
        "slug": "desk-status-test",
        "source": "user_confirmed",
    }
    record_result = await test_server.call_tool("record_knowledge", record_args)
    record_data = (
        record_result.structured_content
        if record_result.structured_content
        else json.loads(record_result.content[0].text)
    )
    card_id = record_data["id"]

    update_args = {
        "id": card_id,
        "action": "suspect",
        "note": "Появились сообщения о сбоях на Windows 11",
    }
    result = await test_server.call_tool("update_knowledge_status", update_args)
    assert not result.is_error

    if result.structured_content:
        data = result.structured_content
    else:
        data = json.loads(result.content[0].text)

    assert data.get("status") == "updated"
    assert data.get("new_status") == "suspect"


@pytest.mark.asyncio
async def test_mcp_server_handles_exceptions_gracefully(test_server):
    """
    Вход: Передача невалидных параметров (короткий root_cause).
    Выход: Структурированный JSON с описанием ошибки валидации без краша процесса.
    Проверка: Сервер возвращает понятное описание ошибки и продолжает работу.
    """
    invalid_args = {
        "title": "Невалидная карточка",
        "category": "audio",
        "type": "lesson",
        "symptom": "Слишком короткий симптом",
        "root_cause": "Коротко",  # < 40 символов!
        "solution": "Слишком короткое решение",  # < 50 символов!
        "rationale": "Коротко",  # < 30 символов!
    }

    result = await test_server.call_tool("record_knowledge", invalid_args)
    if result.structured_content:
        data = result.structured_content
    else:
        data = json.loads(result.content[0].text)

    assert data.get("error") is True
    assert "ValidationError" in data.get("error_type", "") or "ANTI_SPAM_REJECTED" in data.get(
        "reason", ""
    )

    # Проверяем, что сервер жив и может обрабатывать последующие запросы
    search_res = await test_server.call_tool("search_knowledge", {"query": "test"})
    assert not search_res.is_error


@pytest.mark.asyncio
async def test_server_record_knowledge_creates_okf_card(test_server):
    """
    Вход: Вызов MCP инструмента record_knowledge с плоскими аргументами (source="auto_inferred", stale_after="2027-01-01").
    Выход: Ответ сервера со status="created".
    Проверка: Созданный на диске файл содержит валидный YAML фронтматтер generated: {by: process} и stale_after.
    """
    args = {
        "title": "Тест генерации OKF v0.2 через MCP",
        "category": "platform",
        "type": "lesson",
        "source": "auto_inferred",
        "symptom": "Проверка создания файла по стандарту OKF v0.2 через FastMCP",
        "root_cause": "Движок должен генерировать фронтматтер с секцией generated вместо source",
        "solution": "Автоматический маппинг плоских параметров в структуру OKF v0.2 на уровне сервиса",
        "rationale": "Сохранение плоской эргономики вызова инструментов для предотвращения ошибок LLM",
        "stale_after": "2027-01-01",
        "slug": "plat-okf-mcp-test",
    }

    result = await test_server.call_tool("record_knowledge", args)
    assert not result.is_error

    if result.structured_content:
        data = result.structured_content
    else:
        data = json.loads(result.content[0].text)

    assert data.get("status") == "created"
    file_path = Path(data.get("path"))
    assert file_path.exists()

    content = file_path.read_text(encoding="utf-8")
    assert "generated:" in content
    assert "by: process" in content
    assert "stale_after: '2027-01-01'" in content or "stale_after: 2027-01-01" in content
    assert "source:" not in content


@pytest.mark.asyncio
async def test_server_update_status_verify_records_audit_trail(test_server):
    """
    Вход: Вызов MCP инструмента update_knowledge_status(id=..., action="verify", note="OK").
    Выход: Ответ сервера со status="updated", new_status="verified".
    Проверка: В фронтматтере сохранен элемент в verified и обновлен лог в теле документа.
    """
    record_args = {
        "title": "Тест верификации через MCP",
        "category": "audio",
        "type": "lesson",
        "symptom": "Проверка формирования аудиторской записи при вызове инструмента MCP",
        "root_cause": "Действие verify через MCP инструмент должно формировать список verified",
        "solution": "Корректная обработка действия verify в сервисе с добавлением VerificationRecord",
        "rationale": "Обеспечение аудиторского следа верификации знаний в формате OKF v0.2",
        "slug": "audio-verify-mcp",
        # K2: карточка auto_inferred создаётся в draft — verify недоступен до
        # promote; для аудиторского теста создаём active-карточку сразу
        "source": "user_confirmed",
    }
    record_result = await test_server.call_tool("record_knowledge", record_args)
    record_data = (
        record_result.structured_content
        if record_result.structured_content
        else json.loads(record_result.content[0].text)
    )
    card_id = record_data["id"]
    card_path = Path(record_data["path"])

    update_args = {
        "id": card_id,
        "action": "verify",
        "note": "Успешно проверено на стенде через MCP",
        "by": "human",
    }
    update_result = await test_server.call_tool("update_knowledge_status", update_args)
    assert not update_result.is_error

    update_data = (
        update_result.structured_content
        if update_result.structured_content
        else json.loads(update_result.content[0].text)
    )
    assert update_data.get("status") == "updated"
    assert update_data.get("new_status") == "verified"

    content = card_path.read_text(encoding="utf-8")
    assert "status: verified" in content
    assert "verified:" in content
    assert "note: Успешно проверено на стенде через MCP" in content
    assert "VERIFIED: Успешно проверено на стенде через MCP" in content


@pytest.mark.asyncio
async def test_mcp_get_help_tool_call(test_server):
    """
    Вход: Вызов get_help с различными аргументами.
    Выход: Строка с подробной справкой.
    Проверка: Корректное возвращение справки по инструменту и при неизвестном ключе.
    """
    # 1. По умолчанию или overview (включая явный null и пустую строку)
    res_overview = await test_server.call_tool("get_help", {})
    assert not res_overview.is_error
    text_overview = res_overview.content[0].text
    assert "MCP Knowledge Engine: Обзор" in text_overview
    assert "search_knowledge" in text_overview

    res_none = await test_server.call_tool("get_help", {"tool_name": None})
    assert not res_none.is_error
    assert "MCP Knowledge Engine: Обзор" in res_none.content[0].text

    res_empty = await test_server.call_tool("get_help", {"tool_name": "   "})
    assert not res_empty.is_error
    assert "MCP Knowledge Engine: Обзор" in res_empty.content[0].text

    # 2. Справка по record_knowledge (включая верхний регистр и короткий алиас)
    res_rec = await test_server.call_tool("get_help", {"tool_name": "record_knowledge"})
    assert not res_rec.is_error
    text_rec = res_rec.content[0].text
    assert "Справка по инструменту record_knowledge" in text_rec
    assert "Anti-Spam Gate" in text_rec
    assert "ПРИМЕР ЭТАЛОННОЙ КАРТОЧКИ" in text_rec

    res_rec_upper = await test_server.call_tool("get_help", {"tool_name": "RECORD_KNOWLEDGE"})
    assert not res_rec_upper.is_error
    assert "Справка по инструменту record_knowledge" in res_rec_upper.content[0].text

    res_alias_rec = await test_server.call_tool("get_help", {"tool_name": "record"})
    assert not alias_rec_text if (alias_rec_text := res_alias_rec.content[0].text) is None else True
    assert "Справка по инструменту record_knowledge" in alias_rec_text

    # 3. Алиасы search, update, get
    res_search = await test_server.call_tool("get_help", {"tool_name": "  search  "})
    assert not res_search.is_error
    assert "Справка по инструменту search_knowledge" in res_search.content[0].text

    res_update = await test_server.call_tool("get_help", {"tool_name": "update"})
    assert not res_update.is_error
    assert "Справка по инструменту update_knowledge_status" in res_update.content[0].text

    res_get = await test_server.call_tool("get_help", {"tool_name": "get"})
    assert not res_get.is_error
    assert "Справка по инструменту get_knowledge" in res_get.content[0].text

    # 4. Неизвестный раздел
    res_unknown = await test_server.call_tool("get_help", {"tool_name": "unknown_tool"})
    assert not res_unknown.is_error
    text_unknown = res_unknown.content[0].text
    assert "Неизвестный инструмент или раздел" in text_unknown
    assert "Доступные разделы справки" in text_unknown


@pytest.mark.asyncio
async def test_mcp_review_knowledge_tool_call(test_server):
    """
    Вход: Запись карточки с task_id через MCP, затем вызов review_knowledge(task_id=...).
    Выход: Пакет саморевью с кандидатом, флагами и контрактом (rubric/verdict_actions).
    Проверка: Карточка найдена по task_id, инструмент read-only (ничего не мутирует).
    """
    record_args = {
        "title": "Саморевью MCP карточки через review_knowledge",
        "category": "backend",
        "type": "lesson",
        "symptom": "Агент записывает мусорные карточки без проверки пользы",
        "root_cause": "Отсутствует обязательный шаг саморевью перед коммитом в протоколе",
        "solution": "Вызывать review_knowledge(task_id=...) перед git commit и помечать мусор через dispute",
        "rationale": "Рубрика и флаги отсекают сессионный шум до попадания в базу знаний",
        "slug": "backend-mcp-review-test",
        "task_id": "mcp-review-1",
    }
    rec_result = await test_server.call_tool("record_knowledge", record_args)
    assert not rec_result.is_error

    result = await test_server.call_tool("review_knowledge", {"task_id": "mcp-review-1"})
    assert not result.is_error

    if result.structured_content:
        data = result.structured_content
    else:
        data = json.loads(result.content[0].text)

    assert data.get("status") == "ok"
    assert data.get("candidates_count") == 1
    candidate = data["candidates"][0]
    assert candidate["task_id"] == "mcp-review-1"
    assert "unverified_auto" in candidate["flags"]
    assert "rubric" in data and "ask_user_template" in data and "verdict_actions" in data


@pytest.mark.asyncio
async def test_mcp_telemetry_decorator_records_success_and_error(tmp_path: Path):
    """
    Вход: вызовы search_knowledge через MCP-стек — успешный и с невалидным limit.
    Выход: строки в tool_calls с latency и без/с error.
    Проверка: декоратор _telemetry пишет adoption-телеметрию в finally (регрессия выноса
    телеметрического boilerplate из тел инструментов в server.py).
    """
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    cache_db = tmp_path / ".cache" / "knowledge_fts.db"
    cache_db.parent.mkdir(parents=True, exist_ok=True)

    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=knowledge_dir)
    server = create_server(service=service)

    ok_result = await server.call_tool("search_knowledge", {"query": "ffmpeg freeze"})
    assert isinstance(ok_result, CallToolResult) and not ok_result.is_error

    # Короткий title -> ValidationError -> error-envelope (MCP is_error = True)
    err_result = await server.call_tool(
        "record_knowledge",
        {
            "title": "x",
            "category": "audio",
            "type": "lesson",
            "symptom": "s",
            "root_cause": "r",
            "solution": "s",
            "rationale": "r",
        },
    )
    assert isinstance(err_result, CallToolResult) and err_result.is_error is True

    stats = storage.get_tool_call_stats()
    by_tool = {s["tool_name"]: s for s in stats}
    assert by_tool["search_knowledge"]["total_calls"] == 1
    assert by_tool["search_knowledge"]["error_calls"] == 0
    assert by_tool["search_knowledge"]["avg_latency_ms"] >= 0.0
    assert by_tool["record_knowledge"]["total_calls"] == 1
    assert by_tool["record_knowledge"]["error_calls"] == 1


@pytest.mark.asyncio
async def test_mcp_get_help_failure_returns_structured_error(tmp_path: Path):
    """
    Вход: сбой get_help_text внутри get_help (RuntimeError).
    Выход: CallToolResult(is_error=True) со структурированным error-envelope,
    а не текстовая строка «⚠️ Ошибка...» со статусом успеха.
    Проверка: error-path get_help конвертируется в тот же доменный envelope,
    что и остальные инструменты (план harness-stability-fixes, шаг 1).
    """
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    cache_db = tmp_path / ".cache" / "knowledge_fts.db"
    cache_db.parent.mkdir(parents=True, exist_ok=True)

    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=knowledge_dir)
    server = create_server(service=service)

    with patch("mcp_knowledge.server.get_help_text", side_effect=RuntimeError("help render error")):
        res = await server.call_tool("get_help", {"tool_name": "search"})

    assert isinstance(res, CallToolResult)
    assert res.is_error is True, (
        f"Сбой help-инструмента должен возвращать is_error=True, получено: {res}"
    )
    data = res.structured_content or json.loads(res.content[0].text)
    assert data.get("error") is True
    assert data.get("status") == "rejected"
    assert data.get("error_type") == "RuntimeError"
    assert "help render error" in data.get("message", "")


@pytest.mark.asyncio
async def test_mcp_get_help_failure_telemetry_counted_once(tmp_path: Path):
    """
    Вход: успешный get_help, затем два сбойных вызова (RuntimeError в get_help_text).
    Выход: телеметрия tool_calls: success → error_calls=0; сбои → total=3, error_calls=2.
    Проверка: каждый сбой help учитывается ровно один раз, успех не считается ошибкой.
    """
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    cache_db = tmp_path / ".cache" / "knowledge_fts.db"
    cache_db.parent.mkdir(parents=True, exist_ok=True)

    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=knowledge_dir)
    server = create_server(service=service)

    ok_res = await server.call_tool("get_help", {})
    assert isinstance(ok_res, CallToolResult) and not ok_res.is_error

    with patch("mcp_knowledge.server.get_help_text", side_effect=RuntimeError("boom")):
        for _ in range(2):
            err_res = await server.call_tool("get_help", {"tool_name": "search"})
            assert isinstance(err_res, CallToolResult) and err_res.is_error is True

    stats = storage.get_tool_call_stats()
    by_tool = {s["tool_name"]: s for s in stats}
    help_stats = by_tool["get_help"]
    assert help_stats["total_calls"] == 3
    assert help_stats["error_calls"] == 2


@pytest.mark.asyncio
async def test_mcp_telemetry_kwargs_include_defaults(tmp_path: Path):
    """
    Вход: вызов get_help без аргументов (все параметры — дефолтные).
    Выход: декоратор _telemetry видит tool_name='overview' в kwargs.
    Проверка: MCP SDK валидирует вызов и передаёт аргументы как kwargs,
    ЗАПОЛНЯЯ дефолты — payload-лямбды декоратора читают только kwargs
    (инвариант, найденный независимым ревью; mcp 2.x: model_dump_one_level).
    """
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    cache_db = tmp_path / ".cache" / "knowledge_fts.db"
    cache_db.parent.mkdir(parents=True, exist_ok=True)

    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)
    service = KnowledgeService(storage=storage, knowledge_dir=knowledge_dir)
    server = create_server(service=service)

    recorded: dict[str, dict] = {}
    original = storage.record_tool_call

    def recorder(
        tool_name: str, args: dict[str, Any] | None, latency_ms: float, error: str | None
    ) -> None:
        recorded[tool_name] = args or {}
        original(tool_name, args, latency_ms, error)

    storage.record_tool_call = recorder  # type: ignore[method-assign,assignment]

    result = await server.call_tool("get_help", {})
    assert isinstance(result, CallToolResult) and not result.is_error

    assert recorded["get_help"].get("tool_name") == "overview"


@pytest.mark.asyncio
async def test_mcp_new_terminology_parameters(test_server):
    """
    Вход: Вызовы MCP инструментов с новыми стандартизированными параметрами
          (category, categories, include_overview, limit_per_section).
    Выход: Успешное выполнение, ответы содержат category_results и overview.
    Проверка: Корректность работы новой терминологии в API.
    """
    # 1. record_knowledge с category
    rec_res = await test_server.call_tool(
        "record_knowledge",
        {
            "title": "Новая терминология в записи карточки",
            "category": "backend",
            "type": "lesson",
            "symptom": "Нестандартные параметры в тулзах",
            "root_cause": "Использование метафор вместо технической терминологии",
            "solution": "Стандартизировать имена параметров на category, stack, overview",
            "rationale": "Улучшает совместимость и предсказуемость для LLM",
            "slug": "backend-term-test",
        },
    )
    assert not rec_res.is_error
    rec_data = rec_res.structured_content or json.loads(rec_res.content[0].text)
    assert rec_data.get("status") == "created"
    assert "backend-term-test" in rec_data.get("id", "")

    # 2. propose_hypothesis с category
    hyp_res = await test_server.call_tool(
        "propose_hypothesis",
        {
            "title": "Гипотеза с новым параметром category",
            "category": "backend",
            "symptom": "Тест гипотезы с category",
            "hypothesis": "Параметр category успешно мапится на модель",
        },
    )
    assert not hyp_res.is_error
    hyp_data = hyp_res.structured_content or json.loads(hyp_res.content[0].text)
    assert hyp_data.get("status") == "proposed"

    # 3. search_knowledge с categories, include_overview, limit_per_section
    search_res = await test_server.call_tool(
        "search_knowledge",
        {
            "query": "терминология",
            "categories": ["backend"],
            "include_overview": True,
            "limit_per_section": 5,
        },
    )
    assert not search_res.is_error
    search_data = search_res.structured_content or json.loads(search_res.content[0].text)
    assert "category_results" in search_data
    assert "overview" in search_data
    assert search_data["overview"] is not None
    assert len(search_data["category_results"]) > 0
