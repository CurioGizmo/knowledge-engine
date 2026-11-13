"""Справочные материалы для инструмента get_help в MCP Skills Engine.

Источник справки — docs/help/<tool>.md в репозитории скиллов (контент,
редактируется без релиза движка). Встроенный словарь SKILLS_HELP_DOCS —
fallback при отсутствии файлов. Докстринги тулз остаются короткими
(экономия токенов schema), расширенные протоколы LLM запрашивает через get_help.
"""

from pathlib import Path

__all__ = ["SKILLS_HELP_DOCS", "get_skills_help_text"]

SKILLS_HELP_DOCS: dict[str, str] = {
    "overview": (
        "=== Skills Engine: рабочие паттерны (5 инструментов) ===\n\n"
        "Выбор скилла под задачу:\n"
        "  search_skills() → search_skills(category='...') → load_skill(name, stack)\n"
        "  Поиск автоматически скрывает карточки под стек проекта; stack_values в ответе подскажет оси сборки; при сомнении в механике — get_help('load_skill')\n\n"
        "Поиск по теме (симптом, технология):\n"
        "  search_skills(query='git' | 'postgres' | 'диаризация') — FTS по тегам, описаниям и контенту\n\n"
        "Сборка под окружение проекта:\n"
        "  load_skill(name, stack=['python', 'fastapi']) — инструкции под фактический стек проекта\n"
        "  load_skill(name, stack=['ruby']) — тот же скилл для другого языка: каркас общий, блоки другие\n"
        "  load_skill без stack — автодетект из окружения проекта (манифесты + режим); источник см. в stack_source ответа\n"
        "  slot_variants в ответе показывает, какие вариации есть у данного скилла\n\n"
        "Работа с внешним сервисом vs локальные файлы:\n"
        "  если сервис/инструмент доступен — stack=['mcp'] даёт протокол через его вызовы\n"
        "  если недоступен — stack=['offline'] даёт файловый протокол (без вызовов)\n"
        "  один и тот же скилл, выбор зависит от того, что есть в текущем окружении\n\n"
        "Агенты под OpenCode (bundle_agent target='opencode'):\n"
        "  СТРОГИЙ read-only профиль (write=false) БЕЗ подтверждения отклоняется при экспорте\n"
        "  (R2, ревизия 4): запрет неизвестных инструментов через tools: {'*': false} не гарантирован\n"
        "  семантикой клиента. Экспорт возможен только с readonly_acknowledged: true —\n"
        "  подтверждением владельца фактической проверки read-only прав в целевой версии OpenCode;\n"
        "  тогда tools: {'*': false, <проверенные read-only>: true} — deny-by-default;\n"
        "  отсутствие ключа ≠ запрет, поэтому первым идёт wildcard-запрет ВСЕХ инструментов;\n"
        "  расширение списка разрешённых — только после живой проверки в OpenCode;\n"
        "  task: deny при subagents=false И при write=false (субагент не наследует ограничения родителя);\n"
        "  при write=true+subagents=false tools.task также явно false (права согласованы в обоих полях);\n"
        "  edit/bash=deny при write=false. Приоритет правил '*' vs точечных зависит от версии клиента —"
        " проверяйте в выбранной версии OpenCode.\n\n"
        "Авторинг нового скилла:\n"
        "  get_help('create_skill') — туториал структуры (каркас + блоки + конвенции),\n"
        "  затем create_skill(name, content, blocks, stack_descriptions, ...)\n\n"
        "Пошаговое выполнение флоу:\n"
        "  review-flow — ревью плана или кода; execute-plan-flow — исполнение плана\n"
        "  step_flow(flow='execute-plan-flow', flow_options={...}) — старт; step_flow(action='status') — текущий шаг\n"
        "  mcp_audit — источник плана, шагов и статусов, если доступен\n\n"
        "Комбинирование осей:\n"
        "  load_skill(name, stack=['python', 'fastapi', 'mcp']) — значения комбинируются,\n"
        "  каждое подхватывается теми слотами, где есть блок"
    ),
    "search_skills": (
        "=== search_skills: каталог, drilldown и FTS-поиск ===\n\n"
        "Режимы (Progressive Disclosure — минимальные токены на каждом уровне):\n"
        "- Zero-query: {mode: overview} — категории со счётчиками, flows {name, description}, "
        "stack_values {значение: когда использовать}. ~800-900 токенов.\n"
        "- category='...' без query: {mode: category} — карточки {name, title, description} "
        "скиллов категории + stack_values. Лёгкий drilldown для выбора скилла.\n"
        "- query='...': FTS5-поиск по name/title/description/stack/content с BM25-ранжированием. "
        "Тематические теги (git, postgres, testing) — это ПАРАМЕТРЫ ПОИСКА, не значения стека.\n"
        "- type='flow': публичные входные флоу; с query — поиск по алиасам с арбитражем неоднозначности.\n"
        "  В области ревью и исполнения плана обычные входы: review-flow и execute-plan-flow;\n"
        "  internal/deprecated wrappers доступны прямым вызовом, но скрыты из поиска.\n\n"
        "Поиск автоматически скрывает карточки, привязанные к чужому стеку (requires_stack) "
        "или несовместимому режиму (activation); блок stack_filter в ответе показывает применённый стек проекта.\n\n"
        "Ответ search-режима отдаёт карточки с непустыми значениями (дефолтные поля опущены — "
        "Progressive Disclosure; всегда присутствуют name, title, category/categories, score); "
        "усечение длинного контента возможно только на стороне клиента."
    ),
    "load_skill": (
        "=== load_skill: сборка скилла под стек ===\n\n"
        "Механика:\n"
        "- touches=[...] — выбор секций; stack=[...] — технологии, НЕ имена слотов.\n"
        "- optional_slots из frontmatter не включаются по умолчанию; доступны через touches.\n"
        "- SLOT: первый вариант; SLOT+: все совпадения. Локальный tech → общий tech; затем local/shared default.\n"
        "- slot_variants включает локальные и общие блоки; available_slots — все секции.\n"
        "- Каркас SKILL.md содержит маркеры <!-- SLOT: <slot> --> в местах вариаций.\n"
        "- stack=[...] — ЗНАЧЕНИЯ осей (не слоты): движок ищет blocks/<slot>/<value>.md "
        "в каждом слоте; нет файла — default.md; нет default — заглушка.\n"
        "- Ответ: content (собранный markdown), resources (список связанных справочников/шаблонов), "
        "slots_filled (только реально заполненные секции), assembly (манифест сборки: requested_stack, "
        "matched_technologies, unmatched_technologies, sections с источниками matched/default/missing, "
        "selected_resources, revision, status), "
        "stack_applied (эффективный входной стек; не гарантирует наличие конкретного блока технологии — "
        "см. assembly.matched_technologies), slot_variants {slot: [values]}.\n"
        "- status: 'complete' (все слоты заполнены) или 'incomplete' (с missing_slots при отсутствии блоков).\n"
        "- was_truncated: true — контент превышает бюджет TokenGuard, опущены примеры. "
        "При превышении бюджета обязательным ядром возвращается отказ ASSEMBLY_BUDGET_EXCEEDED "
        "с оценкой и remediation (хвостовые правила не отрезаются молча).\n"
        '- stack не указан (None или "") — движок автодетектит язык/фреймворк из '
        "манифестов проекта и режим (MCP_ACTIVE_SERVERS env → .agents-маркеры); "
        "stack=[] — явный пустой стек (без автодетекта технологий); "
        'источник стека — поле stack_source ("explicit" / "autodetect" / null) в ответе; '
        "явный стек не перекрывается и не дополняется технологиями; исключение — "
        "ось режима: без mcp/offline в стеке добавляется подтверждённый режим проекта "
        "(stack_supplemented).\n\n"
        "Доставка справочников и шаблонов: load_skill(name='<owner>', resource_id='path/to/file.md[#anchor]', cursor=None).\n"
        "В режиме ресурса stack и touches запрещены. При больших текстах ответ содержит "
        "next_cursor и revision для продолжения чтения. Передавай next_cursor без изменений для того же "
        "resource_id, якоря и ревизии; при смене ревизии начни с cursor=None. "
        "Центральный ID относителен каталогу; project-skills/<path> — .agents/skills/ проекта. "
        "Читай необходимые страницы до конца выбранной области; первая страница не равна полному ресурсу.\n\n"
        "Значения стека — словарь ОБЩИЙ для движка (см. stack_values в overview): "
        "python/ruby — язык, mcp/offline — режим работы, fastapi/react — фреймворк. "
        "Не выдумывай значения: используй stack_values или slot_variants. "
        "stack принимает список или CSV-строку."
    ),
    "create_skill": (
        "=== create_skill: авторинг модульных скиллов ===\n\n"
        "Скилл = каркас + блоки + конвенции стека. Всё передаётся одной тулзой.\n\n"
        "1. КАРКАС (content): инварианты, триггеры, общий текст. В местах вариаций — "
        "маркеры <!-- SLOT: <slot> -->. Стековая специфика в каркас НЕ входит.\n"
        "   Пиши имя слота kebab-case; слот = осмысленная точка вариации (mode, core, integration).\n\n"
        "2. БЛОКИ (blocks: {slot: {value: markdown}}): файлы blocks/<slot>/<value>.md. "
        "   value=default — fallback без стека; остальные значения = оси сборки "
        "(mcp/offline/python/fastapi/...). Контент блока — самодостаточный markdown-фрагмент "
        "для подстановки на место маркера.\n\n"
        "3. СТЕК И КОНВЕНЦИИ: stack=['python','fastapi'] — оси скилла; новые значения "
        "автоматически попадают в docs/stack-conventions.yaml. stack_descriptions="
        "{value: 'когда применять'} — семантика осей: передавай для новых, "
        "существующая семантика не перетирается.\n\n"
        "Паттерн дуального режима (mcp/offline): если скилл работает и через "
        "MCP-инструменты, и через файлы — слот mode (или integration) с блоками "
        "mcp.md, offline.md и default.md (зонд: проверить доступность MCP и выбрать).\n\n"
        "Правила:\n"
        "- available_slots — имена слотов из маркеров каркаса (для индексации).\n"
        "- description в frontmatter — условие активации: по нему LLM находит скилл через search.\n"
        "- aliases — при создании сформируй из назначения скилла короткие поисковые формулировки "
        "на английском и языке пользователя (обычно 4–8 суммарно; для англоязычного пользователя — "
        "только английский) и передай в этом же вызове. Используй действия, симптомы и технические "
        "синонимы по содержанию скилла; избегай общих слов, дублей и перечисления всего стека. "
        "Названия технологий сохраняй в принятом написании. При обновлении сохраняй подходящие "
        "существующие алиасы. Лимит: 30 алиасов, до 100 символов каждый.\n"
        "- После создания продолжай основную задачу; не вызывай search_skills только для проверки "
        "алиасов. Качество поиска проверяется отдельно при тестировании MCP и каталога.\n"
        "- Update: overwrite=True (перепишет SKILL.md и указанные блоки; конвенции — только дополнение).\n"
        "- Валидация движка: имена kebab-case через is_safe_name, SpamGate на контент (секреты, 100 КБ).\n\n"
        "Эталон структуры (dual-mode): skills/general/technical-debt-management — "
        "каркас + blocks/mode/{default,mcp,offline}.md."
    ),
    "step_flow": (
        "=== step_flow: фасад управления флоу ===\n\n"
        "Действия (Decision Table):\n"
        "- status: текущая инструкция активной сессии (read-only safe).\n"
        "- advance + flow='review-flow'|'execute-plan-flow': старт сессии; advance без flow: следующий шаг "
        "после deliverable/verdict.\n"
        "- flow_options: настройки только при старте, сохраняются на всю сессию.\n"
        "- transition: именованный переход, объявленный текущим этапом; возможен только при advance.\n"
        "  С needs_work разрешён только при allow_needs_work: true в metadata перехода.\n"
        "- retry: повтор текущего этапа.\n"
        "- abort: отмена сессии.\n"
        "- escalation_resolve (resolution: override_approved|retry|abort): снятие Circuit Breaker.\n"
        "Мутирующие действия требуют закреплённого корня проекта; expected_stage_id — "
        "защита от рассинхрона этапов. Публикация в execute-plan-flow требует "
        "явного human_confirmed=true после согласования пользователем в любом режиме."
    ),
}


def get_skills_help_text(tool_name: str | None = None, skills_dir: Path | None = None) -> str:
    """Возвращает справку по тулзе; без аргумента — обзорную.

    Источник — docs/help/<tool>.md в репозитории ДВИЖКА (справка описывает
    тулзы движка и меняется синхронно с его кодом; репозиторий скиллов
    — чистый контент, с движком не связан). Встроенный SKILLS_HELP_DOCS —
    fallback при отсутствии файлов, чтобы get_help никогда не падал.
    ``skills_dir`` в сигнатуре сохранён для совместимости, источником не является.
    """
    key = (tool_name or "overview").strip().lower()
    if key not in SKILLS_HELP_DOCS:
        avail = ", ".join(sorted(SKILLS_HELP_DOCS))
        return (
            f"Справка для '{tool_name}' не найдена. Доступны: {avail}. "
            "Вызовите get_help() без аргументов для обзора."
        )
    # src/mcp_skills/help_docs.py -> src/ -> корень репозитория движка
    engine_root = Path(__file__).resolve().parent.parent.parent
    path = engine_root / "docs" / "help" / f"{key}.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return SKILLS_HELP_DOCS[key]
