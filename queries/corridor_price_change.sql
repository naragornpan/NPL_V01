-- =====================================================================
-- ราคาในแนวถนน/สถานี เปลี่ยนแปลงอย่างไร
--
-- วิธี: difference-in-differences
--   uplift จริง = (ราคาโซนใกล้เปลี่ยนไปกี่ %) − (ราคาโซนไกลเปลี่ยนไปกี่ %)
--
-- ทำไมต้องมีโซนควบคุม:
--   ถ้าราคาแถวสถานีขึ้น 25% แต่ทั้งจังหวัดขึ้น 20% อยู่แล้ว
--   ผลของสถานีคือ 5% ไม่ใช่ 25% — การไม่หักโซนควบคุมคือความผิดพลาด
--   ที่ทำให้คนซื้อแพงเกินจริงมากที่สุด
--
-- ใช้: แทน :project_id และ :event_date ก่อนรัน
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. ราคาต่อตารางวารายแถบระยะ ก่อน/หลังเหตุการณ์
-- ---------------------------------------------------------------------
with proj as (
  select id, name, geom, first_announced_at
  from infra_projects
  where id = :project_id
),
deals as (
  select
    s.external_ref,
    s.sold_date,
    s.sold_price,
    s.land_area_sqwa,
    s.sold_price / nullif(s.land_area_sqwa, 0) as price_per_sqwa,
    st_distance(
      st_setsrid(st_makepoint(s.lng, s.lat), 4326)::geography,
      (select geom from proj)::geography
    ) as dist_m
  from listing_snapshots s
  where s.source_code = 'led_result'
    and s.sold is true
    and s.sold_price > 0
    and s.land_area_sqwa > 0
    and s.lat is not null
),
banded as (
  select *,
    case
      when dist_m <= 500  then '0-500'
      when dist_m <= 1000 then '500-1000'
      when dist_m <= 2000 then '1000-2000'
      when dist_m <= 8000 then 'control'      -- แถบควบคุม
      else 'far'
    end as band,
    case when sold_date < :event_date then 'before' else 'after' end as period
  from deals
  where dist_m <= 8000
)
select
  band,
  period,
  count(*)                                                              as n_deals,
  round(percentile_cont(0.5) within group (order by price_per_sqwa)::numeric) as median_price_sqwa,
  round(percentile_cont(0.25) within group (order by price_per_sqwa)::numeric) as p25,
  round(percentile_cont(0.75) within group (order by price_per_sqwa)::numeric) as p75
from banded
where band <> 'far'
group by band, period
order by
  case band when '0-500' then 1 when '500-1000' then 2
            when '1000-2000' then 3 else 4 end,
  period desc;

-- ---------------------------------------------------------------------
-- 2. คำนวณ net uplift หลังหักโซนควบคุมแล้ว  ← ตัวเลขที่เอาไปใช้จริง
-- ---------------------------------------------------------------------
with changes as (
  -- (ใช้ผลจากบล็อกบน แล้ว pivot before/after)
  select band,
         max(median_price_sqwa) filter (where period = 'before') as before_p,
         max(median_price_sqwa) filter (where period = 'after')  as after_p,
         sum(n_deals)                                            as n_deals
  from (
    -- วาง query ที่ 1 ซ้ำตรงนี้ หรือทำเป็น materialized view
    select null::text as band, null::text as period,
           null::int as n_deals, null::numeric as median_price_sqwa
  ) x
  group by band
)
select
  band,
  n_deals,
  round((after_p / nullif(before_p, 0) - 1) * 100, 1) as raw_change_pct,
  round(
    ((after_p / nullif(before_p, 0))
     / nullif((select after_p / nullif(before_p, 0) from changes where band = 'control'), 0)
     - 1) * 100, 1
  ) as net_uplift_pct
from changes
where band <> 'control'
order by band;

-- ---------------------------------------------------------------------
-- 3. เทียบกับราคาประเมินราชการรายโซน (แหล่งที่สองสำหรับตรวจสอบไขว้)
--    ถ้าสองแหล่งชี้ทางเดียวกัน ความเชื่อมั่นสูงขึ้นมาก
-- ---------------------------------------------------------------------
select
  v.province, v.district, v.subdistrict,
  v.price_prev, v.price_cur, v.change_pct,
  round(st_distance(
    lpz.geom::geography,
    (select geom from infra_projects where id = :project_id)::geography
  )) as dist_m
from v_land_price_change v
join land_price_zones lpz
  on lpz.province = v.province
 and coalesce(lpz.district, '') = coalesce(v.district, '')
 and coalesce(lpz.subdistrict, '') = coalesce(v.subdistrict, '')
where st_dwithin(
        lpz.geom::geography,
        (select geom from infra_projects where id = :project_id)::geography,
        3000)
order by dist_m;

-- ---------------------------------------------------------------------
-- ข้อควรระวังตอนอ่านผล
--
-- 1. n_deals ต่ำกว่า 10 ต่อแถบ อย่าเชื่อ median — รายงานเป็นช่วงแทน
-- 2. ถ้า net_uplift ของแถบ 0-500 ต่ำกว่าแถบ 500-1000 มักแปลว่า
--    ทรัพย์ติดแนวถนนโดนผลลบ (เสียง ฝุ่น เวนคืนบางส่วน) ไม่ใช่ข้อมูลผิด
-- 3. ช่วง before ต้องยาวพอ ๆ กับ after ไม่งั้นเทียบไม่ได้
-- 4. โครงการที่ถูกยกเลิกก็ต้องคำนวณด้วย ไม่งั้น uplift จะสูงเกินจริงทุกครั้ง
-- ---------------------------------------------------------------------
