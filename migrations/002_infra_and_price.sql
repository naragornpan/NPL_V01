-- =====================================================================
-- Migration 002 — ตัวแปรโครงสร้างพื้นฐานและการเปลี่ยนแปลงราคา
--
-- แนวคิดหลัก 3 ข้อ
--   1. ระดับความแน่นอน (certainty ladder) — ข่าวลือกับ พ.ร.ฎ. ไม่เท่ากัน
--   2. ตัวแปรมีทั้งบวกและลบ — ถนนใหม่ขึ้นราคาก็ได้ โดนเวนคืนก็ได้
--   3. ระยะทางเป็นตัวแปร ไม่ใช่ boolean — ผลกระทบลดตามระยะ
-- =====================================================================

-- PostGIS บน Supabase ติดตั้งอยู่ที่ schema "extensions" ไม่ใช่ public
-- ถ้าไม่เพิ่มเข้า search_path จะหา type "geometry" ไม่เจอ แล้วทั้งไฟล์ rollback
create schema if not exists extensions;
create extension if not exists postgis with schema extensions;
set search_path to public, extensions;

-- ---------------------------------------------------------------------
-- ระดับความแน่นอน — เรียงจากอ่อนไปแข็ง
-- ราคาขยับจริงส่วนใหญ่ที่ขั้น 3 (พ.ร.ฎ.) และขั้น 5 (เปิดใช้)
-- ขั้น 1-2 คือช่วงที่คนเก็งกำไรกันเอง ยังพลิกได้
-- ---------------------------------------------------------------------
create table if not exists certainty_levels (
  level       int primary key,
  code        text not null unique,
  label       text not null,
  weight      numeric not null      -- ใช้คูณผลกระทบตอนคำนวณคะแนน
);

insert into certainty_levels (level, code, label, weight) values
  (1, 'study',        'ผลการศึกษา/ข่าวแผนงาน',           0.15),
  (2, 'cabinet',      'มติคณะรัฐมนตรีอนุมัติ',            0.35),
  (3, 'decree',       'พ.ร.ฎ.เวนคืน ประกาศราชกิจจาฯ',     0.70),
  (4, 'construction', 'เริ่มก่อสร้างแล้ว',                0.85),
  (5, 'operational',  'เปิดใช้งานแล้ว',                   1.00)
on conflict (level) do nothing;

-- ---------------------------------------------------------------------
-- โครงการโครงสร้างพื้นฐาน
-- ---------------------------------------------------------------------
create table if not exists infra_projects (
  id              uuid primary key default gen_random_uuid(),
  name            text not null,
  project_type    text not null,        -- road | expressway | rail | station | airport | port | other
  agency          text,                 -- รฟม. | กทพ. | ทล. | ทช. | กทม.
  certainty_level int not null references certainty_levels(level) default 1,
  announced_at    date,
  expected_open   date,
  geom            geometry(Geometry, 4326),   -- Point สำหรับสถานี, LineString สำหรับแนวเส้นทาง
  corridor_m      int default 200,      -- ความกว้างแนวเวนคืนโดยประมาณ (เมตร)
  source_url      text,
  verified_by     text,                 -- ใครยืนยัน — ห้ามให้ AI ยืนยันเอง
  verified_at     timestamptz,
  created_at      timestamptz not null default now()
);
create index if not exists idx_infra_geom on infra_projects using gist (geom);
create index if not exists idx_infra_type on infra_projects(project_type, certainty_level);

-- ไทม์ไลน์ของโครงการ — เก็บทุกเหตุการณ์ ไม่ทับของเดิม
-- ทำให้ย้อนดูได้ว่า "ตอนที่ราคาขยับ โครงการอยู่ขั้นไหน"
create table if not exists infra_events (
  id              uuid primary key default gen_random_uuid(),
  project_id      uuid references infra_projects(id) on delete cascade,
  event_type      text not null,        -- ตรงกับ certainty_levels.code
  event_date      date not null,
  headline        text,
  source_name     text,
  source_url      text,
  excerpt         text,                 -- สรุปด้วยคำของเราเอง ไม่คัดลอกต้นฉบับ
  created_at      timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- คิวรอมนุษย์ยืนยัน — AI สกัดได้ แต่ห้ามเข้าระบบคะแนนก่อนคนกด approve
-- (หลักการข้อ 1 ใน PROJECT_BRIEF: rule-based ที่อธิบายได้)
-- ---------------------------------------------------------------------
create table if not exists infra_candidates (
  id              uuid primary key default gen_random_uuid(),
  raw_document_id uuid references raw_documents(id) on delete set null,
  extracted       jsonb not null,       -- ผลจาก LLM
  confidence      numeric,
  status          text not null default 'pending',  -- pending | approved | rejected | duplicate
  reviewed_by     text,
  reviewed_at     timestamptz,
  project_id      uuid references infra_projects(id),
  created_at      timestamptz not null default now()
);
create index if not exists idx_cand_status on infra_candidates(status, created_at);

-- ---------------------------------------------------------------------
-- ราคาประเมินกรมธนารักษ์ รายโซน รายรอบบัญชี
--
-- สำคัญ: รอบ 2566-2569 หมดอายุ 31 ธ.ค. 2569 รอบใหม่กำลังจะประกาศ
-- ต้องเก็บ baseline รอบปัจจุบันไว้ก่อน ไม่งั้นเทียบ % ไม่ได้
-- ---------------------------------------------------------------------
create table if not exists land_price_rounds (
  id            uuid primary key default gen_random_uuid(),
  round_label   text not null,          -- '2566-2569'
  effective_from date not null,
  effective_to   date,
  unique (round_label)
);

create table if not exists land_price_zones (
  id            uuid primary key default gen_random_uuid(),
  round_id      uuid not null references land_price_rounds(id) on delete cascade,
  province      text not null,
  district      text,
  subdistrict   text,
  zone_ref      text,                   -- หน่วยที่ดิน/บล็อก ตามที่กรมธนารักษ์ระบุ
  price_per_sqwa numeric,
  geom          geometry(Geometry, 4326),
  unique (round_id, province, district, subdistrict, zone_ref)
);
create index if not exists idx_lpz_geo on land_price_zones using gist (geom);
create index if not exists idx_lpz_area on land_price_zones(province, district, subdistrict);

-- % การเปลี่ยนแปลงระหว่างรอบ — ตัวแปรราคาที่ "เป็นทางการ"
create or replace view v_land_price_change as
select
  cur.province, cur.district, cur.subdistrict, cur.zone_ref,
  prev.price_per_sqwa as price_prev,
  cur.price_per_sqwa  as price_cur,
  round((cur.price_per_sqwa / nullif(prev.price_per_sqwa, 0) - 1) * 100, 2) as change_pct
from land_price_zones cur
join land_price_rounds rc on rc.id = cur.round_id
join land_price_rounds rp on rp.effective_to < rc.effective_from
join land_price_zones prev
  on prev.round_id = rp.id
 and prev.province = cur.province
 and coalesce(prev.district, '') = coalesce(cur.district, '')
 and coalesce(prev.subdistrict, '') = coalesce(cur.subdistrict, '')
 and coalesce(prev.zone_ref, '') = coalesce(cur.zone_ref, '');

-- ---------------------------------------------------------------------
-- ฟีเจอร์ที่คำนวณต่อทรัพย์ — ป้อนเข้า rule engine
-- ---------------------------------------------------------------------
create table if not exists property_infra_features (
  property_id            uuid primary key references properties(id) on delete cascade,
  nearest_station_m      numeric,
  nearest_station_name   text,
  nearest_station_certainty int,
  nearest_new_road_m     numeric,
  nearest_new_road_name  text,
  in_expropriation_corridor boolean default false,
  expropriation_project  text,
  gov_price_change_pct   numeric,      -- จาก v_land_price_change
  market_price_trend_pct numeric,      -- จากราคาปิดจริงของเราเอง (led_result)
  computed_at            timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- คำนวณฟีเจอร์ — รันหลัง ingest ทุกวัน
-- ---------------------------------------------------------------------
-- ต้องระบุ search_path ที่ตัว function เอง
-- เพราะ "set search_path" ตอนรัน migration มีผลแค่ session นั้น ไม่ติดไปกับ function
-- ถ้าไม่ใส่ จะสร้าง function ผ่าน แต่พังตอนเรียกใช้ ("type geography does not exist")
create or replace function refresh_infra_features()
returns int
language plpgsql
set search_path = public, extensions
as $$
declare affected int;
begin
  with latest as (
    select distinct on (pl.property_id)
           pl.property_id, s.lat, s.lng, s.province, s.district, s.subdistrict
    from property_links pl
    join listing_snapshots s
      on s.source_code = pl.source_code and s.external_ref = pl.external_ref
    where s.lat is not null and s.lng is not null
    order by pl.property_id, s.observed_at desc
  ),
  pt as (
    select property_id, province, district, subdistrict,
           st_setsrid(st_makepoint(lng, lat), 4326)::geography as g
    from latest
  ),
  station as (
    select p.property_id,
           (select st_distance(p.g, ip.geom::geography)
              from infra_projects ip
             where ip.project_type in ('station', 'rail')
             order by p.g <-> ip.geom limit 1) as dist,
           (select ip.name from infra_projects ip
             where ip.project_type in ('station', 'rail')
             order by p.g <-> ip.geom limit 1) as name,
           (select ip.certainty_level from infra_projects ip
             where ip.project_type in ('station', 'rail')
             order by p.g <-> ip.geom limit 1) as certainty
    from pt p
  ),
  road as (
    select p.property_id,
           (select st_distance(p.g, ip.geom::geography)
              from infra_projects ip
             where ip.project_type in ('road', 'expressway')
             order by p.g <-> ip.geom limit 1) as dist,
           (select ip.name from infra_projects ip
             where ip.project_type in ('road', 'expressway')
             order by p.g <-> ip.geom limit 1) as name
    from pt p
  ),
  corridor as (
    select p.property_id,
           exists (select 1 from infra_projects ip
                    where ip.certainty_level >= 3
                      and st_dwithin(p.g, ip.geom::geography, ip.corridor_m)) as inside,
           (select ip.name from infra_projects ip
             where ip.certainty_level >= 3
               and st_dwithin(p.g, ip.geom::geography, ip.corridor_m)
             limit 1) as proj
    from pt p
  )
  insert into property_infra_features as f
    (property_id, nearest_station_m, nearest_station_name, nearest_station_certainty,
     nearest_new_road_m, nearest_new_road_name,
     in_expropriation_corridor, expropriation_project, gov_price_change_pct, computed_at)
  select p.property_id, s.dist, s.name, s.certainty, r.dist, r.name,
         coalesce(c.inside, false), c.proj, v.change_pct, now()
  from pt p
  left join station s using (property_id)
  left join road r using (property_id)
  left join corridor c using (property_id)
  left join v_land_price_change v
    on v.province = p.province
   and coalesce(v.district, '') = coalesce(p.district, '')
   and coalesce(v.subdistrict, '') = coalesce(p.subdistrict, '')
  on conflict (property_id) do update set
    nearest_station_m = excluded.nearest_station_m,
    nearest_station_name = excluded.nearest_station_name,
    nearest_station_certainty = excluded.nearest_station_certainty,
    nearest_new_road_m = excluded.nearest_new_road_m,
    nearest_new_road_name = excluded.nearest_new_road_name,
    in_expropriation_corridor = excluded.in_expropriation_corridor,
    expropriation_project = excluded.expropriation_project,
    gov_price_change_pct = excluded.gov_price_change_pct,
    computed_at = now();

  get diagnostics affected = row_count;
  return affected;
end $$;

alter table infra_projects            enable row level security;
alter table infra_events              enable row level security;
alter table infra_candidates          enable row level security;
alter table land_price_rounds         enable row level security;
alter table land_price_zones          enable row level security;
alter table property_infra_features   enable row level security;
alter table certainty_levels          enable row level security;

insert into land_price_rounds (round_label, effective_from, effective_to) values
  ('2566-2569', '2023-01-01', '2026-12-31')
on conflict (round_label) do nothing;

insert into sources (code, name, base_url, encoding, rate_limit_s, notes) values
  ('gazette_decree', 'ราชกิจจานุเบกษา - พ.ร.ฎ.เวนคืน', 'https://ratchakitcha.soc.go.th',
   'utf-8', 5.0, 'แหล่งที่แข็งที่สุด ระบุแขวง/ตำบล + วันบังคับใช้ชัดเจน'),
  ('treasury_price', 'กรมธนารักษ์ - ราคาประเมิน', 'https://www.treasury.go.th',
   'utf-8', 5.0, 'รอบ 2566-2569 หมด 31 ธ.ค. 2569 — เก็บ baseline ก่อนรอบใหม่ประกาศ')
on conflict (code) do nothing;
