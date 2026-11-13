---
id: aud-backend-479a
title: 'search_artifacts не индексирует русские структурные заголовки и task_id планов:
  ''ревью план''/''план'' → 0, а ''Review'' → 2; по task_id план не находится'
category: backend
priority: important
autonomy: high
complexity: M
status: completed
target_file: src/mcp_audit/synchronizer.py
symbol: sync_artifacts
task_id: mcp-simulation-round2
created_at: '2026-10-13'
updated_at: '2026-11-09'
---
# aud-backend-479a: search_artifacts не индексирует русские структурные заголовки и task_id планов: 'ревью план'/'план' → 0, а 'Review' → 2; по task_id план не находится

## ❌ Симптом
Симуляция A7: после record_plan + record_review поиск search_artifacts(query='ревью план') → 0 и query='план'/'ревью' → 0, тогда как query='Review'/'Code Review' → 2 (находит тот же артефакт). Запрос по task_id (query='SIM-700') возвращает ревью, но НЕ план — у плана task_id отсутствует в индексируемых полях/title.

## 🔍 Техническая первопричина
В индекс artifacts_fts попадают только латинские/служебные поля артефакта, русские заголовки секций и task_id плана не добавляются в индексируемый текст (тот же класс дефекта, что aud-backend-c77b для knowledge, но в audit-ветке и с обратной стороной: там заголовки-шаблоны создают шум, здесь нужные русские слова и идентификатор задачи вообще отсутствуют). Плюс title плана не включает task_id, поэтому точный поиск по идентификатору задачи даёт частичный результат.

## 🛠 Предлагаемое решение
Добавить в индексируемый текст артефактов: (1) task_id (отдельной колонкой или в title/body) — для планов, ревью и долгов единообразно; (2) нормализованные русские названия типа артефакта и заголовков секций через словарь синонимов (план/plan, ревью/review, аудит/audit), а не дословный шаблонный заголовок. Регресс-тесты: record_plan(task_id=X) → search_artifacts(query=X) находит план; query='ревью план' → count>0. Скоординировать с aud-backend-c77b (единый подход к заголовкам).

## ✅ Фактическое решение (Actual resolution)
- 2026-11-09: A-ARTIFACTS: индексация task_id в body_parts планов и ревью, переиндексация через bump схемы v6 и mtime_ns=-1.
