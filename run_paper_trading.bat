@echo off
title Paper Trading v3
cd /d "D:\0_product_dev\trade_crypto"

:loop
echo [%date% %time%] Killing any orphan paper_trader processes...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*paper_trader.py*' -and $_.ProcessId -ne $PID } | ForEach-Object { Write-Host ('  orphan PID ' + $_.ProcessId + ' -> kill'); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 2 /nobreak >nul

echo [%date% %time%] Starting paper trader...
"C:\Users\alprdev\AppData\Local\Programs\Python\Python310\python.exe" -u paper_trading\paper_trader.py
echo [%date% %time%] Paper trader exited with code %errorlevel%. Restarting in 10s...
timeout /t 10
goto loop
