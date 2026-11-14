@echo off
cd /d "%~dp0"
if exist "Discord-GameBot\.venv\Scripts\python.exe" (
  "Discord-GameBot\.venv\Scripts\python.exe" pc_main.py
) else (
  py -3 pc_main.py
)
pause
