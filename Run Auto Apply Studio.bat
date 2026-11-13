@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" app_main.py
) else (
  py -3 app_main.py
)
if errorlevel 1 pause
