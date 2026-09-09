-- 050_led_results_deed_court.sql — index ช่วย fallback ค้นเลขคดีแดงจากศาล+โฉนด
-- รัน: psql "<DATABASE_URL>" -f migrations/050_led_results_deed_court.sql
--
-- หน้ารายละเอียดทรัพย์ LED ที่ matched_ref ยังไม่ติด จะค้นเลขคดีแดงจากตารางผล
-- report.asp โดยจำกัดด้วย "ศาล" ก่อน (กันโฉนดซ้ำข้ามจังหวัด) แล้วจับเลขโฉนดแบบ
-- token ในลิสต์คั่นด้วย , (เช่น deed='1168,1169') ด้วย regex — index นี้ให้ filter
-- court= ใช้ btree ได้ เหลือสแกน deed เฉพาะในศาลนั้น (~ไม่กี่ร้อยแถว) ไม่ scan ทั้งตาราง

create index if not exists idx_led_results_court_deed
  on led_auction_results (court, deed)
  where case_no is not null;
