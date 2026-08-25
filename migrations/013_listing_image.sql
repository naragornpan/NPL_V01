-- =====================================================================
-- Migration 013 — เก็บ URL รูปหลักของประกาศ
--
-- ทำไมเก็บใน listing_snapshots ไม่ใช่ listing_images
--   รูปที่ต้นทางใช้ประกอบประกาศ เปลี่ยนได้เมื่อเขาอัปเดตประกาศ
--   จึงเป็นส่วนหนึ่งของ "สภาพประกาศ ณ เวลานั้น" ตามหลัก append-only
--   ส่วน listing_images ไว้เก็บรูปที่เราถ่ายเอง ซึ่งเป็นคนละเรื่อง
--
-- เก็บแค่ URL ไม่ดาวน์โหลดมาเก็บ
--   ประหยัดพื้นที่ และไม่เป็นการทำสำเนางานของเขา
--   หน้าเว็บโหลดรูปจากต้นทางโดยตรงพร้อม lazy loading
-- =====================================================================

alter table listing_snapshots add column if not exists image_url text;

comment on column listing_snapshots.image_url is
  'URL รูปจากต้นทาง ไม่ได้ทำสำเนา · แสดงในโหมดใช้ภายในเท่านั้น '
  'โหมดเผยแพร่ต้องใช้รูปที่เราถ่ายเองจาก listing_images';

drop view if exists v_listings_with_grade;
create view v_listings_with_grade as
select distinct on (s.source_code, s.external_ref)
  s.source_code, s.external_ref,
  i.code as institution_code, i.short_name as institution_name, i.kind as institution_kind,
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
