#!/usr/bin/env python3
"""เติมพิกัดและให้คะแนนทรัพย์ — รันหลัง ingest

    python src/enrich.py geocode     # หาพิกัดอำเภอที่ยังไม่มีใน cache
    python src/enrich.py grade       # คำนวณเกรดทุกรายการ
    python src/enrich.py all         # ทำทั้งสองอย่างตามลำดับ

ทำไมแยกจาก run.py
    geocode ต้องยิงเว็บนอกและช้า (1 request/วินาที ตามกติกาของ Nominatim)
    ส่วน grade คำนวณในฐานล้วน เร็วมาก
    ถ้ารวมกันจะทำให้ ingest ที่ควรเร็วกลายเป็นช้าโดยไม่จำเป็น
"""
from __future__ import annotations

import argparse
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from core import env as _env  # noqa: E402,F401
from core.db import connect  # noqa: E402
from core.gallery import (fetch_bam_detail, fetch_led_detail,  # noqa: E402
                          fetch_sam_detail)
from adapters.ghb import fetch_ghb_detail  # noqa: E402
from core.geocode import geocode_place  # noqa: E402
from core import landsmaps  # noqa: E402
from core.grading import compute  # noqa: E402

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("enrich")

# จำนวนทรัพย์ที่ดึงที่อยู่ต่อรอบเมื่อไม่ระบุ --limit
#
# ตั้งไว้ต่ำโดยตั้งใจ เพราะขั้นนี้ยิงเว็บหนึ่งครั้งต่อทรัพย์ (ราว 2 วินาที)
# ถ้าปล่อยไม่จำกัด งานประจำวันจะกินเวลาเป็นชั่วโมงและรบกวนต้นทางหนัก
#
# ทรัพย์ที่ยังไม่ได้ดึงจะถูกดึงเองตอนมีคนเปิดดูอยู่แล้ว
# คำสั่งนี้แค่เร่งให้ครบเร็วขึ้น จึงทยอยทำได้
DEFAULT_DETAIL_LIMIT = 150


# ---------------------------------------------------------------------
def do_geocode(conn, limit: int | None = None) -> dict:
    """หาพิกัดของคู่ (จังหวัด, อำเภอ) ที่ยังไม่เคยหา"""
    missing = conn.execute(
        """select distinct s.province, s.district, s.subdistrict
             from listing_snapshots s
             left join geo_cache g
               on g.province = s.province
              and coalesce(g.district,'') = coalesce(s.district,'')
              and coalesce(g.subdistrict,'') = coalesce(s.subdistrict,'')
            where s.province is not null and g.id is null
            order by s.province, s.district, s.subdistrict""").fetchall()

    if limit:
        missing = missing[:limit]
    log.info("ต้องหาพิกัด %s โซน (ประมาณ %.0f นาที)", len(missing), len(missing) * 1.2 / 60)

    found = failed = 0
    for row in missing:
        province = row["province"]
        district = row["district"]
        subdistrict = row["subdistrict"]
        result = geocode_place(province, district, subdistrict)
        if not result:
            failed += 1
            log.warning("ไม่พบพิกัด: %s %s %s",
                        subdistrict or "-", district or "-", province)
            continue
        conn.execute(
            """insert into geo_cache
                 (district, subdistrict, province, lat, lng, precision,
                  method, osm_type, display_name)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               on conflict do nothing""",
            (district, subdistrict, province, result["lat"], result["lng"],
             result["precision"], result["method"], result["osm_type"],
             result["display_name"]))
        conn.commit()
        found += 1
        log.info("  %s %s %s -> %.4f, %.4f (%s)",
                 subdistrict or "", district or "-", province,
                 result["lat"], result["lng"], result["precision"])

    applied = conn.execute("select apply_geocache_with_precision() as n").fetchone()["n"]
    conn.commit()
    log.info("เติมพิกัดเข้าทรัพย์ %s แถว", applied)

    # คืนพิกัดแปลงให้ snapshot ล่าสุด — กัน parcel กร่อนตอน ingest ใหม่
    # (ต้องรัน migration 027 ก่อน ถ้ายังไม่มีฟังก์ชัน จะข้ามอย่างปลอดภัย)
    try:
        carried = conn.execute("select carry_parcel_coords() as n").fetchone()["n"]
        conn.commit()
        if carried:
            log.info("คืนพิกัดแปลงให้ snapshot ล่าสุด %s แถว", carried)
    except Exception as exc:                                   # noqa: BLE001
        conn.rollback()
        log.warning("ข้าม carry_parcel_coords (รัน migration 027 ก่อน): %s",
                    str(exc)[:80])

    return {"found": found, "failed": failed, "applied": applied}


# ---------------------------------------------------------------------
def zone_stats(conn) -> dict:
    """สถิติรายโซนสำหรับใช้ให้คะแนน — คำนวณครั้งเดียวแล้วใช้ซ้ำ"""
    # คำนวณค่ากลาง 2 แบบ:
    #   ราคา/ตร.ว. — สำหรับที่ดิน/บ้าน (มีเนื้อที่ดิน)
    #   ราคา/ตร.ม. — สำหรับคอนโด/ห้องชุด (ไม่มีเนื้อที่ดิน)
    # percentile_cont ข้าม null อยู่แล้ว (nullif ทำให้แถวที่ไม่เข้าเกณฑ์เป็น null)
    # ใช้ FILTER ไม่ได้กับ ordered-set aggregate จึงพึ่ง null-skip แทน
    rows = conn.execute(
        """select province, district,
                  percentile_cont(0.5) within group (
                    order by opening_price / nullif(land_area_sqwa,0)) as median_price_sqwa,
                  count(*) filter (where land_area_sqwa > 0) as n_land,
                  percentile_cont(0.5) within group (
                    order by opening_price / nullif(usable_area_sqm,0)) as median_price_sqm,
                  count(*) filter (where usable_area_sqm > 0) as n_sqm
             from listing_snapshots
            where opening_price > 0
              and (land_area_sqwa > 0 or usable_area_sqm > 0)
            group by 1,2
           having count(*) filter (where land_area_sqwa > 0) >= 3
               or count(*) filter (where usable_area_sqm > 0) >= 3""").fetchall()
    return {(r["province"], r["district"]): {
        "median_price_sqwa": (float(r["median_price_sqwa"])
                              if r["median_price_sqwa"] and r["n_land"] >= 3 else None),
        "median_price_sqm": (float(r["median_price_sqm"])
                             if r["median_price_sqm"] and r["n_sqm"] >= 3 else None),
    } for r in rows}


def do_grade(conn) -> dict:
    stats = zone_stats(conn)
    log.info("มีสถิติโซน %s โซน", len(stats))

    rows = conn.execute(
        """select distinct on (source_code, external_ref)
                  source_code, external_ref, province, district, property_type,
                  land_area_sqwa, usable_area_sqm, opening_price, appraised_price,
                  list_price, special_price, renovated, lat, occupancy_note,
                  auction_round
             from listing_snapshots
            order by source_code, external_ref, observed_at desc""").fetchall()

    flags_by_key: dict[tuple, list] = {}
    for f in conn.execute(
            """select p.source_code, p.external_ref, f.rule_code, f.severity, f.evidence
                 from property_flags f
                 join property_links p on p.property_id = f.property_id"""
            ).fetchall() if _has_flags(conn) else []:
        flags_by_key.setdefault((f["source_code"], f["external_ref"]), []).append(
            {"code": f["rule_code"], "severity": f["severity"], "evidence": f["evidence"]})

    graded = skipped = 0
    for r in rows:
        row = dict(r)
        key = (row["source_code"], row["external_ref"])
        result = compute(row, flags_by_key.get(key, []),
                         stats.get((row["province"], row["district"]), {}))
        conn.execute(
            """insert into property_grades
                 (source_code, external_ref, grade, score, completeness,
                  reasons, model_version, computed_at)
               values (%s,%s,%s,%s,%s,%s,%s, now())
               on conflict (source_code, external_ref) do update set
                 grade = excluded.grade, score = excluded.score,
                 completeness = excluded.completeness, reasons = excluded.reasons,
                 model_version = excluded.model_version, computed_at = now()""",
            (*key, result.grade, result.score, round(result.completeness, 2),
             _json(result.reasons), "0.1.0"))
        if result.grade:
            graded += 1
        else:
            skipped += 1
    conn.commit()
    log.info("ให้เกรดได้ %s · ข้อมูลไม่พอ %s", graded, skipped)

    dist = conn.execute(
        """select coalesce(grade,'—') as grade, count(*) as n
             from property_grades group by 1 order by 1""").fetchall()
    for d in dist:
        log.info("  เกรด %s : %s รายการ", d["grade"], d["n"])
    return {"graded": graded, "skipped": skipped}


def _has_flags(conn) -> bool:
    return conn.execute(
        "select to_regclass('public.property_flags') is not null as ok").fetchone()["ok"]


def _json(obj):
    import json
    return json.dumps(obj, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------
def do_details(conn, limit: int | None = None) -> dict:
    """ดึงที่อยู่เต็มจากหน้ารายละเอียด — ทำให้พิกัดแม่นระดับตำบล

    ปกติระบบดึงเองตอนมีคนเปิดดูทรัพย์ คำสั่งนี้ใช้เร่งให้ครบทีเดียว
    ควรจำกัดจำนวนต่อรอบ เพราะยิงเว็บหนึ่งครั้งต่อทรัพย์
    """
    rows = conn.execute(
        """select s.source_code, s.external_ref, s.detail_url, s.listing_id
             from (select distinct on (source_code, external_ref)
                          source_code, external_ref, detail_url,
                          raw_fields->>'listing_id' as listing_id
                     from listing_snapshots
                    order by source_code, external_ref, observed_at desc) s
             left join property_details d
               on d.source_code = s.source_code and d.external_ref = s.external_ref
            where d.source_code is null
              and (s.detail_url is not null or s.listing_id is not null)
            -- สลับแหล่งกัน ไม่งั้นแหล่งที่ชื่อขึ้นก่อนจะกินโควตาทั้งหมด
            -- แล้วแหล่งอื่นไม่ได้ทำเลยเป็นสัปดาห์
            order by row_number() over (partition by s.source_code
                                        order by s.external_ref), s.source_code
            limit %s""", (limit or DEFAULT_DETAIL_LIMIT,)).fetchall()

    remaining = conn.execute(
        """select count(*) as n
             from (select distinct on (source_code, external_ref)
                          source_code, external_ref, detail_url,
                          raw_fields->>'listing_id' as listing_id
                     from listing_snapshots
                    order by source_code, external_ref, observed_at desc) s
             left join property_details d
               on d.source_code = s.source_code and d.external_ref = s.external_ref
            where d.source_code is null
              and (s.detail_url is not null or s.listing_id is not null)""").fetchone()["n"]

    log.info("ดึงรายละเอียดรอบนี้ %s รายการ (ค้างอีก %s · ประมาณ %.0f นาที)",
             len(rows), max(0, remaining - len(rows)), len(rows) * 2 / 60)

    got = coords = 0
    for r in rows:
        src = r["source_code"]

        # SAM ให้พิกัดจริงระดับแปลงมาเลย ดีกว่า geocode ทุกกรณี
        if src == "sam" and r["listing_id"]:
            d = fetch_sam_detail(r["listing_id"])
            if not d:
                continue
            conn.execute(
                """insert into property_details
                     (source_code, external_ref, address_full, street,
                      subdistrict, district)
                   values (%s,%s,%s,%s,%s,%s)
                   on conflict (source_code, external_ref) do update set
                     address_full = excluded.address_full, street = excluded.street,
                     subdistrict = excluded.subdistrict, fetched_at = now()""",
                (src, r["external_ref"], d.get("address_full"), d.get("street"),
                 d.get("subdistrict"), d.get("district")))
            if d.get("lat"):
                conn.execute(
                    """update listing_snapshots
                          set lat = %s, lng = %s, geo_precision = 'parcel'
                        where source_code = %s and external_ref = %s""",
                    (d["lat"], d["lng"], src, r["external_ref"]))
                coords += 1
            conn.commit()
            got += 1
            continue

        if not r["detail_url"]:
            continue
        detail = fetch_bam_detail(r["detail_url"])
        addr = detail.get("address")

        # หน้า BAM ฝังพิกัดจริงมากับลิงก์ Google Maps (q=lat,lng)
        # ถ้าเจอ ให้ใช้เป็นพิกัดระดับแปลง 'parcel' เหมือน SAM
        # ดีกว่า geocode ที่ได้แค่จุดกึ่งกลางตำบล — ทรัพย์จะไม่ซ้อนหมุดกัน
        if detail.get("lat"):
            conn.execute(
                """update listing_snapshots
                      set lat = %s, lng = %s, geo_precision = 'parcel'
                    where source_code = %s and external_ref = %s""",
                (detail["lat"], detail["lng"], src, r["external_ref"]))
            coords += 1

        # ทรัพย์บางตัวมีพิกัดแต่ parse ที่อยู่ไม่ได้ (หรือกลับกัน)
        # เก็บสิ่งที่ได้ อย่าทิ้งทั้งแถวเพราะขาดอย่างใดอย่างหนึ่ง
        if addr:
            conn.execute(
                """insert into property_details
                     (source_code, external_ref, address_full, street,
                      subdistrict, district, province)
                   values (%s,%s,%s,%s,%s,%s,%s)
                   on conflict (source_code, external_ref) do update set
                     address_full = excluded.address_full, street = excluded.street,
                     subdistrict = excluded.subdistrict, fetched_at = now()""",
                (src, r["external_ref"], addr.get("full"), addr.get("street"),
                 addr.get("subdistrict"), addr.get("district"), addr.get("province")))

        if not addr and not detail.get("lat"):
            continue
        conn.commit()
        got += 1

    n = conn.execute("select apply_detail_address() as n").fetchone()["n"]
    conn.commit()
    log.info("ได้รายละเอียด %s รายการ · พิกัดจริง %s · เติมตำบล %s แถว",
             got, coords, n)
    return {"fetched": got, "with_coords": coords, "applied": n}


_BACKFILL_CAND_SQL = """
    select s.external_ref, s.detail_url
      from (select distinct on (source_code, external_ref)
                   external_ref, detail_url, geo_precision
              from listing_snapshots
             where source_code = 'bam'
             order by source_code, external_ref, observed_at desc) s
     where s.detail_url is not null
       and coalesce(s.geo_precision, '') <> 'parcel'
"""


def do_backfill_bam_coords(limit: int | None = None,
                           batch: int = 25) -> dict:
    """เติมพิกัดจริงให้ทรัพย์ BAM เก่าที่ดึง detail ไปก่อนมีฟีเจอร์พิกัด

    ทำไมต้องมีคำสั่งนี้แยก
        do_details() ดึงเฉพาะทรัพย์ที่ "ยังไม่เคยดึง detail" (property_details is null)
        ทรัพย์ BAM เก่าที่เคยดึงไปแล้วจึงถูกข้าม ไม่ได้พิกัดจาก Google Maps รอบใหม่
        คำสั่งนี้ไล่ยิงเฉพาะทรัพย์ BAM ที่ยังไม่ได้พิกัดระดับแปลง (parcel)

    *** ห้ามถือ DB connection ค้างไว้ระหว่างยิงเว็บ ***
        บั๊กที่เคยเจอ: เปิด connection เดียวค้างไว้ทั้งลูป แล้วมัวยิงเว็บ BAM
        ทีละหน้า (ช้า/บางหน้า timeout) connection จึง idle นานจน Supabase
        pooler (pgbouncer) ตัดทิ้ง แล้วคำสั่งถัดไปเจอ
        "server closed the connection unexpectedly"

        ทางแก้: ยิงเว็บโดย "ไม่ถือ" connection ไว้เลย เก็บผลใส่ buffer
        แล้วเปิด connection "สั้น ๆ" เขียนเป็นชุด (batch) ทีละ ~25 รายการ
        connection จึงถูกใช้แค่ตอนเขียนไม่กี่วินาที ไม่มีทางค้าง idle นาน

    รันซ้ำได้เรื่อย ๆ จนหมด เพราะตัวที่ได้ parcel แล้วจะไม่ถูกเลือกอีก
    ถ้าถูกขัดจังหวะกลางคัน เสียอย่างมากแค่ batch สุดท้ายที่ยังไม่ได้เขียน
    """
    limit = limit or DEFAULT_DETAIL_LIMIT

    # อ่านรายการงาน + จำนวนคงเหลือ ด้วย connection สั้น ๆ แล้วปิดทันที
    with connect() as conn:
        rows = conn.execute(_BACKFILL_CAND_SQL + " limit %s", (limit,)).fetchall()
        remaining = conn.execute(
            "select count(*) as n from (" + _BACKFILL_CAND_SQL + ") q").fetchone()["n"]
        rows = [(r["external_ref"], r["detail_url"]) for r in rows]

    log.info("เติมพิกัด BAM รอบนี้ %s รายการ (ค้างอีก %s · ประมาณ %.0f นาที)",
             len(rows), max(0, remaining - len(rows)), len(rows) * 2 / 60)

    coords = missing = 0
    buffer: list[tuple] = []

    def flush() -> None:
        """เขียน buffer ลง DB ด้วย connection ใหม่สั้น ๆ (ไม่ถือค้างระหว่างยิงเว็บ)"""
        nonlocal buffer, coords
        if not buffer:
            return
        with connect() as conn:
            for lat, lng, ref in buffer:
                conn.execute(
                    """update listing_snapshots
                          set lat = %s, lng = %s, geo_precision = 'parcel'
                        where source_code = 'bam' and external_ref = %s""",
                    (lat, lng, ref))
            conn.commit()
        coords += len(buffer)
        log.info("  เขียนพิกัดแล้วรวม %s · เหลือรอบนี้อีก ~%s",
                 coords, max(0, len(rows) - coords - missing))
        buffer = []

    for ref, detail_url in rows:
        # ยิงเว็บตรงนี้ — ไม่มี DB connection เปิดค้างอยู่เลย
        detail = fetch_bam_detail(detail_url)
        if detail.get("lat"):
            buffer.append((detail["lat"], detail["lng"], ref))
        else:
            # หน้านี้ไม่มีลิงก์แผนที่/ดึงไม่ได้ — ปล่อยพิกัดเดิม (ระดับตำบล) ไว้
            missing += 1
        if len(buffer) >= batch:
            flush()
    flush()

    log.info("ได้พิกัดจริงเพิ่ม %s รายการ · ไม่มีลิงก์แผนที่/ดึงไม่ได้ %s", coords, missing)
    return {"with_coords": coords, "no_map": missing, "remaining": remaining}


_GHB_CAND_SQL = """
    select s.external_ref, s.detail_url
      from (select distinct on (source_code, external_ref)
                   external_ref, detail_url, geo_precision
              from listing_snapshots
             where source_code = 'ghb'
             order by source_code, external_ref, observed_at desc) s
     where s.detail_url is not null
       and coalesce(s.geo_precision, '') <> 'parcel'
"""


def do_ghb_coords(limit: int | None = None, batch: int = 25) -> dict:
    """เติมพิกัดจริงให้ทรัพย์ ธอส. จากหน้า detail (Google Maps q=lat,lng)

    ธอส. เก็บพิกัดจริงไว้ในหน้า /property-{ID} อยู่แล้ว — ดีกว่า geocode
    ระดับตำบลมาก (ทรัพย์ไม่ซ้อนหมุด) จึงตั้ง geo_precision = 'parcel'
    เหมือน BAM/SAM  หน้า list ไม่มีพิกัด จึงต้องเข้า detail ทีละตัว

    ใช้แพทเทิร์นเดียวกับ backfill BAM: ไม่ถือ DB connection ระหว่างยิงเว็บ
    เขียนเป็น batch สั้น ๆ กัน Supabase pooler ตัด connection ที่ idle
    รันซ้ำได้จนหมด (ตัวที่ได้ parcel แล้วจะไม่ถูกเลือกอีก)
    """
    limit = limit or DEFAULT_DETAIL_LIMIT

    with connect() as conn:
        rows = conn.execute(_GHB_CAND_SQL + " limit %s", (limit,)).fetchall()
        remaining = conn.execute(
            "select count(*) as n from (" + _GHB_CAND_SQL + ") q").fetchone()["n"]
        rows = [(r["external_ref"], r["detail_url"]) for r in rows]

    log.info("เติมพิกัด ธอส. รอบนี้ %s รายการ (ค้างอีก %s · ประมาณ %.0f นาที)",
             len(rows), max(0, remaining - len(rows)), len(rows) * 2 / 60)

    coords = missing = 0
    buffer: list[tuple] = []

    def flush() -> None:
        nonlocal buffer, coords
        if not buffer:
            return
        with connect() as conn:
            for lat, lng, ref in buffer:
                conn.execute(
                    """update listing_snapshots
                          set lat = %s, lng = %s, geo_precision = 'parcel'
                        where source_code = 'ghb' and external_ref = %s""",
                    (lat, lng, ref))
            conn.commit()
        coords += len(buffer)
        log.info("  เขียนพิกัดแล้วรวม %s · หาไม่เจอ %s", coords, missing)
        buffer = []

    for ref, detail_url in rows:
        d = fetch_ghb_detail(detail_url)          # ยิงเว็บ ไม่ถือ conn
        if d.get("lat"):
            buffer.append((d["lat"], d["lng"], ref))
        else:
            missing += 1
        if len(buffer) >= batch:
            flush()
    flush()

    log.info("ได้พิกัดจริง ธอส. เพิ่ม %s รายการ · ไม่มีลิงก์แผนที่/ดึงไม่ได้ %s",
             coords, missing)
    return {"with_coords": coords, "no_map": missing, "remaining": remaining}


def do_led_details(limit: int | None = None, batch: int = 15) -> dict:
    """ดึงรายละเอียด LED จากหน้า asset_open.asp — เติมรูป + ข้อมูลเชิงลึก

    หน้ารายการ LED ไม่มี URL รูป (เป็น path ภายในเซิร์ฟเวอร์) และไม่มีเนื้อที่
    ห้องชุด/ราคาประเมินจริง/สถานะนัด — ต้องเข้าหน้ารายละเอียดถึงได้ครบ
    เติม: image_url, usable_area_sqm, appraised_price, mortgage_carried

    ใช้ payload ที่เก็บไว้ตอน ingest (_open_post) replay POST — ตัด PII แล้ว
    ไม่ถือ DB connection ระหว่างยิงเว็บ (เขียนเป็น batch) กัน pooler ตัด
    """
    limit = limit or DEFAULT_DETAIL_LIMIT

    with connect() as conn:
        rows = conn.execute(
            """select external_ref, raw_fields
                 from (select distinct on (source_code, external_ref)
                              external_ref, image_url, raw_fields, observed_at
                         from listing_snapshots
                        where source_code = 'led_auction'
                        order by source_code, external_ref, observed_at desc) s
                where s.image_url is null
                  and s.raw_fields ? '_open_post'
                limit %s""", (limit,)).fetchall()
        rows = [(r["external_ref"], (r["raw_fields"] or {}).get("_open_post"))
                for r in rows]

    log.info("ดึงรายละเอียด LED รอบนี้ %s รายการ (~%.0f นาที)",
             len(rows), len(rows) * 5 / 60)

    got = with_img = 0
    buffer: list[tuple] = []

    def flush() -> None:
        nonlocal buffer, got
        if not buffer:
            return
        with connect() as conn:
            for ref, d in buffer:
                # อัปเดตเฉพาะช่องที่ได้ค่ามา (coalesce เก็บค่าเดิมถ้า detail ว่าง)
                conn.execute(
                    """update listing_snapshots set
                         image_url = coalesce(%s, image_url),
                         usable_area_sqm = coalesce(%s, usable_area_sqm),
                         appraised_price = coalesce(%s, appraised_price),
                         mortgage_carried = coalesce(%s, mortgage_carried)
                       where source_code = 'led_auction' and external_ref = %s""",
                    (d.get("image_url"), d.get("usable_area_sqm"),
                     d.get("appraised_price"), d.get("mortgage_carried"), ref))
            conn.commit()
        got += len(buffer)
        buffer = []

    for ref, post in rows:
        if not post:
            continue
        d = fetch_led_detail(post)          # ยิงเว็บ ไม่มี DB conn เปิดค้าง
        if d.get("image_url"):
            with_img += 1
        buffer.append((ref, d))
        if len(buffer) >= batch:
            flush()
            log.info("  เขียนแล้ว %s · มีรูป %s", got, with_img)
    flush()

    log.info("ได้รายละเอียด LED %s รายการ · มีรูป %s", got, with_img)
    return {"fetched": got, "with_image": with_img}


def do_landsmaps(limit: int | None = None, batch: int = 15) -> dict:
    """เติมพิกัดแปลงจริงให้ LED ที่มีเลขโฉนด — ผ่าน LandsMaps กรมที่ดิน

    ใช้ได้เฉพาะทรัพย์ที่มี deed_no (ที่ดิน/บ้าน+ที่ดิน/คอนโดที่มีโฉนดที่ดิน)
    ทรัพย์ไม่มีโฉนดจะข้ามไป (ใช้พิกัดตำบลจาก geocode ต่อ)

    ยิงเว็บกรมที่ดิน (หน่วง 1.6 วิ/คำขอ) — ไม่ถือ DB connection ระหว่างยิงเว็บ
    เขียนเป็น batch เหมือน backfill กัน pooler ตัด
    """
    limit = limit or DEFAULT_DETAIL_LIMIT

    with connect() as conn:
        rows = conn.execute(
            """select external_ref, province, district,
                      raw_fields->>'deed_no' as deed_no
                 from (select distinct on (source_code, external_ref)
                              external_ref, province, district,
                              geo_precision, raw_fields, observed_at
                         from listing_snapshots
                        where source_code = 'led_auction'
                        order by source_code, external_ref, observed_at desc) s
                where s.province is not null and s.district is not null
                  and coalesce(s.geo_precision, '') <> 'parcel'
                  and s.raw_fields->>'deed_no' is not null
                  and s.raw_fields->>'deed_no' not in ('-', '0', '')
                limit %s""", (limit,)).fetchall()
        rows = [(r["external_ref"], r["province"], r["district"], r["deed_no"])
                for r in rows]

    log.info("ค้นพิกัดแปลง LandsMaps รอบนี้ %s รายการ (~%.0f นาที)",
             len(rows), len(rows) * 3.5 / 60)

    coords = missing = 0
    buffer: list[tuple] = []

    def flush() -> None:
        nonlocal buffer, coords
        if not buffer:
            return
        with connect() as conn:
            for ref, d in buffer:
                conn.execute(
                    """update listing_snapshots
                          set lat = %s, lng = %s, geo_precision = 'parcel'
                        where source_code = 'led_auction' and external_ref = %s""",
                    (d["lat"], d["lng"], ref))
            conn.commit()
        coords += len(buffer)
        log.info("  เขียนพิกัดแปลงแล้ว %s · หาไม่เจอ %s", coords, missing)
        buffer = []

    for ref, prov, dist, deed in rows:
        d = landsmaps.parcel_coords(prov, dist, deed)     # ยิงเว็บ ไม่ถือ conn
        if d and d.get("lat"):
            buffer.append((ref, d))
        else:
            missing += 1
        if len(buffer) >= batch:
            flush()
    flush()

    log.info("ได้พิกัดแปลงจริง %s รายการ · ไม่มีในระบบโฉนด/ค้นไม่เจอ %s", coords, missing)
    return {"with_parcel": coords, "not_found": missing}


def do_infra() -> dict:
    """เปิดใช้เครื่องยนต์ทำเล/เวนคืน (Phase B)

    refresh links -> refresh_infra_features() (PostGIS คำนวณระยะ)
    -> rules_infra.evaluate_infra -> เขียน property_flags -> เข้าเกรด

    ต้องมีข้อมูลใน infra_projects ก่อน (โหลดด้วย tools/load_infra.py)
    ถ้าตาราง/ข้อมูลยังไม่พร้อม จะข้ามอย่างปลอดภัย (ไม่ทำ ingest/enrich อื่นพัง)
    """
    from core import rules_infra

    with connect() as conn:
        ready = conn.execute(
            "select to_regclass('public.property_flags') is not null "
            "and to_regclass('public.infra_projects') is not null "
            "and to_regclass('public.property_infra_features') is not null as ok"
        ).fetchone()["ok"]
        if not ready:
            log.warning("ยังไม่มีตาราง infra (รัน migration 002 + 030 ก่อน) — ข้าม")
            return {"skipped": "no_tables"}

        n_proj = conn.execute("select count(*) as n from infra_projects "
                              "where geom is not null").fetchone()["n"]
        if not n_proj:
            log.warning("infra_projects ว่าง — โหลดข้อมูลด้วย tools/load_infra.py ก่อน")
            return {"infra_projects": 0}

        nl = conn.execute("select refresh_property_links() as n").fetchone()["n"]
        conn.commit()
        try:
            conn.execute("set statement_timeout = '300000'")   # 5 นาที กันงานหนักถูกตัด
            nf = conn.execute("select refresh_infra_features() as n").fetchone()["n"]
            conn.commit()
        except Exception as exc:                                   # noqa: BLE001
            conn.rollback()
            log.warning("refresh_infra_features ล้มเหลว (PostGIS ติดตั้งครบไหม?): %s",
                        str(exc)[:120])
            return {"error": str(exc)[:120]}

        rows = conn.execute("""
            select f.property_id,
                   f.nearest_station_m, f.nearest_station_name,
                   cl.weight as station_certainty_weight,
                   f.nearest_new_road_m, f.nearest_new_road_name,
                   f.in_expropriation_corridor, f.expropriation_project
              from property_infra_features f
              left join certainty_levels cl on cl.level = f.nearest_station_certainty
        """).fetchall()

    # ประเมิน flag ด้วย Python (ไม่ถือ conn ระหว่างวน)
    computed_ids: list = []
    upserts: list[tuple] = []
    for r in rows:
        feat = dict(r)
        # numeric ของ psycopg เป็น Decimal — rules_infra คูณกับ float
        # ต้องแปลงเป็น float ก่อน ไม่งั้น TypeError (float * Decimal)
        w = r["station_certainty_weight"]
        w = float(w) if w is not None else 0.15
        feat["certainty_weight"] = w
        feat["station_certainty_weight"] = w
        for k in ("nearest_station_m", "nearest_new_road_m"):
            if feat.get(k) is not None:
                feat[k] = float(feat[k])
        computed_ids.append(r["property_id"])
        for fl in rules_infra.evaluate_infra(feat):
            upserts.append((r["property_id"], fl.code, fl.severity,
                            round(fl.score, 2), fl.evidence))

    # เขียนใหม่: ลบ flag เดิมของทรัพย์ที่คำนวณ แล้วใส่ชุดปัจจุบัน
    with connect() as conn:
        for i in range(0, len(computed_ids), 500):
            conn.execute("delete from property_flags where property_id = any(%s)",
                         (computed_ids[i:i + 500],))
        conn.commit()
        # ลบ flag เดิมของทรัพย์ที่คำนวณไปแล้วด้านบน จึง insert ตรง ๆ ได้
        # (ไม่พึ่ง unique constraint (property_id, rule_code) ของตารางเดิม)
        for row in upserts:
            conn.execute(
                """insert into property_flags
                     (property_id, rule_code, severity, score, evidence)
                   values (%s,%s,%s,%s,%s)""", row)
        conn.commit()

    log.info("infra: ผูกทรัพย์ +%s · คำนวณฟีเจอร์ %s · flag %s (จาก %s ทรัพย์ใกล้โครงการ)",
             nl, nf, len(upserts), len(computed_ids))
    return {"links_new": nl, "features": nf, "flags": len(upserts)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command",
                    choices=["geocode", "grade", "details", "all",
                             "backfill-coords", "ghb-coords",
                             "led-details", "landsmaps", "infra"])
    ap.add_argument("--limit", type=int,
                    help="จำกัดจำนวนต่อรอบ (โซนที่ geocode / ทรัพย์ที่ดึงที่อยู่)")
    ap.add_argument("--detail-limit", type=int,
                    help=f"จำนวนทรัพย์ที่ดึงที่อยู่ต่อรอบ (ค่าเริ่มต้น {DEFAULT_DETAIL_LIMIT})")
    ap.add_argument("--skip-details", action="store_true",
                    help="ข้ามการดึงที่อยู่ ใช้เมื่ออยากให้รอบนั้นเร็ว")
    args = ap.parse_args()

    # backfill จัดการ connection เองเป็นชุดสั้น ๆ (ยิงเว็บนานห้ามถือ conn ค้าง)
    if args.command == "backfill-coords":
        do_backfill_bam_coords(args.detail_limit or args.limit)
        return 0
    if args.command == "ghb-coords":
        do_ghb_coords(args.detail_limit or args.limit)
        return 0
    if args.command == "led-details":
        do_led_details(args.detail_limit or args.limit)
        return 0
    if args.command == "landsmaps":
        do_landsmaps(args.detail_limit or args.limit)
        return 0
    if args.command == "infra":
        do_infra()
        return 0

    # ghb: พิกัดจริงอยู่หน้า detail — เติมก่อน geocode (ตั้ง parcel กัน geocode ทับ)
    # จัดการ connection เองเป็นชุดสั้น ๆ จึงเรียกนอก with ด้านล่าง
    if args.command == "all":
        do_ghb_coords(args.detail_limit or args.limit)

    with connect() as conn:
        if args.command == "details":
            do_details(conn, args.detail_limit or args.limit)
        elif args.command == "all" and not args.skip_details:
            do_details(conn, args.detail_limit or args.limit)
        if args.command in ("geocode", "all"):
            do_geocode(conn, args.limit)
        if args.command == "all":
            # ทำเล/เวนคืน: คำนวณ flag ก่อน grade เพื่อให้เกรดใช้ flag ได้
            # (ข้ามเองถ้ายังไม่มีข้อมูล infra — ไม่ทำ all พัง)
            do_infra()
        if args.command in ("grade", "all"):
            do_grade(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
