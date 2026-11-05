@echo off
rem SekisanNaviData operations console (127.0.0.1:8765): start. Not a service, not a scheduled task, not registered in the Startup folder.
rem Only the process recorded by ops_ctl.py (PID + creation time + command line) is handled. No kill by image name.
rem This file is ASCII only on purpose (cmd.exe reads batch files in the OEM code page); Japanese messages come from ops_ctl.py.
rem Python: %OPS_PYTHON% if set, else the first line of ops_python.local.txt next to this file (git-ignored, optional), else .venv\Scripts\python.exe next to this file.
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PY=%OPS_PYTHON%"
if not defined PY if exist "%~dp0ops_python.local.txt" set /p PY=<"%~dp0ops_python.local.txt"
if not defined PY set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Cannot start: Python was not found ^(.venv or OPS_PYTHON^). Contact the administrator.
  ping -n 31 127.0.0.1 >nul
  exit /b 1
)
"%PY%" -X utf8 ops_ctl.py start %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo START FAILED ^(code %RC%^). See the explanation above.
  ping -n 61 127.0.0.1 >nul
)
exit /b %RC%
