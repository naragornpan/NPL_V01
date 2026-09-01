@echo off
REM ============================================================
REM  LED ทุกสำนักงาน (all offices) - เก็บประกาศขายทอดตลาดทั่วประเทศ
REM  เพิ่ม coverage การจับคู่ผลจบประมูล (ไม่จำกัด tier)
REM
REM  วิธีใช้:
REM    ดับเบิลคลิกไฟล์นี้            -> ดึงล่วงหน้า 45 วัน
REM    run_led_alloffices.bat 14   -> ดึงล่วงหน้า 14 วัน (backfill รอบแรก เร็วกว่า)
REM
REM  ตั้ง Task Scheduler รายสัปดาห์ได้ (ชี้มาที่ไฟล์นี้)
REM  หมายเหตุ: ปริมาณมาก ใช้เวลานาน (14 วัน ~30-45 นาที, 45 วัน ~1-2 ชม.)
REM  รันซ้ำได้ปลอดภัย (upsert ตาม external_ref) - หลุดกลางคันก็รันใหม่ต่อได้
REM ============================================================
setlocal
cd /d "%~dp0"

set "DAYS=%~1"
if "%DAYS%"=="" set "DAYS=45"

if not exist "logs" mkdir "logs"
set "LOGFILE=logs\led_alloffices.log"

echo. >> "%LOGFILE%"
echo ==== LED all-offices (days_ahead=%DAYS%) - %DATE% %TIME% ==== >> "%LOGFILE%"

call ".venv\Scripts\activate.bat"
python src\run.py led_auction --all-offices --days-ahead %DAYS% --days-back 0 >> "%LOGFILE%" 2>&1

echo ==== done - %DATE% %TIME% ==== >> "%LOGFILE%"
echo เสร็จแล้ว - ดู log ที่ logs\led_alloffices.log
endlocal
