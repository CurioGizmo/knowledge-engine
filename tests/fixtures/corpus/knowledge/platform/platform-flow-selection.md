---
id: platform-flow-selection
title: Интерактивный арбитраж сценариев разработки (Flow Selection) при неоднозначных
  намерениях
category: platform
type: decision
status: active
confidence: 0.75
task_id: feat/flow-selection-user-choice
refuted_by: null
refutation_reason: null
generated:
  by: process
  agent: null
  created_at: '2026-10-10'
verified: []
stale_after: null
supersedes: null
superseded_by: null
related_to: []
derived_from: null
aliases: []
tags: []
---

### Симптом / Контекст
При запросах пользователей на выполнение или создание плана ('выполни план', 'составь план') в каталоге доступно несколько специализированных сценариев (автономный plan-execution-auto-flow, параллельный parallel-execute-flow волнами субагентов, итеративный plan-execution-flow). Ранее LLM могла произвольно выбирать один из них без согласования с пользователем.

### Первопричина (Root Cause)
При запросах пользователей на выполнение или создание плана ('выполни план', 'составь план') в каталоге доступно несколько специализированных сценариев (автономный plan-execution-auto-flow, параллельный parallel-execute-flow волнами субагентов, итеративный plan-execution-flow). Ранее LLM могла произвольно выбирать один из них без согласования с пользователем.

### Решение / Выводы
1. В регламенте skills-flow/SKILL.md закреплен строгий запрет на случайный выбор сценария и зафиксирована матрица арбитража с вызовом нативных инструментов сред (question в OpenCode, ask_question в Antigravity, AskUserQuestion в Claude Code, вопрос в чат в Cursor/терминале). 2. Исключены хардкодные словари синонимов (COMMON_SYNONYMS, FLOW_CROSS_SYNONYMS) — внедрена нативная поддержка aliases в манифестах FlowDefinition (Single Source of Truth) через YAML frontmatter. В search_skills(type='flow') выполняется ранжирование по стеммам и алиасам с детекцией неоднозначности: при близких скорах кандидатов возвращаются флаг is_ambiguous=True, рекомендация и arbitration_options. 3. В flow_parser улучшена ошибка FlowNotFoundError с выводом списка реально доступных сценариев.

### Обоснование / Почему именно так (Rationale)
Самовольный выбор тяжелого параллельного или автоматического сценария вместо итеративного ломает ожидания пользователя. Во всех современных окружениях (OpenCode question, Google Antigravity ask_question, Claude Code AskUserQuestion) имеются нативные модальные/CLI-инструменты опроса, которые обязаны вызываться до старта процесса при неоднозначности. Хардкодные словари синонимов масштабируются плохо и загрязняют ядро; алиасы внутри сценариев обеспечивают децентрализованное расширение каталога.

### Журнал сбоев и наблюдений (Log)
Записей пока нет.
