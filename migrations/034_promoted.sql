-- =====================================================================
-- Migration 034 — promoted_properties: ทรัพย์ที่ "ดันโปรโมท"
-- โชว์ใน rail ด้านขวาของหน้าแรก (จอกว้าง)
-- การจัดการรายการ (เพิ่ม/ลบ) จะทำผ่านหลังบ้าน Phase ถัดไป (พร้อมหน้า add ทรัพย์)
-- ระหว่างนี้เพิ่มได้ด้วย SQL:
--   insert into promoted_properties (source_code, external_ref, rank, note)
--   values ('bam','12345', 1, 'ดีลเด่นสัปดาห์นี้');
-- =====================================================================

create table if not exists promoted_properties (
  id           bigserial primary key,
  source_code  text not null,
  external_ref text not null,
  rank         int  not null default 100,   -- น้อย = อยู่บนสุด
  note         text,                          -- โน้ตภายใน (ไม่โชว์หน้าเว็บ)
  active       boolean not null default true,
  created_at   timestamptz not null default now(),
  unique (source_code, external_ref)
);

create index if not exists idx_promoted_active_rank
  on promoted_properties (active, rank) where active is true;
