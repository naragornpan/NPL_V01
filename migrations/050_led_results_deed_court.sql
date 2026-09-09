-- 050_led_results_deed_court.sql — index ช่วย fallback ค้นเลขคดีแดงจากโฉนด+ศาล
-- รัน: psql "<DATABASE_URL>" -f migrations/050_led_results_deed_court.sql
--
-- หน้ารายละเอียดทรัพย์ LED ที่ matched_ref ยังไม่ติด จะค้นเลขคดีแดงจากตารางผล
-- report.asp ด้วย (deed, court) — index นี้ทำให้ query นั้นเร็ว (ไม่ seq scan 160k+ แถว)

create index if not exists idx_led_results_deed_court
  on led_auction_results (deed, court)
  where case_no is not null and deed is not null and deed not in ('', '-', '0');
