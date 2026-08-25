-- =====================================================================
-- Migration 006 — รูปภาพทรัพย์
--
-- ประเด็นที่ต้องตัดสินใจก่อนเขียนโค้ด: รูปจากเว็บอื่นมีลิขสิทธิ์
--
--   ใช้ภายใน (เครื่องตัวเอง วิเคราะห์งาน)  -> เก็บ URL + cache ได้ ความเสี่ยงต่ำ
--   เผยแพร่ต่อ (SaaS ให้ลูกค้าดู)          -> ต้องมีสิทธิ์ ไม่งั้นเสี่ยงจริง
--
-- ระบบจึงติดธง usage_scope ทุกภาพ และเว็บจะซ่อนภาพที่ไม่มีสิทธิ์
-- อัตโนมัติเมื่อรันในโหมด public
-- =====================================================================

create table if not exists image_sources (
  code          text primary key,
  label         text not null,
  usage_scope   text not null,      -- internal_only | publishable
  note          text
);

insert into image_sources (code, label, usage_scope, note) values
  ('led_announcement', 'รูปแนบประกาศกรมบังคับคดี', 'internal_only',
   'เป็นเอกสารราชการเผยแพร่สาธารณะ แต่ยังไม่ได้ตรวจเงื่อนไขการนำไปใช้ซ้ำเชิงพาณิชย์'),
  ('bank_npa', 'รูปจากเว็บธนาคาร/AMC', 'internal_only',
   'ห้ามเผยแพร่ต่อ เว้นแต่ได้รับอนุญาตในฐานะนายหน้าที่ขึ้นทะเบียนแล้ว'),
  ('portal_listing', 'รูปจากเว็บประกาศขาย', 'internal_only',
   'ลิขสิทธิ์ของผู้ลงประกาศ ห้ามเผยแพร่ต่อเด็ดขาด'),
  ('own_survey', 'รูปที่เราถ่ายเอง', 'publishable',
   'ทางเดียวที่ปลอดภัย 100% สำหรับสินค้าที่เปิดให้คนอื่นดู'),
  ('broker_authorized', 'รูปที่ได้รับอนุญาตจากเจ้าของทรัพย์', 'publishable',
   'ต้องมีหลักฐานการอนุญาตเก็บไว้'),
  ('map_static', 'ภาพแผนที่/ดาวเทียม', 'internal_only',
   'ขึ้นกับเงื่อนไขของผู้ให้บริการแผนที่ ต้องอ่านก่อนใช้เชิงพาณิชย์')
on conflict (code) do nothing;

create table if not exists listing_images (
  id            uuid primary key default gen_random_uuid(),
  source_code   text not null,
  external_ref  text not null,
  property_id   uuid references properties(id) on delete set null,
  image_source  text not null references image_sources(code),

  origin_url    text,               -- URL ต้นทาง
  cached_path   text,               -- ไฟล์ในเครื่อง (โหมดภายในเท่านั้น)
  content_hash  text,               -- กันเก็บซ้ำ
  width         int,
  height        int,
  caption       text,
  sort_order    int default 0,
  is_primary    boolean default false,

  attribution   text,               -- ต้องแสดงเสมอเมื่อโชว์ภาพ
  captured_at   timestamptz,
  created_at    timestamptz not null default now(),
  unique (source_code, external_ref, content_hash)
);
create index if not exists idx_img_ref on listing_images(source_code, external_ref, sort_order);

-- ภาพหลักของแต่ละรายการ เลือกภาพที่เผยแพร่ได้ก่อนเสมอ
create or replace view v_primary_image as
select distinct on (i.source_code, i.external_ref)
  i.source_code, i.external_ref, i.id as image_id,
  i.origin_url, i.cached_path, i.caption, i.attribution,
  s.usage_scope
from listing_images i
join image_sources s on s.code = i.image_source
order by i.source_code, i.external_ref,
         (s.usage_scope = 'publishable') desc,
         i.is_primary desc, i.sort_order;

alter table listing_images enable row level security;
alter table image_sources  enable row level security;
