"""Справочные материалы, протокольные тексты и руководство для инструмента get_help в MCP Knowledge Engine."""

AGENT_RULE_TEMPLATE = """# Knowledge Engine Protocol (Институциональная память AI-агентов)
Устарело: правила в .agents/rules/ упразднены в пользу скилла .agents/skills/knowledge-memory/.
"""

SKILL_TEMPLATE = """---
name: knowledge-memory
description: >-
  Long-term project memory via MCP knowledge-engine.
  Apply before designing features, debugging, reverse engineering, or investigating subtle bugs:
  search for past solutions and antipatterns. When encountering non-trivial forks, record hypotheses;
  persist only institutional knowledge valuable beyond the current task.
---

# Knowledge Engine Lifecycle

This skill defines the 3-phase lifecycle for managing long-term project memory via the `knowledge-engine` MCP server (8 native tools).

> 💡 **Plans and Tasks:** Working plans and task tracking are NOT stored in knowledge base cards. While the MCP server is available, maintain plans exclusively via `audit-engine` API tools (`record_plan` / `update_plan` / `step_plan` / `record_review`) — this is the Single Source of Truth. The file `docs/plans/<date>-<slug>.md` is an explicit offline fallback used only when the API server is unavailable: once MCP access is restored, import the plan into the server and reindex, avoiding duplicate sources.

## 🛫 1. Pre-check (Step 0: Preparation and Exploratory Search)

Before writing code, changing architecture, or planning:
1. **Project Knowledge Overview (Zero-query):**
   Call `search_knowledge()` without parameters to inspect the current topic summary (Project Overview) and category distribution.
2. **Faceted Search by Task and Category:**
   Call `search_knowledge` specifying target categories (`categories`):
   ```python
   search_knowledge(query="feature name or error message", categories=["backend", "database"])
   ```
3. **ALWAYS check the `antipatterns` section:**
   If the search results include antipatterns (`⛔ REFUTED`), inspect their root causes to avoid repeating dead-end approaches and disproven hypotheses.
4. **Inspect matched cards** via `get_knowledge(ids=[...])` (up to 10 cards at once) and account for them in implementation plans or reviews.

## 🔬 2. Investigation & Debugging (Step 1: Working Hypotheses During Research and Debugging)

> **Mandatory Rule:** Never keep potential root causes or implementation alternatives solely in hidden internal monologue (CoT). If a bug or task has > 1 possible cause, subtle failure mode, architectural fork, or requires exploratory solutions — immediately record hypotheses via `propose_hypothesis`.

If you explain to the user or yourself *why* a failure occurred, compare alternatives, or test a technical solution — that is a working hypothesis:

```python
propose_hypothesis(
    title="Brief theory / approach name",
    category="backend",
    symptom="Observed behavior / problem being solved",
    hypothesis="Why it happens / core idea of proposed solution",
    test_plan="How to verify it worked",  # optional
    task_id="my-task-123",  # optional, recommended for grouping
)
```

❌ **DO NOT create hypotheses (skip this step):**
- Fixing trivial typos, file paths, syntax errors;
- Routine CRUD code and basic business logic without trade-offs;
- Mechanical edits and refactorings requested directly by the user.

**Rapid Feedback Loop:**
- **Refuted:** As soon as a test or log demonstrates a false lead — immediately call `resolve_hypotheses(task_id="...", resolutions=[{"id": "...", "action": "refute", "refutation_reason": "..."}])`. The card is immediately recorded as an antipattern (`⛔`), preventing repeated attempts.
- **Confirmed:** Promote to a permanent lesson via `action="confirm"`, supplying `solution`.
- Multiple competing hypotheses can share the same `task_id` (e.g., hypothesis A and hypothesis B).

## 🛬 3. Resolution & Summary (Step 2: Recording Engineering Decisions and Lessons Learned)

The knowledge base is long-term institutional memory, NOT a work report or a bug tracker!

> 💡 **The normal outcome of a standard task is 0 new cards.** Clean code, tests, and commit messages fully document standard development work. Create a card ONLY if the gained experience must outlive the current session and protect future developers from hidden traps or architectural regressions.

1. **Resolving Hypotheses (`resolve_hypotheses`) — IF hypotheses were proposed:**
   If hypotheses were recorded via `propose_hypothesis` during debugging, close them by `task_id` (never leave orphaned hypotheses):
   - `action="confirm"`: hypothesis confirmed **and** provides long-term architectural value or subtle lesson → supply `solution` (agent hypotheses remain in `draft` quarantine until human confirmation; human-confirmed cards become active lessons).
   - `action="refute"`: hypothesis disproven → provide `refutation_reason`; with `merge_into`, the reason is appended to the target card's nuances; without it, recorded as an antipattern `⛔`.
   - `action="drop"`: hypothesis was noise, outdated, or trivial (typo, obvious fix) → deletes draft without polluting the database.

   ```python
   resolve_hypotheses(
       task_id="my-task-123",
       resolutions=[
           {
               "id": "backend-cache-race",
               "action": "confirm",
               "solution": "Use atomic CAS flag",
           },
           {
               "id": "backend-null-ptr",
               "action": "refute",
               "refutation_reason": "Trace revealed different root cause: ...",
           },
           {"id": "backend-stale-config", "action": "drop"},
       ],
   )
   ```

2. **Recording Knowledge Cards (`record_knowledge`) — ONLY for Long-Term Knowledge:**
   Persist ONLY what cannot be prevented by clean code and unit tests:
   - **`type="decision"` (Architectural Decisions & ADRs):** team conventions, structural invariants, library/tooling selections.
   - **`type="lesson"` (Lessons Learned & Gotchas):** counterintuitive or dangerous behavior of third-party libraries, OS, runtime, or network that cannot be deduced from reading clean code.
   - **`type="research"` (Research & Reverse Engineering):** analysis results of external protocols, undocumented APIs, or algorithms.
   - **`type="hint"` (Technical Recommendations & Proven Workarounds):** proven platform workarounds, delicate performance tunings.

   ```python
   record_knowledge(
       title="Brief solution or rule title",
       category="backend",
       type="decision",  # "decision" | "lesson" | "research" | "hint"
       context="Problem context or architectural trade-off",  # or symptom + root_cause
       solution="Reproducible solution or rule (>= 50 chars)",
       rationale="Engineering justification (>= 30 chars)",
       task_id="my-task-123",  # optional, for grouping
   )
   ```

   ⛔ **STRICTLY FORBIDDEN to create cards for:**
   - **Work reports:** lists of completed task steps, sprint checklists, changed file lists, session transcripts, or narratives ("Implementation of plan...", "User requested...");
   - **Micro-bugs and trivial typos:** code bugs belong in unit tests (pytest), not knowledge cards;
   - **User developer preferences:** chat language, LLM model choice, brevity style — these belong in the operator's IDE profile, not the repository knowledge base;
   - **Routine CRUD code** and standard business logic.

3. **Self-Reviewing New Cards (`review_knowledge`) — ONLY if new cards were created:**
   - If cards were created in the session via `record_knowledge`, call `review_knowledge(task_id="...")`.
   - The engine checks quality flags (`plan_markers`, `stale_after_expired`, `thin_solution`, `unverified_auto`) and duplicate suggestions (`similar`).
   - Evaluate each card against criteria: value for future agents, specificity, longevity (not a plan!), absence of duplicates.
   - At the slightest suspicion of work reports or noise, reject the card: `update_knowledge_status(action="dispute", note="...")` or delete the file from disk.

4. **Draft Quarantine and Promotion:**
   - Autonomous agent cards (source=auto_inferred) are created with `draft` status and isolated from general search.
   - Locate drafts within current task: `search_knowledge(query="...", task_id="my-task-123")` or `search_knowledge(include_drafts=True, task_id="my-task-123")` — use `include_drafts` ONLY with `task_id`.
   - At task completion, after `review_knowledge`, present candidates to the user and promote only upon explicit approval:
     ```python
     update_knowledge_status(id="...", action="promote", source="user_confirmed", by="human")
     ```
   - Promotion readiness criteria — **absence of red flags** (`plan_markers`, `stale_after_expired`, `thin_solution`); `draft_candidate` / `unverified_auto` flags are applied to EVERY fresh agent card and are not blockers by themselves.
   - `promote` without human confirmation is prohibited: server requires `source="user_confirmed"` or `by="human"` (guard cannot be weakened). Provenance is preserved on promotion (`generated_by` and `confidence` are not overwritten); confirmation is recorded in verified entries and PROMOTED log.
   - Unapproved cards remain in `draft` — this is a healthy protection against noise, not an error.

## 🏁 4. Completion Checkpoint: Final Answer, walkthrough.md, or git commit

Before formulating the final response, `walkthrough.md`, or `git commit`:
1. **Close open hypotheses:** If hypotheses were created under `task_id`, resolve them via `resolve_hypotheses` (confirm, refute, or drop). Never leave hanging hypotheses.
2. **Evaluate necessity of knowledge cards:**
   - Was a fundamental architectural decision (ADR) made?
   - Was a non-obvious third-party library/OS gotcha discovered that cannot be inferred from clean code and tests?
   - If **NO** — do not call `record_knowledge`! Normal task completion consists of clean code and passing tests.
   - If **YES** — record the card via `record_knowledge`, self-review via `review_knowledge(task_id=...)`, present candidates to user and promote on approval (`update_knowledge_status(action="promote", source="user_confirmed", by="human")`); include the created file in the commit.

## 🔄 Card Self-Correction (`update_knowledge_status`)
- `action="verify"`: confirmation of successful production/testbench operation.
- `action="suspect"`: doubt raised by a failure, with symptom described in note (historical version available via include_archived=True).
- `action="append_note"`: add an observation to the card's history log without altering status.
- `action="dispute"`: exclude an erroneous card from search.
- `action="promote"`: advance card from `draft` to active database — ONLY with explicit human confirmation (`source="user_confirmed"`, `by="human"`).

Transition matrix (K2): suspect/verify apply ONLY to active/verified cards:
- `draft` + suspect/verify → ValueError (quarantine: requires `promote` via human consent first);
- `hypothesis` + verify → ValueError (hypotheses are closed via `resolve_hypotheses`: confirm/refute/drop);
- `draft`/`hypothesis` + append_note/promote — allowed.
"""

SERVER_INSTRUCTIONS = (
    "Institutional memory & knowledge base for AI agents (8 native tools). 3-step lifecycle:\n"
    "- Pre-check (Preparation): Call search_knowledge() with no arguments or with categories to inspect the project overview "
    "before planning. Call search_knowledge(query='...') before debugging or implementing features. "
    "Inspect 'antipatterns' block to avoid proven dead-ends. Use get_knowledge(ids=[...]) to read matched cards (up to 10).\n"
    "- Investigation & Debugging (MANDATORY during debugging, research, and non-trivial branches): Do not keep alternative causes "
    "or implementation ideas solely in hidden internal monologue (CoT). If there are competing hypotheses or gotchas, "
    'immediately record them via propose_hypothesis(title="...", category="...", symptom="...", hypothesis="...", task_id="..."). '
    "When a hypothesis is disproven, call resolve_hypotheses(task_id=..., resolutions=[{'id': ..., 'action': 'refute', 'refutation_reason': ...}]) to turn it into an antipattern.\n"
    "- Resolution & Summary (before final answer, walkthrough, or git commit): If hypotheses were proposed, resolve them via resolve_hypotheses(). "
    "Do NOT record work reports or task progress as cards. Persist ONLY long-term architectural decisions (decision/ADR), "
    "runtime/library gotchas (lesson), or workarounds (hint) via record_knowledge(category=...). "
    "Normal tasks result in 0 new cards. If new cards were recorded, run review_knowledge(task_id=...) to dispute any accidental report or noise. "
    "Working plans and reviews belong in task files, not cards."
)

HELP_DOCS: dict[str, str] = {
    "overview": (
        "=== MCP Knowledge Engine: Обзор и жизненный цикл знаний (8 инструментов) ===\n\n"
        "Движок предоставляет долгосрочную память для AI-агентов: архитектурные решения (ADR), проверенные технические рекомендации и воркэраунды, выводы исследований и реверса, подводные камни платформ и библиотек (gotchas).\n\n"
        "💡 ПРАВИЛО ХРАНЕНИЯ: Рабочие планы и задачи ведутся в файлах репозитория (например, docs/plans/), а НЕ в карточках базы знаний. Карточка хранит только стабильный инженерный опыт, ADR и готовые решения.\n"
        "Plan tracking: while the MCP server is available, keep plans in `audit-engine` via its API tools "
        "(record_plan / update_plan / step_plan); a `docs/plans/<date>-<slug>.md` file is an explicit offline "
        "fallback only — after MCP access is restored, re-import it via record_plan (reindex) instead of dual tracking.\n\n"
        "Рекомендуемый 3-фазный цикл работы:\n"
        "1. Pre-check Search (перед началом работы):\n"
        "   - search_knowledge(): просмотр общей сводки тем и структуры проекта (Project Overview);\n"
        "   - ОБЯЗАТЕЛЬНО проверьте блок 'antipatterns' в поисковой выдаче, чтобы не повторять опровергнутых путей;\n"
        "   - get_knowledge(ids=[...]): точечное чтение полных карточек найденных решений (до 10 штук).\n"
        "2. Investigation & Debugging (ОБЯЗАТЕЛЬНО при исследовании, отладке и развилках):\n"
        "   - propose_hypothesis(): запрещено держать версии в скрытом монологе (CoT)! Если >1 причины сбоя, развилка вариантов реализации или поиск неочевидных решений — немедленно зафиксируйте гипотезу;\n"
        "   - при опровержении версии тестом/логом сразу вызовите resolve_hypotheses(task_id=..., resolutions=[{'id': ..., 'action': 'refute', 'refutation_reason': ...}]), чтобы превратить её в антипаттерн (⛔) и исключить повторы.\n"
        "3. Resolution & Summary (перед финальным ответом, walkthrough или коммитом):\n"
        "   - resolve_hypotheses(): закрытие гипотез (ЕСЛИ выдвигались в ходе задачи: confirm -> боевая карточка, refute -> антипаттерн, drop -> удаление). Запрещено оставлять «зависшие» гипотезы!\n"
        "   - record_knowledge(): сохранение долгосрочного инженерного опыта ТОЛЬКО при наличии новых ADR или скрытых ловушек рантайма/библиотек (gotchas). Для 95% обычных задач нормальный результат — 0 карточек;\n"
        "   - review_knowledge(): самопроверка свежих карточек перед коммитом (ТОЛЬКО если вызывался record_knowledge) — проверка по рубрике, отсечение случайного шума.\n"
        "4. Двухэтапная самокоррекция (update_knowledge_status):\n"
        "   - verify (подтверждение на стенде) / suspect (сомнение при сбое) / dispute (оспаривание неверной карточки).\n\n"
        "Для детальной справки по конкретному инструменту вызовите get_help(tool_name='имя_инструмента')."
    ),
    "propose_hypothesis": (
        "=== Справка по инструменту propose_hypothesis ===\n\n"
        "Назначение: Фиксация рабочих гипотез, конкурирующих версий или вариантов реализации во время исследования, отладки и поиска решений (Investigation Recording).\n\n"
        "ОБЯЗАТЕЛЬНО: Если у сбоя > 1 возможной причины, баг неочевиден, или есть развилка архитектурных подходов/инженерных приёмов — ЗАПРЕЩЕНО держать версии только в скрытом CoT. Зафиксируйте их через propose_hypothesis.\n\n"
        "В отличие от record_knowledge, не требует готового решения (solution) и обоснования (rationale).\n"
        "Карточки-гипотезы изолированы от боевого поиска и видны только при include_hypotheses=True или передаче task_id.\n\n"
        "ПАРАМЕТРЫ:\n"
        "- title: Понятный заголовок гипотезы (>= 3 символов)\n"
        "- category: Категория/подсистема ('platform', 'audio', 'ml', 'backend', 'frontend', 'database', 'ui', 'cicd' и др.)\n"
        "- symptom: Наблюдаемый контекст, решаемая задача или симптом (>= 10 символов)\n"
        "- hypothesis: Суть рабочей гипотезы, проверяемого подхода или решения (>= 15 символов)\n"
        "- test_plan: Опциональный план проверки гипотезы\n"
        "- task_id: Идентификатор задачи или исследовательской сессии (обязательно для группировки)\n"
        "- confidence: Начальный вес уверенности (0.0..1.0, по умолчанию 0.20)\n"
        "- tags, aliases, slug: Метаданные для классификации и именования."
    ),
    "resolve_hypotheses": (
        "=== Справка по инструменту resolve_hypotheses ===\n\n"
        "Назначение: Подведение итогов исследования и резолюция пачки рабочих гипотез (двухпроходная валидация).\n\n"
        "ДЕЙСТВИЯ (action):\n"
        "1. 'confirm' - Гипотеза подтвердилась на практике.\n"
        "   - Преобразуется в карточку-урок (type='lesson'); гипотеза агента остаётся в статусе draft\n"
        "     (карантин, не видна в общем поиске) до согласования человеком — перевод в active\n"
        "     выполняется через update_knowledge_status(action='promote') по явному аппруву пользователя;\n"
        "   - Обязательно поле: solution (>= 10 символов, законченное решение);\n"
        "   - Поле rationale опционально;\n"
        "   - Бейдж: 'ok' / 'verified'.\n"
        "2. 'refute' - Гипотеза опровергнута / путь оказался тупиковым.\n"
        "   - Требуется: refutation_reason (>= 15 символов);\n"
        "   - Если указан merge_into='card-id' — аргумент опровержения дописывается в секцию 'Нюансы и ограничения' целевой карточки, а черновик гипотезы удаляется;\n"
        "   - Если без merge_into — карточка переводится в status='refuted' и type='antipattern' (попадает в блок antipatterns поисковой выдачи).\n"
        "3. 'drop' - Черновик гипотезы признан неактуальным/мусорным.\n"
        "   - Физическое удаление (unlink) с диска и очистка из индекса SQLite.\n"
        "4. 'duplicate' - Настоящий дубликат существующей карточки (совпадают симптом и причина).\n"
        "   - Обязательно поле: duplicate_of='card-id' (ID существующей карточки-дубля);\n"
        "   - Закрывает гипотезу как status='superseded' со ссылкой superseded_by=duplicate_of;\n"
        "   - Каноническая карточка не мутируется (включая user_confirmed);\n"
        "   - При отличной причине (distinct) или нехватке данных (uncertain) — отказ без записи."
    ),
    "record_knowledge": (
        "=== Справка по инструменту record_knowledge ===\n\n"
        "Назначение: Сохранение ценного инженерного опыта, неочевидных багов и архитектурных решений.\n\n"
        "💡 ПРАВИЛО ХРАНЕНИЯ: Рабочие планы и задачи ведутся в файлах репозитория (например, docs/plans/), а НЕ в карточках базы знаний. Карточка хранит только проверенный инженерный опыт, ADR и готовые решения.\n"
        "Plan tracking: while the MCP server is available, keep plans in `audit-engine` via its API tools "
        "(record_plan / update_plan / step_plan); a `docs/plans/<date>-<slug>.md` file is an explicit offline "
        "fallback only — after MCP access is restored, re-import it via record_plan (reindex) instead of dual tracking.\n\n"
        "✅ КОГДА ВЫЗЫВАТЬ (достаточно одного условия):\n"
        "- Решение потребовало >2 попыток или глубокой отладки (non-trivial fix);\n"
        "- Обнаружено скрытое поведение ОС, рантайма или сторонней библиотеки (gotcha/trap);\n"
        "- Принят архитектурный выбор/ADR, отказ от которого сломает систему;\n"
        "- Найден надежный воркэраунд для бага, не имеющего быстрого апстрим-исправления.\n\n"
        "❌ КОГДА НЕ ВЫЗЫВАТЬ (будет отклонено):\n"
        "- Личные предпочтения пользователя-разработчика (язык общения в чате, выбор моделей LLM, краткость стиля, экономия токенов — это уровень профиля IDE оператора, а не Git-репозитория проекта. Настройки пользователей самого приложения — разрешены);\n"
        "- Написание обычного CRUD-кода или базовой бизнес-логики;\n"
        "- Исправление опечаток, путей к файлам, синтаксических ошибок;\n"
        "- Добавление стандартных тестов без выявления специфичных граничных случаев.\n\n"
        "ОБЯЗАТЕЛЬНЫЕ ПОЛЯ И МИНИМАЛЬНЫЕ ДЛИНЫ (Anti-Spam Gate):\n"
        "- title: Понятный заголовок (>= 3 символов)\n"
        "- category: Категория/подсистема ('platform', 'audio', 'ml', 'backend', 'frontend', 'database', 'ui', 'cicd' или проектный slug)\n"
        "- type: 'lesson' (урок / gotcha), 'decision' (ADR), 'research' (исследование), 'hint' (рекомендация / воркэраунд)\n"
        "- context: Контекст проблемы / выбора архитектуры (универсальная замена symptom + root_cause)\n"
        "  ИЛИ пара полей:\n"
        "  * symptom: Точные симптомы/ошибка (>= 10 символов)\n"
        "  * root_cause: Технический анализ первопричины (>= 40 символов)\n"
        "- solution: Законченное воспроизводимое решение (>= 50 символов)\n"
        "- rationale: Обоснование правильности / True Correctness Rule (>= 30 символов)\n\n"
        "ОПЦИОНАЛЬНЫЕ ПАРАМЕТРЫ:\n"
        "- force: Игнорировать дубликат при первом вызове (вернет статус confirmation_required)\n"
        "- confirmed_force: Подтверждение записи дубликата (вторая фаза, если force=True запросил подтверждение)\n"
        "- supersedes: ID вытесняемой карточки (переводит её в status=superseded)\n"
        "- related_to: Список ID связанных карточек\n\n"
        "ПРИМЕР ЭТАЛОННОЙ КАРТОЧКИ:\n"
        "title='FFmpeg SIGTTIN freeze in background process'\n"
        "category='audio'\n"
        "type='lesson'\n"
        "symptom='FFmpeg зависает на 0% в фоне без терминала при чтении stdin'\n"
        "root_cause='Ядро Linux посылает сигнал SIGTTIN фоновому процессу, читающему stdin'\n"
        "solution='Обязательный флаг -nostdin и stdin=subprocess.DEVNULL при запуске процесса'\n"
        "rationale='Исключает блокировку фонового воркера сигналом терминала операционной системы'\n"
        "slug='ffmpeg-sigttin'"
    ),
    "update_knowledge_status": (
        "=== Справка по инструменту update_knowledge_status ===\n\n"
        "Назначение: Обратная связь и двухэтапная самокоррекция базы знаний.\n\n"
        "ДОСТУПНЫЕ ДЕЙСТВИЯ (action):\n"
        "1. 'suspect' - Первый сигнал сбоя. Статус меняется на suspect, карточка пессимизируется в поиске.\n"
        "   В note укажите конкретный симптом сбоя. Если карточка ранее вытеснила другую, ответ подскажет старую версию.\n"
        "2. 'verify' - Подтверждение на стенде. Статус меняется на verified, растет verified_count.\n"
        "   Если note содержит содержательное описание условий, оно добавляется в секцию 'Нюансы и ограничения'.\n"
        "3. 'dispute' - Карточка признана неверной/вредной. Статус меняется на disputed, исключается из поиска.\n"
        "   Основной вердикт саморевью review_knowledge для мусора: карточка мгновенно исчезает из поиска.\n"
        "   При полной уверенности файл карточки можно удалить с диска — индекс выздоровеет сам при следующем поиске.\n"
        "4. 'append_note' - Добавление наблюдения в журнал (Log) без изменения статуса.\n"
        "5. 'promote' - Перевод карточки из draft (карантин агентов) в active. ТОЛЬКО по явному подтверждению\n"
        "   человека: сначала покажите карточку пользователю и получите согласие. Никогда не выполняйте promote\n"
        "   по собственной инициативе. При промоушене происхождение карточки сохраняется (generated_by и\n"
        "   confidence не перезаписываются); факт согласования фиксируется в verified-записи и логе PROMOTED.\n"
        "   note опционален только для promote (default: 'Promoted from draft to active'); для остальных\n"
        "   действий содержательный note обязателен.\n\n"
        "ВАЖНЫЕ ПРАВИЛА:\n"
        "- Вытесненные карточки (status=superseded) НЕЛЬЗЯ мутировать. Изменяйте замещающую карточку.\n"
        "- Карточки человека (source=user_confirmed) нельзя оспорить (dispute) в автоматическом режиме (auto_inferred).\n"
        "- Защита от авто-мутаций (KS13 Вариант Б): карточки с source='user_confirmed' и продвинутые карточки\n"
        "  (с action='promote' в verified) защищены от автоматических dispute/verify.\n"
        "- Карантин draft — соглашение о процессе, а НЕ криптографическая гарантия: сервер НЕ отклоняет\n"
        "  подставленные source='user_confirmed'/by='human' (технической границы доверия нет; см. ADR\n"
        "  docs/analysis/adr-promote-consent-boundary.md). Статус 'human' в verified-записи отражает\n"
        "  утверждение вызывающего, реальное согласование остаётся на стороне человека. Единственная\n"
        "  защита — протокол: покажите карточку пользователю и получите явное согласие ДО promote.\n"
        "- Избегайте тривиальных заметок ('ok', 'подтверждено') в note: описывайте окружение и версии."
    ),
    "review_knowledge": (
        "=== Справка по инструменту review_knowledge ===\n\n"
        "Назначение: Самопроверка свежих карточек перед коммитом/финальным ответом. "
        "Движок отбирает кандидатов, считает детерминированные флаги и подсказки дублей; "
        "семантическую проверку полезности выполняет агент по встроенной рубрике, при сомнениях спрашивает пользователя.\n\n"
        "КОГДА ВЫЗЫВАТЬ:\n"
        "- Сразу после фазы record_knowledge, ДО git commit или финального ответа;\n"
        "- После сессии с несколькими record_knowledge для проверки, что в базу не попал мусор.\n\n"
        "ОТБОР КАНДИДАТОВ:\n"
        "1. ids: явный список ID карточек (до 10) — точечная выборка, фильтры source/since карточек не применяются "
        "(task_id при этом продолжает фильтровать блок open_hypotheses);\n"
        "2. task_id и/или since: комбинируются как AND (карточка должна удовлетворить обоим условиям). "
        "task_id: карточки, записанные с этим task_id (рекомендуется передавать task_id в record_knowledge);\n"
        "   since: карточки с created_at >= даты (ISO, YYYY-MM-DD; время в строке отбрасывается — сравнение на уровне дня); "
        "мусорный формат отвергается ошибкой INVALID_ARGUMENTS;\n"
        "3. По умолчанию (без селекторов): активные auto_inferred-карточки за последний день (уровень даты, без user_confirmed). "
        "Проверенные на стенде (verified) тоже попадают в выборку до конца дня.\n"
        "   include_user_confirmed=True расширяет выборку на карточки, подтверждённые пользователем.\n"
        "   limit: максимум карточек в выборке (клампинг 1..50, по умолчанию 10).\n\n"
        "СТРУКТУРА ОТВЕТА:\n"
        "- candidates[]: карточка с полями (id, title, category, type, status, created_at, task_id, symptom, root_cause, solution, rationale) +:\n"
        "  * flags: детерминированные сигналы (plan_markers — маркеры планов/TODO, stale_after_expired, thin_solution, unverified_auto);\n"
        "  * similar: топ-3 похожие активные карточки той же категории (проверка на дубль);\n"
        "- open_hypotheses: незакрытые гипотезы (запрещено оставлять «зависшие»: вызовите resolve_hypotheses);\n"
        "- rubric: семантическая рубрика самопроверки (польза, специфичность, тип контента, дубль, проверяемость);\n"
        "- ask_user_template: шаблон вопроса пользователю о пользе карточки при неуверенности;\n"
        "- verdict_actions: маппинг вердиктов на существующие инструменты.\n\n"
        "ВЕРДИКТЫ (применяй существующими инструментами, review_knowledge ничего не мутирует):\n"
        "- Полезно и уверен → ничего / update_knowledge_status(action='verify') после подтверждения человеком;\n"
        "- Не уверен → спроси пользователя по ask_user_template и действуй по ответу;\n"
        "- Мусор / план / сессионный шум → update_knowledge_status(action='dispute', note='почему это не знание');\n"
        "- Дубль → append_note в существующую карточку или пересоздать с supersedes."
    ),
    "search_knowledge": (
        "=== Справка по инструменту search_knowledge ===\n\n"
        "Назначение: Токен-эффективный поиск сниппетов, группированная выборка по категориям и секциям и общая сводка проекта (Project Overview).\n\n"
        "РЕЖИМЫ РАБОТЫ:\n"
        "1. Zero-query обзор проекта (без параметров):\n"
        "   search_knowledge()\n"
        "   Возвращает сводку каталога базы знаний проекта (Project Overview: число карточек по каждой категории и примеры тем).\n"
        "2. Группированная выборка по категориям и запросу:\n"
        '   search_knowledge(query="модальное окно", categories=["ui", "backend"])\n'
        "   Возвращает структурированный ответ: query_results, category_results, antipatterns (до 2 тупиков).\n"
        "3. Пакетная выборка нескольких категорий разом:\n"
        '   search_knowledge(categories=["ocr", "transcription", "training"])\n'
        "4. Классический точечный поиск по ошибке / симптому:\n"
        '   search_knowledge(query="SIGTTIN freeze in background")\n'
        "   Возвращает словарь с блоком query_results.\n\n"
        "ПАРАМЕТРЫ:\n"
        "- query: Текстовый поисковый запрос (симптомы, стек ошибки, технологии);\n"
        "- categories: Список категорий для пакетной выборки (до 5 штук);\n"
        "- task_id: Идентификатор задачи для выборки гипотез;\n"
        "- include_hypotheses: Включать ли гипотезы (по умолчанию False);\n"
        "- include_antipatterns: Включать ли блок антипаттернов (по умолчанию True, максимум 2 карточки с ⛔);\n"
        "- limit: Максимальное число сниппетов в текстовом поиске (от 1 до 20, по умолчанию 5);\n"
        "- limit_per_section: Максимум карточек на секцию/группу (от 1 до 5, по умолчанию 3);\n"
        "- include_overview: Включение сводки проекта (Project Overview) в ответ (по умолчанию авто: True только при zero-query);\n"
        "- include_archived: Включать ли устаревшие (superseded, disputed) карточки.\n\n"
        "СТРУКТУРА ОТВЕТА:\n"
        "- query_results: Сниппеты точечного FTS5-поиска по тексту запроса;\n"
        "- category_results: Сниппеты по запрошенным категориям;\n"
        "- antipatterns: Опровергнутые решения и тупики (⛔);\n"
        "- hypotheses: Открытые рабочие гипотезы (status='hypothesis');\n"
        "- overview: Сводка каталога тем проекта при zero-query;\n"
        "- results: (DEPRECATED) Переходный алиас query_results для совместимости с v0.1 (будет удален в v0.3).\n\n"
        "ЗАЩИТА КОНТЕКСТА (Token Budget Guard):\n"
        "- Сквозная дедупликация: одна и та же карточка не дублируется между блоками ответа;\n"
        "- Блок antipatterns ограничен максимум 2 сниппетами с ярким предупреждением;\n"
        "- Жесткий суммарный потолок всего ответа <= 12 сниппетов."
    ),
    "get_knowledge": (
        "=== Справка по инструменту get_knowledge ===\n\n"
        "Назначение: Получение полного текста и метаданных карточек по их идентификаторам.\n\n"
        "ПРАВИЛА И ОГРАНИЧЕНИЯ:\n"
        "- ids: Список ID карточек (например, ['audio-ffmpeg-sigttin', 'platform-sqlite-wal']) или один ID строкой.\n"
        "- Защита контекста (Clamping): Максимальное число возвращаемых карточек — 10.\n"
        "- Если передано >10 ID, избыточные карточки усекаются, а в ответе возвращается поле warning с перечнем отброшенных ID.\n"
        "- Возвращает поля: frontmatter (YAML-метаданные), content (полный Markdown), badge (статус верификации), see_also (связанные карточки, включая вытесненные/замещающие), superseded_info."
    ),
}


ALIASES: dict[str, str] = {
    "record": "record_knowledge",
    "search": "search_knowledge",
    "update": "update_knowledge_status",
    "status": "update_knowledge_status",
    "get": "get_knowledge",
    "propose": "propose_hypothesis",
    "resolve": "resolve_hypotheses",
    "hypothesis": "propose_hypothesis",
    "review": "review_knowledge",
    "self_review": "review_knowledge",
    "selfreview": "review_knowledge",
}


def get_help_text(tool_name: str | None = None) -> str:
    """Возвращает текст справки по указанному инструменту или общий обзор."""
    clean_name = "overview" if not tool_name or not tool_name.strip() else tool_name.strip().lower()
    target_name = ALIASES.get(clean_name, clean_name)
    if target_name in HELP_DOCS:
        return HELP_DOCS[target_name]

    available = ", ".join(f"'{k}'" for k in HELP_DOCS)
    return (
        f"Неизвестный инструмент или раздел: '{tool_name}'.\n"
        f"Доступные разделы справки: {available}."
    )
