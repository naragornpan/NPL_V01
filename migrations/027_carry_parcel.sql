-- =====================================================================
-- Migration 027 — คืนพิกัดแปลงให้ snapshot ล่าสุด (กัน parcel กร่อน)
--
-- ปัญหา: พิกัดแปลง (geo_precision='parcel') ถูกเซ็ตด้วย UPDATE บน snapshot
--   หนึ่ง ๆ (จาก do_details/backfill) พอแหล่งนั้นถูก ingest ใหม่ จะได้
--   snapshot ใหม่ที่ไม่มีพิกัด แล้ว geocode เติมระดับตำบลทับ → ตัวล่าสุด
--   กลายเป็นตำบล ทำให้ "แปลงจริง" หายไป (เจอกับ sam: 510 -> 42)
--
-- วิธีแก้ทั่วไป: ถ้า ref ไหน "เคยมี" snapshot ที่เป็น parcel แต่ตัวล่าสุด
--   ไม่ใช่ parcel → คัดลอกพิกัดแปลงล่าสุดที่รู้จักมาใส่ตัวล่าสุด
--   (ttb ไม่ต้องพึ่งอันนี้ เพราะเก็บ parcel ตอน ingest อยู่แล้ว)
-- =====================================================================

create or replace function carry_parcel_coords() returns integer
language plpgsql as $$
declare n integer;
begin
  with latest as (
    select distinct on (source_code, external_ref)
           id, source_code, external_ref, geo_precision
      from listing_snapshots
     order by source_code, external_ref, observed_at desc
  ),
  parcel as (
    select distinct on (source_code, external_ref)
           source_code, external_ref, lat, lng
      from listing_snapshots
     where geo_precision = 'parcel' and lat is not null
     order by source_code, external_ref, observed_at desc
  )
  update listing_snapshots s
     set lat = p.lat, lng = p.lng, geo_precision = 'parcel'
    from latest l
    join parcel p
      on p.source_code = l.source_code and p.external_ref = l.external_ref
   where s.id = l.id
     and coalesce(l.geo_precision, '') <> 'parcel';
  get diagnostics n = row_count;
  return n;
end $$;

-- รันทันทีเพื่อกู้ sam (และแหล่งอื่นที่กร่อน) กลับมา
select carry_parcel_coords() as คืนพิกัดแปลง;
