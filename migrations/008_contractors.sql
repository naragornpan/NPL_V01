-- =====================================================================
-- Migration 008 — สมาชิกผู้รับเหมา + จับคู่งาน
--
-- ทำไมฝั่งนี้ทำเงินง่ายกว่าฝั่งผู้ซื้อ
--   ผู้รับเหมาคำนวณ ROI ได้ตรง ๆ: งานเดียวที่ได้คุ้มค่าสมาชิกทั้งปีไหม
--   ผู้ซื้อทั่วไปตัดสินใจจ่ายรายเดือนยากกว่ามาก
--
-- ความเสี่ยงหลักคือ leakage — ผู้รับเหมากับลูกค้าเจอกันผ่านเรา
-- แล้วไปตกลงกันเองนอกระบบ
--   -> ค่าสมาชิกรายเดือน ทนต่อ leakage ได้ดีกว่าค่าคอมต่องาน
--      เพราะเก็บล่วงหน้าและไม่ต้องพิสูจน์ว่าใครปิดงานได้
--   -> อย่าเริ่มด้วยโมเดลหักเปอร์เซ็นต์จากงาน มันบังคับใช้ไม่ได้จริง
--      และจะผลักผู้รับเหมาที่ดีที่สุดออกจากระบบ
--
-- ลำดับที่ห้ามสลับ
--   มี deal flow จริงก่อน -> ผู้รับเหมาสมัครฟรี -> พิสูจน์ว่ามีงานส่งให้จริง
--   -> ค่อยเก็บเงิน   (เก็บเงินก่อนมีงาน = เสียเครดิตถาวร)
-- =====================================================================

-- ---------------------------------------------------------------------
-- ความชำนาญ — ให้เลือกได้หลายอย่าง แต่ต้องระบุระดับ
-- ---------------------------------------------------------------------
create table if not exists specialties (
  code        text primary key,
  label       text not null,
  category    text not null,       -- structure | system | finishing | specialty
  sort_order  int default 0
);

insert into specialties (code, label, category, sort_order) values
  ('structure_repair', 'ซ่อมโครงสร้าง/เสาคาน',      'structure', 10),
  ('roofing',          'หลังคา/ฝ้าเพดาน',            'structure', 20),
  ('extension',        'ต่อเติม/ขยายพื้นที่',         'structure', 30),
  ('demolition',       'รื้อถอน/เคลียร์พื้นที่',       'structure', 40),
  ('electrical',       'ระบบไฟฟ้า',                  'system',    50),
  ('plumbing',         'ประปา/สุขาภิบาล',            'system',    60),
  ('waterproofing',    'กันซึม/แก้รั่ว',              'system',    70),
  ('aircon',           'ระบบปรับอากาศ',              'system',    80),
  ('tiling',           'ปูกระเบื้อง/พื้น',            'finishing', 90),
  ('painting',         'ทาสี/ฉาบผนัง',               'finishing', 100),
  ('carpentry',        'งานไม้/บิลท์อิน',             'finishing', 110),
  ('door_window',      'ประตูหน้าต่าง/กระจก',         'finishing', 120),
  ('condo_reno',       'รีโนเวทห้องชุด',              'specialty', 130),
  ('full_reno',        'รีโนเวททั้งหลัง',             'specialty', 140),
  ('cleanout',         'เก็บกวาดทรัพย์ทิ้งร้าง',       'specialty', 150),
  ('survey_photo',     'สำรวจ+ถ่ายรูปประเมิน',        'specialty', 160)
on conflict (code) do nothing;

comment on table specialties is
  'survey_photo สำคัญกว่าที่คิด — ผู้รับเหมาที่รับงานสำรวจก่อนจะได้งานรีโนเวทต่อสูงมาก';

-- ---------------------------------------------------------------------
-- ผู้รับเหมา
-- ---------------------------------------------------------------------
create table if not exists contractors (
  id                uuid primary key default gen_random_uuid(),
  user_id           uuid,                 -- ผูกกับ auth ถ้ามี
  display_name      text not null,
  contact_name      text,
  phone             text,
  line_user_id      text,
  email             text,

  business_type     text,                 -- individual | registered | company
  tax_id            text,
  registered_no     text,
  has_insurance     boolean default false,

  verification_level text not null default 'unverified',
                     -- unverified | documents_checked | site_verified
  verified_at       timestamptz,
  verified_by       text,

  crew_size         int,
  years_experience  int,
  min_job_value     numeric,              -- ไม่รับงานต่ำกว่านี้
  max_job_value     numeric,              -- รับไหวสูงสุด
  accepts_npa       boolean default true, -- รับงานทรัพย์ยึด/ทรัพย์ทิ้งร้างไหม

  status            text not null default 'pending',
                    -- pending | active | paused | suspended
  suspended_reason  text,
  consent_version   text,
  consent_at        timestamptz,
  created_at        timestamptz not null default now()
);

comment on column contractors.accepts_npa is
  'ทรัพย์ยึดมักสกปรก มีของเดิมทิ้งไว้ บางเจ้าไม่รับ ต้องถามตั้งแต่สมัคร';

create table if not exists contractor_specialties (
  contractor_id  uuid not null references contractors(id) on delete cascade,
  specialty      text not null references specialties(code),
  skill_level    text not null default 'competent',  -- competent | strong | expert
  years          int,
  primary key (contractor_id, specialty)
);

-- ---------------------------------------------------------------------
-- พื้นที่ให้บริการ — สำคัญที่สุดในการจับคู่
-- ผู้รับเหมาไม่ข้ามจังหวัดเพราะค่าเดินทางกินกำไร
-- ---------------------------------------------------------------------
create table if not exists contractor_areas (
  contractor_id   uuid not null references contractors(id) on delete cascade,
  province        text not null,
  district        text,                  -- null = ทั้งจังหวัด
  is_primary      boolean default false, -- โซนหลักที่อยู่จริง
  travel_fee_note text,
  primary key (contractor_id, province, district)
);
create index if not exists idx_carea on contractor_areas(province, district);

-- ---------------------------------------------------------------------
-- แผนสมาชิกและการสมัคร
-- ---------------------------------------------------------------------
create table if not exists contractor_plans (
  code             text primary key,
  label            text not null,
  price_monthly    numeric not null,
  leads_per_month  int,                  -- null = ไม่จำกัด
  early_access_hrs int default 0,        -- เห็นงานก่อนกี่ชั่วโมง
  max_areas        int,
  can_bid_outside  boolean default false,
  show_badge       boolean default false,
  is_active        boolean default true,
  note             text
);

insert into contractor_plans
  (code, label, price_monthly, leads_per_month, early_access_hrs,
   max_areas, can_bid_outside, show_badge, note) values
  ('free',   'ฟรี',        0,    1,    0, 1, false, false,
   'ให้สมัครฟรีช่วงแรกจนกว่าจะพิสูจน์ได้ว่ามีงานส่งให้จริง'),
  ('pro',    'Pro',        0,    8,   12, 3, false, true,
   'ราคายังไม่กำหนด — ตั้งหลังรู้ว่าหนึ่ง lead มีมูลค่าเท่าไหร่จริง'),
  ('premium','Premium',    0, null,   24, 8, true,  true,
   'ราคายังไม่กำหนด')
on conflict (code) do nothing;

comment on table contractor_plans is
  'price_monthly = 0 ทุกแผนโดยตั้งใจ ห้ามตั้งราคาก่อนรู้ conversion rate จริง';

create table if not exists contractor_subscriptions (
  id              uuid primary key default gen_random_uuid(),
  contractor_id   uuid not null references contractors(id) on delete cascade,
  plan_code       text not null references contractor_plans(code),
  status          text not null default 'active',   -- active | past_due | cancelled
  started_at      timestamptz not null default now(),
  current_period_end timestamptz,
  cancelled_at    timestamptz,
  cancel_reason   text
);
create index if not exists idx_csub on contractor_subscriptions(contractor_id, status);

-- ---------------------------------------------------------------------
-- คำขอประเมินราคา (งานที่จะส่งให้ผู้รับเหมา)
-- ---------------------------------------------------------------------
create table if not exists job_requests (
  id              uuid primary key default gen_random_uuid(),
  property_id     uuid references properties(id) on delete set null,
  survey_id       uuid,                  -- โยงกับ surveys ถ้ามี
  requester_user  uuid,
  province        text not null,
  district        text,
  property_type   text,
  specialties     text[] not null,       -- งานที่ต้องการ
  budget_estimate numeric,               -- จาก BOQ ร่าง
  urgency         text default 'normal', -- normal | urgent
  site_accessible boolean,               -- เข้าไปดูข้างในได้ไหม
  notes           text,
  status          text not null default 'open',
                  -- open | quoted | awarded | closed | expired
  created_at      timestamptz not null default now(),
  expires_at      timestamptz
);

-- ส่ง lead ให้ผู้รับเหมาแต่ละราย — นับโควตาจากตารางนี้
create table if not exists lead_offers (
  id              uuid primary key default gen_random_uuid(),
  job_request_id  uuid not null references job_requests(id) on delete cascade,
  contractor_id   uuid not null references contractors(id) on delete cascade,
  match_score     numeric,
  match_reasons   jsonb not null default '[]'::jsonb,
  sent_at         timestamptz not null default now(),
  viewed_at       timestamptz,
  responded_at    timestamptz,
  response        text,                  -- interested | declined | no_response
  counts_toward_quota boolean default true,
  unique (job_request_id, contractor_id)
);
create index if not exists idx_lead_quota
  on lead_offers(contractor_id, sent_at) where counts_toward_quota;

create table if not exists contractor_quotes (
  id              uuid primary key default gen_random_uuid(),
  job_request_id  uuid not null references job_requests(id) on delete cascade,
  contractor_id   uuid not null references contractors(id) on delete cascade,
  amount          numeric not null,
  breakdown       jsonb,
  days_estimate   int,
  warranty_months int,
  valid_until     date,
  submitted_at    timestamptz not null default now(),
  is_awarded      boolean default false,
  unique (job_request_id, contractor_id)
);

-- งานที่จบแล้ว — ป้อนกลับเข้า unit_costs และคะแนนผู้รับเหมา
create table if not exists job_outcomes (
  job_request_id  uuid primary key references job_requests(id) on delete cascade,
  contractor_id   uuid not null references contractors(id),
  quoted_amount   numeric,
  final_amount    numeric,
  started_on      date,
  completed_on    date,
  days_actual     int,
  overrun_pct     numeric generated always as
                  (case when quoted_amount > 0
                   then (final_amount / quoted_amount - 1) * 100 end) stored,
  quality_rating  int check (quality_rating between 1 and 5),
  would_rehire    boolean,
  review_note     text,
  recorded_at     timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- คะแนนผู้รับเหมา — จากงานจริงเท่านั้น ไม่รับรีวิวลอย
-- ---------------------------------------------------------------------
create or replace view v_contractor_performance as
select
  c.id as contractor_id, c.display_name, c.verification_level,
  count(distinct o.job_request_id)                          as jobs_done,
  round(avg(o.quality_rating)::numeric, 2)                  as avg_rating,
  round(avg(o.overrun_pct)::numeric, 1)                     as avg_overrun_pct,
  round(100.0 * count(*) filter (where o.would_rehire)
        / nullif(count(*), 0), 0)                           as rehire_pct,
  round(avg(extract(epoch from (lo.responded_at - lo.sent_at)) / 3600)::numeric, 1)
                                                            as avg_response_hours,
  round(100.0 * count(distinct q.job_request_id)
        / nullif(count(distinct lo.job_request_id), 0), 0)  as quote_rate_pct
from contractors c
left join job_outcomes o     on o.contractor_id = c.id
left join lead_offers lo     on lo.contractor_id = c.id
left join contractor_quotes q on q.contractor_id = c.id
group by c.id, c.display_name, c.verification_level;

-- ---------------------------------------------------------------------
-- จับคู่ — คืนคะแนนพร้อมเหตุผลที่อธิบายได้ (หลักการข้อ 1 ของโปรเจกต์)
-- ---------------------------------------------------------------------
create or replace function match_contractors(p_job uuid, p_limit int default 5)
returns table (
  contractor_id uuid,
  display_name  text,
  score         numeric,
  reasons       jsonb
) language plpgsql as $$
declare j record;
begin
  select * into j from job_requests where id = p_job;
  if j is null then
    raise exception 'ไม่พบ job_request %', p_job;
  end if;

  return query
  with quota as (
    select lo.contractor_id, count(*) as used
    from lead_offers lo
    where lo.counts_toward_quota
      and lo.sent_at >= date_trunc('month', now())
    group by 1
  ),
  base as (
    select
      c.id, c.display_name, c.verification_level,
      c.min_job_value, c.max_job_value, c.accepts_npa,
      coalesce(p.leads_per_month, 999999) as lead_cap,
      coalesce(q.used, 0)                 as leads_used,
      -- พื้นที่: โซนหลักได้เต็ม โซนรองได้ครึ่ง
      max(case when ca.district = j.district and ca.is_primary then 40
               when ca.district = j.district then 30
               when ca.district is null and ca.province = j.province then 22
               else 0 end)                as area_pts,
      -- ความชำนาญ: นับว่าครอบคลุมงานที่ต้องการกี่ %
      count(distinct cs.specialty) filter (
        where cs.specialty = any(j.specialties))            as matched_spec,
      count(distinct cs.specialty) filter (
        where cs.specialty = any(j.specialties)
          and cs.skill_level in ('strong', 'expert'))       as strong_spec
    from contractors c
    join contractor_subscriptions sub
      on sub.contractor_id = c.id and sub.status = 'active'
    join contractor_plans p on p.code = sub.plan_code
    join contractor_areas ca
      on ca.contractor_id = c.id and ca.province = j.province
    left join contractor_specialties cs on cs.contractor_id = c.id
    left join quota q on q.contractor_id = c.id
    where c.status = 'active'
    group by c.id, c.display_name, c.verification_level, c.min_job_value,
             c.max_job_value, c.accepts_npa, p.leads_per_month, q.used
  ),
  scored as (
    select b.*,
      round(
        b.area_pts
        + 35.0 * b.matched_spec / nullif(array_length(j.specialties, 1), 0)
        + 5.0 * b.strong_spec
        + case b.verification_level when 'site_verified' then 10
                                    when 'documents_checked' then 6 else 0 end
        + coalesce(least(perf.avg_rating, 5) * 2, 0)
        + case when perf.avg_overrun_pct is not null
                    and perf.avg_overrun_pct <= 10 then 5 else 0 end
        - case when j.budget_estimate is not null
                    and (j.budget_estimate < b.min_job_value
                         or j.budget_estimate > b.max_job_value) then 25 else 0 end
        - case when not b.accepts_npa then 40 else 0 end
      , 1) as score,
      perf.avg_rating, perf.jobs_done, perf.avg_overrun_pct
    from base b
    left join v_contractor_performance perf on perf.contractor_id = b.id
    where b.leads_used < b.lead_cap        -- เคารพโควตาของแผน
      and b.matched_spec > 0
  )
  select s.id, s.display_name, s.score,
    jsonb_build_array(
      jsonb_build_object('factor', 'พื้นที่',      'points', s.area_pts),
      jsonb_build_object('factor', 'ความชำนาญ',   'matched', s.matched_spec,
                         'strong', s.strong_spec),
      jsonb_build_object('factor', 'การยืนยันตัวตน', 'level', s.verification_level),
      jsonb_build_object('factor', 'ผลงาน', 'jobs', coalesce(s.jobs_done, 0),
                         'rating', s.avg_rating, 'overrun_pct', s.avg_overrun_pct),
      jsonb_build_object('factor', 'โควตาคงเหลือ',
                         'used', s.leads_used, 'cap', s.lead_cap)
    )
  from scored s
  order by s.score desc
  limit p_limit;
end $$;

-- ---------------------------------------------------------------------
-- ป้อนกลับเข้า unit_costs — เหตุผลที่แท้จริงที่อยากได้ผู้รับเหมาในระบบ
-- ทุกใบเสนอราคาทำให้ตารางต้นทุนของเราแม่นขึ้น
-- ---------------------------------------------------------------------
create or replace view v_quote_calibration as
select
  j.province, j.district, j.property_type,
  unnest(j.specialties)                                   as specialty,
  count(distinct q.id)                                    as n_quotes,
  round(percentile_cont(0.5) within group (order by q.amount)::numeric) as median_quote,
  round(percentile_cont(0.25) within group (order by q.amount)::numeric) as p25_quote,
  round(percentile_cont(0.75) within group (order by q.amount)::numeric) as p75_quote,
  round(avg(o.final_amount / nullif(q.amount, 0))::numeric, 3) as actual_to_quote_ratio
from job_requests j
join contractor_quotes q on q.job_request_id = j.id
left join job_outcomes o on o.job_request_id = j.id and o.contractor_id = q.contractor_id
group by 1, 2, 3, 4;

alter table contractors              enable row level security;
alter table contractor_specialties   enable row level security;
alter table contractor_areas         enable row level security;
alter table contractor_subscriptions enable row level security;
alter table job_requests             enable row level security;
alter table lead_offers              enable row level security;
alter table contractor_quotes        enable row level security;
alter table job_outcomes             enable row level security;
alter table contractor_plans         enable row level security;
alter table specialties              enable row level security;
