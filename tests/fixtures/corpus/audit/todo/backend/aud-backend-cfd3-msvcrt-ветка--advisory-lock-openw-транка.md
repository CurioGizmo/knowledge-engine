---
id: aud-backend-cfd3
title: 'msvcrt-ветка _advisory_lock: open(\"w\") транкатирует lock-файл и LK_LOCK-таймаут
  ~10с не документирован'
category: backend
priority: suggestion
autonomy: high
complexity: L
status: planned
target_file: src/mcp_audit/synchronizer.py
symbol: _advisory_lock
created_at: '2026-11-09'
updated_at: '2026-11-09'
---

# aud-backend-cfd3: msvcrt-ветка _advisory_lock: open(\"w\") транкатирует lock-файл и LK_LOCK-таймаут ~10с не документирован

## ❌ Симптом
Windows-специфичный риск: параллельный open(\"w\") на lock-файле может транкатировать залоченные байты мимо локa; длительная конкуренция >10с завершается сырым OSError без внятного сообщения.

## 🔍 Техническая первопричина
_advisory_lock открывает lock-файл через open(\"w\") (synchronizer.py:75) — режим с обрезкой. На Windows транкатация (SetEndOfFile) конкурующим процессом может пройти мимо байтового лока (LockFile защищает область 0-1, но не обрезку хвоста) — классическая гонка truncate-vs-lock. Также семантика msvcrt.LK_LOCK (10 попыток × 1с, затем OSError) не отражена в docstring.

## 🛠 Предлагаемое решение
Открывать lock-файл без обрезки: open(\"a\") или os.open(path, O_CREAT|O_RDWR) в обеих ветках (fcntl и msvcrt). Дополнить docstring: «Windows-ветка msvcrt.LK_LOCK ждёт до ~10 сек и бросает OSError; при длительной конкуренции это ошибка, а не бесконечное ожидание».
