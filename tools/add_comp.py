#!/usr/bin/env python3
"""บันทึกสถิติราคาตลาดจากเว็บประกาศขาย — ด้วยมือ ไม่ scrape

    python tools/add_comp.py

ทำไมต้องกรอกมือ
    เว็บประกาศขายส่วนใหญ่ห้ามดึงข้อมูลอัตโนมัติในเงื่อนไขการใช้งาน
    และการทำสำเนาประกาศ/รูปคือการละเมิดลิขสิทธิ์ชัดเจน

    สิ่งที่เราเก็บคือ *สถิติ* ที่อ่านจากหน้าผลค้นหา (มีกี่รายการ ราคากลางเท่าไหร่)
    ซึ่งเป็นข้อเท็จจริงเชิงตัวเลข ไม่ใช่การทำซ้ำเนื้อหาของเขา
    และเราลิงก์กลับไปหน้าค้นหาของเขาเสมอ

วิธีเก็บที่ใช้เวลาน้อยที่สุด
    1. เปิดหน้าค้นหาของเว็บนั้น กรองเขต + ประเภท + ช่วงราคา
    2. เรียงตามราคา ดูจำนวนผลลัพธ์ทั้งหมด
    3. เปิดรายการที่อยู่ตรงกลาง = median คร่าว ๆ
       รายการที่ 25% และ 75% = p25/p75
    4. กรอกลงเครื่องมือนี้ ใช้เวลาราว 3-4 นาทีต่อเขตต่อเว็บ

ทำเดือนละครั้งต่อเขตโฟกัสก็พอ ราคาตั้งขายไม่ได้เปลี่ยนรายวัน
"""
from __future__ import annotations

import pathlib
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from core import env as _env  # noqa: E402,F401

from core.db import connect  # noqa: E402

TYPES = ["land", "house", "townhouse", "condo", "commercial"]


def ask(prompt: str, default: str | None = None, required: bool = True) -> str | None:
    suffix = f" [{default}]" if default else ""
    while True:
        val = input(f"{prompt}{suffix}: ").strip() or default
        if val or not required:
            return val
        print("  ต้องกรอกค่านี้")


def ask_num(prompt: str, required: bool = True) -> float | None:
    while True:
        raw = input(f"{prompt}: ").strip().replace(",", "")
        if not raw and not required:
            return None
        try:
            return float(raw)
        except ValueError:
            print("  กรอกเป็นตัวเลข")


def main() -> int:
    print(__doc__)
    with connect() as conn:
        sources = conn.execute(
            "select code, label, price_kind from comp_sources where is_active"
        ).fetchall()
        print("\nแหล่งข้อมูล:")
        for i, s in enumerate(sources, 1):
            print(f"  {i}. {s['code']:12} {s['label']} ({s['price_kind']})")

        idx = int(ask("เลือกแหล่ง (หมายเลข)")) - 1
        source = sources[idx]

        province = ask("จังหวัด")
        district = ask("อำเภอ/เขต", required=False)
        print(f"ประเภท: {', '.join(TYPES)}")
        ptype = ask("ประเภททรัพย์", default="townhouse")

        period = date.today().replace(day=1)
        n = int(ask_num("จำนวนประกาศที่พบ"))
        median = ask_num("ราคากลาง (median)")
        p25 = ask_num("ราคาที่ 25% (เว้นว่างได้)", required=False)
        p75 = ask_num("ราคาที่ 75% (เว้นว่างได้)", required=False)
        per_sqwa = ask_num("ราคากลางต่อ ตร.ว. (เว้นว่างได้)", required=False)
        url = ask("ลิงก์หน้าค้นหาที่ใช้เก็บ", required=False)
        note = ask("หมายเหตุ (เช่น ช่วงราคาที่กรอง)", required=False)
        by = ask("ผู้บันทึก", default="me")

        conn.execute(
            """insert into market_comps
                 (source_code, province, district, property_type, period_month,
                  n_listings, median_price, p25_price, p75_price, median_per_sqwa,
                  collected_by, search_url, note)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               on conflict (source_code, province, district, property_type, period_month)
               do update set n_listings = excluded.n_listings,
                             median_price = excluded.median_price,
                             p25_price = excluded.p25_price,
                             p75_price = excluded.p75_price,
                             median_per_sqwa = excluded.median_per_sqwa,
                             collected_at = now(),
                             search_url = excluded.search_url,
                             note = excluded.note""",
            (source["code"], province, district, ptype, period, n, median,
             p25, p75, per_sqwa, by, url, note),
        )
        conn.commit()
        print(f"\nบันทึกแล้ว: {source['label']} · {district or province} · {ptype} · {period}")

        gaps = conn.execute(
            """select source_label, market_median, auction_median,
                      raw_gap_pct, adjusted_gap_pct, freshness
               from v_price_gap
               where province = %s and coalesce(district,'') = coalesce(%s,'')
                 and property_type = %s""",
            (province, district, ptype),
        ).fetchall()
        if gaps:
            print("\nส่วนต่างล่าสุดในพื้นที่นี้")
            for g in gaps:
                a = f"{g['auction_median']:,.0f}" if g["auction_median"] else "ยังไม่มี"
                print(f"  {g['source_label']:22} ตลาด {g['market_median']:>12,.0f} | "
                      f"ประมูล {a:>12} | ส่วนต่างดิบ {g['raw_gap_pct']}% | {g['freshness']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
