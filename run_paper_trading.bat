@echo off
title Paper Trading v3
cd /d "D:\0_product_dev\trade_crypto"

:loop
echo [%date% %time%] Starting paper trader...
"C:\Users\alprdev\AppData\Local\Programs\Python\Python310\python.exe" -u paper_trading\paper_trader.py
echo [%date% %time%] Paper trader exited with code %errorlevel%. Restarting in 10s...
timeout /t 10
goto loop
