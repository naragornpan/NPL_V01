-- =====================================================================
-- Migration 041 — Marketplace เพิ่มเติม
--   • pets_allowed  : ประกาศเช่า ระบุการเลี้ยงสัตว์ได้
--   • last_bumped_at: เวลาที่ดันประกาศล่าสุด (ใช้ทำระบบดัน + หมดอายุ 60 วัน)
-- =====================================================================
alter table member_listings
  add column if not exists pets_allowed  text,          -- 'yes' | 'no' | 'ask' | null
  add column if not exists last_bumped_at timestamptz not null default now();

-- ประกาศเดิม: ตั้งเวลาดันล่าสุด = วันที่สร้าง เพื่อให้เริ่มนับหมดอายุจากตอนลง
update member_listings
   set last_bumped_at = created_at
 where last_bumped_at is null or last_bumped_at < created_at;

-- ดัชนีสำหรับฟีดที่เรียงตาม "ดันล่าสุด" + กรองที่ยังไม่หมดอายุ
create index if not exists idx_member_listings_active
  on member_listings(status, last_bumped_at desc);
