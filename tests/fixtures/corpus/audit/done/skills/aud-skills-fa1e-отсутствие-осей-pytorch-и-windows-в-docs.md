---
id: aud-skills-fa1e
title: Отсутствие осей pytorch и windows в docs/stack-conventions.yaml
category: skills
priority: important
autonomy: high
complexity: L
status: completed
target_file: docs/stack-conventions.yaml
created_at: '2026-11-23'
updated_at: '2026-11-24'
---
# aud-skills-fa1e: Отсутствие осей pytorch и windows в docs/stack-conventions.yaml

## ❌ Симптом
Неполная спецификация стековых осей в docs/stack-conventions.yaml и отсутствие контроля конвенций в валидаторе.

## 🔍 Техническая первопричина
Скилы profile-lora-training и windows-packaging-ci требуют pytorch и windows, однако эти оси отсутствуют в docs/stack-conventions.yaml. scripts/verify_catalog.py не проверяет валидность осей по конвенциям.

## 🛠 Предлагаемое решение
Добавить спецификации осей pytorch и windows в docs/stack-conventions.yaml и добавить проверку в scripts/verify_catalog.py.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-24: Оси pytorch и windows добавлены в docs/stack-conventions.yaml, а проверка осей включена в scripts/verify_catalog.py.
