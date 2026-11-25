---
id: aud-skills-70a9
title: Двухбуквенный поиск матчит подстроку внутри любых слов с весом 1.0
category: skills
priority: critical
autonomy: high
complexity: M
status: completed
target_file: src/mcp_skills/storage.py
created_at: '2026-11-23'
updated_at: '2026-11-24'
---
# aud-skills-70a9: Двухбуквенный поиск матчит подстроку внутри любых слов с весом 1.0

## ❌ Симптом
Двухбуквенный поиск матчит подстроки внутри любых слов и вытесняет релевантные карточки с наивысшим скором 1.0.

## 🔍 Техническая первопричина
В src/mcp_skills/storage.py:595 условие `substr_hit = any(f' {st} ' in f' {doc_lower} ' or st in doc_lower for st in short_tokens)` содержит подстрочный поиск без границ слов. Любое вхождение (id в guide, ai в container, go в algorithm) дает score = 1.0.

## 🛠 Предлагаемое решение
Убрать st in doc_lower, использовать short_acronym_matches(short_tokens, doc_blob) с границами слов или regex \b{st}\b.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-24: Устранён ложный подстрочный матчинг st in doc_lower в mcp_skills/storage.py; поиск 2-буквенных токенов ограничен границами слов через short_acronym_matches. Покрыто тестами в test_e_search_integrity_fixes.py (AUDIT-FIX-P0).
