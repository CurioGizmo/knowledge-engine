---
id: backend-review-knowledge-mcp-self-review
title: 'ADR: саморевью карточек через MCP-инструмент review_knowledge без git-хуков'
category: backend
type: decision
status: active
confidence: 0.75
task_id: null
refuted_by: null
refutation_reason: null
generated:
  by: process
  agent: null
  created_at: '2026-09-29'
verified: []
stale_after: null
supersedes: null
superseded_by: null
related_to: []
derived_from: null
aliases:
- саморевью карточек
- review knowledge mcp
- проверка качества карточек перед коммитом
- card quality review llm
tags:
- review
- mcp
- adr
---

### Симптом / Контекст
Карточки попадают в базу знаний без семантической проверки качества: мусор, планы и дубли засоряют выдачу; внешние хуки (pre-commit/CI) не подходят — нужен полностью MCP-уровень

### Первопричина (Root Cause)
Протокол требовал record_knowledge перед коммитом, но не имел шага верификации полезности записанного: Anti-Spam Gate проверяет только длины полей, дедуп — только точные совпадения симптомов. Физически карточки — md-файлы в git, поэтому хард-delete через MCP обошёл бы PR-ревью

### Решение / Выводы
8-й инструмент review_knowledge (read-only): селекция ids>task_id>since>окно 24ч auto_inferred-карточек; движок считает детерминированные флаги (plan_markers, stale_after_expired, thin_solution, unverified_auto) и similar-подсказки дублей через FTS BM25; в ответ встроены рубрика самопроверки, ask_user_template для вопроса пользователю и маппинг вердиктов. Вердикты применяются существующими инструментами: мусор -> update_knowledge_status(dispute), физическое удаление — только rm файла (индекс выздоровеет через synchronizer.sync). Пререквизит: record_knowledge получил task_id, в FTS-схему добавлена колонка created_at (BM25-весов теперь 20)

### Обоснование / Почему именно так (Rationale)
CQRS не ломается (новый инструмент read-only), LLM-агент саморевьюит по рубрике и спрашивает человека при неуверенности — весь цикл внутри MCP, без pre-commit хуков; двухфазный force/confirmed_force паттерн не понадобился, т.к. dispute уже исключает карточку из поиска

### Журнал сбоев и наблюдений (Log)
Записей пока нет.
