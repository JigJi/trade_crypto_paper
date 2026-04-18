@echo off
title Data Collector v1
cd /d "D:\0_product_dev\trade_crypto"
:loop
echo [%date% %time%] Starting data collector...
python -u data_collector/daemon.py
echo [%date% %time%] Exited with code %errorlevel%. Restarting in 10s...
timeout /t 10
goto loop
