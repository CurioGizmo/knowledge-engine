---
id: aud-backend-a4db
title: 'step_flow: abort без сессии возвращает FLOW_NOT_FOUND вместо SESSION_NOT_FOUND;
  кириллическое имя флоу даёт «Недопустимое имя» вместо «не найдено»'
category: backend
priority: suggestion
autonomy: high
complexity: M
status: completed
target_file: src/mcp_skills/flow_engine.py
symbol: _resolve_session_id
task_id: mcp-simulation-flows
created_at: '2026-10-12'
updated_at: '2026-11-24'
---
# aud-backend-a4db: step_flow: abort без сессии возвращает FLOW_NOT_FOUND вместо SESSION_NOT_FOUND; кириллическое имя флоу даёт «Недопустимое имя» вместо «не найдено»

## ❌ Симптом
Симуляция F10: abort без активной сессии → reason=FLOW_NOT_FOUND (сообщение при этом «Активная сессия для abort не найдена» — семантика кода неверна). F7: flow=нет-такого-flow → INVALID_ARGUMENTS «Недопустимое имя сценария» (is_safe_name отсекает кириллицу до поиска).

## 🔍 Техническая первопричина
_resolve_session_id бросает FlowNotFoundError для отсутствующей сессии; is_safe_name разрешает только латиницу, поэтому несуществующее кириллическое имя выглядит как ошибка валидации, а не как «не найдено».

## 🛠 Предлагаемое решение
Ввести отдельный SESSION_NOT_FOUND reason для _resolve_session_id; для кириллических имён флоу возвращать FLOW_NOT_FOUND с подсказкой о допустимых символах. Мелкие правки + тесты на коды причин.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-24: Введён класс SessionNotFoundError(FlowNotFoundError) в errors.py, зарегистрирован в SKILLS_ERROR_RULES перед FlowNotFoundError. При вызове step_flow для abort/retry/escalation_resolve/status без активной сессии возвращается SESSION_NOT_FOUND вместо FLOW_NOT_FOUND. Покрыто тестами.
