@echo off
rem SE command watcher - call every 2 minutes from Task Scheduler. ASCII ONLY.
rem Night guard: skip before 08:00 (bot is idle, no need to spend process spawns).
set H=%time:~0,2%
set H=%H: =0%
if %H% LSS 8 exit /b 0

set PY=D:\YOUR_BOT\.venv\Scripts\python.exe
set SE=D:\YOUR_BOT\SE\evolve.py
set LOG=D:\YOUR_BOT\SE\se_task.log

"%PY%" "%SE%" --watch-once >> "%LOG%" 2>&1
