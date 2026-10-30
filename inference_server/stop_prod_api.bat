@echo off
rem SekisanNaviData production inference API: stop. Stops ONLY the PID recorded at start (verified by creation time and command line). No kill by image name.
rem It waits for the running inference to finish (grace = shutdown_grace_sec in the config). The shared server.yaml is not changed.
rem This file is ASCII only on purpose (cmd.exe reads batch files in the OEM code page); Japanese messages come from prod_ctl.py.
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Cannot stop: the runtime environment ^(.venv^) was not found. Contact the administrator.
  timeout /t 30 >nul
  exit /b 1
)
"%PY%" -X utf8 prod_ctl.py stop
set "RC=%ERRORLEVEL%"
echo.
timeout /t 10 >nul
exit /b %RC%
