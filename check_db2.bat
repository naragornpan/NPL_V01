@echo off
cd /d "%~dp0"
call ".venv\Scripts\activate.bat"
set PYTHONIOENCODING=utf-8
python tools\check_db2.py
pause
