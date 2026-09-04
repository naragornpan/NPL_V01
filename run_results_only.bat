@echo off
REM ============================================================
REM  LED auction RESULTS only (catch-up) - Thai IP required
REM
REM  Fetches final auction prices from report.led.go.th for
REM  (office, past auction date) pairs. Fast: only re-fetches
REM  recent dates + fills missing ones (skips already-fetched).
REM
REM  Usage:
REM    double-click              -> days_back = 30 (covers recent gap)
REM    run_results_only.bat 60   -> days_back = 60 (wider catch-up)
REM    run_results_only.bat 185  -> full 6-month re-check
REM
REM  Safe to re-run: skips pairs already fetched (except within
REM  ~45 days, which always re-fetch). If the net drops, just run
REM  again - it continues from where it stopped. This file also
REM  auto-retries up to 3 times on failure.
REM
REM  NOTE: keep this file ASCII-only (Windows reads .bat with the
REM  OS code page before chcp, so non-ASCII comments can break it).
REM ============================================================
setlocal
cd /d "%~dp0"

set "DAYS=%~1"
if "%DAYS%"=="" set "DAYS=30"

if not exist "logs" mkdir "logs"
set "LOGFILE=logs\led_results.log"

chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

if not exist ".venv\Scripts\activate.bat" (
  echo ERROR: .venv not found. Run: python -m venv .venv
  exit /b 1
)
call ".venv\Scripts\activate.bat"

echo. >> "%LOGFILE%"
echo ==== LED results days_back=%DAYS% - %DATE% %TIME% ==== >> "%LOGFILE%"

set "TRIES=3"
set /a N=0

:retry
set /a N+=1
echo ---- attempt %N%/%TRIES% - %DATE% %TIME% ---- >> "%LOGFILE%"
echo Fetching auction results (attempt %N% of %TRIES%, days_back=%DAYS%)...
python src\led_results.py --days-back %DAYS% >> "%LOGFILE%" 2>&1
if %ERRORLEVEL%==0 goto done

if %N% LSS %TRIES% (
  echo Net/fetch error - retrying in 20s (attempt %N% done)...
  echo retry after error, waiting 20s >> "%LOGFILE%"
  timeout /t 20 /nobreak >nul
  goto retry
)

echo Still failing after %TRIES% attempts. Check logs\led_results.log and your internet (Thai IP required).
echo FAILED after %TRIES% attempts - %DATE% %TIME% >> "%LOGFILE%"
endlocal & exit /b 1

:done
echo Done. Auction results updated - open plaengdee.com/auction-results to check.
echo ==== done - %DATE% %TIME% ==== >> "%LOGFILE%"
endlocal & exit /b 0
