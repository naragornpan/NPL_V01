-- =====================================================================
-- Migration 004 — วงจรพัฒนาโมเดลอย่างปลอดภัย
--
-- ปัญหาที่แก้: โมเดลที่ปรับตัวเองโดยไม่มีระบบตรวจสอบจะแย่ลงเรื่อย ๆ
-- โดยที่เจ้าของยังรู้สึกว่ามันฉลาดขึ้น
--
-- แยกการเปลี่ยนแปลงเป็นสองประเภทที่ปฏิบัติต่างกัน
--
--   A. Parameter refresh  — เส้นโค้ง uplift, base rate อัปเดตตามข้อมูลใหม่
--                           ทำอัตโนมัติได้ รายเดือน  version bump = patch
--
--   B. Structure change   — เพิ่มตัวแปร เปลี่ยนสูตร เปลี่ยนวิธีถ่วงน้ำหนัก
--                           ต้อง backtest + คนอนุมัติ  version bump = minor/major
--
-- การปล่อยให้ B เกิดอัตโนมัติคือจุดที่ระบบพังโดยไม่มีใครรู้
-- =====================================================================

-- ---------------------------------------------------------------------
-- ทะเบียนเวอร์ชันโมเดล
-- ---------------------------------------------------------------------
do $$
begin
  if to_regclass('public.uplift_observations') is null then
    raise exception 'ต้องรัน 003_appreciation.sql ให้สำเร็จก่อน';
  end if;
end $$;

create table if not exists model_versions (
  version        text primary key,          -- semver: 0.1.0
  status         text not null default 'challenger',
                 -- challenger | champion | retired
  change_type    text not null,             -- parameter | structure
  changelog      text not null,
  params         jsonb not null default '{}'::jsonb,
  created_at     timestamptz not null default now(),
  promoted_at    timestamptz,
  promoted_by    text,
  retired_at     timestamptz,
  retire_reason  text
);

-- มี champion ได้ตัวเดียวเท่านั้น
create unique index if not exists idx_single_champion
  on model_versions (status) where status = 'champion';

insert into model_versions (version, status, change_type, changelog)
values ('0.1.0', 'champion', 'structure',
        'เวอร์ชันแรก: difference-in-differences + ถ่วงน้ำหนักด้วยโอกาสโครงการเกิดจริง')
on conflict (version) do nothing;

-- ---------------------------------------------------------------------
-- Point-in-time snapshot ของเส้นโค้ง uplift
--
-- สำคัญที่สุดในไฟล์นี้: ถ้า backtest ใช้เส้นโค้งที่คำนวณจากข้อมูล "อนาคต"
-- ผลจะดูดีเกินจริงเสมอ (data leakage) แล้วคุณจะปล่อยโมเดลห่วย ๆ ขึ้น production
-- ด้วยความมั่นใจเต็มเปี่ยม
--
-- กฎ: การพยากรณ์ ณ วันที่ D ใช้ได้เฉพาะ snapshot ที่ as_of_date <= D
-- ---------------------------------------------------------------------
create table if not exists uplift_curve_snapshots (
  id             uuid primary key default gen_random_uuid(),
  as_of_date     date not null,
  project_type   text not null,
  event_type     text not null,
  distance_band  text not null,
  horizon_months int not null,
  n_obs          int not null,
  n_deals        int,
  p25            numeric,
  p50            numeric,
  p75            numeric,
  created_at     timestamptz not null default now(),
  unique (as_of_date, project_type, event_type, distance_band, horizon_months)
);
create index if not exists idx_curve_asof on uplift_curve_snapshots(as_of_date desc);

-- สร้าง snapshot ประจำเดือน
create or replace function snapshot_uplift_curves(p_as_of date default current_date)
returns int language plpgsql as $$
declare n int;
begin
  insert into uplift_curve_snapshots
    (as_of_date, project_type, event_type, distance_band, horizon_months,
     n_obs, n_deals, p25, p50, p75)
  select p_as_of, project_type, event_type, distance_band, horizon_months,
         n_obs, n_deals, p25, p50, p75
  from v_uplift_curves
  on conflict do nothing;
  get diagnostics n = row_count;
  return n;
end $$;

-- ---------------------------------------------------------------------
-- ตรวจจับการเปลี่ยนแปลงผิดปกติของเส้นโค้ง
-- ถ้า p50 กระโดดเกิน 30% จากเดือนก่อน มักไม่ใช่ตลาดเปลี่ยน
-- แต่เป็น parser พัง หรือมีดีลผิดปกติหลุดเข้ามา
-- ---------------------------------------------------------------------
create or replace view v_curve_drift as
with ranked as (
  select *, row_number() over (
      partition by project_type, event_type, distance_band, horizon_months
      order by as_of_date desc) as rn
  from uplift_curve_snapshots
)
select
  c.project_type, c.event_type, c.distance_band, c.horizon_months,
  p.as_of_date as prev_date, p.p50 as prev_p50, p.n_obs as prev_n,
  c.as_of_date as cur_date,  c.p50 as cur_p50,  c.n_obs as cur_n,
  round(abs(c.p50 - p.p50) / nullif(abs(p.p50), 0) * 100, 1) as drift_pct,
  case
    when abs(c.p50 - p.p50) / nullif(abs(p.p50), 0) > 0.30 then 'ต้องตรวจสอบ'
    when c.n_obs < p.n_obs then 'ตัวอย่างลดลง — ผิดปกติ'
    else 'ปกติ'
  end as verdict
from ranked c
join ranked p on p.rn = 2
  and p.project_type = c.project_type and p.event_type = c.event_type
  and p.distance_band = c.distance_band and p.horizon_months = c.horizon_months
where c.rn = 1;

-- ---------------------------------------------------------------------
-- ผูกการพยากรณ์เข้ากับ snapshot ที่ใช้ + ธง shadow
-- ---------------------------------------------------------------------
alter table property_forecasts
  add column if not exists curve_snapshot_date date,
  add column if not exists is_shadow boolean not null default false;
comment on column property_forecasts.is_shadow is
  'true = challenger รันคู่ขนานเพื่อเปรียบเทียบ ไม่แสดงให้ลูกค้าเห็น';

-- ห้าม UPDATE ค่าพยากรณ์เก่า — ต้อง insert แถวใหม่เสมอ
-- ไม่งั้นจะประเมินความแม่นย้อนหลังไม่ได้เลย
create or replace function forbid_forecast_update()
returns trigger language plpgsql as $$
begin
  raise exception 'ห้ามแก้การพยากรณ์เดิม ให้ insert แถวใหม่แทน (append-only)';
end $$;

drop trigger if exists trg_forecast_immutable on property_forecasts;
create trigger trg_forecast_immutable
  before update on property_forecasts
  for each row execute function forbid_forecast_update();

-- ---------------------------------------------------------------------
-- ผลการประเมินโมเดล
--
-- band_coverage คือตัวชี้วัดที่สำคัญกว่า error
--   ถ้าใช้ p25-p75 เป็นช่วง ค่าจริงควรตกในช่วงราว 50%
--   สูงกว่านั้นมาก = ช่วงกว้างเกินไปจนไร้ประโยชน์
--   ต่ำกว่านั้นมาก = มั่นใจเกินจริง อันตรายกว่า
-- ---------------------------------------------------------------------
create table if not exists forecast_evaluations (
  id             uuid primary key default gen_random_uuid(),
  model_version  text not null references model_versions(version),
  evaluated_at   timestamptz not null default now(),
  horizon_months int not null,
  n_outcomes     int not null,
  median_ape     numeric,        -- median absolute percentage error
  band_coverage  numeric,        -- % ที่ค่าจริงตกในช่วง bear-bull
  bias_pct       numeric,        -- บวก = พยากรณ์สูงเกินจริง
  notes          text
);

create or replace function evaluate_model(p_version text, p_horizon int)
returns uuid language plpgsql as $$
declare eval_id uuid;
begin
  insert into forecast_evaluations
    (model_version, horizon_months, n_outcomes, median_ape, band_coverage, bias_pct)
  select
    p_version, p_horizon,
    count(*),
    round(percentile_cont(0.5) within group (
      order by abs(o.actual_value - f.mid_value) / nullif(o.actual_value, 0) * 100
    )::numeric, 1),
    round(100.0 * count(*) filter (
      where o.actual_value between f.bear_value and f.bull_value) / count(*), 1),
    round(avg((f.mid_value - o.actual_value) / nullif(o.actual_value, 0) * 100)::numeric, 1)
  from property_forecasts f
  join forecast_outcomes o on o.forecast_id = f.id
  where f.model_version = p_version and f.horizon_months = p_horizon
  returning id into eval_id;
  return eval_id;
end $$;

-- ---------------------------------------------------------------------
-- กฎการเลื่อนขั้น challenger -> champion
-- ประกาศเกณฑ์ไว้ล่วงหน้า ห้ามเปลี่ยนเกณฑ์หลังเห็นผลแล้ว
-- ---------------------------------------------------------------------
create table if not exists promotion_criteria (
  id             int primary key default 1,
  min_outcomes   int not null default 30,
  max_median_ape numeric not null default 25.0,
  min_coverage   numeric not null default 40.0,
  max_coverage   numeric not null default 65.0,
  max_abs_bias   numeric not null default 10.0,
  note           text,
  constraint single_row check (id = 1)
);
insert into promotion_criteria (id, note) values
  (1, 'ประกาศไว้ก่อนเห็นผล ห้ามแก้เกณฑ์เพื่อให้โมเดลที่อยากใช้ผ่าน')
on conflict (id) do nothing;

create or replace view v_promotion_check as
select
  e.model_version, e.horizon_months, e.n_outcomes,
  e.median_ape, e.band_coverage, e.bias_pct,
  (e.n_outcomes    >= c.min_outcomes)   as pass_sample,
  (e.median_ape    <= c.max_median_ape) as pass_error,
  (e.band_coverage between c.min_coverage and c.max_coverage) as pass_coverage,
  (abs(e.bias_pct) <= c.max_abs_bias)   as pass_bias,
  (e.n_outcomes >= c.min_outcomes
   and e.median_ape <= c.max_median_ape
   and e.band_coverage between c.min_coverage and c.max_coverage
   and abs(e.bias_pct) <= c.max_abs_bias) as eligible
from forecast_evaluations e
cross join promotion_criteria c
where e.evaluated_at = (
  select max(evaluated_at) from forecast_evaluations e2
  where e2.model_version = e.model_version and e2.horizon_months = e.horizon_months
);

alter table model_versions          enable row level security;
alter table uplift_curve_snapshots  enable row level security;
alter table forecast_evaluations    enable row level security;
alter table promotion_criteria      enable row level security;
