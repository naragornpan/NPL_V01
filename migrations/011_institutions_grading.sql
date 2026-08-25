-- =====================================================================
-- Migration 011 — รวมทรัพย์ NPA หลายสถาบัน + ระบบเกรด
--
-- บทเรียนจากการสำรวจ: ธนาคารออมสินมีข้อความห้ามนำข้อมูลไปใช้
-- เหมือนกรมบังคับคดีทุกตัวอักษร แปลว่าเป็นแพทเทิร์นของหน่วยงานไทย
-- ไม่ใช่กรณีพิเศษ -> ต้องตรวจทีละเจ้าและบังคับที่ระดับฐานข้อมูล
-- =====================================================================

create table if not exists institutions (
  code            text primary key,
  short_name      text not null,          -- ใช้แสดงบนการ์ด
  full_name       text not null,
  kind            text not null,          -- bank | amc | government
  npa_url         text,
  partner_url     text,                   -- หน้าโครงการนายหน้า/พันธมิตร

  legal_status    text not null default 'unknown',
  -- unknown          ยังไม่ได้ตรวจ
  -- tos_reviewed     อ่าน ToS แล้ว ไม่มีข้อห้ามชัดเจน
  -- restricted       มีข้อความห้ามนำข้อมูลไปใช้ ต้องขออนุญาต
  -- permitted        ได้รับอนุญาตหรือเป็นพันธมิตรแล้ว
  -- prohibited       ตรวจแล้วห้ามชัดเจนและขอไม่ได้

  legal_note      text,
  legal_checked_at date,
  partner_status  text default 'none',    -- none | applied | approved
  commission_note text,
  is_active       boolean default false,  -- เปิดใช้ได้เมื่อ legal_status เหมาะสม
  sort_order      int default 100
);

insert into institutions
  (code, short_name, full_name, kind, npa_url, legal_status, legal_note, sort_order) values
  ('bam',       'BAM',      'บริษัทบริหารสินทรัพย์ กรุงเทพพาณิชย์', 'amc',
   'https://www.bam.co.th/th/npa', 'unknown',
   'มีโครงการพันธมิตรที่ bam.co.th/th/npa/partner — สมัครแล้วสิทธิ์ชัดขึ้น', 10),
  ('gsb',       'ออมสิน',    'ธนาคารออมสิน', 'bank',
   'https://npa-assets.gsb.or.th/asset/npa/all', 'restricted',
   'เว็บระบุห้ามนำข้อมูลไปใช้ ทำซ้ำ ดัดแปลง โดยมิได้รับอนุญาต — ต้องขอก่อน', 20),
  ('ktb',       'กรุงไทย',   'ธนาคารกรุงไทย', 'bank',
   'https://npa.krungthai.com/', 'unknown', 'ยังไม่ได้ตรวจ ToS', 30),
  ('kbank',     'กสิกรไทย',  'ธนาคารกสิกรไทย', 'bank',
   'https://www.kasikornbank.com/th/PropertyForSale', 'unknown', 'ยังไม่ได้ตรวจ ToS', 40),
  ('krungsri',  'กรุงศรี',   'ธนาคารกรุงศรีอยุธยา', 'bank',
   'https://www.krungsriproperty.com/home', 'unknown', 'ยังไม่ได้ตรวจ ToS', 50),
  ('scb',       'ไทยพาณิชย์', 'ธนาคารไทยพาณิชย์', 'bank',
   'https://www.scb.co.th/th/personal-banking/promotions/loans/npa-broker.html',
   'unknown', 'มีโครงการรับสมัครนายหน้าขายทรัพย์ NPA', 60),
  ('sam',       'SAM',      'บริษัทบริหารสินทรัพย์สุขุมวิท', 'amc',
   null, 'unknown', 'ยังไม่ได้ตรวจ', 70),
  ('ghb',       'ธอส.',     'ธนาคารอาคารสงเคราะห์', 'bank',
   null, 'unknown', 'ยังไม่ได้ตรวจ', 80),
  ('ttb',       'ttb',      'ธนาคารทหารไทยธนชาต', 'bank',
   null, 'unknown', 'ยังไม่ได้ตรวจ', 90),
  ('led',       'กรมบังคับคดี', 'กรมบังคับคดี', 'government',
   'https://asset.led.go.th/newbidreg/', 'restricted',
   'เว็บระบุห้ามนำข้อมูลไปใช้โดยมิได้รับอนุญาต — ดู docs/DATA_PERMISSION.md', 200)
on conflict (code) do nothing;

-- ผูก source กับสถาบัน
alter table sources add column if not exists institution_code text references institutions(code);
update sources set institution_code = 'bam' where code = 'bam' and institution_code is null;
update sources set institution_code = 'led' where code like 'led_%' and institution_code is null;

-- กันไม่ให้เปิด source ที่สถาบันยังไม่เคลียร์เรื่องสิทธิ์
create or replace function guard_source_activation()
returns trigger language plpgsql as $$
declare st text;
begin
  if new.is_active and new.institution_code is not null then
    select legal_status into st from institutions where code = new.institution_code;
    if st in ('unknown', 'restricted', 'prohibited') then
      raise exception
        'เปิด source % ไม่ได้ — สถาบัน % มีสถานะสิทธิ์ "%" ให้ตรวจ ToS หรือขออนุญาตก่อน',
        new.code, new.institution_code, st;
    end if;
  end if;
  return new;
end $$;

drop trigger if exists trg_source_guard on sources;
create trigger trg_source_guard
  before insert or update on sources
  for each row execute function guard_source_activation();

-- ---------------------------------------------------------------------
-- เกรดทรัพย์
--
-- หลักการ 3 ข้อ
--   1. เกรดต้องอธิบายได้ทุกตัว มาจากกฎ ไม่ใช่โมเดล
--   2. ข้อมูลไม่พอ = ไม่ให้เกรด ไม่ใช่ให้เกรดต่ำ
--      (ทรัพย์ที่ข้อมูลน้อยไม่ได้แปลว่าแย่ แค่เรายังไม่รู้)
--   3. flag ระดับ critical กดเพดานเกรดทันที ไม่ให้คะแนนดีด้านอื่นมากลบ
-- ---------------------------------------------------------------------
create table if not exists grade_definitions (
  grade       text primary key,
  label       text not null,
  min_score   numeric not null,
  description text not null
);

insert into grade_definitions (grade, label, min_score, description) values
  ('A', 'น่าสนใจมาก',  80, 'ราคาต่ำกว่าตลาดชัดเจน ไม่มีความเสี่ยงร้ายแรง โซนขายต่อได้'),
  ('B', 'น่าสนใจ',     65, 'มีจุดเด่นชัด แต่มีข้อควรระวังที่จัดการได้'),
  ('C', 'พอได้',       50, 'ราคาสมเหตุสมผล ไม่มีจุดเด่นพิเศษ'),
  ('D', 'ต้องระวัง',   30, 'มีข้อควรระวังหลายข้อ ต้องตรวจสอบละเอียดก่อน'),
  ('E', 'ไม่แนะนำ',     0, 'มีความเสี่ยงร้ายแรง หรือราคาไม่สมเหตุสมผล')
on conflict (grade) do nothing;

create table if not exists property_grades (
  source_code    text not null,
  external_ref   text not null,
  grade          text references grade_definitions(grade),
  score          numeric,
  completeness   numeric,          -- 0-1 สัดส่วนข้อมูลที่มีครบ
  reasons        jsonb not null default '[]'::jsonb,
  computed_at    timestamptz not null default now(),
  model_version  text not null default '0.1.0',
  primary key (source_code, external_ref)
);

-- มุมมองรวมสำหรับหน้าเว็บ — ทรัพย์ + สถาบัน + เกรด
create or replace view v_listings_with_grade as
select distinct on (s.source_code, s.external_ref)
  s.source_code, s.external_ref,
  i.code as institution_code, i.short_name as institution_name, i.kind as institution_kind,
  s.title, s.detail_url, s.property_type,
  s.province, s.district, s.subdistrict, s.lat, s.lng,
  s.land_area_sqwa, s.usable_area_sqm, s.bedrooms, s.bathrooms, s.parking,
  s.opening_price, s.list_price, s.special_price, s.appraised_price, s.renovated,
  s.auction_date, s.auction_round, s.occupancy_note,
  g.grade, g.score, g.completeness, g.reasons,
  s.observed_at
from listing_snapshots s
left join sources src on src.code = s.source_code
left join institutions i on i.code = src.institution_code
left join property_grades g
  on g.source_code = s.source_code and g.external_ref = s.external_ref
where s.auction_date is null or s.auction_date >= current_date
order by s.source_code, s.external_ref, s.observed_at desc;

alter table institutions      enable row level security;
alter table property_grades   enable row level security;
alter table grade_definitions enable row level security;
