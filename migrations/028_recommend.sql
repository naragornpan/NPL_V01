-- =====================================================================
-- Migration 028 — recommend_score + section "ทรัพย์แนะนำ"
--
-- คะแนนแนะนำ = คุณภาพ (เกรด/score ซึ่งรวมส่วนลด+ความเสี่ยงแล้ว)
--   + โบนัส "ผิว" ที่เกรดไม่ได้มอง: พิกัดแปลงจริง, มีรูป, ของมาใหม่
-- ทรัพย์เสี่ยง (flag critical) เกรดเป็น E -> score ต่ำ -> ไม่ลอยขึ้นเอง
--   (กติกา: ทรัพย์เสี่ยงห้ามขึ้น "แนะนำ" — ผูกกับเกรดที่มีเพดานอยู่แล้ว)
--
-- first_seen = ครั้งแรกที่เห็น ref นี้ (ใช้ทำ badge "มาใหม่" + freshness)
-- =====================================================================

create or replace view v_recommended as
with first_seen as (
  select source_code, external_ref, min(observed_at) as first_seen
    from listing_snapshots
   group by source_code, external_ref
)
select g.*,
       f.first_seen,
       (f.first_seen >= now() - interval '7 days') as is_fresh,
       round((
           coalesce(g.score, 35)
           + case when g.geo_precision = 'parcel' then 6 else 0 end
           + case when g.image_url is not null   then 3 else 0 end
           + case when f.first_seen >= now() - interval '7 days'  then 8
                  when f.first_seen >= now() - interval '30 days' then 4
                  else 0 end
       )::numeric, 1) as recommend_score
  from v_listings_with_grade g
  left join first_seen f
    on f.source_code = g.source_code and f.external_ref = g.external_ref;

-- ตรวจผลเร็ว ๆ: 10 อันดับแรกที่จะขึ้น "ทรัพย์แนะนำ"
select source_code, external_ref, grade, score, geo_precision,
       is_fresh, recommend_score
  from v_recommended
 where grade in ('A','B')
 order by recommend_score desc nulls last
 limit 10;
