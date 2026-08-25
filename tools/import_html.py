#!/usr/bin/env python3
"""นำเข้าไฟล์ HTML ที่เซฟไว้เอง — สำหรับหน้าที่มีรหัสยืนยัน

    python tools/import_html.py "C:\\path\\ผลค้นหา.html" --source led_auction

ทำไมต้องมีเครื่องมือนี้
    ฟอร์มค้นหาละเอียดของกรมบังคับคดีมีช่อง "รหัสยืนยัน" ซึ่งเป็นกลไกกันบอท
    เราไม่หลบมัน แต่คุณค้นด้วยมือได้ตามปกติ

วิธีใช้
    1. เปิด https://asset.led.go.th/newbidreg/ ค้นหาตามเงื่อนไขที่ต้องการ
       กรอกรหัสยืนยันเอง แล้วกดค้นหา
    2. บนหน้าผลลัพธ์ กด Ctrl+S เซฟเป็น "Webpage, HTML Only"
    3. รันคำสั่งนี้ชี้ไปที่ไฟล์ที่เซฟ

ข้อมูลจะเข้าฐานเหมือนกับที่ scraper ดึงมาทุกประการ
ต่างกันแค่ช่องทางที่ได้มา
"""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from core import env as _env  # noqa: E402,F401

from adapters.led_auction import LedAuctionAdapter  # noqa: E402
from core.db import Repo, connect  # noqa: E402
from core.http import Fetcher, Response  # noqa: E402

ADAPTERS = {"led_auction": LedAuctionAdapter}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="ไฟล์ HTML ที่เซฟไว้")
    ap.add_argument("--source", default="led_auction", choices=sorted(ADAPTERS))
    ap.add_argument("--bid-date", help="วันที่ขาย (YYYY-MM-DD) ถ้าหน้าไม่ได้ระบุ")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    path = pathlib.Path(args.path)
    if not path.exists():
        print(f"ไม่พบไฟล์ {path}")
        return 1

    raw = path.read_bytes()
    fetcher = Fetcher(encoding="utf-8", rate_limit_s=0)
    text = fetcher.decode(raw)
    if Fetcher._looks_like_mojibake(text):
        print("เตือน: ข้อความดูเป็น mojibake — เซฟไฟล์ใหม่แบบ HTML Only")

    resp = Response(url=f"file://{path.name}", status=200, text=text,
                    content_hash=hashlib.sha256(text.encode()).hexdigest())

    bid = (datetime.strptime(args.bid_date, "%Y-%m-%d").date()
           if args.bid_date else None)
    task = {"meta": {"bid_date": bid}}

    with connect() as conn:
        repo = Repo(conn)
        adapter = ADAPTERS[args.source](fetcher, {})
        run_id = repo.start_run(args.source)
        raw_id = None if args.dry_run else repo.save_raw(args.source, run_id, resp)

        parsed = new = 0
        for fields in adapter.parse(resp, task):
            parsed += 1
            fields.setdefault("_import_file", path.name)
            snap = adapter.to_snapshot(fields, raw_id)
            if args.dry_run:
                print(f"  DRY {snap.get('external_ref')} | "
                      f"{snap.get('property_type')} | {snap.get('opening_price')}")
            elif repo.save_snapshot(snap):
                new += 1

        if not args.dry_run:
            repo.finish_run(run_id, "ok", pages_fetched=1,
                            rows_parsed=parsed, rows_new=new)
        print(f"\nอ่านได้ {parsed} แถว · ใหม่ {new} แถว")
        if parsed == 0:
            print("อ่านไม่ได้เลย — ส่งไฟล์นี้มาให้ปรับ parser")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
