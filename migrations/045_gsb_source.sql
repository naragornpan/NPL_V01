-- Migration 045 — แหล่งทรัพย์ ออมสิน (GSB NPA) — สำรวจ 2026-08-29 (adapters/gsb.py)
--   list: /asset/npa/all?page={n} (SSR __NEXT_DATA__ 12/หน้า ~372 หน้า ~4,453 รายการ)
--   ไม่มีพิกัด -> geocode ระดับตำบล · external_ref = gsb:{asset_id}
--   ⚠️ footer ห้ามทำซ้ำข้อมูล -> ประเมินความเสี่ยง/ขออนุญาตก่อนใช้เชิงพาณิชย์
insert into sources (code, name, base_url, encoding, rate_limit_s, is_active,
                     institution_code, notes) values
  ('gsb', 'ออมสิน - ทรัพย์ NPA (GSB)',
   'https://npa-assets.gsb.or.th', 'utf-8', 2.0, false,
   'gsb',
   'list: /asset/npa/all?page={n} (SSR __NEXT_DATA__ 12/หน้า) '
   '· รูป /apipr/npa/image?id= · external_ref=gsb:{asset_id} · ~4,453 รายการ '
   '· ไม่มีพิกัด(geocode ระดับตำบล) · footer ห้ามทำซ้ำ — ขออนุญาตก่อนใช้เชิงพาณิชย์')
on conflict (code) do update set base_url=excluded.base_url, name=excluded.name,
  encoding=excluded.encoding, institution_code=excluded.institution_code, notes=excluded.notes;
