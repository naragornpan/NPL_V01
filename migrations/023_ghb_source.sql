-- =====================================================================
-- Migration 023 — เพิ่มแหล่ง ธอส. (GHB Home Center)
--
-- ผลสำรวจ 2026-08-24 (asadapters/ghb.py มีบันทึกละเอียด)
--   เว็บจริง: https://www.ghbhomecenter.com  (ไม่ใช่ blog.ghbank.co.th)
--   robots.txt: "User-agent: * / Disallow:" (ว่าง = อนุญาตทุกอย่าง)
--   ไม่พบข้อความห้ามนำข้อมูลไปใช้บนหน้าที่ตรวจ (การ์ด/หน้ารายละเอียด)
--   หน้า list server-render กรองด้วยจังหวัด (slug อังกฤษในพาธ)
--     แบ่งหน้าจริงด้วย ?pg=  (?page/?p/?pageSize เว็บไม่สน)
--   หน้า detail มี "พิกัดจริง" ฝังในลิงก์ Google Maps + ไม่มี PII ลูกหนี้
--     -> ได้พิกัดระดับแปลงฟรี (เติมด้วย enrich.py ghb-coords)
--   ทรัพย์ทั้งประเทศ ~30,000 รายการ — มากสุดในบรรดาแหล่งที่มี
--
-- เปิดใช้ (is_active) ไว้ = false ตามแนวเดียวกับ ktb/sam
--   ยังต้องอ่าน ToS ฉบับเต็ม + สมัครนายหน้า/พันธมิตร ธอส. ก่อน
--   guard_source_activation() จะบล็อกการเปิดจนกว่าสถานะสิทธิ์ของสถาบัน
--   ghb จะพ้น 'unknown' (ดูบล็อกปลดล็อกท้ายไฟล์)
-- =====================================================================

insert into sources (code, name, base_url, encoding, rate_limit_s, is_active,
                     institution_code, notes) values
  ('ghb', 'ธอส. - บ้านมือสอง (GHB Home Center)',
   'https://www.ghbhomecenter.com', 'utf-8', 2.0, false,
   'ghb',
   'list: /property-grid-for-sale/{ProvinceEng}?pg={n} (server-render ~20/หน้า) '
   '· detail: /property-{ID} มีพิกัดจริงจาก Google Maps (เติมด้วย ghb-coords) '
   '· external_ref = ghb:{ID จาก URL} ต่างจาก "รหัสทรัพย์" บนการ์ด '
   '· ~30,000 รายการทั่วประเทศ · ต้องอ่าน ToS + สมัครนายหน้าก่อนเปิดใช้')
on conflict (code) do update set
  base_url = excluded.base_url, name = excluded.name,
  encoding = excluded.encoding, institution_code = excluded.institution_code,
  notes = excluded.notes;

update institutions set
  npa_url = 'https://www.ghbhomecenter.com',
  legal_note = 'robots.txt อนุญาตทุกอย่าง (Disallow ว่าง) · ไม่พบข้อความห้ามใช้ '
               'ข้อมูลบนหน้าที่ตรวจ (2026-08-24) · หน้า detail มีพิกัดจริง + ไม่มี PII '
               '· ยังต้องอ่าน ToS ฉบับเต็มและสมัครนายหน้า/พันธมิตร ธอส. ก่อนเปิดใช้',
  legal_checked_at = date '2026-08-24'
 where code = 'ghb';

-- ---------------------------------------------------------------------
-- ปลดล็อกให้ดึงอัตโนมัติ (run_all.py) — รันหลังเคลียร์ ToS/นายหน้าแล้วเท่านั้น
-- ทำเป็นไฟล์แยกภายหลัง (แบบ 021 ของ LED) หรือ uncomment บล็อกนี้:
--
-- update institutions
--    set legal_status = 'permitted',
--        partner_status = 'approved',
--        legal_note = 'อ่าน ToS + สมัครพันธมิตร ธอส. เรียบร้อย (ใส่วันที่/เลขอ้างอิง)',
--        legal_checked_at = current_date
--  where code = 'ghb';
-- update sources set is_active = true where code = 'ghb';
-- ---------------------------------------------------------------------
