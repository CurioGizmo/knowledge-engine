---
id: aud-skills-ea0f
title: Circuit Breaker в flow_engine ссылается на несуществующий MCP-инструмент resolve_flow_escalation
category: skills
priority: critical
autonomy: high
complexity: L
status: completed
target_file: src/mcp_skills/flow_engine.py
symbol: _apply_needs_work
created_at: '2026-11-14'
updated_at: '2026-11-15'
---
# aud-skills-ea0f: Circuit Breaker в flow_engine ссылается на несуществующий MCP-инструмент resolve_flow_escalation

## ❌ Симптом
При достижении loop_max стейт-машина возвращает директиву вызвать инструмент resolve_flow_escalation, которого нет в MCP-сервере, вызывая ошибку агента.

## 🔍 Техническая первопричина
resolve_flow_escalation был упразднён как внешний инструмент и перенесён в step_flow(action='escalation_resolve'), но строки директив в flow_engine.py не были обновлены.

## 🛠 Предлагаемое решение
Обновить директивы Circuit Breaker в flow_engine.py (строки 285, 1974, 2302) на вызов step_flow(action='escalation_resolve', resolution=...).

## ✅ Фактическое решение (Actual resolution)
- 2026-11-15: Директивы Circuit Breaker в flow_engine.py обновлены на step_flow(action='escalation_resolve', resolution=...)
