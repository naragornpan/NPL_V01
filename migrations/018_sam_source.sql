-- =====================================================================
-- Migration 018 — เพิ่มแหล่ง SAM (บสส.)
--
-- ระบบค้นหาอยู่ที่ sam.or.th/site/npa/ ซึ่งไม่มีลิงก์จากเมนูหลัก
-- (เว็บองค์กรกับระบบขายทรัพย์เป็นคนละส่วน จึงหาไม่เจอตอนสำรวจรอบแรก)
--
-- ผลตรวจ 2026-08-23
--   ไม่พบข้อความห้ามนำข้อมูลไปใช้
--   query param ทำงานจริง: s_province / s_status_id / limit / page
--   ตั้ง limit สูงได้ ดึงทั้งจังหวัดในคำขอเดียว
--   รหัสจังหวัดครบ 77 (เรียงตามตัวอักษรไทย ไม่ใช่รหัสกรมการปกครอง)
--
-- โครงสร้างข้อมูลดีที่สุดในบรรดาแหล่งที่สำรวจมา
-- =====================================================================

insert into sources (code, name, base_url, encoding, rate_limit_s, is_active,
                     institution_code, notes) values
  ('sam', 'SAM - ทรัพย์สินรอการขาย', 'https://sam.or.th/site/npa/', 'utf-8', 2.0, false,
   'sam',
   'ตั้ง limit ได้เอง ดึงทั้งจังหวัดในคำขอเดียว · รหัสจังหวัดเรียงตามตัวอักษรไทย 1-77 '
   '· ห้องชุดให้พื้นที่เป็น ตร.ม. ไม่ใช่ ตร.ว. ต้องแยกฟิลด์ '
   '· ต้องตรวจ ToS และสมัครนายหน้าก่อนเปิดใช้')
on conflict (code) do update set
  base_url = excluded.base_url, institution_code = excluded.institution_code,
  notes = excluded.notes;

update institutions set
  npa_url = 'https://sam.or.th/site/npa/',
  legal_note = 'ระบบค้นหาทรัพย์อยู่ที่ /site/npa/ (ไม่มีลิงก์จากเมนูหลัก) '
               'ไม่พบข้อความห้ามใช้ข้อมูล query param ทำงานปกติ (ตรวจ 2026-08-23) '
               'ยังต้องอ่าน ToS และสมัครนายหน้า โทร 02-686-1888 ก่อนเปิดใช้',
  legal_checked_at = date '2026-08-23'
 where code = 'sam';
