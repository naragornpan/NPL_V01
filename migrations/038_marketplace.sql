-- =====================================================================
-- Migration 038 — Marketplace: ผู้ใช้ลงประกาศขาย/เช่าเอง (แยกจากทรัพย์ NPA)
--
-- member_listings เป็นตารางแยก ไม่ปนกับ listing_snapshots (NPA)
-- → หน้า /market โชว์เฉพาะประกาศสมาชิก, หน้าหลัก (/) ยังเป็น NPA เหมือนเดิม
-- =====================================================================
create table if not exists member_listings (
  id             uuid primary key default gen_random_uuid(),
  posted_by      text not null,                 -- app_users.line_user_id (เช่น line:.. / google:..)
  listing_kind   text not null default 'sale',  -- 'sale' | 'rent'
  property_type  text,                          -- land/house/townhouse/condo/commercial/...
  title          text not null,
  description    text,
  province       text, district text, subdistrict text,
  address_raw    text,
  lat            numeric, lng numeric,
  price          numeric,                        -- ราคาขาย หรือ ค่าเช่า/เดือน
  deposit        numeric,                        -- เงินมัดจำ (กรณีเช่า)
  land_area_sqwa numeric,
  usable_area_sqm numeric,
  bedrooms       int, bathrooms int, parking int,
  contact_name   text, contact_phone text, contact_line text,
  status         text not null default 'pending',  -- pending | approved | rejected
  reject_reason  text,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
create index if not exists idx_member_listings_status
  on member_listings(status, created_at desc);
create index if not exists idx_member_listings_user
  on member_listings(posted_by, created_at desc);
create index if not exists idx_member_listings_browse
  on member_listings(status, listing_kind, province, created_at desc);

create table if not exists member_listing_images (
  id          uuid primary key default gen_random_uuid(),
  listing_id  uuid not null references member_listings(id) on delete cascade,
  url         text not null,
  sort_order  int not null default 0,
  created_at  timestamptz not null default now()
);
create index if not exists idx_member_img_listing
  on member_listing_images(listing_id, sort_order);
