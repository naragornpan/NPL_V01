-- =====================================================================
-- Migration 020 — กันไม่ให้ geocode เขียนทับพิกัดจริงระดับแปลง (parcel)
--
-- ปัญหาที่แก้:
--   apply_geocache_with_precision() เดิมมีเงื่อนไข
--     (s.lat is null or s.geo_precision is distinct from g.precision)
--   ทำให้ทรัพย์ที่ได้พิกัดจริงจากหน้ารายละเอียดแล้ว (geo_precision='parcel')
--   ถูกทับด้วยพิกัดกึ่งกลางตำบลจาก geo_cache ในรอบ geocode ถัดไป
--   เพราะ 'parcel' ย่อม distinct จาก 'subdistrict' เสมอ
--
--   กระทบทั้ง SAM (ให้พิกัดแปลงมาอยู่แล้ว) และ BAM (เพิ่งเพิ่มการดึงพิกัด
--   จากลิงก์ Google Maps ในหน้ารายละเอียด) — หมุดที่เคยตรงจะเด้งกลับไป
--   ซ้อนกันกลางตำบลโดยไม่มีใครรู้ตัว
--
-- ทางแก้: พิกัด 'parcel' คือละเอียดที่สุด ห้ามแตะไม่ว่ากรณีใด
-- =====================================================================

create or replace function apply_geocache_with_precision()
returns int language plpgsql as $$
declare n int;
begin
  -- ใช้พิกัดที่ละเอียดที่สุดที่มี: ตำบล > อำเภอ > จังหวัด
  -- แต่ห้ามแตะแถวที่เป็น parcel เพราะนั่นคือพิกัดจริงของทรัพย์
  update listing_snapshots s
     set lat = g.lat, lng = g.lng, geo_precision = g.precision
    from geo_cache g
   where s.province = g.province
     and coalesce(s.district, '') = coalesce(g.district, '')
     and coalesce(s.subdistrict, '') = coalesce(g.subdistrict, '')
     and coalesce(s.geo_precision, '') <> 'parcel'
     and (s.lat is null or s.geo_precision is distinct from g.precision);
  get diagnostics n = row_count;

  -- ทรัพย์ที่ยังไม่มีตำบล ใช้พิกัดระดับอำเภอไปก่อน (parcel ก็ยังกันไว้)
  update listing_snapshots s
     set lat = g.lat, lng = g.lng, geo_precision = g.precision
    from geo_cache g
   where s.lat is null
     and coalesce(s.geo_precision, '') <> 'parcel'
     and g.subdistrict is null
     and s.province = g.province
     and coalesce(s.district, '') = coalesce(g.district, '');
  return n;
end $$;
