---
id: aud-backend-b7e9
title: Дефекты межпроцессных блокировок и падение fsync каталога на Windows в sync.py
category: backend
priority: critical
autonomy: high
complexity: M
status: completed
target_file: src/mcp_core/sync.py
created_at: '2026-11-23'
updated_at: '2026-11-24'
---
# aud-backend-b7e9: Дефекты межпроцессных блокировок и падение fsync каталога на Windows в sync.py

## ❌ Симптом
Гонка PID в локах карточек, падение перемещения карточек с PermissionError на Windows и гонка удаления лок-файла.

## 🔍 Техническая первопричина
В src/mcp_core/sync.py:175-178 move_card открывает lock_path на 'w' и пишет PID до fcntl.flock; в 204-208 вызывает os.open(dir, os.O_RDONLY) на Windows; в 57-60 _stale_sweep делает LOCK_UN перед unlink.

## 🛠 Предлагаемое решение
Не открывать лок-файл с усечением до flock; писать PID только после взятия лока; проверять os.name != 'nt' перед fsync каталога; не вызывать LOCK_UN перед unlink в _stale_sweep.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-24: В src/mcp_core/sync.py:175-208, 57-60 лок-файл открывается через os.O_CREAT | os.O_RDWR без усечения, PID пишется строго после flock, исключен LOCK_UN до unlink в _stale_sweep, fsync каталога отключен на Windows (sys.platform == 'win32'). Покрыто тестами в test_audit_storage_and_sync.py (AUDIT-FIX-P0).
