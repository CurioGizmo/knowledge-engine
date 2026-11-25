---
id: aud-skills-ea8b
title: Утечка проектной специфики и устаревший формат ISS-X в github-plan-publishing
category: skills
priority: important
autonomy: high
complexity: M
status: completed
target_file: skills/general/github-plan-publishing/SKILL.md
created_at: '2026-11-23'
updated_at: '2026-11-24'
---
# aud-skills-ea8b: Утечка проектной специфики и устаревший формат ISS-X в github-plan-publishing

## ❌ Симптом
Загрязнение промптов нерелевантной ML/desktop спецификой, блокировка публикации бэкенд/CLI-задач из-за требования 4 UI-состояний, рассинхрон с форматом карточек aud-*.

## 🔍 Техническая первопричина
github-plan-publishing содержит захардкоженные термины конкретного десктоп-ML проекта (CUDA 12, Tauri, Win32, PyInstaller), устаревший формат ISS-X, безусловное требование 4 UI-состояний для всех задач, клиентский синтаксис call_mcp_tool/ask_question и не требует подтверждения человека перед push/PR.

## 🛠 Предлагаемое решение
Очистить скилл от чужой специфики, заменить ISS-X на aud-*, сделать 4 UI-состояния условными (только для UI), добавить обязательное подтверждение человека перед push и публикацией PR согласно README.md.

## ✅ Фактическое решение (Actual resolution)
- 2026-11-24: github-plan-publishing очищен от специфики ML/Tauri/Win32; синтаксис call_mcp_tool/ask_question заменен на нативные вызовы инструментов; префикс ISS-X заменен на aud-*/task_id; 4 UI-состояния сделаны условными; добавлен блок обязательного подтверждения человека.
