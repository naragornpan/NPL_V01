@echo off
REM ============================================================
REM  LED all offices - nationwide auction listings (no tier filter)
REM  Improves match coverage for the auction-results page.
REM
REM  Usage:
REM    double-click            -> days_ahead = 45
REM    run_led_alloffices.bat 15   -> days_ahead = 15 (faster backfill)
REM
REM  Can be pointed to by Task Scheduler.
REM  Heavy / slow: 15 days ~30-45 min, 45 days ~1-2 hours.
REM  Safe to re-run (upsert by external_ref) - if interrupted, run again.
REM  NOTE: keep this file ASCII-only (Windows code page reads .bat
REM  before chcp, so non-ASCII comments can break it).
REM ============================================================
setlocal
cd /d "%~dp0"

set "DAYS=%~1"
if "%DAYS%"=="" set "DAYS=45"

if not exist "logs" mkdir "logs"
set "LOGFILE=logs\led_alloffices.log"

chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

echo. >> "%LOGFILE%"
echo ==== LED all-offices days_ahead=%DAYS% - %DATE% %TIME% ==== >> "%LOGFILE%"

if not exist ".venv\Scripts\activate.bat" (
  echo ERROR: .venv not found. Run: python -m venv .venv
  exit /b 1
)
call ".venv\Scripts\activate.bat"

python src\run.py led_auction --all-offices --days-ahead %DAYS% --days-back 0 >> "%LOGFILE%" 2>&1

echo ==== done - %DATE% %TIME% ==== >> "%LOGFILE%"
echo Done. See logs\led_alloffices.log
endlocal
