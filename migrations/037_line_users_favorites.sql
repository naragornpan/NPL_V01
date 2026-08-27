-- =====================================================================
-- Migration 037 — LINE Login + ทรัพย์โปรด (retention)
--
-- app_users: ผู้ใช้ที่ล็อกอินด้วย LINE (userId เป็น PK)
-- user_favorites: ทรัพย์ที่ผู้ใช้กดโปรด (ต่อ user + ต่อทรัพย์)
-- =====================================================================
create table if not exists app_users (
  line_user_id text primary key,
  display_name text,
  picture_url  text,
  created_at   timestamptz not null default now(),
  last_login   timestamptz not null default now()
);

create table if not exists user_favorites (
  line_user_id text not null references app_users(line_user_id) on delete cascade,
  source_code  text not null,
  external_ref text not null,
  created_at   timestamptz not null default now(),
  primary key (line_user_id, source_code, external_ref)
);

create index if not exists idx_user_fav_user
  on user_favorites(line_user_id, created_at desc);
