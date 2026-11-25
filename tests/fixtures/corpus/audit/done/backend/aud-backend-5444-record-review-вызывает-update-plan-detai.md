---
id: aud-backend-5444
title: record_review вызывает update_plan_details без category (сбой с AmbiguousPlanError)
category: backend
priority: important
autonomy: high
complexity: L
status: completed
target_file: src/mcp_audit/server.py
created_at: '2026-11-23'
updated_at: '2026-11-24'
---
# aud-backend-5444: record_review вызывает update_plan_details без category (сбой с AmbiguousPlanError)

## ❌ Симптом
Статус плана не обновляется при ревью, если в проекте есть планы с одинаковым task_id в разных категориях (ошибка silently logged в warning).

## 🔍 Техническая первопричина
В src/mcp_audit/server.py:1170, 1172, 1177 sync.update_plan_details вызывается без параметра category, что приводит к AmbiguousPlanError при наличии планов в нескольких категориях и глушится в warning.

## 🛠 Предлагаемое решение
Передавать category=clean_cat во все вызовы sync.update_plan_details внутри record_review.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-24: Исправлено в рамках AUDIT-FIX-P2: category передается в update_plan_details во все вызовы в record_review
