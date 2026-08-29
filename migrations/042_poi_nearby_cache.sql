-- =====================================================================
-- Migration 042 — cache ผลวิเคราะห์ "รอบทรัพย์" (POI จาก OpenStreetMap)
--   เก็บผลต่อพิกัด (ปัด ~110m) เพื่อไม่ต้องยิง Overpass ซ้ำ
-- =====================================================================
create table if not exists poi_nearby_cache (
  coord_key   text primary key,               -- 'lat3,lng3' (ปัดทศนิยม 3 ตำแหน่ง)
  data        jsonb not null,                  -- ผลจัดหมวด + นับ 1/3/5km
  fetched_at  timestamptz not null default now()
);
create index if not exists idx_poi_cache_fetched on poi_nearby_cache(fetched_at);
