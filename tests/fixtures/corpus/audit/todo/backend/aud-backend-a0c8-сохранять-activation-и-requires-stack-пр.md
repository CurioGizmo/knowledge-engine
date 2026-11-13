---
id: aud-backend-a0c8
title: Сохранять activation и requires_stack при create_skill(overwrite=True)
category: backend
priority: important
autonomy: high
complexity: M
status: planned
target_file: src/mcp_skills/creator.py
symbol: create_skill
created_at: '2026-10-15'
updated_at: '2026-10-15'
---

# aud-backend-a0c8: Сохранять activation и requires_stack при create_skill(overwrite=True)

## ❌ Симптом
creator.py:225-236 молча теряет activation и requires_stack при create_skill(overwrite=True)

## 🔍 Техническая первопричина
creator.py собирает frontmatter из фиксированного набора полей без activation и requires_stack

## 🛠 Предлагаемое решение
Считывать существующие activation и requires_stack при overwrite=True или расширить сигнатуру create_skill
