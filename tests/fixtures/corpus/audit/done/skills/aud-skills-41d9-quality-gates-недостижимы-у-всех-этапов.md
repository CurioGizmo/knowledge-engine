---
id: aud-skills-41d9
title: 'Quality Gates недостижимы: у всех этапов 12 канонических флоу gate: null,
  единственный гейт validate_plan из GATE_REGISTRY нигде не подключён — сценарий gate_blocked
  мёртв'
category: skills
priority: important
autonomy: high
complexity: M
status: completed
target_file: src/mcp_skills/flow_engine.py
symbol: GATE_REGISTRY
task_id: mcp-simulation-round2
created_at: '2026-10-13'
updated_at: '2026-11-25'
---
# aud-skills-41d9: Quality Gates недостижимы: у всех этапов 12 канонических флоу gate: null, единственный гейт validate_plan из GATE_REGISTRY нигде не подключён — сценарий gate_blocked мёртв

## ❌ Симптом
Симуляция F3/F5: продвижение с deliverable никогда не возвращает gate_blocked — этап проходит безусловно. Механика Quality Gates (flow_engine.py:286 gate_func = GATE_REGISTRY.get(stage.meta.gate), L300-304 verdict/status='gate_blocked') существует и протестирована, но в бою не активируется; deliverable на auto-этапах игнорируется.

## 🔍 Техническая первопричина
Конфигурационный пробел в каталоге сценариев: grep -h 'gate:' /home/anton/projects/mcp_skills/flows/*.md не находит ни одного задания gate — во всех этапах всех 12 файлов meta.gate=None. В GATE_REGISTRY (flow_engine.py:119) зарегистрирован ровно один гейт validate_plan, и он не привязан ни к одному этапу. Движок при этом корректен: это не баг кода, а отсутствие данных.

## 🛠 Предлагаемое решение
Привязать гейты к этапам в flows/*.md: минимум validate_plan к validate_plan_draft в plan-drafting-flow, плюс плановые гейты на финальных этапах исполнения (проверка существования deliverable-файла, зелёные тесты, отсутствие незакрытых блокеров). Расширить GATE_REGISTRY гейтами под эти проверки. После этого добавить E2E-тест, который реально ловит gate_blocked с заполненным blockers[] (сейчас путь покрыт только юнит-тестами на синтетической стадии).

## ✅ Фактическое решение (Actual resolution)
- 2026-11-25: Исправлено ранее: gate заменён на VALIDATOR_REGISTRY (src/mcp_skills/validators.py:661-666) и stage.meta.validator; валидаторы подключены к этапам в mcp_skills/flows/*.md и покрыты тестами gate_blocked.
