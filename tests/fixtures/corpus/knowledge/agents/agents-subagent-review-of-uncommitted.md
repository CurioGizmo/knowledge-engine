---
id: agents-subagent-review-of-uncommitted
title: 'Subagent review of uncommitted changes: Workspace=share creates worktree from
  HEAD — reviewer sees empty diff'
category: agents
type: lesson
status: active
confidence: 0.75
task_id: docs-audit-2026-10-01
refuted_by: null
refutation_reason: null
generated:
  by: process
  agent: null
  created_at: '2026-10-01'
verified: []
stale_after: null
supersedes: null
superseded_by: null
related_to: []
derived_from: null
aliases: []
tags:
- invoke_subagent
- workspace
- share-vs-inherit
- code-review
- false-approve
- antigravity
---

### Симптом / Контекст
Дельта-ревью субагентом незакоммиченных правок: вердикт «всё чисто/Approve», но проверки фиксов дают противоречивый результат; git-worktree ревьюера не содержит свежих правок (нет бейджей/якорей из diff).

### Первопричина (Root Cause)
invoke_subagent с Workspace='share' создаёт git-worktree от HEAD: незакоммиченные правки и untracked-файлы в него не попадают. Ревьюер физически не видит свежих фиксов и читает пустой/устаревший diff, при этом не сообщает об этом явно.

### Решение / Выводы
Для ревью рабочей копии вызывать invoke_subagent с Workspace='inherit' (прямой доступ к /home/user/projects/mcp-knowledge-engine). Диагностика: grep известных свежих маркеров (например img.shields.io в README) в worktree-пути ревьюера. В промпт ревьюеру добавлять маркер свежести: «бейджи обязаны присутствовать, иначе ты читаешь не тот diff». Плюс: сторонние сбои (external model error на позднем шаге) требуют перезапуска с тем же промптом.

### Обоснование / Почему именно так (Rationale)
Ошибка тихая: ревьюер честно отчитывается «проверил diff, всё чисто», хотя diff пуст/старый — вердикт оказывается ложнопозитивным и вводит в заблуждение цикл approve.

### Нюансы и ограничения (Nuances)
не пуать с branch-воркспейсом (клон для изоляции изменений — там незакоммиченное тоже не попадает). Маркер свежести: попросить ревьюера убедиться в наличии известной незакоммиченной строки.

### Журнал сбоев и наблюдений (Log)
Записей пока нет.
