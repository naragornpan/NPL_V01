#!/usr/bin/env python3
"""เก็บพิกัดแปลงจริงที่ "ค้นเอง" จาก LandsMaps เข้า DB (แบบ manual)

ทำไมต้อง manual
    LandsMaps มี WAF กันการดึงอัตโนมัติ + ToS ห้ามทำซ้ำข้อมูล
    จึงดึงเป็นชุดไม่ได้ แต่ "คนเปิดค้นเอง" ทีละแปลงทำได้ปกติ
    เครื่องมือนี้แค่รับพิกัดที่คุณค้นเจอมาบันทึก (geo_precision='parcel')

วิธีใช้
    1) ดูรายการที่ยังไม่มีพิกัดแปลง + ข้อมูลไว้ค้น (จังหวัด/อำเภอ/เลขโฉนด)
        python tools/set_parcel.py list
        python tools/set_parcel.py list --limit 30

    2) เปิด https://landsmaps.dol.go.th ค้นด้วย จังหวัด+อำเภอ+เลขโฉนด
       คัดลอก "ค่าพิกัดแปลง" (เช่น 13.78202850,100.58332907)

    3) บันทึกทีละตัว
        python tools/set_parcel.py set led:1968758 13.78202850,100.58332907

       หรือทีละหลายตัวจากไฟล์ (บรรทัดละ  ref,lat,lng  หรือ  ref lat lng)
        python tools/set_parcel.py import coords.txt
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from core import env as _env  # noqa: E402,F401
from core.db import connect  # noqa: E402

TH_BOUNDS = (5.5, 20.5, 97.0, 106.0)   # lat_min, lat_max, lng_min, lng_max


def _parse_coord(text: str) -> tuple[float, float] | None:
    m = re.search(r"(-?\d{1,2}\.\d+)\s*[,\s]\s*(-?\d{2,3}\.\d+)", text)
    if not m:
        return None
    lat, lng = float(m.group(1)), float(m.group(2))
    if TH_BOUNDS[0] <= lat <= TH_BOUNDS[1] and TH_BOUNDS[2] <= lng <= TH_BOUNDS[3]:
        return lat, lng
    return None


def cmd_list(limit: int) -> int:
    with connect() as conn:
        rows = conn.execute(
            """select external_ref, province, district,
                      raw_fields->>'deed_no' as deed_no,
                      geo_precision
                 from (select distinct on (source_code, external_ref)
                              external_ref, province, district,
                              geo_precision, raw_fields, observed_at
                         from listing_snapshots
                        where source_code = 'led_auction'
                        order by source_code, external_ref, observed_at desc) s
                where s.raw_fields->>'deed_no' is not null
                  and s.raw_fields->>'deed_no' not in ('-', '0', '')
                  and coalesce(s.geo_precision, '') <> 'parcel'
                order by external_ref
                limit %s""", (limit,)).fetchall()
    if not rows:
        print("ไม่มีทรัพย์ LED ที่มีเลขโฉนดและยังไม่มีพิกัดแปลง (ครบแล้ว หรือยังไม่ ingest)")
        return 0
    print(f"ทรัพย์ที่ค้นพิกัดแปลงได้ {len(rows)} รายการ — เปิด LandsMaps ค้นด้วยข้อมูลนี้:\n")
    print(f"{'external_ref':16}  {'จังหวัด':14}  {'อำเภอ':14}  โฉนด")
    print("-" * 60)
    for r in rows:
        print(f"{r['external_ref']:16}  {(r['province'] or '-'):14}  "
              f"{(r['district'] or '-'):14}  {r['deed_no']}")
    print("\nบันทึก:  python tools/set_parcel.py set <ref> <lat,lng>")
    return 0


def _save(conn, ref: str, lat: float, lng: float) -> bool:
    n = conn.execute(
        """update listing_snapshots
              set lat = %s, lng = %s, geo_precision = 'parcel'
            where source_code = 'led_auction' and external_ref = %s""",
        (lat, lng, ref)).rowcount
    return n > 0


def cmd_set(ref: str, coord_text: str) -> int:
    c = _parse_coord(coord_text)
    if not c:
        print(f"พิกัดไม่ถูกต้อง/นอกกรอบไทย: {coord_text!r} "
              f"(รูปแบบ: 13.7820,100.5833)")
        return 1
    with connect() as conn:
        ok = _save(conn, ref, c[0], c[1])
        conn.commit()
    print(f"{'✓ บันทึก' if ok else '✗ ไม่พบ ref'} {ref} -> {c[0]}, {c[1]}")
    return 0 if ok else 1


def cmd_import(path: str) -> int:
    p = pathlib.Path(path)
    if not p.exists():
        print(f"ไม่พบไฟล์ {p}")
        return 1
    saved = bad = 0
    with connect() as conn:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = re.split(r"[,\s]+", line, maxsplit=1)
            if len(parts) < 2:
                bad += 1
                continue
            ref, rest = parts[0], parts[1]
            c = _parse_coord(rest)
            if not c:
                print(f"  ข้าม (พิกัดผิด): {line}")
                bad += 1
                continue
            if _save(conn, ref, c[0], c[1]):
                saved += 1
            else:
                print(f"  ข้าม (ไม่พบ ref): {ref}")
                bad += 1
        conn.commit()
    print(f"\nบันทึก {saved} รายการ · ข้าม {bad}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("list", help="ดูทรัพย์ที่ยังไม่มีพิกัดแปลง + ข้อมูลไว้ค้น")
    pl.add_argument("--limit", type=int, default=50)
    ps = sub.add_parser("set", help="บันทึกพิกัดหนึ่งรายการ")
    ps.add_argument("ref")
    ps.add_argument("coord", help="เช่น 13.7820,100.5833")
    pi = sub.add_parser("import", help="บันทึกจากไฟล์ (บรรทัดละ ref,lat,lng)")
    pi.add_argument("path")
    args = ap.parse_args()

    if args.cmd == "list":
        return cmd_list(args.limit)
    if args.cmd == "set":
        return cmd_set(args.ref, args.coord)
    if args.cmd == "import":
        return cmd_import(args.path)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
