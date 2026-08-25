-- =====================================================================
-- Migration 019 — เปิดใช้แหล่งข้อมูลที่พร้อมแล้ว
--
-- รันไฟล์นี้เมื่ออ่านเงื่อนไขการใช้งานและสมัครนายหน้าเรียบร้อยแล้วเท่านั้น
-- ถ้ายังไม่ได้ทำ ให้ข้ามไฟล์นี้ไปก่อน ระบบยังใช้งานได้ปกติ
-- =====================================================================

-- SAM — แหล่งหลัก ข้อมูลครบที่สุด
update institutions set
  legal_status = 'permitted', is_active = true,
  partner_status = 'applied',
  legal_note = coalesce(legal_note,'') || ' | เปิดใช้เมื่อ ' || current_date
 where code = 'sam';
update sources set is_active = true where code = 'sam';

-- BAM
update institutions set
  legal_status = 'permitted', is_active = true,
  partner_status = 'approved'
 where code = 'bam';
update sources set is_active = true where code = 'bam';

-- กรุงไทย (ได้ราว 80 รายการ)
update institutions set
  legal_status = 'permitted', is_active = true
 where code = 'ktb';
update sources set is_active = true where code = 'ktb';

-- ---------------------------------------------------------------------
-- ปิดแหล่งที่ยังไม่มี adapter
--
-- ถ้าเปิดค้างไว้ รายงานสุขภาพจะขึ้นเตือนทุกวันจนคนเลิกอ่าน
-- ซึ่งอันตรายกว่า error จริงที่ซ่อนอยู่ในนั้น
-- ---------------------------------------------------------------------
update sources set is_active = false
 where code in ('gazette_decree', 'treasury_price');
