-- =====================================================================
-- Migration 039 — เก็บ log ผู้กดดูประกาศสมาชิก + ยอดวิว
-- =====================================================================
create table if not exists member_listing_views (
  id          bigserial primary key,
  listing_id  uuid not null references member_listings(id) on delete cascade,
  viewer_uid  text,                       -- app_users.line_user_id ถ้าล็อกอิน (null ถ้าไม่)
  viewed_at   timestamptz not null default now()
);
create index if not exists idx_mlv_listing
  on member_listing_views(listing_id, viewed_at desc);
