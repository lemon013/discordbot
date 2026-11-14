@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 main.py
) else (
    python main.py
)
if errorlevel 1 (
    echo.
    echo main.py の起動に失敗しました。上のエラーを確認してください。
    pause
)
