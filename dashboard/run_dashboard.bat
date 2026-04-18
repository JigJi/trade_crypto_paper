@echo off
title Trade Crypto Dashboard (auto-reload)
cd /d "%~dp0.."

echo ============================================
echo   Trade Crypto Dashboard - Auto Reload
echo   Port: 5000
echo   Press Ctrl+C to stop
echo ============================================
echo.

:loop
echo [%date% %time%] Starting dashboard...
python -c "from dashboard.app import app; app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=True)"

echo.
echo [%date% %time%] Dashboard stopped. Restarting in 3 seconds...
timeout /t 3 /nobreak >nul
goto loop
