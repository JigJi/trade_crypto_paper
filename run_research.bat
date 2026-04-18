@echo off
title Research Scheduler
echo ============================================
echo   RESEARCH SCHEDULER (AUTO-DISCOVERY)
echo   Jobs: validate, test_untested, coin_screening, leaderboard
echo ============================================
cd /d "%~dp0"
python research\scheduler.py
pause
