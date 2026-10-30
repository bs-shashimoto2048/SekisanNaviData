@echo off
rem SekisanNaviData production inference API: start (never starts twice). Used from the Startup folder (after user logon) or by hand.
rem Not a Windows service. This file is ASCII only on purpose (cmd.exe reads batch files in the OEM code page); Japanese messages come from prod_ctl.py.
rem prod_ctl.py checks the listen address / port / health and the model identity, and prints a fixed explanation on failure.
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Cannot start: the runtime environment ^(.venv^) was not found. Contact the administrator.
  timeout /t 30 >nul
  exit /b 1
)
"%PY%" -X utf8 prod_ctl.py start
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo START FAILED ^(code %RC%^). The API is not running. See the explanation above.
  timeout /t 60 >nul
)
exit /b %RC%
