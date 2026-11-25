---
id: aud-skills-5b5e
title: Ссылка на несуществующего субагента research в fast-audit-flow
category: skills
priority: important
autonomy: high
complexity: L
status: completed
target_file: flows/fast-audit-flow.md
created_at: '2026-11-23'
updated_at: '2026-11-24'
---
# aud-skills-5b5e: Ссылка на несуществующего субагента research в fast-audit-flow

## ❌ Симптом
Попытка оркестратора запустить субагента research в fast-audit-flow завершится ошибкой отсутствия профиля агента.

## 🔍 Техническая первопричина
flows/fast-audit-flow.md:38 предписывает вызывать субагента research, однако профиль agents/research/AGENT.yaml в каталоге отсутствует. scripts/verify_catalog.py проверяет только stage.meta.agent (где стоит null) и пропускает битую ссылку в тексте.

## 🛠 Предлагаемое решение
Заменить вызов на существующего агента code-reviewer / perf-investigator либо создать профиль agents/research/AGENT.yaml.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-24: В flows/fast-audit-flow.md:38 ссылка на фантомный research заменена на профиль code-reviewer.
