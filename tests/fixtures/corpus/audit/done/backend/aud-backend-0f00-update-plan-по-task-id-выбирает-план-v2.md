---
id: aud-backend-0f00
title: update_plan по task_id выбирает план V2 из-за совпадения префикса имени
category: backend
priority: critical
autonomy: high
complexity: M
status: completed
target_file: src/mcp_audit/repository.py
symbol: PlanRepository.get_unique
task_id: AUDIT-REMEDIATION-2026-11-V2
created_at: '2026-11-05'
updated_at: '2026-11-09'
---
# aud-backend-0f00: update_plan по task_id выбирает план V2 из-за совпадения префикса имени

## ❌ Симптом
При существовании планов AUDIT-REMEDIATION-2026-11 и AUDIT-REMEDIATION-2026-11-V2 в одной категории вызов update_plan(task_id=первый, status=cancelled, category=backend) изменил второй план и вернул его ID. Статус второго восстановлен через точный task_id; первый остался discussed.

## 🔍 Техническая первопричина
PlanRepository.get_unique объединяет точные индексные совпадения с дисковыми кандидатами _plan_disk_matches по glob f'*-{task_id}-*.md'; glob первого task_id захватывает файл V2. При нескольких совпадениях внутри категории выбирается наиболее свежий по mtime без сверки frontmatter.task_id.

## 🛠 Предлагаемое решение
Фильтровать дисковые кандидаты по точному frontmatter.task_id до объединения и выбора; не разрешать неоднозначность внутри категории по mtime для разных task_id. Регресс-тест: plan task-1 + task-1-v2, update_plan(task-1) меняет только первый; проверить step_plan и record_review, использующие тот же lookup.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-09: A-PREFIX: PlanRepository и synchronizer фильтруют дисковые кандидаты по точному frontmatter.task_id до выбора по mtime, устраняя коллизии префиксов задач.
