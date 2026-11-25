---
id: aud-backend-c812
title: Неатомарность Pass 2 резолюции гипотез и избыточный sync в search_faceted
category: backend
priority: important
autonomy: high
complexity: M
status: completed
target_file: src/mcp_knowledge/use_cases/resolve_hypotheses.py
created_at: '2026-11-23'
updated_at: '2026-11-25'
---
# aud-backend-c812: Неатомарность Pass 2 резолюции гипотез и избыточный sync в search_faceted

## ❌ Симптом
Рассинхрон поискового индекса с диском при сбое части батча резолюций гипотез; трехкратный I/O оверхед при фасетном поиске знаний.

## 🔍 Техническая первопричина
Pass 2 в resolve_hypotheses.py:532-570 выполняет последовательные мутации на диске без try...finally для storage.sync(), теряя накопленные details при ошибке на k-м элементе. search_faceted вызывает sync() 3 раза за запрос.

## 🛠 Предлагаемое решение
Обернуть Pass 2 в try...finally с гарантированным self.storage.sync(); возвращать обработанные details при частичном отказе; передавать sync=False во внутренние вызовы search/get_project_overview в search_faceted.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-25: Исправлено ранее: Pass 2 в ResolveHypothesesUseCase.execute (src/mcp_knowledge/use_cases/resolve_hypotheses.py:567-604) обёрнут в try...finally с self.storage.sync() и возвратом partial_failure/details; search_faceted (src/mcp_knowledge/storage.py:977-1130) вызывает sync() 1 раз и передаёт sync=False.
