@echo off
REM ============================================================
REM  Shared runner - do not call this file directly.
REM  Use run_daily.bat / run_weekly.bat / run_monthly.bat
REM
REM  %1 = tier (1/2/3)   %2 = pages per province   %3 = log label
REM  %4 = detail limit (properties to fetch full address for)
REM  %5 = LED all-offices days_ahead (blank = skip this step)
REM
REM  NOTE: this file must stay ASCII-only.
REM  Windows reads .bat with the OS code page before chcp runs,
REM  so Thai comments become garbage and get run as commands.
REM ============================================================
setlocal
cd /d "%~dp0"

set "TIER=%~1"
set "PAGES=%~2"
set "LABEL=%~3"
set "DETAILS=%~4"
set "LEDDAYS=%~5"
if "%TIER%"=="" set "TIER=1"
if "%PAGES%"=="" set "PAGES=30"
if "%LABEL%"=="" set "LABEL=run"
if "%DETAILS%"=="" set "DETAILS=150"

if not exist "logs" mkdir "logs"

for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value 2^>nul') do set "DT=%%I"
set "STAMP=%DT:~0,8%"
if "%STAMP%"=="" set "STAMP=nodate"
set "LOGFILE=logs\%LABEL%_%STAMP%.log"

chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

echo. >> "%LOGFILE%"
echo ==== %LABEL% tier %TIER% - %DATE% %TIME% ==== >> "%LOGFILE%"

if not exist ".venv\Scripts\activate.bat" (
  echo ERROR: .venv not found. Run: python -m venv .venv >> "%LOGFILE%"
  echo ERROR: .venv not found. Run: python -m venv .venv
  exit /b 1
)
call ".venv\Scripts\activate.bat"

REM 1) fetch listings (tier-limited adapters)
python src\run_all.py --tier %TIER% --max-pages %PAGES% >> "%LOGFILE%" 2>&1
set "RC=%ERRORLEVEL%"

REM 1b) LED listings ALL offices nationwide (only if %5 set)
REM     run before results so new listings can be matched in the same run
if not "%LEDDAYS%"=="" (
  echo ---- LED all-offices days_ahead=%LEDDAYS% ---- >> "%LOGFILE%"
  python src\run.py led_auction --all-offices --days-ahead %LEDDAYS% --days-back 0 >> "%LOGFILE%" 2>&1
)

REM 2) full address + coordinates + grades
REM    details is capped per run because it hits the site once per property
python src\enrich.py all --detail-limit %DETAILS% >> "%LOGFILE%" 2>&1

REM 3) LED auction RESULTS (final prices) - Thai IP only, non-fatal
python src\led_results.py >> "%LOGFILE%" 2>&1

echo Finished %DATE% %TIME% (exit %RC%) >> "%LOGFILE%"

forfiles /p "logs" /m *.log /d -30 /c "cmd /c del @path" >nul 2>&1

endlocal & exit /b %RC%
