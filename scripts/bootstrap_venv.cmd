@echo off
setlocal

cd /d "%~dp0.."

if not exist ".venv" (
    py -3.11 -m venv .venv
)

call ".venv\Scripts\activate.bat"
if errorlevel 1 goto :fail

python -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto :fail

if exist "C:\Program Files\Teledyne\Spinnaker" (
    set "SPINNAKER_ROOT=C:\Program Files\Teledyne\Spinnaker"
    set "INCLUDE=%SPINNAKER_ROOT%\include;%SPINNAKER_ROOT%\dependencies\GenICam_v3_0\library\CPP\include;%INCLUDE%"
    set "LIB=%SPINNAKER_ROOT%\lib64\vs2015;%LIB%"
    set "PATH=%SPINNAKER_ROOT%\bin64;%SPINNAKER_ROOT%\cti64\vs2015;%PATH%"
)

python -m pip install -r requirements-windows-py311.lock
if errorlevel 1 goto :fail

python -m pip install -e . --no-deps
if errorlevel 1 goto :fail

echo.
echo .venv is ready.
echo Run: scripts\run_gui.cmd
exit /b 0

:fail
echo.
echo Failed to bootstrap .venv.
exit /b 1
