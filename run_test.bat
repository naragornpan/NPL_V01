@echo off
REM Quick visual test - 2 pages per province, output on screen.
REM Always run this before setting up a scheduled task.
REM   run_test.bat        -> tier 1
REM   run_test.bat 2      -> tier 2
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

echo === Fetch tier %TIER% (2 pages per province) ===
python src\run_all.py --tier %TIER% --max-pages 2
echo.
echo === Geocode + grade ===
python src\enrich.py all
echo.
echo Done. Press any key to close.
pause >nul
endlocal
