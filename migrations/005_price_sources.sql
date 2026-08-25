-- =====================================================================
-- Migration 005 — จัดชั้นความน่าเชื่อถือของราคา
--
-- ความจริงที่ต้องยอมรับ:
--   ไทยไม่มีฐานข้อมูลราคาซื้อขายรายแปลงที่เปิดสาธารณะ
--   ราคาโอนที่กรมที่ดินไม่เปิดรายแปลง และมักถูกแจ้งต่ำกว่าจริง
--   เพื่อลดค่าธรรมเนียมและภาษี
--
--   -> ราคาปิดประมูล LED คือราคาซื้อขายจริงรายทรัพย์ที่ดีที่สุดที่หาได้
--
-- ประเด็นทางเทคนิคที่สำคัญที่สุดในไฟล์นี้:
--   ราคาประมูล != ราคาตลาด ใน "ระดับ" (มีส่วนลดจากการบังคับขาย)
--   แต่ difference-in-differences วัด "การเปลี่ยนแปลง" ไม่ใช่ระดับ
--   ตราบใดที่อัตราส่วนลดคงที่ ส่วนลดจะถูกหักล้างออกไปเอง
--
--   ดังนั้น uplift ที่คำนวณจากราคาประมูล ใช้ได้
--   แต่ "มูลค่าตลาดวันนี้" ต้องแปลงด้วยตัวปรับที่วัดแยกต่างหาก
--   และต้องทดสอบสมมติฐานว่าอัตราส่วนลดคงที่จริงไหม (ดู v_spread_stability)
-- =====================================================================

-- ---------------------------------------------------------------------
-- ชั้นความน่าเชื่อถือของแหล่งราคา
-- ---------------------------------------------------------------------
create table if not exists price_tiers (
  tier         text primary key,
  label        text not null,
  is_actual_money boolean not null,
  granularity  text not null,          -- asset | area_aggregate
  usable_for_did boolean not null,
  note         text
);

insert into price_tiers (tier, label, is_actual_money, granularity, usable_for_did, note) values
  ('A_auction_close', 'ราคาปิดประมูล LED', true, 'asset', true,
   'เงินจริง รายทรัพย์ มีพิกัด — แกนหลักของระบบ'),
  ('A_own_deal', 'ดีลที่เราปิดเอง', true, 'asset', true,
   'น้อยแต่แม่นที่สุด ใช้สอบเทียบตัวปรับ auction->market'),
  ('B_reic_aggregate', 'REIC โอนกรรมสิทธิ์รายจังหวัด/ไตรมาส', true, 'area_aggregate', true,
   'มูลค่า/จำนวนหน่วย = ราคาเฉลี่ยต่อหน่วย ใช้เป็นชุดควบคุมระดับจังหวัด'),
  ('C_gov_appraisal', 'ราคาประเมินกรมธนารักษ์', false, 'area_aggregate', false,
   'ไม่ใช่ธุรกรรม ใช้ยืนยันแนวโน้มยาวและคำนวณส่วนต่างประเมิน-ตลาด'),
  ('D_asking', 'ราคาประกาศขายบนแพลตฟอร์ม', false, 'asset', false,
   'สูงกว่าราคาปิดเสมอ ห้ามใช้คำนวณ uplift ใช้ได้แค่ดูอุปทาน'),
  ('E_npa_listed', 'ราคาตั้งขาย NPA ธนาคาร', false, 'asset', false,
   'ทรัพย์หายจากเว็บ = อาจขายแล้ว แต่ราคาปิดไม่เปิดเผย เป็นได้แค่สัญญาณ')
on conflict (tier) do nothing;

-- ---------------------------------------------------------------------
-- ตารางราคาแบบรวมศูนย์ — ทุกแหล่งลงที่เดียว พร้อมชั้นกำกับ
-- ---------------------------------------------------------------------
create table if not exists price_observations (
  id             uuid primary key default gen_random_uuid(),
  tier           text not null references price_tiers(tier),
  property_id    uuid references properties(id) on delete set null,
  province       text,
  district       text,
  subdistrict    text,
  lat            numeric,
  lng            numeric,
  property_type  text,
  observed_on    date not null,
  price_total    numeric,
  area_sqwa      numeric,
  area_sqm       numeric,
  price_per_sqwa numeric generated always as
                 (case when area_sqwa > 0 then price_total / area_sqwa end) stored,
  n_units        int default 1,          -- >1 สำหรับข้อมูลรวม (tier B)
  source_ref     text,
  created_at     timestamptz not null default now()
);
create index if not exists idx_po_tier_area
  on price_observations(tier, province, district, observed_on);
create index if not exists idx_po_type on price_observations(property_type, observed_on);

-- เติมจากข้อมูล LED ที่มีอยู่แล้ว
create or replace function sync_auction_prices()
returns int language plpgsql as $$
declare n int;
begin
  insert into price_observations
    (tier, province, district, subdistrict, lat, lng, property_type,
     observed_on, price_total, area_sqwa, source_ref)
  select 'A_auction_close', s.province, s.district, s.subdistrict, s.lat, s.lng,
         s.property_type, s.sold_date, s.sold_price, s.land_area_sqwa,
         s.source_code || ':' || s.external_ref
  from listing_snapshots s
  where s.source_code = 'led_result'
    and s.sold is true and s.sold_price > 0 and s.sold_date is not null
    and not exists (
      select 1 from price_observations p
      where p.source_ref = s.source_code || ':' || s.external_ref
        and p.tier = 'A_auction_close');
  get diagnostics n = row_count;
  return n;
end $$;

-- ---------------------------------------------------------------------
-- ตัวปรับ ราคาประมูล -> ราคาตลาด
--
-- วิธีวัดที่ใช้ได้จริง 3 ทาง เรียงตามความน่าเชื่อถือ
--   1. resale tracking  ทรัพย์ที่ประมูลได้แล้วถูกขายต่อ -> เห็นส่วนต่างจริง
--   2. own deals        ดีลที่เราปิดเอง เทียบกับราคาประมูลในเขตเดียวกัน
--   3. reic benchmark   ราคาเฉลี่ยต่อหน่วยของ REIC เทียบราคาประมูลจังหวัดเดียวกัน
--
-- ทางที่ 1 ดีที่สุดแต่ต้องรอ 1-2 ปี ทางที่ 3 ใช้ได้ทันทีแต่หยาบ
-- ---------------------------------------------------------------------
create table if not exists auction_market_spread (
  id              uuid primary key default gen_random_uuid(),
  province        text not null,
  district        text,
  property_type   text,
  period_quarter  text not null,        -- '2569Q2'
  method          text not null,        -- resale | own_deal | reic_benchmark
  auction_median  numeric,
  market_median   numeric,
  ratio           numeric,              -- auction / market  (คาดว่า < 1)
  sample_n        int not null,
  computed_at     timestamptz not null default now(),
  unique (province, district, property_type, period_quarter, method)
);

-- ทดสอบว่าอัตราส่วนลดคงที่จริงไหม — สมมติฐานหลักของ DiD
-- ถ้า stddev สูง แปลว่าส่วนลดไม่คงที่ และ uplift ที่คำนวณจากราคาประมูล
-- จะปนเปื้อนการเปลี่ยนแปลงของส่วนลดเข้าไปด้วย
create or replace view v_spread_stability as
select
  province, property_type, method,
  count(*)                                     as n_quarters,
  round(avg(ratio)::numeric, 3)                as mean_ratio,
  round(stddev_samp(ratio)::numeric, 3)        as sd_ratio,
  round((stddev_samp(ratio) / nullif(avg(ratio), 0))::numeric, 3) as cv,
  case
    when count(*) < 4 then 'ข้อมูลยังน้อย'
    when stddev_samp(ratio) / nullif(avg(ratio), 0) <= 0.10 then 'คงที่พอ — DiD ใช้ได้'
    when stddev_samp(ratio) / nullif(avg(ratio), 0) <= 0.20 then 'แกว่งปานกลาง — ระวัง'
    else 'ไม่คงที่ — uplift จากราคาประมูลอาจปนเปื้อน'
  end as verdict
from auction_market_spread
group by province, property_type, method;

-- ---------------------------------------------------------------------
-- ติดตามการขายต่อ — วิธีวัดส่วนต่างที่แม่นที่สุด
-- บันทึกทุกครั้งที่เห็นทรัพย์ที่เคยประมูล ถูกประกาศขายหรือขายจริงอีกครั้ง
-- ---------------------------------------------------------------------
create table if not exists resale_tracking (
  id                uuid primary key default gen_random_uuid(),
  property_id       uuid references properties(id) on delete cascade,
  auction_price     numeric not null,
  auction_date      date not null,
  resale_price      numeric,
  resale_date       date,
  resale_price_type text,               -- 'asking' | 'closed'
  -- ใช้ลบวันตรง ๆ แทน age() เพราะ age() ไม่ immutable
  -- จึงใช้ใน generated column ไม่ได้ (Postgres ปฏิเสธตอน create)
  months_held       int generated always as
                    (case when resale_date is not null
                     then ((resale_date - auction_date) / 30)::int
                     end) stored,
  gross_ratio       numeric generated always as
                    (case when auction_price > 0
                     then resale_price / auction_price end) stored,
  reno_cost         numeric,
  source_note       text,
  created_at        timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- ข้อมูลรวมจาก REIC — ชุดควบคุมระดับจังหวัด
-- ---------------------------------------------------------------------
create table if not exists reic_transfer_series (
  id             uuid primary key default gen_random_uuid(),
  province       text not null,
  property_type  text not null,
  period_quarter text not null,
  units          int,
  value_mbaht    numeric,
  avg_price      numeric generated always as
                 (case when units > 0 then value_mbaht * 1000000 / units end) stored,
  source_ref     text,
  unique (province, property_type, period_quarter)
);

-- ---------------------------------------------------------------------
-- บังคับให้ uplift ใช้ได้เฉพาะ tier ที่เป็นเงินจริง
-- ---------------------------------------------------------------------
-- ส่วนนี้ต้องการตาราง uplift_observations จาก migration 003
-- ห่อด้วย DO block เพื่อไม่ให้ทั้งไฟล์ rollback ถ้ายังไม่ได้รัน 003
do $$
begin
  if to_regclass('public.uplift_observations') is not null then
    alter table uplift_observations
      add column if not exists price_tier text references price_tiers(tier);
  else
    raise notice 'ข้าม: ยังไม่มีตาราง uplift_observations (รัน 003 ก่อน แล้วรัน 005 ซ้ำ)';
  end if;
end $$;

create or replace function enforce_price_tier()
returns trigger language plpgsql as $$
begin
  if new.price_tier is null then
    raise exception 'uplift_observations ต้องระบุ price_tier';
  end if;
  if not (select usable_for_did from price_tiers where tier = new.price_tier) then
    raise exception 'tier % ใช้คำนวณ uplift ไม่ได้ (ไม่ใช่ราคาธุรกรรมจริง)', new.price_tier;
  end if;
  return new;
end $$;

do $$
begin
  if to_regclass('public.uplift_observations') is not null then
    drop trigger if exists trg_uplift_tier on uplift_observations;
    create trigger trg_uplift_tier
      before insert or update on uplift_observations
      for each row execute function enforce_price_tier();
  end if;
end $$;

alter table price_observations     enable row level security;
alter table auction_market_spread  enable row level security;
alter table resale_tracking        enable row level security;
alter table reic_transfer_series   enable row level security;
alter table price_tiers            enable row level security;
