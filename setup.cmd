@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    py -3.10 -m venv .venv
    if errorlevel 1 exit /b 1
)
.venv\Scripts\python.exe -m pip --isolated install --index-url https://pypi.org/simple --only-binary=:all: -r requirements-lock.txt
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe -m pip check
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe -B run_tests.py
if errorlevel 1 exit /b 1
echo Setup complete. For camera use, run .venv\Scripts\python.exe download_model.py
