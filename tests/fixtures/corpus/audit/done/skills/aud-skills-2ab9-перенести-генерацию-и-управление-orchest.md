---
id: aud-skills-2ab9
title: Перенести генерацию и управление orchestration-state.md из промптов флоу в
  Python-код step_flow
category: skills
priority: important
autonomy: high
complexity: M
status: completed
target_file: src/mcp_skills/flow_engine.py
created_at: '2026-11-24'
updated_at: '2026-11-25'
---
# aud-skills-2ab9: Перенести генерацию и управление orchestration-state.md из промптов флоу в Python-код step_flow

## ❌ Симптом
Агент вручную создаёт файлы состояния оркестрации через write_file по произвольным путям, что засоряет рабочее дерево и историю git.

## 🔍 Техническая первопричина
Инструкции флоу (execute-plan-flow, parallel-execute-flow, plan-execution-auto-flow) требуют от агента загрузить шаблон и вручную создать markdown-файл состояния оркестрации через инструмент клиента write_file. Это нарушает инвариант детерминированного управления путями со стороны MCP-сервера и приводит к созданию мусорных файлов в непредсказуемых местах (.agents/, docs/ и т.д.).

## 🛠 Предлагаемое решение
1. Научить стейт-машину flow_engine.py автоматически материализовать .agents/plans/.orchestration/<slug>/orchestration-state.md на основе context_data активной сессии (либо полностью инкапсулировать волны в сессионном контексте без необходимости отдельного markdown-файла).
2. Удалить из текстов этапов флоу инструкции, требующие от агента ручного вызова write_file для служебного состояния оркестрации.
3. Покрыть unit-тестами генерацию и обновление трекера в tests/test_flow_*.py.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-25: Реализовано серверное управление orchestration-state.md: модуль src/mcp_skills/orchestration_state.py интегрирован в flow_engine.py, stage_compiler.py и server.py (step_flow/start_flow/advance_flow/abort_flow/resolve_flow_escalation/status); удалены инструкции ручного write_file и загрузки шаблона из execute-plan-flow.md, parallel-execute-flow.md и plan-execution-auto-flow.md; добавлены тесты tests/test_flow_orchestration_state.py, пройдена живая симуляция OpenCode (flow-ba7e414e) и независимое ревью (APPROVED).
