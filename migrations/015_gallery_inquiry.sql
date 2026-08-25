-- =====================================================================
-- Migration 015 — แกลเลอรีรูป + ฟอร์มติดต่อกลับ
--
-- แกลเลอรี: ดึงเฉพาะตอนมีคนเปิดดูจริง (lazy)
--   ถ้าดึงล่วงหน้าทุกทรัพย์คือ 3,400 request เพิ่ม
--   แต่ทรัพย์ที่ไม่มีใครเปิดดูเลยก็ไม่ต้องมีแกลเลอรี
--   จึงดึงครั้งแรกที่มีคนกดเข้าไปดู แล้ว cache ถาวร
-- =====================================================================

create table if not exists property_gallery (
  id            bigserial primary key,
  source_code   text not null,
  external_ref  text not null,
  image_url     text not null,
  sort_order    int not null default 0,
  fetched_at    timestamptz not null default now(),
  unique (source_code, external_ref, image_url)
);
create index if not exists idx_gallery_ref
  on property_gallery(source_code, external_ref, sort_order);

-- บันทึกว่าเคยดึงแกลเลอรีของทรัพย์นี้แล้วหรือยัง
-- แยกจาก property_gallery เพราะบางทรัพย์ดึงแล้วไม่เจอรูปเลย
-- ถ้าไม่บันทึกไว้จะไปดึงซ้ำทุกครั้งที่มีคนเปิด
create table if not exists gallery_fetch_log (
  source_code   text not null,
  external_ref  text not null,
  fetched_at    timestamptz not null default now(),
  image_count   int not null default 0,
  status        text not null default 'ok',   -- ok | not_found | error
  primary key (source_code, external_ref)
);

-- ---------------------------------------------------------------------
-- คำขอติดต่อกลับ
--
-- ผูกกับ leads ที่มีอยู่แล้วใน migration 009
-- ไม่สร้างระบบผู้ใช้ใหม่ เพราะคนที่สนใจทรัพย์ยังไม่ใช่สมาชิก
-- ---------------------------------------------------------------------
create table if not exists property_inquiries (
  id             uuid primary key default gen_random_uuid(),
  lead_id        uuid references leads(id) on delete set null,
  source_code    text not null,
  external_ref   text not null,
  contact_name   text,
  phone          text,
  line_id        text,
  email          text,
  message        text,
  preferred_time text,          -- ช่วงเวลาที่สะดวกให้ติดต่อกลับ
  budget_note    text,
  funding_source text,          -- cash | loan | unsure
  status         text not null default 'new',
                 -- new | contacted | qualified | closed | spam
  handled_by     text,
  handled_at     timestamptz,
  internal_note  text,
  session_hash   text,          -- โยงกับ page_events แบบไม่ระบุตัวตน
  created_at     timestamptz not null default now()
);
create index if not exists idx_inq_status on property_inquiries(status, created_at desc);
create index if not exists idx_inq_prop on property_inquiries(source_code, external_ref);

-- สรุปสำหรับ admin — ทรัพย์ไหนมีคนถามเยอะ
create or replace view v_inquiries_summary as
select
  i.source_code, i.external_ref,
  count(*)                                          as total,
  count(*) filter (where i.status = 'new')          as pending,
  max(i.created_at)                                 as latest,
  s.title, s.province, s.district, s.opening_price, g.grade
from property_inquiries i
left join lateral (
  select distinct on (1) title, province, district, opening_price
  from listing_snapshots l
  where l.source_code = i.source_code and l.external_ref = i.external_ref
  order by 1, l.observed_at desc
) s on true
left join property_grades g
  on g.source_code = i.source_code and g.external_ref = i.external_ref
group by i.source_code, i.external_ref, s.title, s.province, s.district,
         s.opening_price, g.grade
order by pending desc, total desc;

alter table property_gallery    enable row level security;
alter table gallery_fetch_log   enable row level security;
alter table property_inquiries  enable row level security;
