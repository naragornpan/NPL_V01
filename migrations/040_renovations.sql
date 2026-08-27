-- =====================================================================
-- Migration 040 — AI Renovate: เก็บภาพจำลองรีโนเวท (before/after) ของประกาศสมาชิก
-- =====================================================================
create table if not exists member_renovations (
  id          uuid primary key default gen_random_uuid(),
  listing_id  uuid not null references member_listings(id) on delete cascade,
  source_url  text not null,           -- รูปต้นฉบับ (before)
  result_url  text not null,           -- รูปที่ AI สร้าง (after)
  style       text,                    -- สไตล์ที่เลือก
  created_by  text,                    -- ผู้กดสร้าง (uid หรือ 'admin')
  created_at  timestamptz not null default now()
);
create index if not exists idx_member_reno_listing
  on member_renovations(listing_id, created_at desc);
