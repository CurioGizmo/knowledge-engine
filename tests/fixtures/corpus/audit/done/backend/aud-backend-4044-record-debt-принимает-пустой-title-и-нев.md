---
id: aud-backend-4044
title: record_debt принимает пустой title и невалидный priority — мусор в реестре
category: backend
priority: important
autonomy: high
complexity: M
status: completed
target_file: src/mcp_audit/server.py
symbol: record_debt
task_id: mcp-simulation-audit
created_at: '2026-10-12'
updated_at: '2026-11-09'
---
# aud-backend-4044: record_debt принимает пустой title и невалидный priority — мусор в реестре

## ❌ Симптом
Симуляция A10a: record_debt(title="") вернул status=created и создал карточку aud-backend-fe5f с пустым заголовком и fallback-слагом task. A10b: priority="normal" (нет в enum) молча сохранён как "important". После update_debt_status пустой title перезаписался значением id.

## 🔍 Техническая первопричина
В record_debt нет валидации непустого title (в отличие от record_knowledge, где anti-spam gate проверяет поля) и нет enum-валидации priority; неизвестные значения молча коэрсятся в дефолт.

## 🛠 Предлагаемое решение
Добавить в record_debt: (1) отказ при пустом/whitespace title с reason=INVALID_ARGUMENTS; (2) enum-валидацию priority (planned enum: critical/important/normal/low — уточнить по models.py) с явной ошибкой вместо молчаливого fallback; (3) unit-тесты на оба кейса.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-09: A-DEBT: дедупликация открытых карточек долга по (title, symptom, target_file, symbol) под lock с поддержкой force=True.
