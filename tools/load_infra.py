#!/usr/bin/env python3
"""โหลดข้อมูลโครงสร้างพื้นฐานจากไฟล์ GeoJSON เข้า infra_projects

ใช้กับ: สถานีรถไฟฟ้า, แนวเส้นทางราง, ทางด่วน/มอเตอร์เวย์, แนวเวนคืน (พ.ร.ฎ.)

ตัวอย่าง
    # สถานีรถไฟฟ้าที่เปิดใช้แล้ว (certainty 5) — จุด ไม่มี corridor
    python tools/load_infra.py transit_stations.geojson --type station --certainty 5

    # แนวเส้นทางรถไฟฟ้ากำลังก่อสร้าง (certainty 4)
    python tools/load_infra.py rail_lines.geojson --type rail --certainty 4

    # ทางด่วนสายใหม่ประกาศ พ.ร.ฎ. (certainty 3) corridor 60 ม.
    python tools/load_infra.py expressway.geojson --type expressway --certainty 3 --corridor 60

    # แนวเวนคืน พ.ร.ฎ. (certainty 3) corridor 40 ม.
    python tools/load_infra.py expropriation.geojson --type road --certainty 3 --corridor 40 --name-field project

ค่าใน properties ของแต่ละ feature จะ override ค่า default จาก flag:
    name / project_type / certainty_level / corridor_m / agency / source_url

*** สำคัญ: verified_by ถูกปล่อยว่างเสมอ — ต้องให้ "คน" ยืนยันข้อมูลเอง ***
(รหัสไม่ยืนยันข้อมูลภูมิศาสตร์ให้ตัวเอง โดยเฉพาะแนวเวนคืนที่กระทบคนจริง)
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from core import env as _env  # noqa: E402,F401
from core.db import connect  # noqa: E402

VALID_TYPES = {"road", "expressway", "rail", "station", "airport", "port", "other"}

# สีสายรถไฟฟ้าไทย (fallback เมื่อ OSM ไม่มี tag colour) — จับจากชื่อ/ref ของสาย
# เรียงเฉพาะเจาะจงก่อนทั่วไป (เจอตัวแรกใช้เลย)
LINE_COLORS = [
    ("สุขุมวิท", "#69BE28"), ("sukhumvit", "#69BE28"),
    ("สีลม", "#0B6E3B"), ("silom", "#0B6E3B"),
    ("เฉลิมรัชมงคล", "#1E52A0"), ("น้ำเงิน", "#1E52A0"), ("blue", "#1E52A0"),
    ("ฉลองรัช", "#8E258D"), ("ม่วง", "#8E258D"), ("purple", "#8E258D"),
    ("แอร์พอร์ต", "#B01116"), ("airport", "#B01116"), ("arl", "#B01116"),
    ("นครวิถี", "#E4002B"), ("ธานีรัถยา", "#E4002B"), ("แดง", "#E4002B"), ("red", "#E4002B"),
    ("ทอง", "#CBA63C"), ("gold", "#CBA63C"),
    ("นครา", "#FDD100"), ("เหลือง", "#FDD100"), ("yellow", "#FDD100"),
    ("ชมพู", "#E5007D"), ("pink", "#E5007D"),
    ("เขียว", "#4CAF50"), ("green", "#4CAF50"),
]


def _color_from_name(name: str | None) -> str | None:
    n = (name or "").lower()
    for kw, col in LINE_COLORS:
        if kw.lower() in n:
            return col
    return None


def _feature_rows(gj: dict, args) -> list[tuple]:
    feats = gj.get("features") if gj.get("type") == "FeatureCollection" else [gj]
    rows = []
    skipped = 0
    for ft in feats or []:
        geom = ft.get("geometry")
        props = ft.get("properties") or {}
        if not geom or not geom.get("coordinates"):
            skipped += 1
            continue
        name = str(props.get(args.name_field) or props.get("name")
                   or props.get("ref") or args.name or "").strip()
        if not name:
            skipped += 1
            continue
        ptype = str(props.get("project_type") or args.type).strip()
        if ptype not in VALID_TYPES:
            print(f"  ข้าม (project_type ไม่ถูกต้อง: {ptype}): {name}")
            skipped += 1
            continue
        try:
            certainty = int(props.get("certainty_level") or args.certainty)
        except (TypeError, ValueError):
            certainty = args.certainty
        try:
            corridor = int(props.get("corridor_m") if props.get("corridor_m") is not None
                           else args.corridor)
        except (TypeError, ValueError):
            corridor = args.corridor
        color = (props.get("colour") or props.get("color")
                 or props.get("stroke") or args.color
                 or _color_from_name(name))
        rows.append((
            name, ptype, props.get("agency") or args.agency, certainty,
            corridor, props.get("source_url") or args.source_url, color,
            json.dumps(geom, ensure_ascii=False),
        ))
    if skipped:
        print(f"  ข้าม {skipped} feature (ไม่มี geometry/ชื่อ หรือ type ผิด)")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("geojson", help="ไฟล์ .geojson (FeatureCollection หรือ Feature เดียว)")
    ap.add_argument("--type", default="station",
                    help=f"project_type เริ่มต้น {sorted(VALID_TYPES)}")
    ap.add_argument("--certainty", type=int, default=5,
                    help="1=ศึกษา 2=ครม. 3=พ.ร.ฎ. 4=ก่อสร้าง 5=เปิดใช้")
    ap.add_argument("--corridor", type=int, default=200,
                    help="ความกว้างแนว (เมตร) — สถานีใช้ 0, เวนคืน 40-60")
    ap.add_argument("--agency", default=None, help="รฟม./กทพ./ทล. ...")
    ap.add_argument("--source-url", default=None)
    ap.add_argument("--name-field", default="name", help="ชื่อ property ที่ใช้เป็นชื่อ")
    ap.add_argument("--name", default=None,
                    help="ชื่อเริ่มต้น (ใช้เมื่อ feature ไม่มี property ชื่อ)")
    ap.add_argument("--color", default=None,
                    help="สีสาย (hex เช่น #009639) — ใช้เมื่อไฟล์ไม่มี colour/color")
    ap.add_argument("--replace-type", action="store_true",
                    help="ลบ infra_projects ทั้งหมดของ project_type นี้ก่อนโหลด (โหลดทับทั้งชุด)")
    args = ap.parse_args()

    p = pathlib.Path(args.geojson)
    if not p.exists():
        print(f"ไม่พบไฟล์ {p}")
        return 1
    gj = json.loads(p.read_text(encoding="utf-8"))
    rows = _feature_rows(gj, args)
    if not rows:
        print("ไม่มี feature ที่โหลดได้")
        return 1

    print(f"เตรียมโหลด {len(rows)} feature เข้า infra_projects "
          f"(type={args.type} certainty={args.certainty} corridor={args.corridor})")

    with connect() as conn:
        if args.replace_type:
            n = conn.execute("delete from infra_projects where project_type = %s",
                             (args.type,)).rowcount
            print(f"  ลบของเดิม type={args.type} {n} แถว")
        loaded = 0
        for name, ptype, agency, certainty, corridor, url, color, geom_json in rows:
            # กันซ้ำแบบ idempotent: ลบชื่อ+type เดิมก่อน (โหลดซ้ำได้ไม่บวม)
            conn.execute("delete from infra_projects where name = %s and project_type = %s",
                         (name, ptype))
            conn.execute(
                """insert into infra_projects
                     (name, project_type, agency, certainty_level, corridor_m,
                      source_url, line_color, geom)
                   values (%s,%s,%s,%s,%s,%s,%s,
                           st_setsrid(st_geomfromgeojson(%s), 4326))""",
                (name, ptype, agency, certainty, corridor, url, color, geom_json))
            loaded += 1
        conn.commit()

    print(f"✓ โหลด {loaded} โครงการเข้า infra_projects เรียบร้อย "
          f"(verified_by ว่าง — ต้องให้คนยืนยันก่อนใช้ตัดสินใจจริง)")
    print("  จากนั้นรัน:  python src/enrich.py infra   เพื่อคำนวณระยะ + สร้าง flag")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
