---
id: aud-skills-7732
title: 'Разрыв связки Flow и Skill: 6 публичных флоу не объявляют skills в meta этапов'
category: skills
priority: critical
autonomy: high
complexity: M
status: completed
target_file: flows/plan-drafting-flow.md
created_at: '2026-11-23'
updated_at: '2026-11-24'
---
# aud-skills-7732: Разрыв связки Flow и Skill: 6 публичных флоу не объявляют skills в meta этапов

## ❌ Симптом
Разрыв связки Flow <-> Skill: стейт-машина не подгружает скиллы и стековые блоки при переходе по этапам; доктрина дублируется в тексте флоу.

## 🔍 Техническая первопричина
В 6 из 9 публичных flow (systematic-debugging, brainstorming, plan-drafting, commit-flow, release-flow, live-qa-flow) в meta этапов не указано skills: [...]. flow_engine.py:1046-1047 компилирует скиллы только при наличии stage.meta.skills, поэтому оркестратор не получает доктрину скиллов. В качестве костыля авторы скопировали текст доктрины прямо в тело этапов (нарушение DRY/SSOT).

## 🛠 Предлагаемое решение
Добавить декларацию skills: [...] в meta этапов всех 6 флоу и очистить тело этапов от дублирования текста доктрины скиллов.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-24: Во всех 6 публичных flow (systematic-debugging, brainstorming, plan-drafting, commit-flow, release-flow, live-qa-flow) прописаны точные meta.skills и стандартизирован frontmatter.
