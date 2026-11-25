---
id: aud-backend-adf8
title: Удалить deprecated-свойство FlowStageMeta.gate и legacy-алиасы GATE_REGISTRY/GateResult
category: backend
priority: suggestion
autonomy: high
complexity: L
status: completed
target_file: src/mcp_skills/models.py
symbol: FlowStageMeta.gate
created_at: '2026-11-20'
updated_at: '2026-11-24'
---
# aud-backend-adf8: Удалить deprecated-свойство FlowStageMeta.gate и legacy-алиасы GATE_REGISTRY/GateResult

## ❌ Симптом
В кодовой базе сохраняются временные адаптеры обратной совместимости: свойство FlowStageMeta.gate и алиасы GATE_REGISTRY, GateResult, validate_*_gate в flow_engine.py после унификации каталога на validator.

## 🔍 Техническая первопричина
Свойство gate и алиасы GATE_REGISTRY/GateResult были введены как временный мост обратной совместимости при переходе на канонический validator, чтобы исключить сбои у внешних клиентов и устаревших сценариев.

## 🛠 Предлагаемое решение
После завершения переходного периода: 1. Удалить свойство gate и предвалидатор _migrate_gate_to_validator из FlowStageMeta (src/mcp_skills/models.py). 2. Удалить legacy-алиасы GATE_REGISTRY, GateResult, GateFunc, validate_*_gate из src/mcp_skills/flow_engine.py и исключить их из __all__. 3. Актуализировать юнит-тесты.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-24: Исправлено в рамках AUDIT-FIX-P2: удалены свойство gate и предвалидатор из FlowStageMeta, удалены GATE_* из flow_engine.py
