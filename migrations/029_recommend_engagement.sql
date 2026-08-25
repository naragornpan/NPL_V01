-- =====================================================================
-- Migration 029 — เติม "ความสนใจจริง" (engagement) เข้า recommend_score
--
-- ต่อจาก 028: เดิม recommend_score = คุณภาพ + พิกัดจริง + รูป + มาใหม่
-- เพิ่ม: โบนัสจากพฤติกรรมจริง 30 วัน (ทัก/บันทึก/คลิกต้นทาง/ผู้ชม)
--   -> ทรัพย์ที่คน "สนใจจริง" จะลอยขึ้น "ทรัพย์แนะนำ" ด้วย ไม่ใช่แค่เกรด
--
-- ดึงจาก daily_rollup โดยตรง (เบากว่า v_hot_properties เพราะไม่มี lateral)
-- โบนัส cap ที่ +12 กันทรัพย์ที่มี inquiry ครั้งเดียวพุ่งเกินจริง
-- =====================================================================

create or replace view v_recommended as
with first_seen as (
  select source_code, external_ref, min(observed_at) as first_seen
    from listing_snapshots
   group by source_code, external_ref
),
engagement as (
  select key1 as source_code, key2 as external_ref,
         sum(inquiries) * 25 + sum(saves) * 8
           + sum(source_clicks) * 3 + sum(unique_sessions) as interest
    from daily_rollup
   where dimension = 'property' and day >= current_date - 30
   group by key1, key2
)
select g.*,
       f.first_seen,
       (f.first_seen >= now() - interval '7 days') as is_fresh,
       coalesce(e.interest, 0) as interest_30d,
       round((
           coalesce(g.score, 35)
           + case when g.geo_precision = 'parcel' then 6 else 0 end
           + case when g.image_url is not null   then 3 else 0 end
           + case when f.first_seen >= now() - interval '7 days'  then 8
                  when f.first_seen >= now() - interval '30 days' then 4
                  else 0 end
           + least(12, coalesce(e.interest, 0) * 0.3)   -- ความสนใจจริง
       )::numeric, 1) as recommend_score
  from v_listings_with_grade g
  left join first_seen f
    on f.source_code = g.source_code and f.external_ref = g.external_ref
  left join engagement e
    on e.source_code = g.source_code and e.external_ref = g.external_ref;

-- ดูท็อป 10 ใหม่ (ควรเห็นทรัพย์ที่คนสนใจจริงขยับขึ้น)
select source_code, external_ref, grade, score, interest_30d, recommend_score
  from v_recommended
 where grade in ('A','B')
 order by recommend_score desc nulls last
 limit 10;
