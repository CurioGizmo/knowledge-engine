---
id: aud-backend-7c16
title: verify_catalog top1:code review хрупок к размеру корпуса FTS (ломается при
  любом росте каталога >~30 скиллов)
category: backend
priority: important
autonomy: medium
complexity: M
status: planned
target_file: scripts/verify_catalog.py
symbol: CONTROL_QUERIES
created_at: '2026-11-11'
updated_at: '2026-11-11'
---

# aud-backend-7c16: verify_catalog top1:code review хрупок к размеру корпуса FTS (ломается при любом росте каталога >~30 скиллов)

## ❌ Симптом
verify_catalog.py FAIL top1:code review (top1=receiving-code-review вместо code-review) при росте каталога скиллов выше ~30 документов

## 🔍 Техническая первопричина
SQLite FTS5 bm25 возвращает -0.0 для частых термов в малом корпусе (N<~30): при росте каталога (импорт скиллов) IDF термов 'code'/'review' становится ненулевым, включается length-нормализация, и длинный code-review (75KB) проигрывает коротким plan-review (40KB) и receiving-code-review. Эмпирически: 29 доков → top1=code-review (PASS), 32/38/47 доков → top1=plan-review/receiving-code-review (FAIL), независимо от содержимого добавленных скиллов (контроль: дубли существующего скилла дают тот же эффект).

## 🛠 Предлагаемое решение
Либо реранковать в verify_catalog CONTROL_QUERIES с учётом match_name/match_title флагов (сейчас поле flags уже вычисляется, но порядок определяется сырым bm25), либо добавить name-буст в _rerank_and_fuzzy_skills для точного совпадения имени скилла с запросом, либо зафиксировать контрольный запрос как 'strict code review' с уникальным термом. Воспроизводимость: python scripts/verify_catalog.py на дереве с 29 SKILL.md (PASS) и +3 любых скилла (FAIL top1:code review).
