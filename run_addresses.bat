@echo off
REM Catch-up on full addresses only - no listing fetch.
REM Use this to fix the "all pins stacked on one spot" problem faster.
REM   run_addresses.bat        -> 500 properties
REM   run_addresses.bat 2000   -> 2000 properties (about 70 minutes)
setlocal
cd /d "%~dp0"

set "N=%~1"
if "%N%"=="" set "N=500"

chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

if not exist ".venv\Scripts\activate.bat" (
  echo ERROR: .venv not found. Run: python -m venv .venv
  pause
  exit /b 1
)
call ".venv\Scripts\activate.bat"

echo === Fetching full addresses for up to %N% properties ===
python src\enrich.py details --detail-limit %N%
echo.
echo === Geocoding (subdistrict level) ===
python src\enrich.py geocode
echo.
echo === Grading ===
python src\enrich.py grade
echo.
echo Done. Press any key to close.
pause >nul
endlocal
