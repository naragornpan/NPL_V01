-- =====================================================================
-- Migration 033 — site_feedback: เก็บความเห็นจากปุ่ม feedback (ช่วง beta)
-- ไม่เก็บข้อมูลระบุตัวตน นอกจากช่อง contact ที่ผู้ใช้กรอกเองโดยสมัครใจ
-- =====================================================================

create table if not exists site_feedback (
  id           bigserial primary key,
  created_at   timestamptz not null default now(),
  message      text,
  rating       smallint check (rating between 1 and 5),
  contact      text,        -- ชื่อ/LINE ที่ผู้ใช้กรอกเอง (ไม่บังคับ)
  page_url     text,        -- หน้าที่เปิดตอนส่ง (path เท่านั้น)
  device_class text,        -- mobile / desktop
  sid          text,        -- session id ฝั่ง client (ไม่ผูกตัวตน)
  -- อย่างน้อยต้องมีข้อความหรือคะแนน
  constraint site_feedback_has_content check (message is not null or rating is not null)
);

create index if not exists idx_site_feedback_created on site_feedback (created_at desc);
