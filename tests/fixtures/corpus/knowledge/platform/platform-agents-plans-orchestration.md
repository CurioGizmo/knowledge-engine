---
id: platform-agents-plans-orchestration
title: Каноническое размещение временного состояния оркестрации рядом с планами (.agents/plans/.orchestration/)
category: platform
type: decision
status: draft
confidence: 0.5
task_id: AUDIT-PLAN-VERSIONS
refuted_by: null
refutation_reason: null
generated:
  by: process
  agent: null
  created_at: '2026-11-24'
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
В процессе параллельного исполнения планов (execute-plan-flow, parallel-execute-flow, plan-execution-auto-flow) создаются служебные трекеры координации субагентов по волнам и Compaction Guard

### Первопричина (Root Cause)
В процессе параллельного исполнения планов (execute-plan-flow, parallel-execute-flow, plan-execution-auto-flow) создаются служебные трекеры координации субагентов по волнам и Compaction Guard (orchestration-state.md). Ранее пути были не согласованы (docs/orchestration/, корень .agents/), из-за чего временные файлы попадали в git и засоряли историю коммитов.

### Решение / Выводы
Зафиксирован единый канонический стандарт размещения служебного состояния оркестрации во всех флоу mcp_skills:
1. В MCP-режиме: `.agents/plans/.orchestration/<slug>/orchestration-state.md`.
2. В оффлайн-режиме: `docs/plans/.orchestration/<slug>/orchestration-state.md`.
3. Служебные файлы оркестрации категорически запрещено коммитить в git. Попадание в каталог планов автоматически изолирует их через стандартный .gitignore.

### Обоснование / Почему именно так (Rationale)
Каталоги планов (.agents/plans/ и docs/plans/) уже включены в .gitignore во всех проектах экосистемы. Размещение временного состояния оркестрации внутри каталога планов гарантирует 100% изоляцию от git, устраняет мусор в корне и связывает жизненный цикл оркестрации с самим планом.

### Журнал сбоев и наблюдений (Log)
Записей пока нет.
