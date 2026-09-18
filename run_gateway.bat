@echo off
REM ===================================================================
REM  Nexucon field gateway
REM
REM  Watches a folder of instrument exports and pushes each settled
REM  file to the platform. Runs on the site machine, not on the server,
REM  and under a settings module with no database configured — this
REM  laptop holds no credential to the registry, only a device token
REM  for its own instrument.
REM
REM  Point Windows Task Scheduler at THIS FILE. See FIELD_GATEWAY.md for
REM  the task's three boxes (Program, Arguments, Start in) and for what
REM  to check when a file does not appear on the platform.
REM
REM  Usage by hand:
REM      run_gateway.bat                  watch, using gateway.json beside this file
REM      run_gateway.bat --once           sweep once and exit
REM      run_gateway.bat --once --dry-run report only; sends nothing
REM
REM  Any argument is passed straight to the gateway as a flag, so the
REM  config file is always gateway.json beside this script. To use a
REM  different one, set GATEWAY_CONFIG to its full path first.
REM
REM  Exit codes: 0 stopped cleanly (Ctrl+C), 2 setup problem (the
REM  message says which), 1 the gateway refused to run or stopped on an
REM  error — which is what makes Task Scheduler show a failed task.
REM ===================================================================

setlocal

set "REPO=%~dp0"
set "PYTHON=%REPO%.venv\Scripts\python.exe"

if not defined GATEWAY_CONFIG set "GATEWAY_CONFIG=%REPO%gateway.json"
set "CONFIG=%GATEWAY_CONFIG%"

if not exist "%PYTHON%" (
  echo.
  echo ERROR: no Python at "%PYTHON%"
  echo        The repository's virtualenv is missing. Recreate it with:
  echo            python -m venv .venv
  echo            .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo.
  exit /b 2
)

if not exist "%CONFIG%" (
  echo.
  echo ERROR: no gateway config at "%CONFIG%"
  echo        Copy gateway.example.json to gateway.json beside this script
  echo        and fill in api_url, device_token and device.
  echo        See FIELD_GATEWAY.md.
  echo.
  exit /b 2
)

if not exist "%REPO%logs" mkdir "%REPO%logs"
set "LOG=%REPO%logs\gateway.log"

REM The settings module with no database. The gateway is an HTTP client.
set "DJANGO_SETTINGS_MODULE=config.settings.gateway"

REM Write the log as UTF-8. Python emits UTF-8 but a plain `>>` redirect on a
REM machine with a legacy code page would re-encode it as cp1252 and turn
REM every em dash into a replacement character. Both lines are needed: the
REM code page for the console, the variable for the redirected stream.
chcp 65001 >nul 2>&1
set "PYTHONIOENCODING=utf-8"

REM Task Scheduler starts tasks in C:\Windows\System32, so the working
REM directory has to be set explicitly or every relative app path fails.
cd /d "%REPO%"

echo. >> "%LOG%"
echo ==== %DATE% %TIME% starting ==== >> "%LOG%"

"%PYTHON%" manage.py run_gateway --config "%CONFIG%" %* >> "%LOG%" 2>&1
set "CODE=%ERRORLEVEL%"

echo ==== %DATE% %TIME% exited %CODE% ==== >> "%LOG%"

if not "%CODE%"=="0" (
  echo.
  echo The gateway stopped with code %CODE%. The reason is in:
  echo     %LOG%
  echo.
)

exit /b %CODE%
