-- 049_appraisal_bump.sql — Flag "ราคาประเมินขึ้น" ของทรัพย์ LED
-- รัน: psql "<DATABASE_URL>" -f migrations/049_appraisal_bump.sql
--
-- เทียบราคาประเมิน (appraised_price = ค่ามากสุดของ assetprice ต่อ snapshot)
-- ระหว่าง snapshot "แรกสุด" กับ "ล่าสุด" ของแต่ละทรัพย์ — ถ้าล่าสุด > แรกสุด
-- = ราคาประเมินถูกปรับขึ้น (คณะกรรมการ/เจ้าพนักงานประเมินใหม่) ใช้เป็นตัวกรอง/flag

-- ค้นประวัติราคาต่อทรัพย์ให้เร็ว
create index if not exists idx_ls_led_ref_obs
  on listing_snapshots (external_ref, observed_at)
  where source_code = 'led_auction';

create or replace view v_appraisal_bump as
with a as (
  select external_ref,
         first_value(appraised_price) over (partition by external_ref order by observed_at asc)  as first_appr,
         first_value(appraised_price) over (partition by external_ref order by observed_at desc) as last_appr,
         row_number()                 over (partition by external_ref order by observed_at desc) as rn
  from listing_snapshots
  where source_code = 'led_auction'
    and appraised_price is not null and appraised_price > 0
)
select external_ref,
       first_appr,
       last_appr,
       (last_appr - first_appr)                                  as appr_delta,
       round(((last_appr - first_appr) / first_appr) * 100.0, 1) as appr_up_pct
from a
where rn = 1
  and last_appr > first_appr;
