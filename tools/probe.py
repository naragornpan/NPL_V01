#!/usr/bin/env python3
"""Probe — สำรวจหน้าเว็บต้นทางก่อนเขียน parser

รันตัวนี้ก่อนเสมอ อย่าเดาโครงสร้าง HTML เอง

    python tools/probe.py https://asset.led.go.th/newbidreg/ --encoding tis-620

URL ที่ยืนยันแล้ว (จากหน้า e-Service ของ led.go.th)
    ค้นหาทรัพย์ประกาศขายทอดตลาด  https://asset.led.go.th/newbidreg/
    รายงานผลการขายทอดตลาด        https://asset.led.go.th/report
    ระบบลงทะเบียนซื้อล่วงหน้า      https://svasset.led.go.th/assetregis/session3.asp

จะได้:
  data/probe/<timestamp>_<host>.html   ไฟล์ดิบไว้เปิดดู
  รายงานบนจอ: form ทั้งหมด + input names + ตารางที่เจอ + ตัวอย่างแถว

form input names คือสิ่งที่ต้องเอาไปใส่ใน discover() ของ adapter
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime
from urllib.parse import urlparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from core import env as _env  # noqa: E402,F401

from bs4 import BeautifulSoup  # noqa: E402

from core.http import Fetcher  # noqa: E402

OUT_DIR = pathlib.Path(__file__).resolve().parents[1] / "data" / "probe"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--encoding", default="utf-8")
    ap.add_argument("--post", action="store_true", help="ส่งแบบ POST")
    ap.add_argument("--data", default="", help="k=v&k2=v2")
    args = ap.parse_args()

    fetcher = Fetcher(encoding=args.encoding, rate_limit_s=0)
    if args.post:
        payload = dict(p.split("=", 1) for p in args.data.split("&") if "=" in p)
        resp = fetcher.post(args.url, data=payload)
    else:
        resp = fetcher.get(args.url)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    host = urlparse(args.url).netloc.replace(":", "_")
    path = OUT_DIR / f"{stamp}_{host}.html"
    path.write_text(resp.text, encoding="utf-8")

    print(f"HTTP {resp.status}  |  {len(resp.text):,} chars  |  บันทึกที่ {path}")
    print(f"อัตราส่วนอักขระไทย: {Fetcher._thai_ratio(resp.text):.1%}"
          "  (ถ้าใกล้ 0% แปลว่า encoding ผิด)")

    soup = BeautifulSoup(resp.text, "html.parser")

    print("\n=== FORMS ===")
    for i, form in enumerate(soup.find_all("form")):
        print(f"\n[form {i}] action={form.get('action')!r} method={form.get('method')!r}")
        for tag in form.find_all(["input", "select", "textarea"]):
            name = tag.get("name")
            if not name:
                continue
            detail = f"  {tag.name:8} name={name!r} type={tag.get('type')!r}"
            if tag.name == "select":
                opts = [(o.get("value"), o.get_text(strip=True))
                        for o in tag.find_all("option")[:8]]
                detail += f" options={opts}"
            print(detail)

    print("\n=== TABLES ===")
    for i, table in enumerate(soup.find_all("table")):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        cells = [c.get_text(strip=True)[:28] for c in rows[0].find_all(["th", "td"])]
        print(f"\n[table {i}] {len(rows)} rows, {len(cells)} cols")
        print(f"  header: {cells}")
        if len(rows) > 1:
            sample = [c.get_text(strip=True)[:28] for c in rows[1].find_all(["th", "td"])]
            print(f"  row[1]: {sample}")

    print("\n=== LINKS ที่น่าจะเป็นหน้ารายละเอียด ===")
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(k in href.lower() for k in ("detail", "view", "asset", "item", "id=")):
            key = href.split("?")[0]
            if key not in seen:
                seen.add(key)
                print(f"  {href[:110]}")
        if len(seen) >= 10:
            break


if __name__ == "__main__":
    main()
