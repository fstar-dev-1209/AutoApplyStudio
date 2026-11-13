@echo off
setlocal
cd /d "%~dp0"

echo === Auto Apply Studio Windows release build ===
echo.

if exist ".venv\Scripts\python.exe" (
  set PY=".venv\Scripts\python.exe"
  goto have_python
)
where py >nul 2>nul
if errorlevel 1 (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python was not found. Install Python 3.11+ from https://www.python.org/downloads/
    exit /b 1
  )
  set PY=python
) else (
  set PY=py -3
)
:have_python
echo Using %PY%

echo Installing build dependencies...
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements.txt pyinstaller
if errorlevel 1 exit /b 1

echo.
echo Building AutoApplyStudio.exe ...
%PY% -m PyInstaller --noconfirm --clean resume_exporter.spec
if errorlevel 1 exit /b 1

if not exist "release" mkdir release
copy /Y "dist\AutoApplyStudio.exe" "release\AutoApplyStudio.exe" >nul
echo.
echo Done.
echo Portable app: %cd%\release\AutoApplyStudio.exe
echo Double-click that file on any Windows PC.
echo.
endlocal
