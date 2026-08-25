-- =====================================================================
-- Migration 024 — เพิ่มแหล่ง ttb (PAMCO)
--
-- ผลสำรวจ 2026-08-24 (ดูรายละเอียดใน src/adapters/ttb.py)
--   เว็บจริง: property.pamco.co.th (PAMCO บริหารทรัพย์ให้ ttb)
--     tmbbank.com/property เป็นแค่ mirror และ robots.txt คืน error 500
--   หน้า list (Next.js) ปุ่ม "โหลดเพิ่ม" เรียก JSON API สาธารณะ:
--     GET https://property-api-prod.automer.io/property-new/display
--         ?page={n}&limit={m}  ->  {"total":1302,"list":[...]}
--   **พิกัดจริงมากับ list เลย** (npaProductLatitude/Longitude)
--     -> เก็บ geo_precision='parcel' ตั้งแต่ ingest (ไม่ต้องเข้า detail)
--   ~1,300 รายการ · ไม่มี PII ลูกหนี้ (มีเบอร์ AO แต่ adapter ไม่เก็บ)
--
-- base_url เก็บเป็นหน้าเว็บ (ให้คนเปิดดู) ส่วน adapter ยิง API host โดยตรง
-- is_active = false ไว้ก่อน (แนวเดียวกับ ktb/sam/ghb) — อ่าน ToS +
-- สมัครนายหน้า ก่อนเปิดใช้ (guard บล็อกจนสถานะสิทธิ์ ttb พ้น 'unknown')
-- =====================================================================

insert into sources (code, name, base_url, encoding, rate_limit_s, is_active,
                     institution_code, notes) values
  ('ttb', 'ttb (PAMCO) - ทรัพย์รอการขาย',
   'https://property.pamco.co.th/assets/ttb', 'utf-8', 1.0, false,
   'ttb',
   'API: property-api-prod.automer.io/property-new/display?page=&limit= (JSON) '
   '· พิกัดจริงมากับ list -> geo_precision=parcel ตั้งแต่ ingest '
   '· detail: property.pamco.co.th/assets/ttb/{slug} · external_ref = ttb:{idMarket} '
   '· ~1,300 รายการ ดึงครบทุกจังหวัด (ไม่ผูก tier) · อ่าน ToS + นายหน้าก่อนเปิดใช้')
on conflict (code) do update set
  base_url = excluded.base_url, name = excluded.name,
  encoding = excluded.encoding, institution_code = excluded.institution_code,
  notes = excluded.notes;

update institutions set
  npa_url = 'https://property.pamco.co.th/assets/ttb',
  legal_note = 'เว็บจริงคือ property.pamco.co.th (PAMCO) · หน้า list เรียก JSON API '
               'สาธารณะ property-api-prod.automer.io (ตรวจ 2026-08-24) · มีพิกัดจริง '
               '+ ไม่มี PII ลูกหนี้ · ยังต้องอ่าน ToS ฉบับเต็มและสมัครนายหน้าก่อนเปิดใช้',
  legal_checked_at = date '2026-08-24'
 where code = 'ttb';

-- ---------------------------------------------------------------------
-- ปลดล็อกให้ดึงอัตโนมัติ (run_all.py) — รันหลังเคลียร์ ToS/นายหน้าแล้วเท่านั้น
-- (แบบ 021 ของ LED) หรือ uncomment:
--
-- update institutions
--    set legal_status = 'permitted', partner_status = 'approved',
--        legal_note = 'อ่าน ToS + สมัครนายหน้า PAMCO/ttb เรียบร้อย (ใส่วันที่/เลขอ้างอิง)',
--        legal_checked_at = current_date
--  where code = 'ttb';
-- update sources set is_active = true where code = 'ttb';
-- ---------------------------------------------------------------------
