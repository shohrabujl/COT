@echo off
setlocal
cd /d "%~dp0"
title UJL Plan Control Center

set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY goto :nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
if errorlevel 1 goto :oldpython

if not exist ".venv\Scripts\python.exe" (
  echo First start: preparing a private Python environment...
  %PY% -m venv .venv
  if errorlevel 1 goto :fail
)

".venv\Scripts\python.exe" -c "import flask, openpyxl" >nul 2>nul
if errorlevel 1 (
  echo Installing Flask and openpyxl. This needs internet once, about a minute...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
  if errorlevel 1 goto :fail
)

".venv\Scripts\python.exe" run.py
goto :end

:nopython
echo.
echo Python 3.9 or newer was not found on this computer.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
echo then double-click start.bat again.
goto :end

:oldpython
echo.
echo This computer has an older Python. Version 3.9 or newer is needed.
echo Install the latest from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
echo then double-click start.bat again.
goto :end

:fail
echo.
echo Setup did not finish. Check the internet connection and run start.bat again.

:end
echo.
pause
