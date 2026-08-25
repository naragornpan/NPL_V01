-- =====================================================================
-- Migration 012 — cache พิกัด + ให้คะแนนทรัพย์
--
-- เก็บพิกัดถาวร ไม่ยิง Nominatim ซ้ำ
-- (เขาให้ใช้ฟรีโดยขอแค่ 1 request/วินาที และห้ามยิงซ้ำสิ่งที่รู้แล้ว)
-- =====================================================================

create table if not exists geo_cache (
  id            bigserial primary key,
  district      text,
  province      text not null,
  lat           numeric not null,
  lng           numeric not null,
  precision     text not null,        -- district | province
  method        text,                 -- structured | freetext | province_only
  osm_type      text,
  display_name  text,
  looked_up_at  timestamptz not null default now(),
  unique (province, district)
);

comment on column geo_cache.precision is
  'district = จุดกึ่งกลางอำเภอ · province = จุดกึ่งกลางจังหวัด (หยาบมาก) '
  'ห้ามใช้คำนวณระยะถึงสถานีหรือแนวเวนคืน จะได้คำตอบผิดที่ดูน่าเชื่อถือ';

-- เติมพิกัดกลับเข้า snapshot ที่ยังว่าง
create or replace function apply_geocache()
returns int language plpgsql as $$
declare n int;
begin
  update listing_snapshots s
     set lat = g.lat, lng = g.lng
    from geo_cache g
   where s.lat is null
     and s.province = g.province
     and coalesce(s.district, '') = coalesce(g.district, '');
  get diagnostics n = row_count;
  return n;
end $$;

-- บันทึกว่าพิกัดมาจากไหน เพื่อไม่ให้เผลอใช้ผิดที่
alter table listing_snapshots add column if not exists geo_precision text;

create or replace function apply_geocache_with_precision()
returns int language plpgsql as $$
declare n int;
begin
  update listing_snapshots s
     set lat = g.lat, lng = g.lng, geo_precision = g.precision
    from geo_cache g
   where (s.lat is null or s.geo_precision is null)
     and s.province = g.province
     and coalesce(s.district, '') = coalesce(g.district, '');
  get diagnostics n = row_count;
  return n;
end $$;

alter table geo_cache enable row level security;
