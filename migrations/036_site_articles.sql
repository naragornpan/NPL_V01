-- =====================================================================
-- Migration 036 — site_articles (บทความ CMS สำหรับ agent นักเขียน SEO)
--
-- แอปอ่านบทความจากตารางนี้ + รวมกับบทความ hardcoded ในโค้ด (DB ชนะถ้า slug ซ้ำ)
-- agent เขียนบทความแล้ว POST เข้า /admin/articles (ยืนยันด้วย ADMIN_TOKEN)
-- upsert เข้าตารางนี้ → หน้าเว็บขึ้นทันที (cache 5 นาที) ไม่ต้อง deploy
-- =====================================================================
create table if not exists site_articles (
  slug         text primary key,          -- a-z0-9- เท่านั้น (แอป sanitize ให้)
  title        text not null,
  excerpt      text,                       -- สรุปสั้น (ใช้ทำ meta description + การ์ด)
  body_html    text not null,              -- เนื้อหาเป็น HTML (h2/p/ul/blockquote ...)
  emoji        text default '📝',
  updated      date not null default current_date,
  published    boolean not null default true,
  created_at   timestamptz not null default now()
);

create index if not exists idx_site_articles_pub
  on site_articles(published, updated desc);
