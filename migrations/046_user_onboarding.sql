-- =====================================================================
-- Migration 046 — Onboarding: บทบาท + ความสนใจของผู้ใช้ (segment + alert)
--
-- roles        : บทบาทที่ผู้ใช้เลือก (เลือกได้หลายอย่าง) เช่น {buy,invest}
-- intent       : เกณฑ์ที่ต้องการ (ทำเล/งบ/ประเภท/เลี้ยงสัตว์/สนใจประมูล ฯลฯ) เป็น jsonb
-- onboarded_at : เวลาที่กรอก onboarding ครั้งแรก (null = ยังไม่กรอก → เด้งฟอร์ม)
--
-- ใช้แยก segment (ซื้ออยู่จริง/นักลงทุน/เช่า/ปล่อยบ้าน) และเป็นฐานของ
-- saved_search + LINE alert ในเฟสถัดไป
-- =====================================================================
alter table app_users add column if not exists roles        text[];
alter table app_users add column if not exists intent       jsonb;
alter table app_users add column if not exists onboarded_at  timestamptz;

-- ค้น user ตามบทบาทได้เร็ว (เช่น ยิง alert เฉพาะนักลงทุน)
create index if not exists idx_app_users_roles on app_users using gin(roles);
