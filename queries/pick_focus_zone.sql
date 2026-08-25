-- =====================================================================
-- เลือก "เขตโฟกัส" จากข้อมูลจริง — รันหลังเก็บข้อมูลอย่างน้อย 4 สัปดาห์
--
-- อย่าเลือกเขตด้วยความรู้สึกหรือเพราะบ้านอยู่ใกล้
-- เขตที่ดีต้องผ่านทั้ง 3 เกณฑ์ ไม่ใช่แค่ข้อเดียว
-- =====================================================================

-- ---------------------------------------------------------------------
-- เกณฑ์ 1: ปริมาณ — มีของให้ทำงานพอไหม
-- เขตที่มีทรัพย์ใหม่น้อยกว่า ~5 รายการ/เดือน จะไม่มีดีลให้ปิดพอเลี้ยงตัว
-- ---------------------------------------------------------------------
select
  province,
  district,
  count(distinct external_ref)                        as listings,
  count(distinct external_ref) filter
    (where auction_date >= current_date)              as upcoming,
  round(count(distinct external_ref)::numeric
        / greatest(1, extract(day from now()
          - min(observed_at))::numeric / 30), 1)      as per_month
from listing_snapshots
where source_code = 'led_auction'
group by 1, 2
having count(distinct external_ref) >= 5
order by per_month desc
limit 30;

-- ---------------------------------------------------------------------
-- เกณฑ์ 2: ส่วนลด — ราคาลดลงแรงแค่ไหนเมื่อนัดขายซ้ำ
-- เขตที่ราคาไม่ลดเลยแม้นัดหลายรอบ = ผู้ขายไม่ยืดหยุ่น ทำกำไรยาก
-- ---------------------------------------------------------------------
with rounds as (
  select
    province, district, external_ref,
    min(opening_price) filter (where auction_round = 1) as price_r1,
    min(opening_price)                                  as price_latest,
    max(auction_round)                                  as max_round
  from listing_snapshots
  where source_code = 'led_auction' and opening_price > 0
  group by 1, 2, 3
)
select
  province, district,
  count(*)                                              as tracked,
  round(avg(max_round), 1)                              as avg_rounds,
  round(avg(1 - price_latest / nullif(price_r1, 0)) * 100, 1) as avg_discount_pct
from rounds
where price_r1 is not null and max_round > 1
group by 1, 2
having count(*) >= 3
order by avg_discount_pct desc
limit 30;

-- ---------------------------------------------------------------------
-- เกณฑ์ 3: สภาพคล่อง — ขายจบจริงกี่ % (ต้องมี adapter led_result ก่อน)
-- เขตที่ประกาศเยอะแต่ขายไม่จบ = ซื้อไปแล้วออกยาก อันตรายที่สุด
-- ---------------------------------------------------------------------
select
  province, district,
  count(*)                                              as total,
  count(*) filter (where sold)                          as sold_count,
  round(100.0 * count(*) filter (where sold) / count(*), 1) as sell_through_pct,
  round(avg(sold_price) filter (where sold))            as avg_sold_price
from listing_snapshots
where source_code = 'led_result'
group by 1, 2
having count(*) >= 5
order by sell_through_pct desc
limit 30;

-- ---------------------------------------------------------------------
-- สรุป: เขตที่ควรโฟกัสคือเขตที่ติด top 15 ของทั้งสามตาราง
-- เลือกมา 2-3 เขต แล้วบันทึกใน PROJECT_BRIEF ส่วนที่ 15 Decision Log
--
-- ข้อควรระวัง: เกณฑ์ 2 สูงผิดปกติอาจไม่ใช่โอกาส แต่แปลว่าทรัพย์มีปัญหา
-- ที่คนในพื้นที่รู้กันแต่เราไม่รู้ ให้เอาไปตรวจกับเกณฑ์ 3 เสมอ
-- ---------------------------------------------------------------------
