---
id: aud-backend-7951
title: 'Асимметрия K-HYP: check_duplicate_hypothesis не фильтрует symptom-токены —
  конкурирующие гипотезы блокируются на создании'
category: backend
priority: important
autonomy: high
complexity: M
status: planned
target_file: src/mcp_knowledge/storage.py
symbol: check_duplicate_hypothesis
created_at: '2026-11-09'
updated_at: '2026-11-09'
---

# aud-backend-7951: Асимметрия K-HYP: check_duplicate_hypothesis не фильтрует symptom-токены — конкурирующие гипотезы блокируются на создании

## ❌ Симптом
Live-проб ревью (шаг 3): вторая конкурирующая гипотеза с тем же симптомом и другой причиной отклоняется на propose_hypothesis как «похожая активная гипотеза» — overlap причин 0.5 ≥ 0.45 из-за общих слов симптома. Фикс K-HYP лечит только confirm-путь, до которого карточка не доходит.

## 🔍 Техническая первопричина
Фикс K-HYP применён только в classify_confirm_candidate (storage.py:1879-1945): overlap причин пересчитывается по distinct-токенам с исключением symptom_tokens. check_duplicate_hypothesis (строки 1853-1859) оставлен без фильтра — hypo_overlap там считается по полным текстам причин, где общие слова симптома дают overlap >= 0.45.

## 🛠 Предлагаемое решение
Применить тот же symptom_tokens-фильтр в check_duplicate_hypothesis: перед пороговым сравнением hypo_overlap < ANTIPATTERN_REFUTED_THRESHOLD пересчитать overlap по (tokens_hypo - symptom_tokens) и (tokens_cand - symptom_tokens). Регресс-тест: две конкурирующие гипотезы («задержка из-за DNS» vs «задержка из-за пула PostgreSQL») — вторая должна создаваться без force.
