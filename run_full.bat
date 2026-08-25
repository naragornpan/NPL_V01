@echo off
REM Full sweep - fetch every page, no skipping.
REM Use after changing the parser, or once a month to verify coverage.
REM   run_full.bat        -> tier 1
REM   run_full.bat 2      -> tier 2
setlocal
cd /d "%~dp0"

set "TIER=%~1"
if "%TIER%"=="" set "TIER=1"

chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

if not exist ".venv\Scripts\activate.bat" (
  echo ERROR: .venv not found. Run: python -m venv .venv
  pause
  exit /b 1
)
call ".venv\Scripts\activate.bat"

echo === Full sweep, tier %TIER% ===
python src\run.py bam --tier %TIER% --max-pages 60 --full
python src\run.py sam --tier %TIER% --max-pages 60 --full
python src\run.py ktb --tier %TIER% --max-pages 60 --full
python src\run.py ghb --tier %TIER% --max-pages 60 --full
python src\run.py ttb --full
python src\run.py led_auction --tier %TIER% --full
echo.
echo === Geocode + grade (รวม ghb-coords ใน all แล้ว) ===
python src\enrich.py all
echo.
echo Done. Press any key to close.
pause >nul
endlocal
