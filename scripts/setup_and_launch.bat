@echo off
setlocal EnableExtensions
set EXITCODE=0

pushd "%~dp0\.."
if errorlevel 1 (
  set EXITCODE=1
  goto fail
)

echo [hourBTC] Checking Python launcher...
where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher not found. Install Python 3.11+ first.
  set EXITCODE=1
  goto fail
)

echo [hourBTC] Checking Python version...
for /f "tokens=2 delims= " %%a in ('py -3 -V 2^>^&1') do set PYVER=%%a
if not defined PYVER (
  echo Python 3.11+ not found.
  set EXITCODE=1
  goto fail
)

echo [hourBTC] Ensuring virtual environment...
if not exist .venv (
  py -3 -m venv .venv
  if errorlevel 1 (
    echo Failed to create virtual environment.
    set EXITCODE=1
    goto fail
  )
)

echo [hourBTC] Activating virtual environment...
call .venv\Scripts\activate
if errorlevel 1 (
  echo Failed to activate virtual environment.
  set EXITCODE=1
  goto fail
)

echo [hourBTC] Upgrading pip/setuptools/wheel...
python -m pip install --upgrade pip setuptools wheel
if errorlevel 1 (
  echo Failed to upgrade pip tooling.
  set EXITCODE=1
  goto fail
)

echo [hourBTC] Installing requirements...
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Failed to install requirements.
  set EXITCODE=1
  goto fail
)

echo [hourBTC] Ensuring runtime directories...
if not exist data mkdir data
if errorlevel 1 (
  echo Failed to create data directory.
  set EXITCODE=1
  goto fail
)
if not exist logs mkdir logs
if errorlevel 1 (
  echo Failed to create logs directory.
  set EXITCODE=1
  goto fail
)

echo [hourBTC] Ensuring .env...
if not exist .env (
  copy .env.template .env >nul
  if errorlevel 1 (
    echo Failed to create .env from .env.template.
    set EXITCODE=1
    goto fail
  )
)

echo [hourBTC] Running bootstrap...
python -m scripts.bootstrap
if errorlevel 1 (
  echo Bootstrap failed.
  set EXITCODE=1
  goto fail
)

echo [hourBTC] Verifying environment...
python -m scripts.verify_env
if errorlevel 1 (
  echo Environment verification failed.
  set EXITCODE=1
  goto fail
)

echo [hourBTC] Launching desktop app...
python -m app.main
if errorlevel 1 (
  echo Application launch failed.
  set EXITCODE=1
  goto fail
)

goto end

:fail
echo.
echo Setup or launch failed. Review the error above.
pause

:end
popd
endlocal & exit /b %EXITCODE%
