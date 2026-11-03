@echo off
rem SE daily review - call from Task Scheduler. ASCII ONLY (cmd reads this as ANSI).
rem Replace the two paths below with your own.
set PY=D:\YOUR_BOT\.venv\Scripts\python.exe
set SE=D:\YOUR_BOT\SE\evolve.py
set LOG=D:\YOUR_BOT\SE\se_task.log

echo [%date% %time%] ---- review start ---- >> "%LOG%"
"%PY%" "%SE%" --review >> "%LOG%" 2>&1
echo [%date% %time%] ---- exit %errorlevel% ---- >> "%LOG%"
