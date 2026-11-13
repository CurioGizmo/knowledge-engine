---
id: aud-backend-1191
title: 'record_debt без Anti-Spam Gate: молчаливые дубли и непроверенные target_file/symbol
  принимают ''../..'' — мусор и path-инъекция в реестре техдолга'
category: backend
priority: critical
autonomy: high
complexity: M
status: completed
target_file: src/mcp_audit/server.py
symbol: record_debt
task_id: mcp-simulation-round2
created_at: '2026-10-13'
updated_at: '2026-11-09'
---
# aud-backend-1191: record_debt без Anti-Spam Gate: молчаливые дубли и непроверенные target_file/symbol принимают '../..' — мусор и path-инъекция в реестре техдолга

## ❌ Симптом
Симуляция A10 раунда 2: record_debt принимает пустой/whitespace title (созданы aud-backend-61b3, aud-backend-6fb0), дубли одной и той же карточки создаются молча (aud-backend-2623) без DUPLICATE_FOUND, а target_file и symbol принимают значения вида '../../etc/passwd' без санитайза. Мусорные карточки доходят до get_ready_backlog и портят выдачу исполнителю.

## 🔍 Техническая первопричина
В отличие от record_knowledge (структурный anti-spam gate: минимальные длины symptom/root_cause/solution/rationale, фильтр чеклистов и стектрейсов) и category (которая валидируется — '../evil' отклоняется), в record_debt нет гейта на содержательность полей, нет детекции дублей по title/symptom+target_file, и поля target_file/symbol не проходят проверку на выход за пределы проекта и на допустимые символы.

## 🛠 Предлагаемое решение
Перенести в record_debt тот же гейт, что в record_knowledge: (1) отказ при пустом/whitespace title и symptom короче порога (reason=INVALID_ARGUMENTS / ANTI_SPAM_REJECTED с точными лимитами); (2) двухфазная детекция дублей по нормализованному title+target_file+symbol с force-обходом; (3) санитайз target_file/symbol через is_safe_name/resolve+is_relative_to(project_root) — при выходе за корень отклонять; (4) enum-валидация priority (пересекается с aud-backend-4044 — закрыть вместе). Тесты на каждый пункт.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-09: A-DEBT: валидация title (запрет пустых строк/пробелов), target_file и symbol в record_debt.
