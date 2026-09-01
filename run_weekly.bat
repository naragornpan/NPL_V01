@echo off
REM Weekly - tier 2 (adds EEC + Ayutthaya, 10 provinces)
REM Suggested trigger: Sunday 21:00
REM 5th arg = LED all-offices days_ahead (45 = full nationwide sweep).
call "%~dp0_run_common.bat" 2 30 weekly 300 45
