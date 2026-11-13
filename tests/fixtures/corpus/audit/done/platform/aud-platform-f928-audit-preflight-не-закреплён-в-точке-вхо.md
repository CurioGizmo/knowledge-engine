---
id: aud-platform-f928
title: Audit preflight не закреплён в точке входа skills-flow
category: platform
priority: important
autonomy: high
complexity: M
status: completed
target_file: /home/user/.agents/skills/skills-flow/SKILL.md
symbol: Вход в процесс и границы обсуждения
task_id: unified-review-execute-flows
created_at: '2026-10-28'
updated_at: '2026-10-29'
---
# aud-platform-f928: Audit preflight не закреплён в точке входа skills-flow

## ❌ Симптом
При команде начать реализацию агент сначала запускает skills-flow и может читать код/рабочее дерево, а get_task_context вызывает только после загрузки writing-plans или напоминания пользователя.

## 🔍 Техническая первопричина
Обязательство audit-engine get_task_context описано в tool help и integration-блоке writing-plans, но отсутствует в раннем entry protocol skills-flow и в первом этапе plan-drafting-flow. Из-за progressive disclosure эти инструкции загружаются слишком поздно.

## 🛠 Предлагаемое решение
Добавить в skills-flow/SKILL.md обязательный audit preflight сразу после выбора flow и до чтения кода: при доступном audit-engine вызвать get_task_context(task_id/view). Добавить required_tools/инструкцию в начальные этапы planning/execution/review flow, регрессионный simulation test порядка вызовов и явный offline fallback. Не создавать план до независимого plan-review; после review регистрировать его через record_plan.

## ✅ Фактическое решение (Actual resolution)
- 2026-10-29: Source plugin skill и installer template требуют get_task_context сразу после выбора flow до чтения кода; первый этап plan-drafting-flow уточнён, install regression и 14 targeted tests прошли.
