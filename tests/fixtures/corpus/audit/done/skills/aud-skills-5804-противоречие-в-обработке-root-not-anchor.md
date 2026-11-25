---
id: aud-skills-5804
title: Противоречие в обработке ROOT_NOT_ANCHORED (недопустимый fallback на файлы)
category: skills
priority: critical
autonomy: high
complexity: L
status: completed
target_file: skills/general/technical-debt-management/blocks/mode/mcp.md
created_at: '2026-11-23'
updated_at: '2026-11-24'
---
# aud-skills-5804: Противоречие в обработке ROOT_NOT_ANCHORED (недопустимый fallback на файлы)

## ❌ Симптом
Маскировка инфраструктурной ошибки ROOT_NOT_ANCHORED и переход на неконтролируемое чтение файлов вместо фиксации ошибки конфигурации.

## 🔍 Техническая первопричина
skills/general/technical-debt-management/blocks/mode/mcp.md:24 и docs/stack-conventions.yaml:7 разрешают фолбэк на ручные файлы при ROOT_NOT_ANCHORED, что прямо противоречит writing-plans/blocks/integration/mcp.md:10 и docs/composition-contract.md:9 (требующим fail-fast).

## 🛠 Предлагаемое решение
Запретить переход на файлы при ROOT_NOT_ANCHORED в technical-debt-management/blocks/mode/mcp.md и убрать упоминание из stack-conventions.yaml. Требовать явного закрепления корня.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-24: В blocks/mode/mcp.md, blocks/mode/default.md и docs/stack-conventions.yaml запрещен несанкционированный переход на файлы при ROOT_NOT_ANCHORED; требуется передача явного project_path.
