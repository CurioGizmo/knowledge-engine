"""Тесты инструмента саморевью review_knowledge (ReviewKnowledgeUseCase)."""

from datetime import date, timedelta
from pathlib import Path

import pytest

from mcp_knowledge.models import (
    CardRecordRequest,
    CardStatusUpdateRequest,
    HypothesisRecordRequest,
    KnowledgeCategory,
    KnowledgeSource,
    KnowledgeType,
)
from mcp_knowledge.parser import parse_card_markdown, serialize_card_markdown
from mcp_knowledge.repository import _atomic_write_text
from mcp_knowledge.service import KnowledgeService
from mcp_knowledge.storage import KnowledgeStorage


@pytest.fixture
def service(tmp_path: Path) -> KnowledgeService:
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    cache_db = tmp_path / ".cache" / "knowledge_fts.db"
    cache_db.parent.mkdir(parents=True, exist_ok=True)

    storage = KnowledgeStorage(db_path=cache_db, knowledge_dir=knowledge_dir)
    return KnowledgeService(storage=storage, knowledge_dir=knowledge_dir)


def _make_req(**overrides) -> CardRecordRequest:
    defaults: dict = dict(
        title="Зависание FFmpeg из-за SIGTTIN",
        category=KnowledgeCategory.AUDIO,
        type=KnowledgeType.LESSON,
        symptom="FFmpeg зависает на 0% в фоне при чтении stdin",
        root_cause="Ядро Linux посылает сигнал SIGTTIN фоновому процессу, читающему stdin",
        solution="Обязательный флаг -nostdin и stdin=subprocess.DEVNULL при запуске процесса",
        rationale="Исключает блокировку фонового воркера сигналом терминала ОС",
    )
    defaults.update(overrides)
    return CardRecordRequest(**defaults)


def test_record_with_task_id_writes_frontmatter(service: KnowledgeService):
    """
    Вход: record_knowledge с task_id.
    Выход: frontmatter карточки содержит task_id, выборка review_knowledge(task_id=...) находит её.
    """
    resp = service.record(_make_req(task_id="task-review-1"))
    assert resp["status"] == "created"

    fm, _ = parse_card_markdown(Path(resp["path"]).read_text(encoding="utf-8"))
    assert fm.task_id == "task-review-1"

    result = service.review(task_id="task-review-1")
    assert result["candidates_count"] == 1
    assert result["candidates"][0]["id"] == resp["id"]
    assert result["candidates"][0]["task_id"] == "task-review-1"


def test_review_default_window_excludes_old_cards(service: KnowledgeService):
    """
    Вход: карточка, созданная три дня назад (created_at в прошлом).
    Выход: дефолтное окно 24ч не находит старую карточку, но находит сегодняшнюю.
    """
    old_id = "audio-old-sigttin"
    old_content = serialize_card_markdown(
        card_id=old_id,
        req=_make_req(),
        created_at=(date.today() - timedelta(days=3)).isoformat(),
    )
    category_dir = service.knowledge_dir / "audio"
    category_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(category_dir / f"{old_id}.md", old_content)

    fresh = service.record(
        _make_req(
            title="Свежий урок про SIGTTIN",
            symptom="Фоновый ffmpeg процесс останавливается по сигналу от терминала",
            slug="sigttin-fresh",
        )
    )

    result = service.review()
    ids = [c["id"] for c in result["candidates"]]
    assert fresh["id"] in ids
    assert old_id not in ids


def test_review_flags_plan_markers_and_thin_solution(service: KnowledgeService):
    """
    Вход: карточка с маркерами плана (TODO/чек-лист) и коротким решением.
    Выход: flags содержит plan_markers и thin_solution, plus unverified_auto.
    """
    resp = service.record(
        _make_req(
            title="План работ по рефакторингу TODO",
            symptom="Нужно выполнить TODO чек-лист миграции хранилища",
            root_cause="Технический долг накопился из-за нескольких релизов без ревью архитектуры",
            solution="Выполнить шаг 1 и шаг 2 по списку миграции с проверкой тестов",
            rationale="План фиксирует порядок миграции и снижает риск регрессий",
        )
    )

    result = service.review(ids=[resp["id"]])
    card = result["candidates"][0]
    assert "plan_markers" in card["flags"]
    assert "thin_solution" in card["flags"]
    assert "unverified_auto" in card["flags"]


def test_review_similar_hints_for_duplicate_symptoms(service: KnowledgeService):
    """
    Вход: две карточки с пересекающимся симптомом в одной категории.
    Выход: у второй карточки в similar есть первая (подсказка дубля), сама себя не предлагает.
    """
    first = service.record(
        _make_req(symptom="FFmpeg зависает на 0% в фоне при чтении stdin", slug="sigttin-a")
    )
    second = service.record(
        _make_req(
            symptom="FFmpeg зависает на 0% в фоне при чтении stdin без терминала",
            slug="sigttin-b",
            force=True,
            confirmed_force=True,
        )
    )

    result = service.review(ids=[second["id"]])
    similar_ids = [s["id"] for s in result["candidates"][0]["similar"]]
    assert first["id"] in similar_ids
    assert second["id"] not in similar_ids


def test_review_reports_open_hypotheses(service: KnowledgeService):
    """
    Вход: незакрытая гипотеза с task_id.
    Выход: блок open_hypotheses содержит её с напоминанием про resolve_hypotheses.
    """
    service.propose_hypothesis(
        HypothesisRecordRequest(
            title="Проверить влияние WAL",
            category="database",
            symptom="SQLite блокирует запись при параллельном чтении",
            hypothesis="Режим journal по умолчанию блокирует writer при активном reader",
            task_id="task-review-1",
        )
    )

    result = service.review(task_id="task-review-1")
    assert result["open_hypotheses"]["count"] >= 1
    assert result["open_hypotheses"]["reminder"] is not None


def test_review_disputed_card_excluded_from_candidates(service: KnowledgeService):
    """
    Вход: карточка, переведённая в disputed.
    Выход: дефолтное саморевью её не показывает (исключена из активного индекса).
    """
    resp = service.record(_make_req(slug="to-dispute"))
    # K2: draft-карточку сначала продвигаем (promote), затем спорим
    service.update_status(
        CardStatusUpdateRequest(
            id=resp["id"],
            action="promote",
            note="Продвинута для проверки dispute",
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )
    service.update_status(
        CardStatusUpdateRequest(
            id=resp["id"],
            action="dispute",
            note="Карточка не подтверждается кодом: воспроизведение не удалось",
            by="human",
        )
    )

    result = service.review()
    ids = [c["id"] for c in result["candidates"]]
    assert resp["id"] not in ids


def test_review_contract_fields(service: KnowledgeService):
    """
    Вход: пустая база знаний.
    Выход: ответ содержит контракт (rubric, ask_user_template, verdict_actions) и hint.
    """
    result = service.review()
    assert result["status"] == "ok"
    assert "rubric" in result and "ПОЛЬЗА" in result["rubric"]
    assert "ask_user_template" in result
    assert "verdict_actions" in result and "dispute" in result["verdict_actions"]
    assert result["candidates_count"] == 0
    assert result["hint"]


def test_review_user_confirmed_cards_included_only_explicitly(service: KnowledgeService):
    """
    Вход: карточка с source=user_confirmed.
    Выход: по умолчанию исключена из саморевью, включается при include_user_confirmed=True.
    """
    resp = service.record(_make_req(source=KnowledgeSource.USER_CONFIRMED, slug="confirmed"))

    default_result = service.review()
    assert resp["id"] not in [c["id"] for c in default_result["candidates"]]

    extended = service.review(include_user_confirmed=True)
    assert resp["id"] in [c["id"] for c in extended["candidates"]]


def test_review_ids_branch_finds_cards_beyond_recent_window(service: KnowledgeService):
    """
    Регрессия B-1: ветка ids не должна зависеть от лимитов/сортировки list_recent_cards.
    Вход: 55 активных карточек (прямая запись на диск, мимо record), ревью по id самой старой.
    Выход: карточка найдена, а не «не найдена в активном индексе».
    """
    category_dir = service.knowledge_dir / "audio"
    category_dir.mkdir(parents=True, exist_ok=True)
    for i in range(55):
        card_id = f"audio-bulk-card-{i:03d}"
        content = serialize_card_markdown(
            card_id=card_id,
            req=_make_req(title=f"Объёмная карточка номер {i}"),
        )
        _atomic_write_text(category_dir / f"{card_id}.md", content)

    result = service.review(ids=["audio-bulk-card-000", "audio-bulk-card-054"])
    assert result["candidates_count"] == 2
    for card in result["candidates"]:
        assert "error" not in card, card


def test_review_ids_with_unknown_id_reports_error(service: KnowledgeService):
    """
    Вход: явный несуществующий id.
    Выход: карточка-заглушка с error, остальные найденные карточки ревьюятся.
    """
    resp = service.record(_make_req(slug="known-card"))
    result = service.review(ids=["audio-ghost-card", resp["id"]])

    by_id = {c["id"]: c for c in result["candidates"]}
    assert "error" in by_id["audio-ghost-card"]
    assert "error" not in by_id[resp["id"]]
    assert "flags" in by_id[resp["id"]]


def test_review_flags_chernovik_and_checklist(service: KnowledgeService):
    """
    Регрессия B-2: русское «черновик» и английский checklist тоже ловятся plan_markers.
    """
    resp_ru = service.record(
        _make_req(
            title="Черновик решения по кэшу",
            symptom="Кэш инвалидируется слишком часто при записи метаданных",
            root_cause="Ключ кэша не учитывает версию схемы и инвалидируется на каждую запись",
            solution="Добавить версию схемы в ключ кэша и инвалидировать только при её смене",
            rationale="Устраняет лишние перестроения индекса при стабильной схеме",
            slug="draft-marker",
        )
    )
    resp_en = service.record(
        _make_req(
            title="Checklist для деплоя",
            symptom="Деплой пропускает обязательные шаги проверки окружения",
            root_cause="Нет формализованного списка шагов проверки перед продакшн-релизом",
            solution="Формализовать обязательные шаги проверки в CI-пайплайне перед релизом",
            rationale="Снижает риск пропуска критичных проверок при релизе",
            slug="checklist-marker",
        )
    )

    result = service.review(ids=[resp_ru["id"], resp_en["id"]])
    by_id = {c["id"]: c for c in result["candidates"]}
    assert "plan_markers" in by_id[resp_ru["id"]]["flags"]
    assert "plan_markers" in by_id[resp_en["id"]]["flags"]


def test_review_flag_stale_after_expired(service: KnowledgeService):
    """
    Вход: карточка с просроченным stale_after (вчера).
    Выход: флаг stale_after_expired.
    """
    resp = service.record(
        _make_req(
            stale_after=(date.today() - timedelta(days=1)).isoformat(),
            slug="stale-card",
        )
    )

    result = service.review(ids=[resp["id"]])
    assert "stale_after_expired" in result["candidates"][0]["flags"]


def test_review_verified_card_has_no_unverified_auto_flag(service: KnowledgeService):
    """
    Вход: карточка прошла verify (verified_count > 0).
    Выход: флаг unverified_auto отсутствует.
    """
    resp = service.record(_make_req(slug="verified-card"))
    # K2: draft → promote → verify
    service.update_status(
        CardStatusUpdateRequest(
            id=resp["id"],
            action="promote",
            note="Продвинута для проверки verify",
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )
    service.update_status(
        CardStatusUpdateRequest(
            id=resp["id"],
            action="verify",
            note="Подтверждено на стенде: Python 3.12, Linux 6.x, воспроизводится фикс",
            by="human",
            source=KnowledgeSource.USER_CONFIRMED,
        )
    )

    result = service.review(ids=[resp["id"]])
    assert "unverified_auto" not in result["candidates"][0]["flags"]


def test_review_global_open_hypotheses_without_task_id(service: KnowledgeService):
    """
    Вход: гипотеза с task_id, вызов review без селекторов.
    Выход: глобальный список открытых гипотез всё равно подсвечен.
    """
    service.propose_hypothesis(
        HypothesisRecordRequest(
            title="Гипотеза без ревью-селектора",
            category="backend",
            symptom="Счётчик телеметрии не растёт при повторных вызовах",
            hypothesis="Запись в tool_calls подавляется исключением в finally",
            task_id="task-orphan-1",
        )
    )

    result = service.review()
    assert result["open_hypotheses"]["count"] == 1
    assert result["open_hypotheses"]["items"][0]["id"]
    assert result["open_hypotheses"]["items"][0]["task_id"] == "task-orphan-1"


def test_review_since_with_datetime_truncated_to_date(service: KnowledgeService):
    """
    Вход: since с полным datetime.
    Выход: фильтр честно работает на уровне дня (время отбрасывается, selection эхает дату).
    """
    fresh = service.record(_make_req(slug="since-datetime"))

    old_id = "audio-since-old"
    old_content = serialize_card_markdown(
        card_id=old_id,
        req=_make_req(title="Старая карточка вне окна"),
        created_at=(date.today() - timedelta(days=5)).isoformat(),
    )
    _atomic_write_text(service.knowledge_dir / "audio" / f"{old_id}.md", old_content)

    from datetime import datetime

    now_iso = datetime.now().isoformat(timespec="seconds")
    result = service.review(since=now_iso, include_user_confirmed=True, limit=50)
    ids = [c["id"] for c in result["candidates"]]
    assert fresh["id"] in ids
    assert old_id not in ids
    # Эхо селекции — только дата, без времени.
    assert result["selection"] == f"created_at >= {date.today().isoformat()}"


def test_review_invalid_since_raises_value_error(service: KnowledgeService):
    """
    Вход: мусорная строка в since.
    Выход: явная ValueError (на уровне MCP превратится в INVALID_ARGUMENTS), а не пустая выборка.
    """
    with pytest.raises(ValueError, match="Некорректный формат since"):
        service.review(since="garbage-not-a-date")


def test_review_plan_markers_in_root_cause(service: KnowledgeService):
    """
    Вход: маркер плана спрятан только в root_cause.
    Выход: флаг plan_markers срабатывает (первопричина тоже сканируется).
    """
    resp = service.record(
        _make_req(
            title="Миграция хранилища конфигов",
            symptom="Конфиги размазаны по нескольким файлам и рассинхронизируются",
            root_cause="TODO: план работ по миграции конфигов не завершён, часть файлов осталась в легаси",
            solution="Свести конфиги в единый источник с валидацией схемы при старте приложения",
            rationale="Единый источник устраняет рассинхронизацию конфигов между сервисами",
            slug="plan-in-root-cause",
        )
    )

    result = service.review(ids=[resp["id"]])
    assert "plan_markers" in result["candidates"][0]["flags"]


def test_review_thin_solution_flag_on_empty_solution(service: KnowledgeService):
    """
    Вход: ручной md-файл с пустой секцией решения.
    Выход: thin_solution флагуется (гейт длин обходится ручными файлами).
    """
    card_id = "audio-empty-solution"
    content = serialize_card_markdown(card_id=card_id, req=_make_req())
    # Вырезаем содержимое секции решения до пустой строки.
    content = content.replace(
        "### Решение / Выводы",
        "### Пусто / Выводы",
    ).replace(_make_req().solution, "")
    _atomic_write_text(service.knowledge_dir / "audio" / f"{card_id}.md", content)

    result = service.review(ids=[card_id])
    assert "thin_solution" in result["candidates"][0]["flags"]


def test_review_task_id_and_since_combined_as_and(service: KnowledgeService):
    """
    Вход: карточка с task_id, созданная 5 дней назад (прямой write), и свежая с тем же task_id.
    Выход: task_id + since(сегодня) комбинируются как AND — старая исключена, свежая включена.
    """
    fresh = service.record(_make_req(slug="and-fresh", task_id="task-and"))

    old_id = "audio-and-old"
    old_content = serialize_card_markdown(
        card_id=old_id,
        req=_make_req(title="Старая карточка с task_id", task_id="task-and"),
        created_at=(date.today() - timedelta(days=5)).isoformat(),
    )
    _atomic_write_text(service.knowledge_dir / "audio" / f"{old_id}.md", old_content)

    result = service.review(task_id="task-and", since=date.today().isoformat())
    ids = [c["id"] for c in result["candidates"]]
    assert fresh["id"] in ids
    assert old_id not in ids
    assert result["selection"] == f"task_id='task-and' AND created_at >= {date.today().isoformat()}"


def test_review_hint_when_all_ids_not_found(service: KnowledgeService):
    """
    Вход: все запрошенные ids не существуют.
    Выход: hint объясняет, что ID не найдены (вытеснены/оспорены/не существуют).
    """
    result = service.review(ids=["audio-ghost-1", "audio-ghost-2"])
    assert result["candidates_count"] == 2
    assert all("error" in c for c in result["candidates"])
    assert result["hint"] and "не найден" in result["hint"]
