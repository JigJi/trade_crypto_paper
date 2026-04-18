@echo off
title Trading R&D Dashboard
echo ============================================
echo   TRADING R&D DASHBOARD
echo   http://localhost:5000
echo   Auto-restart on crash (5s delay)
echo ============================================
cd /d "%~dp0"

:loop
echo [%date% %time%] Starting dashboard...
python dashboard\app.py --debug
echo.
echo [%date% %time%] Dashboard stopped (exit code: %errorlevel%)
echo Restarting in 5 seconds... (Ctrl+C to stop)
timeout /t 5 /nobreak >nul
goto loop
