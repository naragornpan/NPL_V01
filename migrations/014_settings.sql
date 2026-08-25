-- =====================================================================
-- Migration 014 — ตั้งค่าระบบที่ admin ปรับได้จากหน้าเว็บ
--
-- เริ่มจากเรื่องลิงก์ไปทรัพย์ต้นทาง ซึ่งมีผลต่อรายได้โดยตรง
--
--   โชว์ลิงก์  = ผู้ซื้อสะดวก ตรวจสอบได้เอง น่าเชื่อถือขึ้น
--               แต่เขาอาจติดต่อสถาบันเองแล้วเราไม่ได้ค่าคอม (leakage)
--   ซ่อนลิงก์  = กัน leakage แต่ดูปิดบัง และผู้ซื้อตรวจสอบเองไม่ได้
--
-- ไม่มีคำตอบตายตัว จึงทำให้ปรับได้ และแยกตามสถาบันด้วย
-- เพราะบางเจ้าอาจกำหนดในสัญญานายหน้าว่าต้องลิงก์กลับ
-- =====================================================================

create table if not exists app_settings (
  key          text primary key,
  value        text not null,
  value_type   text not null default 'bool',   -- bool | text | int
  label        text not null,
  description  text,
  updated_at   timestamptz not null default now(),
  updated_by   text
);

insert into app_settings (key, value, value_type, label, description) values
  ('show_source_link', 'false', 'bool',
   'แสดงลิงก์ไปทรัพย์ต้นทางให้ผู้ใช้ทั่วไป',
   'ปิดไว้เป็นค่าเริ่มต้นเพื่อกัน leakage — ผู้ซื้อติดต่อสถาบันเองแล้วเราไม่ได้ค่าคอม · admin เห็นลิงก์เสมอไม่ว่าตั้งค่าไว้อย่างไร'),
  ('show_institution_name', 'true', 'bool',
   'แสดงชื่อสถาบันเจ้าของทรัพย์',
   'ชื่อสถาบันสร้างความน่าเชื่อถือ แต่ก็ทำให้ผู้ซื้อค้นหาเองได้ง่ายขึ้น'),
  ('show_market_code', 'false', 'bool',
   'แสดงรหัสตลาดของทรัพย์',
   'รหัสตลาดคือกุญแจที่ทำให้ค้นเจอบนเว็บต้นทางได้ทันที ปิดไว้ดีกว่า'),
  ('contact_line_url', '', 'text',
   'ลิงก์ LINE OA สำหรับให้ผู้สนใจติดต่อ',
   'แสดงเป็นปุ่มหลักแทนลิงก์ต้นทางเมื่อปิดการแสดงลิงก์')
on conflict (key) do nothing;

-- ตั้งค่าแยกรายสถาบัน — ทับค่ากลางได้
-- null = ใช้ค่ากลาง · true/false = บังคับเฉพาะสถาบันนั้น
alter table institutions
  add column if not exists allow_source_link boolean;

comment on column institutions.allow_source_link is
  'null = ตามค่ากลาง · true = ต้องลิงก์กลับ (บางสัญญานายหน้ากำหนดไว้) '
  '· false = ห้ามลิงก์';

create or replace function get_setting(p_key text, p_default text default null)
returns text language sql stable as $$
  select coalesce((select value from app_settings where key = p_key), p_default);
$$;

alter table app_settings enable row level security;

-- ---------------------------------------------------------------------
-- ปรับ view ให้ส่ง allow_source_link ออกไปด้วย
-- ---------------------------------------------------------------------
drop view if exists v_listings_with_grade;
create view v_listings_with_grade as
select distinct on (s.source_code, s.external_ref)
  s.source_code, s.external_ref,
  i.code as institution_code, i.short_name as institution_name,
  i.kind as institution_kind, i.allow_source_link,
  s.title, s.detail_url, s.image_url, s.property_type,
  s.province, s.district, s.subdistrict, s.lat, s.lng, s.geo_precision,
  s.land_area_sqwa, s.usable_area_sqm, s.bedrooms, s.bathrooms, s.parking,
  s.opening_price, s.list_price, s.special_price, s.appraised_price, s.renovated,
  s.auction_date, s.auction_round, s.occupancy_note,
  g.grade, g.score, g.completeness, g.reasons,
  s.observed_at
from listing_snapshots s
left join sources src on src.code = s.source_code
left join institutions i on i.code = src.institution_code
left join property_grades g
  on g.source_code = s.source_code and g.external_ref = s.external_ref
where s.auction_date is null or s.auction_date >= current_date
order by s.source_code, s.external_ref, s.observed_at desc;
