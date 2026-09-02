-- 048_service_leads_geo.sql — เก็บพิกัดลีด/ร้าน (สำหรับปักหมุด + เรียงตามรัศมีภายหลัง)
-- รัน: psql "<DATABASE_URL>" -f migrations/048_service_leads_geo.sql
--
-- backward-compatible: คอลัมน์ nullable ทั้งหมด ไม่กระทบการจับคู่เดิม (ยังใช้จังหวัด/อำเภอ)
--   service_leads.lat/lng      = พิกัดทรัพย์ที่ลูกค้ากดมา (auto เติมตอนสร้างลีด)
--   service_providers.lat/lng  = ฐานที่ตั้งร้าน (auto = ศูนย์กลางจังหวัดตอนสมัคร/เพิ่ม)

begin;

alter table service_leads     add column if not exists lat double precision;
alter table service_leads     add column if not exists lng double precision;
alter table service_providers add column if not exists lat double precision;
alter table service_providers add column if not exists lng double precision;

commit;
