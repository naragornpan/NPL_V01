-- =====================================================================
-- Migration 043 — ผลการขายทอดตลาด LED (ราคาจบประมูล)
--   ดึงจาก asset.led.go.th/report/report.asp (รายงานผลการขาย ย้อนหลัง 6 เดือน)
--   จับคู่กับทรัพย์ที่เราเก็บก่อนประมูลด้วย (office_id, sale_date, ราคาประเมิน)
-- =====================================================================
create table if not exists led_auction_results (
  office_id        text        not null,   -- PROVINCE_ID (สำนักงานบังคับคดี/กอง) = _open_post.province_id
  sale_date        date        not null,   -- วันที่ขาย (แปลงจาก พ.ศ. แล้ว)
  row_key          text        not null,   -- คีย์ในหน้ารายงาน = case_no|appraised (กันซ้ำตอน upsert)
  case_no          text,                   -- เลขคดีแดง เช่น ผบE.2657/2565
  seq              text,                   -- ลำดับที่ เช่น "2 - 1"
  court            text,                   -- ศาล
  deed             text,                   -- ที่ดินโฉนด (อาจหลายเลข)
  plaintiff        text,                   -- โจทก์
  property_type_th text,                   -- ประเภททรัพย์ (ไทย)
  appraised_price  numeric,                -- ราคาประเมิน (คีย์จับคู่)
  result           text,                   -- ผลการขาย: ขายได้ / งดขายไม่มีผู้สู้ราคา / ถอน ฯลฯ
  sold_price       numeric,                -- ราคาขายได้/เสนอสูงสุด (0 = ไม่มีผู้สู้ราคา)
  is_sold          boolean,                -- true เมื่อผล = ขายได้
  matched_ref      text,                   -- external_ref ของทรัพย์เราที่จับคู่ได้ (nullable)
  fetched_at       timestamptz not null default now(),
  primary key (office_id, sale_date, row_key)
);

create index if not exists idx_led_res_saledate on led_auction_results (sale_date desc);
create index if not exists idx_led_res_matched  on led_auction_results (matched_ref) where matched_ref is not null;
create index if not exists idx_led_res_appr      on led_auction_results (office_id, sale_date, appraised_price);
create index if not exists idx_led_res_sold      on led_auction_results (is_sold);

-- บันทึกว่าดึงผลของ (office, วันขาย) ไปแล้ว เพื่อไม่ยิงซ้ำโดยไม่จำเป็น
create table if not exists led_result_fetchlog (
  office_id   text not null,
  sale_date   date not null,
  rows_found  int,
  fetched_at  timestamptz not null default now(),
  primary key (office_id, sale_date)
);
