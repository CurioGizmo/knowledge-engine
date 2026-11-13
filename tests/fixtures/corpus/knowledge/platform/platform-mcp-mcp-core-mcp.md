---
id: platform-mcp-mcp-core-mcp
title: 'Модульная архитектура MCP: mcp_core, mcp_knowledge и mcp_audit'
category: platform
type: decision
status: active
task_id: null
refuted_by: null
refutation_reason: null
generated:
  by: process
  agent: null
  created_at: '2026-09-30'
verified: []
stale_after: null
supersedes: null
superseded_by: null
related_to:
- platform-root-failfast-guard
- database-sqlite-pragmas-canon
derived_from: null
aliases: []
tags: []
confidence: 0.75
---

### Симптом / Контекст
Необходимость поддержки нескольких MCP серверов (память и трекинг техдолга) без дублирования FTS5/SQLite/Git инфраструктуры.

### Первопричина (Root Cause)
Разрастание монолитной кодовой базы и смешивание долгосрочной памяти (ADR/уроки) с операционным бэклогом задач/техдолга.

### Решение / Выводы
Выделен пакет mcp_core (общая инфраструктура), сохранён mcp_knowledge со 100% обратной совместимостью и реализован mcp_audit с DAG-резолюцией, lazy-режимом и защитой от коллизий в git (aud-<domain>-<hash4>).

### Обоснование / Почему именно так (Rationale)
Разделение на core/knowledge/audit позволяет использовать независимые SQLite базы (knowledge_fts.db и audit_fts.db) без lock-контеншена, при этом переиспользуя инфраструктурный код (WAL, FTS5 sanitizer, sync, installer).

### Нюансы и ограничения (Nuances)
Файловая синхронизация через mtime и size исключает лишние операции с БД; при перемещении карточек старый путь очищается из file_sync. File scope matching в check_known_debt предотвращает False Positive пропуски дефектов.

### Журнал сбоев и наблюдений (Log)
Записей пока нет.
