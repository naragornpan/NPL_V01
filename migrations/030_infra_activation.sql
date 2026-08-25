-- =====================================================================
-- Migration 030 — เปิดใช้เครื่องยนต์ infra/เวนคืน (Phase B)
--
-- 002 อ้างถึง property_links + property_flags แต่ไม่เคยสร้างตาราง
-- do_grade จึงมี guard to_regclass('property_flags') ข้ามให้ตลอด
-- (เกรดปัจจุบันยังไม่ใช้ flag ทำเล/เวนคืนเลย)
--
-- ไฟล์นี้สร้างตารางที่ขาด + ฟังก์ชัน populate links
-- pipeline จะทำงานเมื่อ (ก) มีข้อมูลใน infra_projects และ
-- (ข) รัน enrich.py infra (refresh links -> refresh_infra_features
--     -> evaluate_infra -> property_flags -> เข้าเกรด)
-- =====================================================================

-- property_links: property_id คงที่ต่อ (source_code, external_ref)
-- refresh_infra_features() ใช้ตัวนี้เชื่อมทรัพย์กับฟีเจอร์ทำเล
-- หมายเหตุ: ตารางนี้อาจมีอยู่แล้วในฐาน (สร้างนอก migrations) — create if not
-- exists จะข้าม แล้วเราทำงานกับ schema เดิม (ใส่ property_id เองด้วย uuid)
create table if not exists property_links (
  property_id   uuid not null default gen_random_uuid(),
  source_code   text not null,
  external_ref  text not null,
  created_at    timestamptz not null default now(),
  primary key (source_code, external_ref)
);
create index if not exists idx_plink_pid on property_links(property_id);

-- property_flags: flag จาก rules_infra (ถ่วง certainty แล้ว) ป้อนเข้าเกรด
create table if not exists property_flags (
  property_id   uuid not null,
  rule_code     text not null,
  severity      text not null,        -- positive | info | caution | critical
  score         numeric,              -- ถ่วงน้ำหนักแล้ว
  evidence      text,
  computed_at   timestamptz not null default now(),
  primary key (property_id, rule_code)
);
create index if not exists idx_pflag_pid on property_flags(property_id);
-- กันกรณีตารางเดิมมีอยู่แล้วแต่ขาดคอลัมน์ที่ต้องใช้
alter table property_flags add column if not exists rule_code   text;
alter table property_flags add column if not exists severity    text;
alter table property_flags add column if not exists score       numeric;
alter table property_flags add column if not exists evidence    text;
alter table property_flags add column if not exists computed_at timestamptz default now();

-- property_links.property_id เป็น FK -> properties(id) (ตารางทรัพย์แม่ canonical
-- ที่วางไว้ในฐานแต่ไม่มีโค้ดไหน populate) จึงต้องสร้าง row ใน properties ก่อน
-- แล้วค่อย link — ทำ 1 ทรัพย์ : 1 properties (ยังไม่ทำ dedup ข้ามแหล่ง)
create or replace function refresh_property_links() returns int
language plpgsql as $$
declare r record; new_id uuid; n int := 0;
begin
  for r in
    select distinct s.source_code, s.external_ref
      from listing_snapshots s
     where not exists (select 1 from property_links pl
                        where pl.source_code = s.source_code
                          and pl.external_ref = s.external_ref)
  loop
    -- สร้างทรัพย์แม่ (ใส่แค่ id — คอลัมน์อื่นต้อง nullable/มี default)
    insert into properties (id) values (gen_random_uuid()) returning id into new_id;
    insert into property_links (property_id, source_code, external_ref)
    values (new_id, r.source_code, r.external_ref);
    n := n + 1;
  end loop;
  return n;
end $$;

-- รันเลยรอบแรก
select refresh_property_links() as ทรัพย์ที่ผูก;

-- ตรวจว่ามีข้อมูล infra หรือยัง (ถ้า 0 = ต้องโหลดก่อนด้วย tools/load_infra.py)
select count(*) as โครงการ_infra, count(*) filter (where geom is not null) as มี_geometry
  from infra_projects;
