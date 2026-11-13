---
id: aud-backend-66e1
title: Дедуп record_debt не нормализует target_file кандидатов — легаси-карточки с
  backslash-путями обходят гейт
category: backend
priority: suggestion
autonomy: high
complexity: L
status: planned
target_file: src/mcp_audit/server.py
symbol: record_debt
created_at: '2026-11-09'
updated_at: '2026-11-09'
---

# aud-backend-66e1: Дедуп record_debt не нормализует target_file кандидатов — легаси-карточки с backslash-путями обходят гейт

## ❌ Симптом
Ревью шага 3: дубликат-карточка может быть создана поверх легаси-карточки с ненормализованным target_file (backslash/«./»-форма), т.к. дедуп сравнивает строки буквально.

## 🔍 Техническая первопричина
Дедуп-гейт record_debt сравнивает norm_target (нормализованный «src/foo.py») с candidate.target_file из индекса как есть (:480-500). Легаси-карточки, записанные до нормализации (с «src\\foo.py» или «./src/foo.py»), не совпадут с канонической формой.

## 🛠 Предлагаемое решение
Нормализовать cand_target при сравнении в дедуп-цикле (та же замена разделителей + свёртка «./»), либо запланировать миграцию легаси-значений при следующем пересборе индекса.
