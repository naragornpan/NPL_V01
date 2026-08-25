-- =====================================================================
-- NPA Ingest — Database Schema
-- ปรัชญา: raw-first, append-only, ไม่เคยทับข้อมูลเดิม
-- รันบน Supabase SQL Editor ได้ตรง ๆ
-- =====================================================================

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------------
-- ชั้น 0: แหล่งข้อมูล
-- ---------------------------------------------------------------------
create table if not exists sources (
  code          text primary key,          -- 'led_auction', 'led_result', 'bam', 'scb'
  name          text not null,
  base_url      text,
  encoding      text default 'utf-8',      -- LED ใช้ 'tis-620'
  robots_ok     boolean default false,     -- ยืนยันแล้วว่าไม่ขัด robots.txt
  rate_limit_s  numeric default 3.0,       -- วินาทีระหว่าง request
  is_active     boolean default true,
  notes         text
);

-- ---------------------------------------------------------------------
-- ชั้น 1: RAW — เก็บ payload ดิบ ไม่แตะ ไม่ parse
-- มี PII (ชื่อโจทก์/จำเลย/เลขคดี) จึงต้องจำกัดสิทธิ์ + auto-purge
-- ---------------------------------------------------------------------
create table if not exists raw_documents (
  id            uuid primary key default gen_random_uuid(),
  source_code   text not null references sources(code),
  run_id        uuid not null,
  url           text not null,
  http_status   int,
  fetched_at    timestamptz not null default now(),
  content_hash  text not null,             -- sha256 ของ body หลัง decode
  body          text,                      -- HTML/JSON ที่ decode เป็น UTF-8 แล้ว
  purge_after   timestamptz not null default (now() + interval '30 days')
);
create index if not exists idx_raw_hash on raw_documents(source_code, content_hash);
create index if not exists idx_raw_purge on raw_documents(purge_after);

-- ---------------------------------------------------------------------
-- ชั้น 2: SNAPSHOT — ผล parse ต่อการ "พบเห็น" หนึ่งครั้ง (append-only)
-- ประกาศเดียวถูกนัดขายหลายรอบ ราคาลดลงเรื่อย ๆ ต้องเก็บทุกรอบ
-- ชั้นนี้ PII-free แล้ว
-- ---------------------------------------------------------------------
create table if not exists listing_snapshots (
  id                uuid primary key default gen_random_uuid(),
  source_code       text not null references sources(code),
  raw_document_id   uuid references raw_documents(id) on delete set null,
  external_ref      text not null,         -- id ฝั่งต้นทาง เช่น เลขที่ประกาศ
  observed_at       timestamptz not null default now(),
  content_hash      text not null,         -- hash เฉพาะฟิลด์ที่มีความหมาย

  -- ระบุตำแหน่ง
  province          text,
  district          text,
  subdistrict       text,
  address_raw       text,                  -- ตัดบ้านเลขที่ออกแล้ว
  lat               numeric,
  lng               numeric,

  -- ตัวทรัพย์
  property_type     text,                  -- land | house | townhouse | condo | commercial | other
  title_deed_type   text,                  -- โฉนด | นส.3ก | อช.2 ...
  land_area_sqwa    numeric,
  usable_area_sqm   numeric,
  building_count    int,

  -- ราคา/การขาย
  opening_price     numeric,
  appraised_price   numeric,               -- ราคาประเมินที่ประกาศระบุ
  auction_round     int,                   -- นัดครั้งที่
  auction_date      date,
  office_name       text,                  -- สำนักงานบังคับคดีที่รับผิดชอบ
  deposit_amount    numeric,
  mortgage_carried  boolean,               -- "การจำนองติดไป"
  occupancy_note    text,                  -- ข้อความเกี่ยวกับผู้อยู่อาศัย

  -- ผลการขาย (เฉพาะ source ที่เป็นรายงานผล)
  sold              boolean,
  sold_price        numeric,
  sold_date         date,

  -- ฟิลด์ที่แหล่ง NPA ธนาคาร/AMC มี แต่ประกาศขายทอดตลาดไม่มี
  title             text,
  detail_url        text,
  bedrooms          int,
  bathrooms         int,
  parking           int,
  list_price        numeric,        -- ราคาตั้งขาย
  special_price     numeric,        -- ราคาโปรโมชัน (ถ้ามี)
  renovated         boolean,

  raw_fields        jsonb not null default '{}'::jsonb,  -- ทุกฟิลด์ที่ parse ได้ ไม่ทิ้ง
  parser_version    text not null,
  unique (source_code, external_ref, content_hash)
);
-- ---------------------------------------------------------------------
-- อัปเกรดตารางที่สร้างไว้ก่อนหน้า
--
-- "create table if not exists" ข้ามตารางที่มีอยู่แล้ว จึงไม่เพิ่มคอลัมน์ใหม่ให้
-- ถ้าเคยรัน schema เวอร์ชันเก่าไปแล้ว ต้อง alter เพิ่มเอง
-- บล็อกนี้ปลอดภัยและรันซ้ำได้ ไม่ว่าจะเป็นฐานใหม่หรือฐานเก่า
-- ---------------------------------------------------------------------
alter table listing_snapshots add column if not exists title           text;
alter table listing_snapshots add column if not exists detail_url      text;
alter table listing_snapshots add column if not exists bedrooms        int;
alter table listing_snapshots add column if not exists bathrooms       int;
alter table listing_snapshots add column if not exists parking         int;
alter table listing_snapshots add column if not exists list_price      numeric;
alter table listing_snapshots add column if not exists special_price   numeric;
alter table listing_snapshots add column if not exists renovated       boolean;
alter table listing_snapshots add column if not exists image_url       text;
alter table listing_snapshots add column if not exists geo_precision   text;

alter table sources add column if not exists is_active             boolean default true;
alter table sources add column if not exists expected_every_hours  int default 26;

create index if not exists idx_snap_ref  on listing_snapshots(source_code, external_ref);
create index if not exists idx_snap_geo  on listing_snapshots(province, district);
create index if not exists idx_snap_date on listing_snapshots(auction_date);
create index if not exists idx_snap_json on listing_snapshots using gin (raw_fields);

-- ---------------------------------------------------------------------
-- ชั้น 3: IDENTITY — ผูก snapshot หลายอันเข้าเป็นทรัพย์เดียว
-- ห้าม merge อัตโนมัติแบบถาวร ให้เก็บ confidence ไว้ตรวจย้อนหลังได้
-- ---------------------------------------------------------------------
create table if not exists properties (
  id             uuid primary key default gen_random_uuid(),
  dedupe_key     text unique,              -- province|district|type|area|geohash
  first_seen_at  timestamptz not null default now(),
  last_seen_at   timestamptz not null default now()
);

create table if not exists property_links (
  property_id    uuid not null references properties(id) on delete cascade,
  source_code    text not null,
  external_ref   text not null,
  confidence     numeric not null default 1.0,
  matched_by     text,                     -- 'exact_ref' | 'geo_area' | 'manual'
  primary key (source_code, external_ref)
);

-- ---------------------------------------------------------------------
-- ชั้น 4: OBSERVABILITY — ไม่มีชั้นนี้จะไม่รู้ว่า scraper เงียบไปตั้งแต่เมื่อไหร่
-- ---------------------------------------------------------------------
create table if not exists ingest_runs (
  id              uuid primary key default gen_random_uuid(),
  source_code     text not null references sources(code),
  started_at      timestamptz not null default now(),
  finished_at     timestamptz,
  status          text not null default 'running',  -- running | ok | partial | failed
  pages_fetched   int default 0,
  rows_parsed     int default 0,
  rows_new        int default 0,
  error_count     int default 0,
  error_sample    text
);

create table if not exists parse_failures (
  id              uuid primary key default gen_random_uuid(),
  run_id          uuid references ingest_runs(id) on delete cascade,
  raw_document_id uuid references raw_documents(id) on delete set null,
  reason          text,
  occurred_at     timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- RLS — เปิดทุกตาราง ยังไม่มี policy = ไม่มีใครอ่านได้จาก anon key
-- backend ใช้ service_role จึงข้าม RLS ได้ตามปกติ
-- ---------------------------------------------------------------------
alter table raw_documents     enable row level security;
alter table listing_snapshots enable row level security;
alter table properties        enable row level security;
alter table property_links    enable row level security;
alter table ingest_runs       enable row level security;
alter table parse_failures    enable row level security;
alter table sources           enable row level security;

-- ---------------------------------------------------------------------
-- Seed
-- ---------------------------------------------------------------------
-- is_active = false สำหรับ LED โดยตั้งใจ
-- เว็บระบุว่าห้ามนำข้อมูลไปใช้โดยมิได้รับอนุญาต (ดู docs/DATA_PERMISSION.md)
-- เปิดใช้งานได้เมื่อได้รับหนังสืออนุญาตแล้วเท่านั้น
insert into sources (code, name, base_url, encoding, rate_limit_s, is_active, notes) values
  ('led_auction', 'กรมบังคับคดี - ประกาศขายทอดตลาด', 'https://asset.led.go.th/newbidreg/', 'utf-8', 4.0, false,
   'รออนุญาต: เว็บระบุห้ามนำข้อมูลไปใช้โดยมิได้รับอนุญาต · flow: asset_day.asp -> asset_search_day.asp · ห้ามแตะฟอร์มที่มี seckey'),
  ('led_result',  'กรมบังคับคดี - รายงานผลการขาย',   'https://asset.led.go.th/report_new/reportm.asp', 'utf-8', 4.0, false,
   'รออนุญาตเช่นกัน · reportm.asp เป็นหน้าเมนู -> report.asp / reports.asp'),
  ('bam',         'BAM - ทรัพย์ NPA',                 'https://www.bam.co.th',  'utf-8',   1.5, true,
   'ตรวจ ToS และสิทธิ์ในฐานะนายหน้าขึ้นทะเบียนก่อนเปิดใช้จริง')
on conflict (code) do nothing;
