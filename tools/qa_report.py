#!/usr/bin/env python3
"""QA ข้อมูล — สแกนคุณภาพทุกแหล่งในหน้าเดียว (Phase A)

    python tools/qa_report.py

ดูว่าแต่ละแหล่ง: มีกี่รายการ, มีพิกัดกี่ % (แปลงจริง/หยาบ), มีรูป/ราคา/เนื้อที่
กี่ %, เกรดกระจายยังไง, และมี parse_failure ค้างไหม
ใช้ไล่หาจุดที่ต้องซ่อมก่อนต่อยอดฟีเจอร์
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from core import env as _env  # noqa: E402,F401
from core.db import connect  # noqa: E402

LATEST = """
  select distinct on (source_code, external_ref) *
    from listing_snapshots
   order by source_code, external_ref, observed_at desc
"""


def pct(n: int, total: int) -> str:
    return f"{100*n/total:4.0f}%" if total else "   -"


def main() -> int:
    with connect() as conn:
        rows = conn.execute(f"""
            with latest as ({LATEST})
            select source_code,
                   count(*)                                          as total,
                   count(lat)                                        as mapped,
                   count(*) filter (where geo_precision='parcel')    as parcel,
                   count(*) filter (where geo_precision in
                        ('subdistrict','district','province'))       as approx,
                   count(image_url)                                  as img,
                   count(*) filter (where opening_price > 0)         as priced,
                   count(*) filter (where coalesce(land_area_sqwa,0) > 0
                        or coalesce(usable_area_sqm,0) > 0)          as area,
                   count(*) filter (where province is null)          as no_prov
              from latest
             group by source_code order by source_code""").fetchall()

        print("=" * 92)
        print(f"{'แหล่ง':14} {'ทั้งหมด':>8} {'มีพิกัด':>12} {'แปลงจริง':>11} "
              f"{'มีรูป':>10} {'มีราคา':>10} {'มีเนื้อที่':>11} {'ไร้จว.':>7}")
        print("-" * 92)
        for r in rows:
            t = r["total"]
            print(f"{r['source_code']:14} {t:>8,} "
                  f"{r['mapped']:>6,} {pct(r['mapped'],t):>5} "
                  f"{r['parcel']:>6,} {pct(r['parcel'],t):>4} "
                  f"{pct(r['img'],t):>10} {pct(r['priced'],t):>10} "
                  f"{pct(r['area'],t):>11} {r['no_prov']:>7,}")
        print("=" * 92)

        # การกระจายเกรด ต่อแหล่ง
        print("\nการกระจายเกรด (A ดีสุด · '—' = ข้อมูลไม่พอให้เกรด)")
        grades = conn.execute("""
            with latest as (%s)
            select l.source_code, coalesce(g.grade,'—') as grade, count(*) as n
              from latest l
              left join property_grades g
                on g.source_code=l.source_code and g.external_ref=l.external_ref
             group by 1,2 order by 1,2""" % LATEST).fetchall()
        by_src: dict[str, dict] = {}
        for g in grades:
            by_src.setdefault(g["source_code"], {})[g["grade"]] = g["n"]
        for src, gd in by_src.items():
            parts = " ".join(f"{k}:{v}" for k, v in sorted(gd.items()))
            print(f"  {src:14} {parts}")

        # parse_failures ค้าง (3 รอบล่าสุดต่อแหล่ง)
        print("\nparse_failures (3 รอบล่าสุดต่อแหล่ง — 0 = สะอาด)")
        pf = conn.execute("""
            select s.code,
                   coalesce(sum(x.n),0) as fails
              from sources s
              left join lateral (
                   select count(*) as n
                     from parse_failures pf
                    where pf.run_id in (select id from ingest_runs
                                         where source_code=s.code
                                         order by started_at desc limit 3)
              ) x on true
             group by s.code order by s.code""").fetchall()
        for p in pf:
            flag = "" if p["fails"] == 0 else "  <-- ตรวจด้วย diag_source.py"
            print(f"  {p['code']:14} {p['fails']:>4}{flag}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
