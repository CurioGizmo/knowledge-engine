---
id: aud-backend-2c24
title: verify_catalog гейт bundle-add-tests падает на всех ревизиях (предсуществующий
  сбой контроля)
category: backend
priority: important
autonomy: medium
complexity: M
status: completed
target_file: scripts/verify_catalog.py
symbol: _bundle_check
created_at: '2026-11-11'
updated_at: '2026-11-25'
---
# aud-backend-2c24: verify_catalog гейт bundle-add-tests падает на всех ревизиях (предсуществующий сбой контроля)

## ❌ Симптом
verify_catalog.py FAIL bundle-add-tests (selected_topic=False) на всех ревизиях каталога, включая базовую 9f929f8 без импортов

## 🔍 Техническая первопричина
Гейт bundle-add-tests проверяет профиль агента 'add-tests' и mandatory-критерии бандла; на текущем каталоге (как в базовом состоянии 9f929f8, так и после импорта скиллов) selected_topic=False — контроль не проходит на любой ревизии. Проверено на деревьях 28, 29, 38 и 47 скиллов — всегда FAIL, т.е. предсуществует импорту.

## 🛠 Предлагаемое решение
Актуализировать контроль в scripts/verify_catalog.py под текущий контракт бандла add-tests (какие критерии считаются mandatory и по какому запросу/теме поднимается бандл) либо починить содержимое карточки add-tests/AGENT-профиля, чтобы бандл действительно выбирался. Не блокирует мерж импорта скиллов — не связано с изменениями.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-25: Исправлено ранее: _bundle_check в scripts/verify_catalog.py:132-170 актуализирован под текущий профиль add-tests; прогон по mcp_skills даёт 11/11 PASS включая bundle-add-tests.
