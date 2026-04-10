@echo off
setlocal

cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo .venv is missing. Run scripts\bootstrap_venv.cmd first.
    exit /b 1
)

call ".venv\Scripts\activate.bat"
if errorlevel 1 exit /b 1

python main.py
