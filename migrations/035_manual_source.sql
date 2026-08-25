-- =====================================================================
-- Migration 035 — แหล่ง "กรอกเอง" (manual) สำหรับหน้า add ทรัพย์
-- ทรัพย์ที่ผู้ดูแลเพิ่มผ่านหน้าเว็บจะถูกเก็บเป็น source_code='manual'
-- รูปที่แนบใช้ image_source='own_survey' (usage_scope='publishable' อยู่แล้ว
-- จาก migration 006) จึงโชว์ได้แม้เปิด PUBLIC_MODE=1
-- =====================================================================

-- สถาบันเจ้าของ = แปลงดี (ลงเอง) — legal_status permitted จึงเปิด is_active ได้
insert into institutions (code, short_name, full_name, kind, legal_status, is_active, sort_order)
values ('manual', 'ลงเอง', 'ทรัพย์ที่ผู้ดูแลเพิ่มเอง (แปลงดี)', 'government', 'permitted', true, 5)
on conflict (code) do update set legal_status = 'permitted', is_active = true;

-- source manual ผูกกับสถาบันข้างต้น
insert into sources (code, name, base_url, robots_ok, is_active, institution_code, notes)
values ('manual', 'กรอกเอง (แปลงดี)', null, true, true, 'manual',
        'ทรัพย์ที่ผู้ดูแลเพิ่มผ่านหน้าเว็บ /admin/add')
on conflict (code) do update set is_active = true, institution_code = 'manual';
