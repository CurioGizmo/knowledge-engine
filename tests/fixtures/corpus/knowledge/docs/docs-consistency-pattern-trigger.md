---
id: docs-consistency-pattern-trigger
title: 'Docs-consistency pattern: единый эталон протокольных текстов + Trigger-docstrings
  MCP-инструментов'
category: docs
type: decision
status: active
confidence: 0.75
task_id: docs-audit-2026-10-01
refuted_by: null
refutation_reason: null
generated:
  by: process
  agent: null
  created_at: '2026-10-01'
verified: []
stale_after: null
supersedes: null
superseded_by: null
related_to:
- docs-audit-3-mcp
- backend-tool-name-suffix-drift
derived_from: null
aliases: []
tags: []
---

### Симптом / Контекст
Аудит документации 3 MCP-серверов нашёл смешанную нумерацию фаз lesson-recording (Фаза 1/2./Фаза 3/4.), RU-description debt-скилла без триггеров, docstrings инструментов audit/skills без Trigger-паттерна и мёртвую константу AUDIT_SKILL_TEMPLATE

### Первопричина (Root Cause)
Протокольные тексты дублировались в 4 местах (templates/, core-зеркало, константа инсталлера, установленная копия) без механической проверки синхронности, а у мёртвой константы AUDIT_SKILL_TEMPLATE не было ни одного потребителя, который бы сигнализировал о расхождении

### Решение / Выводы
Паттерн: (1) протокольный текст живёт в единственном эталоне templates/, зеркала проверяются parity-тестом (assert template == file), мёртвые текстовые константы удаляются; (2) frontmatter description скилла - EN с Trigger-списком по образцу lesson-recording; (3) docstring MCP-инструмента: первая строка-суть (синхронна с lazy-схемой) + 'Trigger:' + 'Пример:'; (4) локализация: рантайм-тексты (frontmatter, instructions) - EN с триггерами, протокольные тела скиллов - RU

### Обоснование / Почему именно так (Rationale)
Протокольные тексты - источник дрейфа: их копии расходятся молча, а мёртвые дубликаты текстов (AUDIT_SKILL_TEMPLATE) живут годами и вводят в заблуждение при аудите документации

### Условия и окружение (Conditions)
Только при правках скиллов/docstrings инсталляторов knowledge-сервера

### Нюансы и ограничения (Nuances)
Паттерн localization констант: тексты для рантайма (frontmatter descriptions, server instructions) — EN с триггерами; протокольные тела скиллов — RU. Parity-тест защищает от дрейфа 4 копий lesson-recording и 2 копий debt-скилла.

### Журнал сбоев и наблюдений (Log)
Записей пока нет.
