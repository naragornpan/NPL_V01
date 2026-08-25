-- =====================================================================
-- Migration 016 — ที่อยู่เต็มจากหน้ารายละเอียด + พิกัดระดับตำบล
--
-- ปัญหาที่แก้: geocoding ระดับอำเภอทำให้ทรัพย์ทุกตัวในอำเภอเดียวกัน
-- ได้พิกัดเดียวกันหมด แผนที่กลายเป็นหมุดซ้อนกันเป็นร้อยที่จุดเดียว
-- ซึ่งไม่ใช่แค่ดูไม่สวย แต่สื่อความหมายผิดว่าทรัพย์กระจุกอยู่ตรงนั้น
--
-- ที่อยู่เต็มอยู่ในหน้ารายละเอียดซึ่งเราดึงแกลเลอรีอยู่แล้ว
-- จึงเก็บเพิ่มได้โดยไม่ต้องยิงเว็บเพิ่มสักครั้ง
-- =====================================================================

create table if not exists property_details (
  source_code   text not null,
  external_ref  text not null,
  address_full  text,
  street        text,
  subdistrict   text,
  district      text,
  province      text,
  fetched_at    timestamptz not null default now(),
  primary key (source_code, external_ref)
);

-- เติมตำบลกลับเข้า snapshot เพื่อให้กรองและ geocode ได้
create or replace function apply_detail_address()
returns int language plpgsql as $$
declare n int;
begin
  update listing_snapshots s
     set subdistrict = d.subdistrict
    from property_details d
   where s.source_code = d.source_code
     and s.external_ref = d.external_ref
     and d.subdistrict is not null
     and s.subdistrict is distinct from d.subdistrict;
  get diagnostics n = row_count;
  return n;
end $$;

-- cache พิกัดระดับตำบล
alter table geo_cache add column if not exists subdistrict text;

-- unique เดิมเป็น (province, district) ต้องขยายให้รวมตำบล
alter table geo_cache drop constraint if exists geo_cache_province_district_key;
create unique index if not exists idx_geo_unique
  on geo_cache (province, coalesce(district,''), coalesce(subdistrict,''));

create or replace function apply_geocache_with_precision()
returns int language plpgsql as $$
declare n int;
begin
  -- ใช้พิกัดที่ละเอียดที่สุดที่มี: ตำบล > อำเภอ > จังหวัด
  update listing_snapshots s
     set lat = g.lat, lng = g.lng, geo_precision = g.precision
    from geo_cache g
   where s.province = g.province
     and coalesce(s.district, '') = coalesce(g.district, '')
     and coalesce(s.subdistrict, '') = coalesce(g.subdistrict, '')
     and (s.lat is null or s.geo_precision is distinct from g.precision);
  get diagnostics n = row_count;

  -- ทรัพย์ที่ยังไม่มีตำบล ใช้พิกัดระดับอำเภอไปก่อน
  update listing_snapshots s
     set lat = g.lat, lng = g.lng, geo_precision = g.precision
    from geo_cache g
   where s.lat is null
     and g.subdistrict is null
     and s.province = g.province
     and coalesce(s.district, '') = coalesce(g.district, '');
  return n;
end $$;

alter table property_details enable row level security;
