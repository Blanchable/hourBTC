@echo off
setlocal

where py >nul 2>nul
if %errorlevel% neq 0 (
  echo Python launcher not found. Install Python 3.11+ first.
  pause
  exit /b 1
)

for /f "tokens=2 delims= " %%a in ('py -3 -V 2^>^&1') do set PYVER=%%a
if not defined PYVER (
  echo Python 3.11+ not found.
  pause
  exit /b 1
)

if not exist .venv (
  py -3 -m venv .venv
)

call .venv\Scripts\activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
if not exist data mkdir data
if not exist logs mkdir logs
if not exist .env copy .env.template .env
python scripts\bootstrap.py
python scripts\verify_env.py
python -m app.main

endlocal
