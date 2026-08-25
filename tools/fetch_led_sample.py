#!/usr/bin/env python3
"""ดึงหน้ารายการทรัพย์ LED มา 1 หน้า — ไว้ทดสอบ parser และเก็บไป calibrate

ยิงเว็บแค่ 2 ครั้ง: asset_day.asp (หน้าสรุป) -> asset_search_day.asp (รายการ 1 สนง.)
ไม่เขียน DB · เคารพ rate limit · ไม่แตะ seckey (ใช้เฉพาะ endpoint สาธารณะ)

    python tools/fetch_led_sample.py                    # วันนี้ สนง.แรกที่มีทรัพย์
    python tools/fetch_led_sample.py --office กรุงเทพ    # เจาะจงสำนักงาน
    python tools/fetch_led_sample.py --date 2026-08-25  # ระบุวันขาย

ผลลัพธ์
    data/probe/led_list_<วันที่>_<officeid>.html   ไฟล์ดิบ (ส่งให้ปรับ parser)
    บนจอ: จำนวนแถวที่ parse ได้ + ตัวอย่าง 3 แถวแรก พร้อม cell ดิบ
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import date, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from core import env as _env  # noqa: E402,F401

from adapters.led_auction import LedAuctionAdapter, to_thai_date  # noqa: E402
from core.db import Repo, connect  # noqa: E402
from core.http import Fetcher  # noqa: E402

OUT_DIR = pathlib.Path(__file__).resolve().parents[1] / "data" / "probe"


def _looks_thai(text: str) -> bool:
    return "ทรัพย์" in text or "สำนักงาน" in text or "กรม" in text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--office", default="", help="สตริงชื่อสำนักงานที่ต้องการ")
    ap.add_argument("--date", help="วันที่ขาย YYYY-MM-DD (ไม่ใส่ = วันนี้)")
    args = ap.parse_args()

    d = (datetime.strptime(args.date, "%Y-%m-%d").date()
         if args.date else date.today())

    # อ่าน rate limit จาก DB แต่ตั้ง encoding เป็น tis-620 (หน้าจริงเป็นไทยเข้ารหัสนี้
    # ไม่ใช่ utf-8 ตามที่ seed ไว้) — ถ้าใช้ utf-8 ตัวอักษรไทยจะกลายเป็น mojibake
    rate = 4.0
    try:
        with connect() as conn:
            src = Repo(conn).get_source("led_auction")
            rate = max(3.0, float(src["rate_limit_s"]))
    except Exception as exc:                                   # noqa: BLE001
        print(f"(อ่าน source จาก DB ไม่ได้ ใช้ค่า default: {exc})")

    fetcher = Fetcher(encoding="tis-620", rate_limit_s=rate)
    adapter = LedAuctionAdapter(fetcher, {})

    # ---- ขั้น 1: หน้าสรุปราย สนง. ----
    summary_task = {
        "url": adapter.DAY_URL, "method": "POST",
        "data": {"search_bid_date": to_thai_date(d), "search": "ok"},
        "meta": {"bid_date": d, "stage": "summary"},
    }
    print(f"[1/2] ดึงหน้าสรุปวันที่ {d} ({to_thai_date(d)}) ...")
    resp1 = adapter.fetch(summary_task)
    print(f"      HTTP {resp1.status} · {len(resp1.text):,} ตัวอักษร · "
          f"decode {'OK' if _looks_thai(resp1.text) else 'อาจ MOJIBAKE'}")

    office_tasks = list(adapter.follow_up(resp1, summary_task))
    if args.office:
        office_tasks = [t for t in office_tasks
                        if args.office in (t["meta"].get("office_name") or "")]
    print(f"      พบสำนักงานที่มีทรัพย์ {len(office_tasks)} แห่ง")
    if not office_tasks:
        print("ไม่พบสำนักงานตามเงื่อนไข — ลองเปลี่ยน --date หรือ --office")
        return 1

    # ---- ขั้น 2: หน้ารายการทรัพย์ของ สนง. แรก ----
    t = office_tasks[0]
    office = t["meta"].get("office_name", "?")
    print(f"[2/2] ดึงรายการทรัพย์: {office} ...")
    resp2 = adapter.fetch(t)
    print(f"      HTTP {resp2.status} · {len(resp2.text):,} ตัวอักษร · "
          f"decode {'OK' if _looks_thai(resp2.text) else 'อาจ MOJIBAKE'}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"led_list_{d}_{t['meta'].get('office_id') or 'x'}.html"
    out.write_text(resp2.text, encoding="utf-8")
    print(f"      เซฟไฟล์ดิบ: {out}")

    # ---- ทดสอบ parser กับหน้าจริง ----
    rows = list(adapter.parse(resp2, t))
    print(f"\nparse() ได้ {len(rows)} แถว")
    for i, r in enumerate(rows[:3], 1):
        print(f"  [{i}] ref={r.get('external_ref')} "
              f"type={r.get('property_type')} price={r.get('opening_price')} "
              f"round={r.get('auction_round')}")
        print(f"      cells={r.get('_cells')}")
    if not rows:
        print("  (ยังอ่านไม่ได้ — ส่งไฟล์ที่เซฟด้านบนมา ผมจะปรับ selector ให้ตรง)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
