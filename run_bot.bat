@echo off
title ORCA Bot
cd /d "%~dp0"

echo ============================================================
echo   Запуск ORCA Bot
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ОШИБКА] Не найдено .venv
    pause
    exit /b 1
)

".venv\Scripts\python.exe" bot_main.py

echo.
echo Бот завершён.
pause