-- =====================================================================
-- Migration 009 — เก็บ lead + consent + ส่งต่อสินเชื่อ
--
-- สินทรัพย์ที่แท้จริงของ Phase แรกคือ "รายชื่อคนที่อยากซื้อพร้อมความต้องการ"
-- ไม่ใช่จำนวนทรัพย์ในฐานข้อมูล
--
-- แต่ข้อมูลคนมาพร้อมภาระตาม PDPA และการส่งต่อให้สถาบันการเงิน
-- ต้องมี consent เฉพาะวัตถุประสงค์นั้น จะเหมารวมกับ "ยอมรับเงื่อนไข" ไม่ได้
-- =====================================================================

-- ---------------------------------------------------------------------
-- วัตถุประสงค์การใช้ข้อมูล — แยกให้ผู้ใช้เลือกทีละข้อ
-- ห้ามรวมเป็นช่องเดียวว่า "ยอมรับทั้งหมด"
-- ---------------------------------------------------------------------
create table if not exists consent_purposes (
  code          text primary key,
  label         text not null,
  is_required   boolean not null,   -- จำเป็นต่อการให้บริการไหม
  description   text not null
);

insert into consent_purposes (code, label, is_required, description) values
  ('service', 'ใช้ข้อมูลเพื่อหาทรัพย์ให้ตรงความต้องการ', true,
   'จำเป็นต่อการให้บริการ ถ้าไม่ยินยอมจะให้บริการไม่ได้'),
  ('marketing', 'ส่งข่าวทรัพย์ใหม่และเนื้อหาที่เกี่ยวข้อง', false,
   'เลือกได้ ถอนได้ทุกเมื่อ'),
  ('lender_referral', 'ส่งข้อมูลให้สถาบันการเงินเพื่อเสนอสินเชื่อ', false,
   'เลือกได้ ต้องระบุว่าส่งให้สถาบันใดบ้าง และถอนได้ทุกเมื่อ'),
  ('contractor_referral', 'ส่งข้อมูลให้ผู้รับเหมาเพื่อเสนอราคา', false,
   'เลือกได้ ส่งเฉพาะตอนที่ผู้ใช้ขอประเมินราคาเท่านั้น')
on conflict (code) do nothing;

-- ---------------------------------------------------------------------
-- Lead — คนที่สนใจ ยังไม่จำเป็นต้องสมัครสมาชิก
-- ช่วงแรกเก็บผ่าน LINE OA / ฟอร์ม ยังไม่ต้องมี auth
-- ---------------------------------------------------------------------
create table if not exists leads (
  id              uuid primary key default gen_random_uuid(),
  user_id         uuid,                  -- ผูกทีหลังถ้าเขาสมัครสมาชิก
  display_name    text,
  phone           text,
  line_user_id    text,
  email           text,

  source          text,                  -- 'line_oa' | 'content' | 'referral' | 'walk_in'
  source_detail   text,                  -- โพสต์ไหน ใครแนะนำ
  first_contact_at timestamptz not null default now(),
  last_contact_at  timestamptz,

  declared_type   text,                  -- buyer | investor
  observed_type   text,
  status          text not null default 'new',
                  -- new | qualified | viewing | negotiating | closed | lost
  lost_reason     text,
  owner_note      text,
  created_at      timestamptz not null default now()
);
create index if not exists idx_leads_status on leads(status, last_contact_at desc);
create unique index if not exists idx_leads_line on leads(line_user_id)
  where line_user_id is not null;

-- ---------------------------------------------------------------------
-- ความต้องการ — ฟิลด์ที่สำคัญที่สุดคือเรื่องเงิน
-- ---------------------------------------------------------------------
create table if not exists lead_requirements (
  id                uuid primary key default gen_random_uuid(),
  lead_id           uuid not null references leads(id) on delete cascade,
  property_types    text[],
  provinces         text[],
  districts         text[],
  budget_max        numeric,
  budget_min        numeric,

  funding_source    text,               -- cash | loan | mixed
  loan_preapproved  boolean,
  preapproved_amount numeric,
  monthly_capacity  numeric,            -- ผ่อนไหวเดือนละเท่าไหร่

  purpose           text,               -- own_use | rental | flip
  timeline          text,               -- now | 3m | 6m | 12m
  accept_occupied   boolean,            -- รับทรัพย์ที่มีผู้อยู่อาศัยเดิมได้ไหม
  accept_auction    boolean,            -- เคยประมูล/กล้าประมูลไหม
  is_active         boolean default true,
  created_at        timestamptz not null default now()
);
create index if not exists idx_req_lead on lead_requirements(lead_id, is_active);

comment on column lead_requirements.funding_source is
  'ฟิลด์สำคัญที่สุดในตารางนี้ — คนที่กู้ไม่ผ่านคือต้นทุนเวลาที่ฆ่านายหน้ามือใหม่';

-- ---------------------------------------------------------------------
-- Consent — เก็บทุกครั้งที่ให้หรือถอน ไม่ทับของเดิม
-- ต้องพิสูจน์ย้อนหลังได้ว่าเขายินยอมอะไร เมื่อไหร่ ด้วยข้อความเวอร์ชันไหน
-- ---------------------------------------------------------------------
create table if not exists lead_consents (
  id             uuid primary key default gen_random_uuid(),
  lead_id        uuid not null references leads(id) on delete cascade,
  purpose        text not null references consent_purposes(code),
  granted        boolean not null,
  policy_version text not null,
  channel        text,                  -- 'line' | 'web_form' | 'paper'
  evidence       text,                  -- ข้อความ/ภาพหน้าจอที่ยืนยัน
  occurred_at    timestamptz not null default now()
);
create index if not exists idx_consent_lookup
  on lead_consents(lead_id, purpose, occurred_at desc);

-- สถานะ consent ปัจจุบัน = รายการล่าสุดของแต่ละวัตถุประสงค์
create or replace view v_current_consent as
select distinct on (lead_id, purpose)
  lead_id, purpose, granted, policy_version, occurred_at
from lead_consents
order by lead_id, purpose, occurred_at desc;

-- ---------------------------------------------------------------------
-- ส่งต่อสินเชื่อ — บันทึกทุกครั้งที่ส่งข้อมูลออกไป
--
-- กฎที่ฝังไว้ในระบบ:
--   ส่งได้เฉพาะเมื่อมี consent 'lender_referral' ที่ granted = true อยู่
--   trigger ด้านล่างบล็อกให้เอง ไม่ต้องพึ่งวินัยของคนกรอก
-- ---------------------------------------------------------------------
create table if not exists lender_partners (
  code            text primary key,
  label           text not null,
  program_name    text,
  agreement_signed boolean default false,
  agreement_date  date,
  compliance_note text,
  is_active       boolean default false
);

comment on table lender_partners is
  'is_active = false จนกว่าจะมีสัญญาโครงการพันธมิตรจริง '
  'อย่าส่งข้อมูลให้สถาบันที่ยังไม่ได้เซ็นสัญญา';

create table if not exists loan_referrals (
  id             uuid primary key default gen_random_uuid(),
  lead_id        uuid not null references leads(id) on delete cascade,
  lender_code    text not null references lender_partners(code),
  referred_at    timestamptz not null default now(),
  consent_id     uuid references lead_consents(id),
  status         text not null default 'sent',
                 -- sent | contacted | approved | rejected | withdrawn
  approved_amount numeric,
  commission_expected numeric,
  commission_received numeric,
  received_at    timestamptz,
  note           text
);

create or replace function require_referral_consent()
returns trigger language plpgsql as $$
declare ok boolean;
begin
  select granted into ok from v_current_consent
   where lead_id = new.lead_id and purpose = 'lender_referral';
  if ok is not true then
    raise exception
      'ส่งข้อมูลให้สถาบันการเงินไม่ได้ — lead นี้ยังไม่ได้ให้ความยินยอม (lender_referral)';
  end if;
  if not exists (select 1 from lender_partners
                  where code = new.lender_code and is_active and agreement_signed) then
    raise exception 'สถาบัน % ยังไม่มีสัญญาโครงการพันธมิตรที่ใช้งานอยู่', new.lender_code;
  end if;
  return new;
end $$;

drop trigger if exists trg_referral_consent on loan_referrals;
create trigger trg_referral_consent
  before insert on loan_referrals
  for each row execute function require_referral_consent();

-- ---------------------------------------------------------------------
-- Lead ที่พร้อมจริง — ใช้จัดลำดับว่าจะโทรหาใครก่อน
-- ---------------------------------------------------------------------
create or replace view v_qualified_leads as
select
  l.id, l.display_name, l.line_user_id, l.declared_type, l.status,
  r.budget_max, r.funding_source, r.loan_preapproved, r.timeline,
  r.provinces, r.property_types,
  (case when r.funding_source = 'cash' then 40
        when r.loan_preapproved then 35
        when r.funding_source = 'loan' then 15
        else 0 end)
  + (case r.timeline when 'now' then 30 when '3m' then 20
                     when '6m' then 10 else 0 end)
  + (case when r.budget_max is not null then 15 else 0 end)
  + (case when r.accept_auction then 10 else 0 end)
  + (case when l.declared_type = 'investor' then 5 else 0 end)
    as readiness_score,
  (current_date - l.last_contact_at::date) as days_since_contact
from leads l
join lead_requirements r on r.lead_id = l.id and r.is_active
where l.status not in ('closed', 'lost');

-- ---------------------------------------------------------------------
-- ตัววัดกรวยการขาย — ดูว่าคอขวดอยู่ขั้นไหน
-- ---------------------------------------------------------------------
create or replace view v_funnel as
select
  date_trunc('month', l.first_contact_at)::date as month,
  count(*)                                                as contacts,
  count(*) filter (where exists (
    select 1 from lead_requirements r where r.lead_id = l.id))  as with_requirement,
  count(*) filter (where l.status in ('viewing','negotiating','closed')) as engaged,
  count(*) filter (where l.status = 'closed')             as closed,
  count(*) filter (where l.status = 'lost')               as lost
from leads l
group by 1 order by 1 desc;

alter table leads             enable row level security;
alter table lead_requirements enable row level security;
alter table lead_consents     enable row level security;
alter table loan_referrals    enable row level security;
alter table lender_partners   enable row level security;
alter table consent_purposes  enable row level security;
