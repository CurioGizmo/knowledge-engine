---
id: aud-docs-8040
title: 'Схема record_review.comments недокументирована: обязательные file_path+symptom
  скрыты в pydantic-трейсе'
category: docs
priority: important
autonomy: high
complexity: M
status: planned
target_file: src/mcp_audit/server.py
symbol: record_review
task_id: mcp-simulation-audit
created_at: '2026-10-12'
updated_at: '2026-10-12'
---

# aud-docs-8040: Схема record_review.comments недокументирована: обязательные file_path+symptom скрыты в pydantic-трейсе

## ❌ Симптом
Симуляция A9: первый вызов record_review с comments=[{"file":...,"text":...}] отклонён с сырым pydantic-трейсом; обязательные поля комментария (file_path, symptom, severity, line) нигде в description инструмента не указаны. Агент с первой попытки гарантированно получит отказ.

## 🔍 Техническая первопричина
В docstring/description record_review тип comments описан как list[dict[str, Any]] без явной схемы ReviewComment; LLM не может узнать обязательные ключи до ошибки.

## 🛠 Предлагаемое решение
Описать ReviewComment явно в description инструмента record_review (обязательные: file_path, symptom; опциональные: severity, line); синхронизировать audit-backlog SKILL.md в src/plugins/audit-engine-plugin; добавить пример корректного comments в docstring.
