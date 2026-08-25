-- =====================================================================
-- Migration 021 — เปิดใช้แหล่ง "กรมบังคับคดี" (led_auction)
--
-- ╔══════════════════════════════════════════════════════════════════╗
-- ║  รันไฟล์นี้ = ประกาศว่าได้จัดการเรื่อง "อนุญาตใช้ข้อมูล / ToS"        ║
-- ║  ของ asset.led.go.th เรียบร้อยแล้วเท่านั้น                          ║
-- ║                                                                    ║
-- ║  สคีมามี trigger guard_source_activation() คอยกันไม่ให้เปิด source  ║
-- ║  ถ้าสถาบันยังเป็น legal_status='restricted' — ไฟล์นี้จึงต้อง        ║
-- ║  "ปลดสถาบันเป็น permitted ก่อน" แล้วค่อยเปิด source ตามลำดับ         ║
-- ║                                                                    ║
-- ║  ยังไม่พร้อม? ข้ามไฟล์นี้ ใช้ run.py led_auction เก็บชั่วคราวได้เลย   ║
-- ║  (run.py ไม่เช็ค is_active — guard บล็อกเฉพาะการรันอัตโนมัติ)        ║
-- ╚══════════════════════════════════════════════════════════════════╝

-- 1) ปลดสถานะสิทธิ์ของสถาบันก่อน (ไม่งั้น trigger จะบล็อกขั้นถัดไป)
update institutions set
  legal_status = 'permitted',
  is_active = true,
  legal_note = coalesce(legal_note,'') || ' | เปิดใช้เมื่อ ' || current_date
 where code in ('led', 'led_auction');

-- 2) เปิด source (ตอนนี้ trigger ยอมให้ผ่านแล้ว)
update sources set is_active = true where code = 'led_auction';

-- 3) ตั้ง rate limit ให้สุภาพกับเว็บราชการ (>= 4 วิ/คำขอ)
update sources set rate_limit_s = 4.0
 where code = 'led_auction' and rate_limit_s < 4.0;
