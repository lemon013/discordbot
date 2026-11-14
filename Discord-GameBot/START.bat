
@echo off
setlocal
cd /d "%~dp0"

echo Starting Discord GameBot...

if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo .env created.
        notepad ".env"
        echo Please configure DISCORD_BOT_TOKEN and restart.
        pause
        exit /b 1
    ) else (
        echo ERROR: .env.example not found.
        pause
        exit /b 1
    )
)

if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 (
        echo ERROR: Python installation failed.
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" -m pip install -r requirements.txt

if errorlevel 1 (
    echo ERROR: Library installation failed.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" bot.py

echo.
echo GameBot stopped.
pause
