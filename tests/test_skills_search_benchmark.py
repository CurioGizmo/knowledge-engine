"""Golden Search Benchmark для проверки релевантности и ранжирования скиллов."""

from pathlib import Path
import pytest

from mcp_skills.storage import SkillsStorage
from mcp_skills.synchronizer import SkillsSynchronizer


# Канонический набор тестовых запросов и ожидаемых Top-1 скиллов
GOLDEN_SKILLS_BENCHMARK = [
    ("как ускорить запросы и базу данных", "sqlite-performance"),
    ("проверь код и найди баги", "code-review"),
    ("подбери цвета и красивый дизайн для интерфейса", "ui-ux-pro-max"),
    ("научи тренировать голос лору", "profile-lora-training"),
    ("собрать инсталлятор под винду", "windows-packaging-ci"),
    ("почему падает тест, найди причину", "systematic-debugging"),
    ("напиши тесты", "add-tests"),
    ("медленные запросы sqlite database is locked", "sqlite-performance"),
    ("спланируй разработку фичи", "writing-plans"),
    ("распознавание речи зависает", "onnx-whisper-engine"),
    ("подготовка релиза и версионирование", "changelog-automation"),
    ("ревью пулл реквеста", "code-review"),
    ("технический долг и рефакторинг", "technical-debt-management"),
    ("диаризация и профили спикеров", "speaker-enrollment-patterns"),
    ("управление зависимостями uv venv", "uv-usage"),
    ("tauri ipc асинхронные команды", "tauri-ipc-patterns"),
]


def test_hermetic_benchmark_fixtures(tmp_path: Path) -> None:
    """Герметичный benchmark на изолированных фикстурах (гарантирует CI Recall@1 = 1.0)."""
    db_path = tmp_path / "skills_fts.db"
    storage = SkillsStorage(db_path)

    # Создаём репрезентативный набор карточек
    from mcp_skills.models import SkillCard

    cards = [
        SkillCard(
            name="db-performance",
            title="High-Performance SQLite & Storage Engineering",
            description="Оптимизация SQLite и хранилища: режим WAL, busy_timeout, FTS5, внешние ключи, bulk-вставки, индексация и устранение взаимных блокировок.",
            category="backend-db",
            stack=["sqlite", "sqlalchemy", "sql"],
            aliases=[
                "ускорить базу данных",
                "оптимизация sql",
                "запросы к бд",
                "тюнинг sqlite",
                "медленные запросы",
            ],
        ),
        SkillCard(
            name="code-review",
            title="Toxic Senior Code Review Standard",
            description="Строгий сеньор-аудит диффов по Scope Cascade: соблюдение границ скоупа, обязательное покрытие тестами, проверка инвариантов и авто-фиксы.",
            category="quality-review",
            stack=["code-review", "git"],
            aliases=[
                "проверить код",
                "найти баги",
                "аудит диффов",
                "код ревью",
                "ревью пулл реквеста",
            ],
        ),
        SkillCard(
            name="ui-ux-pro-max",
            title="UI/UX Pro Max Design System",
            description="Проектирование интерфейсов и дизайн-систем: гармоничные палитры, типографика, доступность WCAG, компоненты Tailwind и эргономика UI.",
            category="frontend",
            stack=["react", "tailwind", "css", "design-system"],
            aliases=[
                "подбери цвета",
                "красивый дизайн",
                "сделай красиво",
                "дизайн интерфейса",
                "палитра",
            ],
        ),
        SkillCard(
            name="profile-lora-training",
            title="Whisper Profile LoRA Training",
            description="Дообучение LoRA-адаптеров для моделей Whisper: подготовка аудиосэмплов, PEFT/PyTorch пайплайны, конвертация весов в GGUF/GGML.",
            category="audio-ml",
            stack=["pytorch", "lora", "peft", "whisper", "transformers"],
            aliases=[
                "тренировать голос",
                "обучение лоры",
                "whisper finetuning",
                "адаптеры речи",
                "дообучение модели",
            ],
        ),
        SkillCard(
            name="windows-packaging-ci",
            title="Windows Desktop Packaging & CI/CD",
            description="Сборка и дистрибуция Windows-приложений: NSIS-инсталляторы, линковка DLL, подписание бинарников и релизные пайплайны CI/CD.",
            category="desktop",
            stack=["windows", "nsis", "tauri", "ci-cd"],
            aliases=[
                "собрать инсталлятор под винду",
                "windows установщик",
                "exe файл",
                "упаковка релиза windows",
            ],
        ),
        SkillCard(
            name="systematic-debugging",
            title="Systematic Debugging Protocol",
            description="Методология локализации первопричины: 4-фазный цикл расследования, анализ конкурирующих гипотез ACH, устранение плавающих сбоев.",
            category="quality-review",
            stack=["debugging", "root-cause"],
            aliases=[
                "почему падает",
                "не работает",
                "найти причину",
                "баг не воспроизводится",
                "флаки тестов",
            ],
        ),
        SkillCard(
            name="add-tests",
            title="Test-Driven Development Standard",
            description="Разработка тестов по методологии TDD: модульное и интеграционное тестирование на Pytest и Vitest, контракты интерфейсов.",
            category="quality-review",
            stack=["pytest", "vitest", "tdd"],
            aliases=["написать тесты", "покрытие тестами", "unit тесты", "контракты тестов"],
        ),
        SkillCard(
            name="writing-plans",
            title="Writing Comprehensive Implementation Plans",
            description="Детализированное проектирование планов реализации: 3-уровневая декомпозиция задач, фиксация контрольных точек верификации.",
            category="planning",
            stack=["planning", "tdd"],
            aliases=["составь план", "план задачи", "проработай архитектуру", "планирование фичи"],
        ),
    ]

    for card in cards:
        storage.index_skill(
            card, content="Полный подробный контент скилла с инструкциями и примерами кода."
        )

    # Проверяем все запросы из фикстур
    test_cases = [
        ("как ускорить запросы и базу данных", "db-performance"),
        ("проверь код и найди баги", "code-review"),
        ("подбери цвета и красивый дизайн для интерфейса", "ui-ux-pro-max"),
        ("научи тренировать голос лору", "profile-lora-training"),
        ("собрать инсталлятор под винду", "windows-packaging-ci"),
        ("почему падает тест, найди причину", "systematic-debugging"),
        ("напиши тесты", "add-tests"),
        ("спланируй разработку фичи", "writing-plans"),
    ]

    for query, expected_name in test_cases:
        res = storage.search_skills(query, limit=3)
        assert res, f"Запрос '{query}' вернул пустой результат"
        assert res[0].name == expected_name, (
            f"Запрос '{query}': ожидался Top-1 '{expected_name}', получен '{res[0].name}' (score={res[0].score})"
        )
        assert res[0].score is not None and res[0].score > 0.8


@pytest.mark.parametrize("query,expected_name", GOLDEN_SKILLS_BENCHMARK)
def test_real_repo_golden_benchmark(query: str, expected_name: str) -> None:
    """Интеграционный benchmark по реальному репозиторию mcp_skills (если доступен на машине)."""
    from tests.conftest import get_companion_skills_dir

    _cat = get_companion_skills_dir()
    skills_repo = (
        _cat if _cat is not None else (Path(__file__).resolve().parents[1].parent / "mcp_skills")
    )
    db_path = skills_repo / ".agents" / ".cache" / "skills_fts.db"
    if not db_path.exists():
        pytest.skip(f"База скиллов {db_path} не найдена, пропуск интеграционного бенчмарка")

    storage = SkillsStorage(db_path)
    if not storage.list_all_skills():
        from mcp_skills.synchronizer import SkillsSynchronizer

        sync = SkillsSynchronizer(storage, [skills_repo / "skills"])
        sync.sync()
    res = storage.search_skills(query, limit=3)
    assert res, f"Запрос '{query}' вернул пустой результат"
    top = res[0]
    assert top.name == expected_name, (
        f"Запрос '{query}': ожидался Top-1 '{expected_name}', но получен '{top.name}' (score={top.score})"
    )
    assert top.score is not None and top.score > 0.8
