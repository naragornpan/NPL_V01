@echo off
REM Address catch-up for Task Scheduler - no pause, writes to log.
REM Fixes the "all pins stacked on one spot" problem by fetching
REM full addresses so coordinates become subdistrict-level.
REM
REM Argument = how many properties to process this run (default 2000).
REM Turn this task off once the log says 0 remaining.
setlocal
cd /d "%~dp0"

set "N=%~1"
if "%N%"=="" set "N=2000"

if not exist "logs" mkdir "logs"

for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value 2^>nul') do set "DT=%%I"
set "STAMP=%DT:~0,8%"
if "%STAMP%"=="" set "STAMP=nodate"
set "LOGFILE=logs\addresses_%STAMP%.log"

chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

echo. >> "%LOGFILE%"
echo ==== addresses (%N%) - %DATE% %TIME% ==== >> "%LOGFILE%"

if not exist ".venv\Scripts\activate.bat" (
  echo ERROR: .venv not found. >> "%LOGFILE%"
  exit /b 1
)
call ".venv\Scripts\activate.bat"

python src\enrich.py details --detail-limit %N% >> "%LOGFILE%" 2>&1
python src\enrich.py geocode >> "%LOGFILE%" 2>&1
python src\enrich.py grade >> "%LOGFILE%" 2>&1

echo Finished %DATE% %TIME% >> "%LOGFILE%"

forfiles /p "logs" /m *.log /d -30 /c "cmd /c del @path" >nul 2>&1

endlocal & exit /b 0
