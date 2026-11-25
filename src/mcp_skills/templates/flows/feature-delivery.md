---
title: "Feature Delivery Workflow"
description: "Канонический процесс пошаговой реализации фичи с контролем качества и защитой от галлюцинаций."
aliases:
  - "реализация фичи"
  - "разработка"
  - "feature delivery"
  - "выполни задачу"
intent_group: "feature-delivery"
when_to_use:
  - "реализация новой функциональности или фичи"
  - "сквозная разработка от ТЗ до верификации"
---

# Feature Delivery Workflow

Канонический процесс пошаговой реализации фичи с контролем качества и защитой от галлюцинаций.

## 1. Анализ ТЗ [id: spec_analysis]
<!-- meta: {"require_human_confirm": true} -->
Проанализируй требования в задаче. Выяви краевые случаи, неопределенности и технические риски.
Если есть белые пятна — сформулируй вопросы пользователю через `ask_question`.
Когда все требования ясны — вызови `step_flow(action="advance", deliverable="Анализ завершен, требования согласованы", notes="Анализ спецификации завершён; требования согласованы с пользователем")`.

## 2. Проектирование плана [id: plan_draft]
<!-- meta: {"validator": "validate_plan", "target_file": "implementation_plan.md", "deliverable_must_exist": true} -->
Составь или обнови `implementation_plan.md` по стандартам платформы.
Убедись, что план содержит User Review Required, Open Questions, Proposed Changes и Verification Plan.
После завершения плана вызови `step_flow(action="advance", deliverable="implementation_plan.md")`. Валидатор автоматически проверит наличие и непустоту файла плана.

## 3. Цикл ревью плана [id: plan_review_loop]
<!-- meta: {"agent": "plan-reviewer", "loop_max": 3} -->
Проведи аудит плана с помощью субагента-ревьюера (`plan-reviewer`).
Если ревьюер выявил замечания — внеси правки в `implementation_plan.md` и вызови `step_flow(action="advance", verdict="needs_work", notes="...")`.
Когда замечаний нет — вызови `step_flow(action="advance", verdict="approved", notes="План утвержден")`.

## 4. Декомпозиция и параллелизация [id: dag_split]
<!-- meta: {"require_verdict": true} -->
Разбей утвержденный план на независимые стримы подзадач (DAG).
Определи, какие подзадачи могут выполняться параллельно, а какие зависят от результатов предыдущих.
Сформируй перечень задач и вызови `step_flow(action="advance", deliverable="Декомпозиция завершена")`.

## 5. Параллельная реализация [id: parallel_coding]
<!-- meta: {"dispatch_agents": true} -->
Запусти реализацию подзадач с помощью специализированных субагентов-кодеров.
Для независимых подзадач используй параллельный запуск.
После завершения кодирования всех компонентов вызови `step_flow(action="advance", deliverable="Реализация завершена")`.

## 6. Верификация и интеграция [id: qa_verification]
Выполни полный цикл верификации:
1. Запусти автоматические тесты проекта.
2. Выполни проверку типов и линтинг (`mypy`, `ruff`).
3. Убедись в отсутствии регрессий.
После успешной проверки вызови `step_flow(action="advance", deliverable="Верификация пройдена успешно")`.
