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
echo [%date% %time%] Killing any orphan dashboard processes...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*dashboard*app*' -or $_.CommandLine -like '*dashboard.app*' } | Where-Object { $_.ProcessId -ne $PID } | ForEach-Object { Write-Host ('  orphan PID ' + $_.ProcessId + ' -> kill'); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 2 /nobreak >nul

echo [%date% %time%] Starting dashboard...
python -c "from dashboard.app import app; app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=True)"

echo.
echo [%date% %time%] Dashboard stopped. Restarting in 3 seconds...
timeout /t 3 /nobreak >nul
goto loop
