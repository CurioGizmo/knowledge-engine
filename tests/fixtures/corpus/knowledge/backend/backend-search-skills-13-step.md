---
id: backend-search-skills-13-step
title: 'Рассинхрон резолюции каталога флоу: search_skills видел 13 сценариев, step_flow
  — только builtin (skills_dir не пробрасывался в фасад)'
category: backend
type: lesson
status: draft
confidence: 0.5
task_id: mcp-simulation-flows
refuted_by: null
refutation_reason: null
generated:
  by: process
  agent: null
  created_at: '2026-10-12'
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
skills-engine, запущенный с --skills-dir (и без MCP_SKILLS_PATH): search_skills(type="flow") показывает 13 сценариев, но step_flow(flow="plan-execution-flow") возвращает FLOW_NOT_FOUND «не найден в проекте, кэше и встроенных шаблонах. Доступные: ['feature-delivery']». Флоу из поиска невозможно стартовать.

### Первопричина (Root Cause)
step_flow_tool в server.py не пробрасывал skills_dir (из --skills-dir) в фасад step_flow() → FlowEngine → load_flow_definition резолвил кэш через дефолт ~/.cache/mcp-skills/repo/flows (пуст). search_skills при этом получал skills_dir и видел 13 флоу. Баг маскировался, если задан MCP_SKILLS_PATH (env-фолбэк в resolve_skills_repo_dir), но стабильно воспроизводился при запуске только с --skills-dir.

### Решение / Выводы
Инвариант: все точки резолюции каталога (search_skills, step_flow/start/advance/status/retry/escalation/list_flows) обязаны получать один и тот же skills_dir. Исправлено: FlowEngine.__init__ принимает skills_dir и передаёт во все load_flow_definition/list_available_flows; фасад step_flow и _handle_step_retry принимают skills_dir; step_flow_tool пробрасывает skills_repo_dir. Регресс-тест: E2E через stdio MCP с --skills-dir (без env) — старт кэшевого флоу plan-execution-flow, advance с verdict, status, abort; 534 теста зелёные.

### Обоснование / Почему именно так (Rationale)
Инвариант «поиск и действие видят один каталог» очевиден только постфактум: баг не ловился тестами (тесты не передавали skills_dir в step_flow), не проявлялся при MCP_SKILLS_PATH (env-фолбэк в resolve_skills_repo_dir спасал), и всплыл ровно тогда, когда хост не прокинул env. Симуляция через изолированные песочницы с явным --skills-dir оказалась единственным способом воспроизведения. Паттерн «tool-обёртка теряет параметр конфигурации при пробросе в фасад» стоит проверять при добавлении любых override-параметров.

### Журнал сбоев и наблюдений (Log)
Записей пока нет.
