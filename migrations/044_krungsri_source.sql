-- =====================================================================
-- Migration 044 — แหล่งทรัพย์ กรุงศรี (Krungsri Property)
--   ผลสำรวจ 2026-08-29 (adapters/krungsri.py มีบันทึกละเอียด)
--     เว็บ: https://www.krungsriproperty.com
--     list: /search-result?page={n} (server-render 10/หน้า ~167 หน้า ~1,666 รายการ)
--     detail: /detail?code={CODE} มีพิกัดจริง (Google Maps) → เติมด้วย krungsri-coords
--     external_ref = krungsri:{CODE}  (CODE จากปุ่ม _open('...'))
--   is_active = false ไว้ก่อน จนกว่าจะเคลียร์ ToS/PDPA + พันธมิตร (ปลดล็อกแบบ 021)
-- =====================================================================

insert into sources (code, name, base_url, encoding, rate_limit_s, is_active,
                     institution_code, notes) values
  ('krungsri', 'กรุงศรี - บ้านมือสอง/NPA (Krungsri Property)',
   'https://www.krungsriproperty.com', 'utf-8', 2.0, false,
   'krungsri',
   'list: /search-result?page={n} (server-render 10/หน้า ~167 หน้า) '
   '· detail: /detail?code={CODE} มีพิกัดจริง (เติมด้วย krungsri-coords) '
   '· external_ref = krungsri:{CODE} · ~1,666 รายการ '
   '· ราคา: originalPrice=ราคาตั้ง, promoPrice=ราคาลด (opening=ราคาลด) '
   '· ต้องอ่าน ToS/PDPA + ขอพันธมิตร/นายหน้ากรุงศรีก่อนเปิดใช้เชิงพาณิชย์')
on conflict (code) do update set
  base_url = excluded.base_url, name = excluded.name,
  encoding = excluded.encoding, institution_code = excluded.institution_code,
  notes = excluded.notes;
