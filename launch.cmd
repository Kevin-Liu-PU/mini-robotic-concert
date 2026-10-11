@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Run setup.cmd first.
    pause
    exit /b 1
)
.venv\Scripts\python.exe -m conductor.app %*
if errorlevel 1 pause
