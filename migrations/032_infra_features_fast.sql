-- =====================================================================
-- Migration 032 — refresh_infra_features() เวอร์ชันเร็ว (กัน statement timeout)
--
-- ของเดิม (002) ช้าจน timeout เมื่อทรัพย์เยอะ (~11k) เพราะ
--   1. subquery station เรียก KNN 3 รอบ (dist/name/certainty แยกกัน) — เปลี่ยน
--      เป็น LATERAL หา "นัดที่ใกล้สุด" ครั้งเดียวได้ครบทุกฟิลด์
--   2. order by p.g(geography) <-> ip.geom(geometry) ไม่ใช้ GiST index —
--      เปลี่ยนเป็น p.gm(geometry) <-> ip.geom(geometry) ใช้ idx_infra_geom
--   3. ตั้ง statement_timeout ระดับฟังก์ชัน (5 นาที) กันถูกตัดกลางคัน
--
-- โครงข้อมูล/ผลลัพธ์เหมือนเดิมทุกอย่าง แค่เร็วขึ้นมาก
-- =====================================================================

create or replace function refresh_infra_features()
returns int
language plpgsql
set search_path = public, extensions
set statement_timeout = '300000'
as $$
declare affected int;
begin
  with latest as (
    select distinct on (pl.property_id)
           pl.property_id, s.lat, s.lng, s.province, s.district, s.subdistrict
    from property_links pl
    join listing_snapshots s
      on s.source_code = pl.source_code and s.external_ref = pl.external_ref
    where s.lat is not null and s.lng is not null
    order by pl.property_id, s.observed_at desc
  ),
  pt as (
    select property_id, province, district, subdistrict,
           st_setsrid(st_makepoint(lng, lat), 4326)              as gm,   -- geometry (ใช้ index)
           st_setsrid(st_makepoint(lng, lat), 4326)::geography   as g     -- geography (วัดเมตร)
    from latest
  ),
  station as (
    select p.property_id, s.dist, s.name, s.certainty
    from pt p
    left join lateral (
      select st_distance(p.g, ip.geom::geography) as dist,
             ip.name, ip.certainty_level as certainty
        from infra_projects ip
       where ip.project_type in ('station', 'rail')
       order by p.gm <-> ip.geom
       limit 1
    ) s on true
  ),
  road as (
    select p.property_id, r.dist, r.name
    from pt p
    left join lateral (
      select st_distance(p.g, ip.geom::geography) as dist, ip.name
        from infra_projects ip
       where ip.project_type in ('road', 'expressway')
       order by p.gm <-> ip.geom
       limit 1
    ) r on true
  ),
  corridor as (
    select p.property_id,
           c.name is not null as inside,
           c.name as proj
    from pt p
    left join lateral (
      select ip.name
        from infra_projects ip
       where ip.certainty_level >= 3
         and st_dwithin(p.g, ip.geom::geography, ip.corridor_m)
       limit 1
    ) c on true
  )
  insert into property_infra_features as f
    (property_id, nearest_station_m, nearest_station_name, nearest_station_certainty,
     nearest_new_road_m, nearest_new_road_name,
     in_expropriation_corridor, expropriation_project, gov_price_change_pct, computed_at)
  select p.property_id, s.dist, s.name, s.certainty, r.dist, r.name,
         coalesce(c.inside, false), c.proj, v.change_pct, now()
  from pt p
  left join station s using (property_id)
  left join road r using (property_id)
  left join corridor c using (property_id)
  left join v_land_price_change v
    on v.province = p.province
   and coalesce(v.district, '') = coalesce(p.district, '')
   and coalesce(v.subdistrict, '') = coalesce(p.subdistrict, '')
  on conflict (property_id) do update set
    nearest_station_m = excluded.nearest_station_m,
    nearest_station_name = excluded.nearest_station_name,
    nearest_station_certainty = excluded.nearest_station_certainty,
    nearest_new_road_m = excluded.nearest_new_road_m,
    nearest_new_road_name = excluded.nearest_new_road_name,
    in_expropriation_corridor = excluded.in_expropriation_corridor,
    expropriation_project = excluded.expropriation_project,
    gov_price_change_pct = excluded.gov_price_change_pct,
    computed_at = now();

  get diagnostics affected = row_count;
  return affected;
end $$;
