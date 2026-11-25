---
id: aud-skills-24a2
title: Нарушение контракта R7 в technical-debt-management (отсутствует requires_servers)
category: skills
priority: critical
autonomy: high
complexity: L
status: completed
target_file: skills/general/technical-debt-management/SKILL.md
created_at: '2026-11-23'
updated_at: '2026-11-24'
---
# aud-skills-24a2: Нарушение контракта R7 в technical-debt-management (отсутствует requires_servers)

## ❌ Симптом
Нарушение контракта R7: при отсутствии audit-engine субагент получает инструкции с недоступными инструментами и падает с ToolNotFound вместо перехода в offline.md.

## 🔍 Техническая первопричина
skills/general/technical-debt-management/SKILL.md содержит блок blocks/mode/mcp.md с вызовами audit-engine, но во frontmatter не указано requires_servers: [audit-engine]. Автоматическая деградация mcp -> offline не срабатывает при отсутствии сервера.

## 🛠 Предлагаемое решение
Добавить requires_servers: [audit-engine] во frontmatter SKILL.md и валидировать это в scripts/verify_catalog.py.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-24: Добавлен requires_servers: [audit-engine] в technical-debt-management/SKILL.md согласно R7; scripts/verify_catalog.py дополнен обязательной проверкой requires_servers при наличии mcp-блоков.
