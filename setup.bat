@echo off
rem One-time setup: creates a local Python environment and installs dependencies.
cd /d "%~dp0"
setlocal

set "PY=py -3.12"
%PY% -c "import sys" >nul 2>nul
if errorlevel 1 set "PY=python"

echo Using %PY%
%PY% -m venv .venv || goto :fail
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install --only-binary=:all: -r requirements.txt || goto :fail

echo.
echo Setup complete. Double-click run.bat to start Wisper.
pause
exit /b 0

:fail
echo.
echo Setup failed. See the messages above.
pause
exit /b 1
