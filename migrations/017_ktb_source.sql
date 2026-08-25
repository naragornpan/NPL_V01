-- =====================================================================
-- Migration 017 — เพิ่มแหล่งกรุงไทย + บันทึกผลสำรวจแหล่งอื่น
--
-- ผลสำรวจ 2026-08-23 (robots.txt + ข้อความห้ามใช้ข้อมูล)
--
--   กรุงไทย   ไม่มีข้อห้าม · หน้า HTML สาธารณะ parse ได้ · ทำได้บางส่วน
--   SCB       ย้ายไป asset.home.scb ซึ่งตอบ 403 · ทำไม่ได้ตอนนี้
--   กสิกร     ตอบ 403 ทันที · ทำไม่ได้
--   กรุงศรี   ตอบ 503 · ทำไม่ได้ตอนนี้
--   ออมสิน    มีข้อความห้ามนำข้อมูลไปใช้ชัดเจน · **ไม่ทำ**
-- =====================================================================

insert into sources (code, name, base_url, encoding, rate_limit_s, is_active,
                     institution_code, notes) values
  ('ktb', 'กรุงไทย - ทรัพย์มือสอง', 'https://npa.krungthai.com', 'utf-8', 2.0, false,
   'ktb',
   'หน้าเดียวได้ราว 16 รายการ เว็บกรองด้วย JS ฝั่งผู้ใช้ query param ไม่ทำงาน '
   'การ์ดมีตำบลมาให้ ต่างจาก BAM · ต้องตรวจ ToS และสมัครนายหน้าก่อนเปิดใช้')
on conflict (code) do update set
  base_url = excluded.base_url, institution_code = excluded.institution_code,
  notes = excluded.notes;

update institutions set
  legal_note = 'robots.txt ไม่มีข้อห้าม · ไม่พบข้อความห้ามใช้ข้อมูลบนหน้าเว็บ '
               '(ตรวจ 2026-08-23) · ยังต้องอ่าน ToS ฉบับเต็มและสมัครนายหน้าก่อนเปิดใช้',
  legal_checked_at = date '2026-08-23'
 where code = 'ktb';

update institutions set
  legal_status = 'restricted',
  legal_note = 'เว็บระบุ "ข้อมูลต่าง ๆ ในเว็บไซต์นี้ ถือเป็นสมบัติของธนาคารออมสิน '
               'ห้ามผู้ใดนำไปใช้ ทำซ้ำ ดัดแปลง โดยมิได้รับอนุญาต" (ยืนยัน 2026-08-23)',
  legal_checked_at = date '2026-08-23'
 where code = 'gsb';

update institutions set
  legal_note = 'เว็บตอบ 403 ปฏิเสธการเข้าถึงอัตโนมัติ (ตรวจ 2026-08-23) '
               'ควรใช้ช่องทางนายหน้าขึ้นทะเบียนแทน',
  legal_checked_at = date '2026-08-23'
 where code = 'kbank';

update institutions set
  npa_url = 'https://asset.home.scb/home',
  legal_note = 'ย้ายจาก scb.co.th ไป asset.home.scb ซึ่งตอบ 403 (ตรวจ 2026-08-23) '
               'มีโครงการรับสมัครนายหน้า NPA — ใช้ช่องทางนั้นแทน',
  legal_checked_at = date '2026-08-23'
 where code = 'scb';

update institutions set
  legal_note = 'เว็บตอบ 503 ตอนตรวจ (2026-08-23) ยังสรุปไม่ได้ ต้องตรวจซ้ำ',
  legal_checked_at = date '2026-08-23'
 where code = 'krungsri';

-- SAM: ไม่มีระบบค้นหาทรัพย์ออนไลน์ ขายแบบจัดประมูลเป็นรอบ ประกาศเป็น PDF
update institutions set
  npa_url = 'https://www.sam.or.th/site/sam/',
  legal_note = 'ไม่พบข้อความห้ามใช้ข้อมูล (ตรวจ 2026-08-23) แต่ไม่มีระบบค้นหาทรัพย์ '
               'ออนไลน์ ขายแบบจัดประมูลเป็นรอบและประกาศเป็นไฟล์ PDF '
               'เขียน scraper แบบเดิมไม่ได้ ควรใช้วิธีตามข่าวรอบประมูลแทน',
  legal_checked_at = date '2026-08-23'
 where code = 'sam';
