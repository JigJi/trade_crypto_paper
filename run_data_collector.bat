@echo off
title Data Collector v1
cd /d "D:\0_product_dev\trade_crypto"

:loop
echo [%date% %time%] Killing any orphan data_collector processes...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*data_collector/daemon.py*' -or $_.CommandLine -like '*data_collector\daemon.py*' } | Where-Object { $_.ProcessId -ne $PID } | ForEach-Object { Write-Host ('  orphan PID ' + $_.ProcessId + ' -> kill'); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 2 /nobreak >nul

echo [%date% %time%] Starting data collector...
python -u data_collector/daemon.py
echo [%date% %time%] Exited with code %errorlevel%. Restarting in 10s...
timeout /t 10
goto loop
