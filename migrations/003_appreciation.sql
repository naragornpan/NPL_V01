-- =====================================================================
-- Migration 003 — ชั้นประเมินมูลค่าอนาคต (appreciation layer)
--
-- ปรัชญา: ไม่พยากรณ์ราคา แต่ "วัดว่าเคยเกิดอะไรขึ้นกับโซนที่เหมือนกัน"
--
-- สิ่งที่ระบบนี้ทำ:      เทียบกับเหตุการณ์ในอดีต แล้วคืนช่วงราคา 3 ฉาก
-- สิ่งที่ระบบนี้ไม่ทำ:   บอกว่าราคาจะเป็นเท่าไหร่ / รับประกันผลตอบแทน
--
-- กับดักใหญ่ที่สุดคือ survivorship bias — โครงการที่สร้างเสร็จเห็นชัด
-- แต่โครงการที่ถูกยกเลิก/ค้างมองไม่เห็น ถ้าไม่บันทึกโครงการที่ล้มด้วย
-- ตัวเลข uplift จะสูงเกินจริงทุกครั้ง
-- =====================================================================

-- ---------------------------------------------------------------------
-- ขยาย infra_projects ให้บันทึกโครงการที่ล้มด้วย
-- ---------------------------------------------------------------------
do $$
begin
  if to_regclass('public.infra_projects') is null then
    raise exception 'ต้องรัน 002_infra_and_price.sql ให้สำเร็จก่อน';
  end if;
end $$;

alter table infra_projects
  add column if not exists lifecycle_status text not null default 'active';
  -- active | stalled | cancelled | completed
comment on column infra_projects.lifecycle_status is
  'ต้องบันทึก stalled/cancelled ด้วย ไม่งั้น base rate จะโกหก';

alter table infra_projects
  add column if not exists first_announced_at date;

-- ---------------------------------------------------------------------
-- อัตราการไปถึงแต่ละขั้นจริง — คำนวณจากข้อมูลของเราเอง
-- ตอบคำถาม "โครงการที่ผ่านมติ ครม. แล้ว สุดท้ายเปิดใช้จริงกี่ %"
-- ---------------------------------------------------------------------
create or replace view v_stage_base_rates as
with reached as (
  select p.id, p.project_type,
         max(cl.level) as max_level,
         p.lifecycle_status,
         min(e.event_date) filter (where e.event_type = 'cabinet') as cabinet_at,
         min(e.event_date) filter (where e.event_type = 'operational') as open_at
  from infra_projects p
  left join infra_events e on e.project_id = p.id
  left join certainty_levels cl on cl.code = e.event_type
  group by p.id, p.project_type, p.lifecycle_status
)
select
  project_type,
  count(*)                                                  as n_projects,
  count(*) filter (where max_level >= 3)                    as reached_decree,
  count(*) filter (where max_level >= 5)                    as reached_open,
  count(*) filter (where lifecycle_status in ('stalled', 'cancelled')) as failed,
  round(100.0 * count(*) filter (where max_level >= 5)
        / nullif(count(*) filter (where max_level >= 2), 0), 1) as pct_cabinet_to_open,
  round(avg(extract(year from age(open_at, cabinet_at)))::numeric, 1) as avg_years_cabinet_to_open
from reached
group by project_type;

-- ---------------------------------------------------------------------
-- การสังเกตการเปลี่ยนแปลงราคา — หนึ่งแถวคือ หนึ่งโซน x หนึ่งเหตุการณ์
--
-- method = 'did' คือ difference-in-differences:
--   uplift ที่แท้จริง = (โซนใกล้เปลี่ยนไปกี่ %) - (โซนควบคุมเปลี่ยนไปกี่ %)
-- ถ้าไม่หักโซนควบคุม จะเอาเงินเฟ้อทั้งเมืองมาเป็นผลงานของถนน
-- ---------------------------------------------------------------------
create table if not exists uplift_observations (
  id               uuid primary key default gen_random_uuid(),
  project_id       uuid references infra_projects(id) on delete cascade,
  project_type     text not null,
  event_type       text not null,          -- ขั้นที่เกิดเหตุการณ์
  event_date       date not null,
  distance_band    text not null,          -- '0-500' | '500-1000' | '1000-2000'
  horizon_months   int not null,           -- 12 | 24 | 36 | 60
  price_source     text not null,          -- 'led_sold' | 'gov_appraisal'

  price_before     numeric,
  price_after      numeric,
  raw_change_pct   numeric,
  control_change_pct numeric,              -- แถบ >2000m ในจังหวัดเดียวกัน
  net_uplift_pct   numeric,                -- raw - control  ← ตัวที่ใช้จริง
  sample_n         int,                    -- จำนวนดีลที่ใช้คำนวณ
  method           text not null default 'did',
  computed_at      timestamptz not null default now()
);
create index if not exists idx_uplift_key
  on uplift_observations(project_type, event_type, distance_band, horizon_months);

-- ---------------------------------------------------------------------
-- เส้นโค้ง uplift — สรุปจาก observations เพื่อเอาไปใช้พยากรณ์
-- ใช้ percentile ไม่ใช่ค่าเฉลี่ย เพราะข้อมูลอสังหาเบ้มาก
-- ---------------------------------------------------------------------
create or replace view v_uplift_curves as
select
  project_type, event_type, distance_band, horizon_months,
  count(*)                                                        as n_obs,
  sum(sample_n)                                                   as n_deals,
  round(percentile_cont(0.25) within group (order by net_uplift_pct)::numeric, 1) as p25,
  round(percentile_cont(0.50) within group (order by net_uplift_pct)::numeric, 1) as p50,
  round(percentile_cont(0.75) within group (order by net_uplift_pct)::numeric, 1) as p75
from uplift_observations
where net_uplift_pct is not null
group by 1, 2, 3, 4
having count(*) >= 3;      -- ต่ำกว่า 3 การสังเกต ไม่ใช้เป็นฐานพยากรณ์

-- ---------------------------------------------------------------------
-- ผลพยากรณ์ต่อทรัพย์ — เก็บทุกครั้งที่คำนวณ เพื่อย้อนตรวจความแม่นได้
--
-- เก็บ assumptions ไว้ด้วยเสมอ พอผ่านไป 2 ปีจะได้รู้ว่า
-- ที่พลาดเพราะสมมติฐานผิด หรือเพราะโครงการไม่เกิด
-- ---------------------------------------------------------------------
create table if not exists property_forecasts (
  id                uuid primary key default gen_random_uuid(),
  property_id       uuid not null references properties(id) on delete cascade,
  computed_at       timestamptz not null default now(),
  horizon_months    int not null,

  base_value        numeric not null,      -- มูลค่าตั้งต้นที่ใช้คำนวณ
  base_value_source text not null,         -- 'comps' | 'gov_appraisal_adjusted'

  bear_value        numeric,
  mid_value         numeric,
  bull_value        numeric,
  expected_uplift_pct numeric,

  confidence        text not null,         -- low | medium | high
  confidence_reason text,
  reasons           jsonb not null default '[]'::jsonb,  -- เหตุผลพร้อมหลักฐาน
  assumptions       jsonb not null default '{}'::jsonb,
  model_version     text not null
);
create index if not exists idx_forecast_prop on property_forecasts(property_id, computed_at desc);

-- ---------------------------------------------------------------------
-- ตรวจความแม่นย้อนหลัง — รันเมื่อทรัพย์ถูกขายจริง
-- ถ้าไม่มีตารางนี้ จะไม่มีวันรู้ว่าโมเดลใช้ได้หรือเปล่า
-- ---------------------------------------------------------------------
create table if not exists forecast_outcomes (
  forecast_id      uuid primary key references property_forecasts(id) on delete cascade,
  actual_value     numeric not null,
  actual_at        date not null,
  error_pct        numeric,
  within_band      boolean,
  note             text
);

alter table uplift_observations enable row level security;
alter table property_forecasts  enable row level security;
alter table forecast_outcomes   enable row level security;
