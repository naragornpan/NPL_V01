-- 047_service_leads.sql — โมดูลขายลีดบริการเรื่องบ้าน (lead fee)
-- รัน: psql "<DATABASE_URL>" -f migrations/047_service_leads.sql

begin;

-- ── หมวดบริการ + ราคาต่อลีด ───────────────────────────────────────────────
create table if not exists service_categories (
  code        text primary key,
  name        text not null,
  emoji       text not null default '🔧',
  lead_price  numeric(10,2) not null default 50,   -- ราคาต่อ 1 ลีด (หักจากเครดิตร้าน)
  max_fanout  int  not null default 3,             -- ส่งลีดเดียวให้กี่ร้าน
  sort        int  not null default 100,
  is_active   boolean not null default true
);

insert into service_categories (code, name, emoji, lead_price, sort) values
  ('cleaning',  'แม่บ้าน/ทำความสะอาด', '🧹', 40, 10),
  ('bigclean',  'Big Cleaning / ล้างบ้านหลังรีโนเวท', '🧽', 80, 20),
  ('aircon',    'ล้าง/ซ่อมแอร์',        '❄️', 50, 30),
  ('renovate',  'รีโนเวท/ต่อเติม',       '🛠', 120, 40),
  ('moving',    'ขนย้าย',               '🚚', 60, 50),
  ('pest',      'กำจัดปลวก/แมลง',        '🐜', 70, 60),
  ('survey',    'ตรวจบ้าน/ตรวจรับโอน',   '📐', 100, 70)
on conflict (code) do nothing;

-- ── ร้าน/ผู้ให้บริการ ─────────────────────────────────────────────────────
create table if not exists service_providers (
  id              uuid primary key default gen_random_uuid(),
  name            text not null,
  contact_phone   text not null,
  line_id         text,
  line_user_id    text,                    -- ถ้าล็อกอิน LINE แล้ว ใช้ push แจ้งลีด
  email           text,
  categories      text[] not null default '{}',
  provinces       text[] not null default '{}',
  districts       text[] not null default '{}',   -- ว่าง = ทั้งจังหวัด
  credit_balance  numeric(12,2) not null default 0,
  daily_lead_cap  int not null default 5,
  status          text not null default 'pending',  -- pending/active/paused/banned
  verified_at     timestamptz,
  verified_by     text,                    -- ต้องเป็นคน (กฎเดียวกับ infra)
  rating          numeric(3,2),
  note            text,
  created_at      timestamptz not null default now()
);
create index if not exists idx_sp_cat  on service_providers using gin (categories);
create index if not exists idx_sp_prov on service_providers using gin (provinces);
create index if not exists idx_sp_live on service_providers (status) where status = 'active';

-- ── ลีดจากลูกค้า ──────────────────────────────────────────────────────────
create table if not exists service_leads (
  id             bigserial primary key,
  category_code  text references service_categories(code),
  province       text,
  district       text,
  source_code    text,        -- ทรัพย์ที่กดมา (ถ้ามี)
  external_ref   text,
  customer_name  text not null,
  customer_phone text not null,
  customer_line  text,
  detail         text,
  consent        boolean not null default false,   -- PDPA
  status         text not null default 'new',      -- new/delivered/no_provider/spam
  ip_hash        text,
  created_at     timestamptz not null default now()
);
create index if not exists idx_lead_new  on service_leads (created_at desc);
create index if not exists idx_lead_zone on service_leads (category_code, province, district);
create index if not exists idx_lead_ip   on service_leads (ip_hash, created_at desc);

-- ── การส่งลีด (= ธุรกรรมที่เก็บเงินจริง) ─────────────────────────────────
create table if not exists lead_deliveries (
  id            bigserial primary key,
  lead_id       bigint not null references service_leads(id) on delete cascade,
  provider_id   uuid   not null references service_providers(id) on delete cascade,
  price         numeric(10,2) not null,
  status        text not null default 'charged',   -- charged/refunded
  refund_reason text,
  viewed_at     timestamptz,
  created_at    timestamptz not null default now(),
  unique (lead_id, provider_id)
);
create index if not exists idx_del_prov on lead_deliveries (provider_id, created_at desc);

-- ── บัญชีเครดิต (ทุกการเคลื่อนไหวต้องมีบรรทัด) ───────────────────────────
create table if not exists provider_credit_ledger (
  id            bigserial primary key,
  provider_id   uuid not null references service_providers(id) on delete cascade,
  delta         numeric(12,2) not null,            -- + เติม / - หัก
  reason        text not null,                     -- topup/lead_charge/refund/adjust
  ref_id        bigint,                            -- lead_deliveries.id ถ้ามี
  balance_after numeric(12,2),
  note          text,
  created_at    timestamptz not null default now()
);
create index if not exists idx_ledger_prov on provider_credit_ledger (provider_id, created_at desc);

-- ── วิวสรุปสำหรับหลังบ้าน ────────────────────────────────────────────────
create or replace view v_lead_daily as
select
  date_trunc('day', l.created_at)::date            as day,
  l.category_code,
  count(*)                                          as leads,
  count(*) filter (where l.status = 'delivered')    as delivered,
  coalesce(sum(d.price) filter (where d.status = 'charged'), 0) as revenue
from service_leads l
left join lead_deliveries d on d.lead_id = l.id
group by 1, 2;

commit;
