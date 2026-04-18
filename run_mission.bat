@echo off
REM ============================================================
REM  Crypto Research Agent - Daily Mission
REM  Launches Claude as autonomous research agent
REM ============================================================
cd /d D:\0_product_dev\trade_crypto
claude --dangerously-skip-permissions -p "You are a Crypto Research Agent. Read missions/instruction.md for your full briefing, then execute today's research mission autonomously. IMPORTANT: Always save results to research/missions.json -- completed missions appear as pins on the Dashboard World Map."
pause
