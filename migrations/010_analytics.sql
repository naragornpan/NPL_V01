-- =====================================================================
-- Migration 010 — Analytics + Monitoring
--
-- PDPA: ไม่เก็บ IP, ไม่เก็บ user agent เต็ม, ไม่เก็บอะไรที่ระบุตัวได้
-- เก็บแค่ session hash ที่หมุนทุกวัน เพื่อนับ unique แบบหยาบ ๆ
-- ข้อมูลระดับ event เก็บ 90 วันแล้วลบ เหลือแค่ยอดรวมรายวัน
-- =====================================================================

create table if not exists page_events (
  id            bigserial primary key,
  occurred_at   timestamptz not null default now(),
  event_type    text not null,     -- view_list | view_detail | view_map |
                                   -- click_source | save | inquire | filter
  source_code   text,
  external_ref  text,
  province      text,
  district      text,
  property_type text,
  session_hash  text,              -- hash(session + วันที่) หมุนทุกวัน
  device_class  text,              -- mobile | desktop | other
  referrer_kind text,              -- direct | line | search | social
  meta          jsonb not null default '{}'::jsonb,
  purge_after   timestamptz not null default (now() + interval '90 days')
);
create index if not exists idx_ev_time on page_events(occurred_at desc);
create index if not exists idx_ev_prop on page_events(source_code, external_ref, occurred_at desc);
create index if not exists idx_ev_area on page_events(province, district, occurred_at desc);
create index if not exists idx_ev_purge on page_events(purge_after);

-- ยอดรวมรายวัน — เก็บถาวร ใช้ดูแนวโน้มยาว
create table if not exists daily_rollup (
  day             date not null,
  dimension       text not null,   -- 'property' | 'zone' | 'source' | 'overall'
  key1            text,            -- source_code / province
  key2            text,            -- external_ref / district
  views           int default 0,
  unique_sessions int default 0,
  saves           int default 0,
  inquiries       int default 0,
  source_clicks   int default 0,
  primary key (day, dimension, key1, key2)
);

create or replace function rollup_events(p_day date default current_date - 1)
returns int language plpgsql as $$
declare n int;
begin
  -- รายทรัพย์
  insert into daily_rollup (day, dimension, key1, key2, views, unique_sessions,
                            saves, inquiries, source_clicks)
  select p_day, 'property', source_code, external_ref,
         count(*) filter (where event_type = 'view_detail'),
         count(distinct session_hash) filter (where event_type = 'view_detail'),
         count(*) filter (where event_type = 'save'),
         count(*) filter (where event_type = 'inquire'),
         count(*) filter (where event_type = 'click_source')
  from page_events
  where occurred_at::date = p_day and external_ref is not null
  group by 1,2,3,4
  on conflict (day, dimension, key1, key2) do update set
    views = excluded.views, unique_sessions = excluded.unique_sessions,
    saves = excluded.saves, inquiries = excluded.inquiries,
    source_clicks = excluded.source_clicks;

  -- รายโซน
  insert into daily_rollup (day, dimension, key1, key2, views, unique_sessions,
                            saves, inquiries)
  select p_day, 'zone', province, district,
         count(*), count(distinct session_hash),
         count(*) filter (where event_type = 'save'),
         count(*) filter (where event_type = 'inquire')
  from page_events
  where occurred_at::date = p_day and province is not null
  group by 1,2,3,4
  on conflict (day, dimension, key1, key2) do update set
    views = excluded.views, unique_sessions = excluded.unique_sessions,
    saves = excluded.saves, inquiries = excluded.inquiries;

  get diagnostics n = row_count;
  delete from page_events where purge_after < now();
  return n;
end $$;

-- ---------------------------------------------------------------------
-- ทรัพย์ที่คนสนใจมากที่สุด
--
-- ให้น้ำหนัก inquire > save > click_source > view
-- เพราะการดูเฉย ๆ กับการทักถามคนละเรื่องกันมาก
-- ---------------------------------------------------------------------
-- daily_rollup เก็บ key แบบทั่วไปเป็น key1/key2
-- สำหรับ dimension = 'property' คือ key1 = source_code, key2 = external_ref
create or replace view v_hot_properties as
with agg as (
  select key1 as source_code, key2 as external_ref,
         sum(views) as views, sum(unique_sessions) as sessions,
         sum(saves) as saves, sum(inquiries) as inquiries,
         sum(source_clicks) as source_clicks
  from daily_rollup
  where dimension = 'property' and day >= current_date - 30
  group by 1, 2
)
select a.*,
  (a.inquiries * 25 + a.saves * 8 + a.source_clicks * 3 + a.sessions) as interest_score,
  s.province, s.district, s.property_type, s.opening_price, s.auction_date
from agg a
left join lateral (
  select distinct on (1) province, district, property_type, opening_price, auction_date
  from listing_snapshots ls
  where ls.source_code = a.source_code and ls.external_ref = a.external_ref
  order by 1, ls.observed_at desc
) s on true
order by interest_score desc;

-- ---------------------------------------------------------------------
-- โซนที่คนดูเยอะ — ใช้ตัดสินว่าควรขยาย ingestion ไปที่ไหนต่อ
--
-- ตัวเลขที่มีค่าที่สุดคือ demand_supply_ratio
-- โซนที่คนดูเยอะแต่ทรัพย์น้อย = ควรหาทรัพย์เพิ่มที่นั่น
-- ---------------------------------------------------------------------
create or replace view v_hot_zones as
with demand as (
  select key1 as province, key2 as district,
         sum(views) as views, sum(unique_sessions) as sessions,
         sum(inquiries) as inquiries
  from daily_rollup
  where dimension = 'zone' and day >= current_date - 30
  group by 1,2
),
supply as (
  select province, district, count(distinct external_ref) as listings
  from listing_snapshots
  where auction_date is null or auction_date >= current_date
  group by 1,2
)
select
  d.province, d.district, d.views, d.sessions, d.inquiries,
  coalesce(s.listings, 0) as listings,
  round(d.sessions::numeric / nullif(s.listings, 0), 2) as demand_supply_ratio
from demand d
left join supply s
  on s.province = d.province and coalesce(s.district,'') = coalesce(d.district,'')
order by d.sessions desc;

-- ---------------------------------------------------------------------
-- สุขภาพการดึงข้อมูลรายแหล่ง
--
-- expected_every_hours = ความถี่ที่ควรมีข้อมูลใหม่
-- ถ้าเงียบเกินนั้น = มีปัญหา แม้ run จะขึ้น ok ก็ตาม
-- (scraper ที่รันผ่านแต่ parse ไม่ได้เลย คือเคสที่อันตรายที่สุด)
-- ---------------------------------------------------------------------
alter table sources add column if not exists expected_every_hours int default 26;

create or replace view v_source_health as
with last_run as (
  select distinct on (source_code) source_code, started_at, finished_at,
         status, pages_fetched, rows_parsed, rows_new, error_count, error_sample
  from ingest_runs order by source_code, started_at desc
),
recent as (
  select source_code,
         count(*) filter (where started_at >= now() - interval '7 days') as runs_7d,
         sum(rows_new) filter (where started_at >= now() - interval '7 days') as new_7d,
         count(*) filter (where status = 'failed'
                            and started_at >= now() - interval '7 days') as failed_7d
  from ingest_runs group by 1
)
select
  s.code, s.name, s.is_active, s.expected_every_hours,
  lr.started_at as last_run_at, lr.status as last_status,
  lr.pages_fetched, lr.rows_parsed, lr.rows_new, lr.error_count, lr.error_sample,
  coalesce(r.runs_7d, 0) as runs_7d,
  coalesce(r.new_7d, 0)  as new_7d,
  coalesce(r.failed_7d, 0) as failed_7d,
  round(extract(epoch from (now() - lr.started_at)) / 3600) as hours_since_run,
  case
    when not s.is_active then 'ปิดใช้งาน'
    when lr.started_at is null then 'ยังไม่เคยรัน'
    when extract(epoch from (now() - lr.started_at)) / 3600
         > s.expected_every_hours * 2 then 'เงียบนานผิดปกติ'
    when extract(epoch from (now() - lr.started_at)) / 3600
         > s.expected_every_hours then 'เลยกำหนด'
    when lr.status = 'failed' then 'รันล้มเหลว'
    when coalesce(r.new_7d, 0) = 0 and coalesce(r.runs_7d, 0) >= 3
      then 'รันผ่านแต่ไม่ได้ข้อมูลเลย'
    when lr.error_count > 0 then 'มีข้อผิดพลาดบางส่วน'
    else 'ปกติ'
  end as verdict
from sources s
left join last_run lr on lr.source_code = s.code
left join recent r on r.source_code = s.code
order by
  case when not s.is_active then 3
       when lr.status = 'failed' then 0 else 1 end,
  s.code;

comment on view v_source_health is
  'รันผ่านแต่ไม่ได้ข้อมูลเลย 3 ครั้งติด = เว็บต้นทางเปลี่ยนโครงสร้าง อันตรายกว่ารันล้มเหลว';

-- ทราฟฟิกรายวันรวม
create or replace view v_daily_traffic as
select day,
       sum(views) as views,
       sum(unique_sessions) as sessions,
       sum(inquiries) as inquiries
from daily_rollup
where dimension = 'zone' and day >= current_date - 30
group by day order by day;

alter table page_events enable row level security;
alter table daily_rollup enable row level security;
