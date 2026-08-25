@echo off
REM ทดสอบด้วยตาก่อนตั้งเวลา — เหมือน run_daily.bat แต่แสดงผลบนจอ
chcp 65001 >nul
cd /d "%~dp0"
call .venv\Scripts\activate.bat
echo === ดึงข้อมูล ===
python src\run_all.py
echo.
echo === เติมพิกัดและให้เกรด ===
python src\enrich.py all
echo.
echo เสร็จแล้ว กด Enter เพื่อปิด
pause
