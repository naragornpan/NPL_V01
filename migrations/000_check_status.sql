-- =====================================================================
-- ตรวจสถานะ migration — รันไฟล์นี้เมื่อไหร่ก็ได้ ไม่แก้อะไรทั้งสิ้น
-- copy ผลลัพธ์ส่งมาได้เลยถ้าติดปัญหา
-- =====================================================================

select
  m.step,
  m.file,
  case when count(t.table_name) = m.expect then 'ครบ'
       when count(t.table_name) = 0 then 'ยังไม่ได้รัน'
       else 'ไม่ครบ (' || count(t.table_name) || '/' || m.expect || ')'
  end as status,
  string_agg(
    case when t.table_name is null then null else t.table_name end, ', '
    order by t.table_name) as found
from (values
  (0, 'schema.sql',        4, array['sources','raw_documents','listing_snapshots','ingest_runs']),
  (2, '002_infra_and_price',3, array['infra_projects','land_price_zones','property_infra_features']),
  (3, '003_appreciation',  3, array['uplift_observations','property_forecasts','forecast_outcomes']),
  (4, '004_model_lifecycle',3,array['model_versions','uplift_curve_snapshots','forecast_evaluations']),
  (5, '005_price_sources', 4, array['price_tiers','price_observations','auction_market_spread','resale_tracking']),
  (6, '006_images',        2, array['image_sources','listing_images']),
  (7, '007_market_comps',  3, array['comp_sources','market_comps','asking_haircut']),
  (8, '008_contractors',   4, array['contractors','specialties','job_requests','contractor_quotes']),
  (9, '009_leads_consent', 4, array['leads','lead_requirements','lead_consents','loan_referrals']),
  (10,'010_analytics',     2, array['page_events','daily_rollup']),
  (11,'011_institutions',  3, array['institutions','property_grades','grade_definitions'])
) as m(step, file, expect, tables)
left join information_schema.tables t
  on t.table_schema = 'public' and t.table_name = any(m.tables)
group by m.step, m.file, m.expect
order by m.step;

-- ตรวจว่า postgis ติดตั้งอยู่ที่ schema ไหน (สาเหตุที่ 002 พังบ่อยที่สุด)
select
  e.extname                         as extension,
  n.nspname                         as installed_schema,
  case when n.nspname = any(current_schemas(true))
       then 'ใช้งานได้' else 'อยู่นอก search_path — ต้องแก้' end as usable
from pg_extension e
join pg_namespace n on n.oid = e.extnamespace
where e.extname in ('postgis', 'pgcrypto');

-- ถ้าไม่มีแถวไหนเลย แปลว่ายังไม่ได้ติดตั้ง extension
