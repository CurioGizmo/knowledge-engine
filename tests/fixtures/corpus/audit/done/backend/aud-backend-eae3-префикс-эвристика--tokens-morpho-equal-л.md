---
id: aud-backend-eae3
title: 'Префикс-эвристика _tokens_morpho_equal: ложный duplicate (table/tablet) и
  ложный distinct (цикле/циклах, кэша/кэшей)'
category: backend
priority: suggestion
autonomy: high
complexity: M
status: completed
target_file: src/mcp_knowledge/storage.py
symbol: _tokens_morpho_equal
created_at: '2026-11-09'
updated_at: '2026-11-24'
pr_url: https://github.com/Ariadne-Tech/mcp-knowledge-engine/commit/d5e0fad
---
# aud-backend-eae3: Префикс-эвристика _tokens_morpho_equal: ложный duplicate (table/tablet) и ложный distinct (цикле/циклах, кэша/кэшей)

## ❌ Симптом
Ревью r3, adversarial-матрица: «воркер в цикле опроса» vs «воркеры в циклах опроса» (одна причина) → ложный distinct; «table vs tablet» (разные слова) → ложный duplicate в классификаторе confirm-дублей.

## 🔍 Техническая первопричина
_tokens_morpho_equal (src/mcp_knowledge/storage.py:422) использует 5-символьный префикс для длинных токенов и префиксное включение для коротких. Это даёт два класса ошибок: (а) ложный duplicate для семантически разных слов с общим префиксом (table/tablet); (б) ложный distinct для русских е/х-флексий, где 5-символьные префиксы расходятся (цикле/циклах, ядре/ядрах, кэша/кэшей).

## 🛠 Предлагаемое решение
Заменить префикс-эвристику стеммингом: подключить уже существующий stem_russian из mcp_core.sanitizer (используется в FTS-индексации skills) для свёртки русских форм перед сравнением; для латиницы — простой суффикс-стеммер или минимальная длина различия (например, относительное расстояние Левенштейна < 0.3 при общем префиксе). Регресс-тесты: table/tablet → не эквивалентны; цикле/циклах, кэша/кэшей → эквивалентны.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-24: Решено в коммитах d6436b0 и d5e0fad: 5-символьная эвристика заменена на гибридный алгоритм со стеммингом (stem_russian с 4-5 буквами) и белыми списками флексий _RU_FLEXION_SUFFIXES / _EN_FLEXION_SUFFIXES.
