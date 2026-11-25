---
id: aud-skills-9e3f
title: Обход Plan Gate через абсолютные пути в deliverable
category: skills
priority: critical
autonomy: high
complexity: M
status: completed
target_file: src/mcp_skills/flow_engine.py
created_at: '2026-11-23'
updated_at: '2026-11-24'
---
# aud-skills-9e3f: Обход Plan Gate через абсолютные пути в deliverable

## ❌ Симптом
deliverable="/nonexistent/plan.md" обходит Plan Gate с passed=True, позволяя сдать несуществующий план.

## 🔍 Техническая первопричина
_looks_like_path возвращает False для строк, начинающихся с / или ~, из-за чего строка трактуется как markdown-текст плана и проходит validate_plan с passed=True.

## 🛠 Предлагаемое решение
Признавать пути, начинающиеся с / и ~, путями в _looks_like_path. Если путь абсолютный и не принадлежит project_root, либо файл не существует, фиксировать блокер GateValidationError.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-24: В src/mcp_skills/flow_engine.py:341 исправлен _looks_like_path для путей на / и ~; validate_plan возвращает ValidationResult(passed=False, blockers=[...]) для несуществующих/внешних путей без необработанных исключений. Покрыто тестами в test_flow_f1_f2.py (AUDIT-FIX-P0).
