-- =====================================================================
-- Migration 007 — เปรียบเทียบราคาตลาดกับราคาประมูล
--
-- สองเรื่องที่ต้องแยกให้ขาดก่อน
--
-- 1. ราคาบนเว็บประกาศขาย = ราคา "ตั้งขาย" ไม่ใช่ราคาปิด
--    เทียบกับราคาปิดประมูลตรง ๆ ส่วนต่างจะดูใหญ่เกินจริง
--    เพราะปนส่วนลดจากการต่อรองเข้าไปด้วย
--    -> ต้องแสดงทั้งส่วนต่างดิบและส่วนต่างหลังปรับ พร้อมบอกสมมติฐาน
--
-- 2. เว็บประกาศขายส่วนใหญ่ห้าม scrape ในเงื่อนไขการใช้งาน
--    -> เก็บเฉพาะ "สถิติรวมรายเขต" ไม่เก็บประกาศรายชิ้น
--       ไม่เก็บรูป ไม่เก็บข้อความ ไม่ทำสำเนาเนื้อหา
--       เก็บแค่ median/p25/p75/จำนวน ซึ่งเป็นข้อเท็จจริงเชิงสถิติ
--       และลิงก์กลับไปหน้าค้นหาของเขาเสมอ
-- =====================================================================

create table if not exists comp_sources (
  code           text primary key,
  label          text not null,
  site_url       text,
  search_url_tpl text,          -- ลิงก์กลับไปหน้าค้นหาของเขา
  price_kind     text not null, -- asking | closed
  collection     text not null, -- manual | api | partner
  tos_note       text not null,
  is_active      boolean default true
);

insert into comp_sources (code, label, site_url, price_kind, collection, tos_note) values
  ('portal_a', 'เว็บประกาศขาย A', null, 'asking', 'manual',
   'ตรวจ ToS ก่อนใช้ทุกครั้ง เก็บได้เฉพาะสถิติรวม ห้ามทำสำเนาประกาศหรือรูป'),
  ('portal_b', 'เว็บประกาศขาย B', null, 'asking', 'manual',
   'เช่นเดียวกับ A — ถ้ามี API หรือโครงการพันธมิตร ให้ใช้ช่องทางนั้นแทน'),
  ('portal_c', 'เว็บประกาศขาย C', null, 'asking', 'manual', 'เช่นเดียวกัน'),
  ('reic', 'REIC โอนกรรมสิทธิ์', 'https://www.reic.or.th', 'closed', 'manual',
   'ข้อมูลเผยแพร่สาธารณะ ระดับจังหวัด/ไตรมาส อ้างอิงแหล่งที่มาเสมอ'),
  ('own_closed', 'ดีลที่เราปิดเอง', null, 'closed', 'manual',
   'ข้อมูลของเราเอง ใช้ได้เต็มที่ และเป็นตัวสอบเทียบที่แม่นที่สุด')
on conflict (code) do nothing;

comment on table comp_sources is
  'ชื่อเว็บให้เติมเองหลังตรวจ ToS แล้ว — ไม่ใส่มาให้ล่วงหน้าโดยตั้งใจ';

-- ---------------------------------------------------------------------
-- สถิติราคาตลาดรายเขต — ไม่มีข้อมูลประกาศรายชิ้น
-- ---------------------------------------------------------------------
create table if not exists market_comps (
  id              uuid primary key default gen_random_uuid(),
  source_code     text not null references comp_sources(code),
  province        text not null,
  district        text,
  property_type   text not null,
  period_month    date not null,        -- วันที่ 1 ของเดือนที่เก็บ

  n_listings      int not null,
  median_price    numeric,
  p25_price       numeric,
  p75_price       numeric,
  median_per_sqwa numeric,
  median_per_sqm  numeric,

  collected_at    timestamptz not null default now(),
  collected_by    text,
  search_url      text,                 -- ลิงก์กลับไปหน้าค้นหาที่ใช้เก็บ
  note            text,
  unique (source_code, province, district, property_type, period_month)
);
create index if not exists idx_comp_area
  on market_comps(province, district, property_type, period_month desc);

-- ---------------------------------------------------------------------
-- สมมติฐานส่วนลดต่อรอง (asking -> closed)
--
-- ค่าตั้งต้นเป็นตัวเลขสมมติ ต้องแทนที่ด้วยค่าจริงจากดีลที่ปิดเอง
-- อย่าปล่อยให้ค่า default ค้างอยู่นานกว่า 6 เดือน
-- ---------------------------------------------------------------------
create table if not exists asking_haircut (
  id             int primary key default 1,
  haircut_pct    numeric not null default 8.0,
  basis          text not null default 'ค่าตั้งต้นสมมติ — ยังไม่ได้สอบเทียบ',
  sample_n       int not null default 0,
  updated_at     timestamptz not null default now(),
  constraint single_row check (id = 1)
);
insert into asking_haircut (id) values (1) on conflict (id) do nothing;

-- ---------------------------------------------------------------------
-- ส่วนต่างราคา: ประมูล vs ตลาด
-- ---------------------------------------------------------------------
-- ต้องการตาราง price_observations จาก migration 005
do $$
begin
  if to_regclass('public.price_observations') is null then
    raise exception 'ต้องรัน 005_price_sources.sql ให้สำเร็จก่อน';
  end if;
end $$;

create or replace view v_price_gap as
with auction as (
  select province, district, property_type,
         date_trunc('month', observed_on)::date as period_month,
         count(*) as n_auction,
         percentile_cont(0.5) within group (order by price_total) as auction_median,
         max(observed_on) as auction_latest
  from price_observations
  where tier = 'A_auction_close' and price_total > 0
  group by 1, 2, 3, 4
),
latest_comp as (
  select distinct on (source_code, province, district, property_type)
         source_code, province, district, property_type,
         median_price, n_listings, period_month, collected_at, search_url
  from market_comps
  order by source_code, province, district, property_type, period_month desc
)
select
  c.source_code,
  cs.label          as source_label,
  cs.price_kind,
  c.province, c.district, c.property_type,
  a.auction_median,
  a.n_auction,
  a.auction_latest,
  c.median_price    as market_median,
  c.n_listings,
  c.period_month    as market_period,
  c.collected_at    as market_collected_at,
  c.search_url,
  -- ต้อง cast เป็น numeric ก่อน round สองอาร์กิวเมนต์
  -- เพราะ Postgres ไม่มี round(double precision, int)
  round(((1 - a.auction_median / nullif(c.median_price, 0)) * 100)::numeric, 1)
                    as raw_gap_pct,
  round(((1 - a.auction_median /
         nullif(c.median_price * (1 - h.haircut_pct / 100), 0)) * 100)::numeric, 1)
                    as adjusted_gap_pct,
  h.haircut_pct,
  h.basis           as haircut_basis,
  (current_date - c.collected_at::date) as days_since_update,
  case
    when current_date - c.collected_at::date <= 14 then 'สด'
    when current_date - c.collected_at::date <= 45 then 'เริ่มเก่า'
    else 'เก่าเกินไป'
  end as freshness
from latest_comp c
join comp_sources cs on cs.code = c.source_code
left join auction a
  on a.province = c.province
 and coalesce(a.district, '') = coalesce(c.district, '')
 and a.property_type = c.property_type
 and a.period_month = c.period_month
cross join asking_haircut h;

alter table comp_sources   enable row level security;
alter table market_comps   enable row level security;
alter table asking_haircut enable row level security;
