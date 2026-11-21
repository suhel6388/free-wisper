@echo off
rem Start Wisper without a console window.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Wisper is not set up yet. Running setup.bat first...
    call setup.bat
)
start "" ".venv\Scripts\pythonw.exe" "wisper.pyw"
