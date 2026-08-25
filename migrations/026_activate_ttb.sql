-- =====================================================================
-- Migration 026 — เปิดใช้ ttb (PAMCO) ให้ทำงานอัตโนมัติใน run_all
--
--   ╔══════════════════════════════════════════════════════════════╗
--   ║  รันไฟล์นี้ "ต่อเมื่อ" ทำครบแล้วเท่านั้น:                     ║
--   ║   1. อ่าน ToS/เงื่อนไขการใช้ข้อมูลของ property.pamco.co.th   ║
--   ║   2. สมัครนายหน้า/พันธมิตรขายทรัพย์กับ PAMCO/ttb (ถ้าจำเป็น) ║
--   ║  guard_source_activation() จะบล็อกการเปิด ถ้าสถานะสิทธิ์      ║
--   ║  ของสถาบันยังเป็น unknown/restricted/prohibited              ║
--   ╚══════════════════════════════════════════════════════════════╝
--
-- แก้ legal_note ให้ใส่ "วันที่อ่าน ToS / เลขที่ทะเบียนนายหน้า" จริงก่อนรัน
-- =====================================================================
begin;

update institutions
   set legal_status   = 'permitted',
       partner_status = 'approved',
       legal_note     = 'อ่าน ToS PAMCO + สมัครนายหน้า ttb เรียบร้อย '
                        '(ใส่วันที่/เลขอ้างอิงตรงนี้)',
       legal_checked_at = current_date
 where code = 'ttb';

-- ต้องอยู่หลัง update institutions ในธุรกรรมเดียวกัน — guard จะเห็น 'permitted'
update sources set is_active = true where code = 'ttb';

commit;

-- ตรวจผล
select code, is_active, institution_code from sources where code = 'ttb';
select code, legal_status, partner_status from institutions where code = 'ttb';
