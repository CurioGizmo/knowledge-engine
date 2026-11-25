---
id: aud-skills-a495
title: Некомплектность агента migration-engineer, пропуск node в stack-invariants
  и git commit -am в release-flow
category: skills
priority: important
autonomy: high
complexity: L
status: completed
target_file: agents/migration-engineer/AGENT.yaml
created_at: '2026-11-23'
updated_at: '2026-11-24'
---
# aud-skills-a495: Некомплектность агента migration-engineer, пропуск node в stack-invariants и git commit -am в release-flow

## ❌ Симптом
Агент миграций лишен профильного скила схемы данных; проекты на Node.js лишены рантайм-инвариантов; release-flow нарушает запрет на массовый коммит.

## 🔍 Техническая первопричина
agents/migration-engineer/AGENT.yaml:7 объявляет только sqlite-performance, но не подключает sqlite-schema-evolution. В blocks/stack-invariants отсутствует node.md (вызывая деградацию в default.md при стеке node). flows/release-flow.md:66 использует git commit -am вместо атомарного git add.

## 🛠 Предлагаемое решение
Подключить sqlite-schema-evolution к migration-engineer, добавить blocks/stack-invariants/node.md, заменить git commit -am в release-flow на точечный git add.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-24: Добавлен sqlite-schema-evolution в agents/migration-engineer/AGENT.yaml, создан blocks/stack-invariants/node.md, удалены python-fastapi.md и tauri-rust.md, в release-flow заменен git commit -am на точечный git add.
