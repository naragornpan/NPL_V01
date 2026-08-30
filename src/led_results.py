#!/usr/bin/env python3
"""led_results.py — ดึง "ผลการขายทอดตลาด" (ราคาจบประมูล) ของกรมบังคับคดี
แล้วจับคู่กับทรัพย์ LED ที่เราเก็บไว้ก่อนประมูล

แหล่ง: POST https://asset.led.go.th/report/report.asp
  ฟอร์ม: PROVINCE_ID (สำนักงาน) + saledate (dd/mm/yyyy พ.ศ.) + Action=" ดูผลการขาย "
  หน้ารายงานเก็บผล "ย้อนหลัง 6 เดือน" → ต้องรันสม่ำเสมอ (เช่นทุกวัน/สัปดาห์)

วิธีทำงาน
  1. อ่าน (office, วันนัดประมูล) จาก listing_snapshots ของเรา (biddate1..8 ที่เลยวันแล้ว
     และอยู่ในกรอบ 6 เดือน) — เรารู้ทุกวันที่นัดอยู่แล้ว ไม่ต้องเดา
  2. ยิงรายงานต่อ (office, วัน) → parse ตารางผล → จับคู่กับทรัพย์เราด้วยราคาประเมิน
  3. upsert ลง led_auction_results (+ บันทึก fetchlog กันยิงซ้ำ)

รัน:  python src/led_results.py                 # ปกติ
      python src/led_results.py --days-back 60  # แคบช่วง
      python src/led_results.py --refetch        # ดึงซ้ำแม้เคยดึงแล้ว

หมายเหตุ: ต้องรันจากเครือข่ายที่เข้าถึง asset.led.go.th ได้ (IP ไทย) — ที่เดียวกับ
adapter LED ปกติ. เซิร์ฟเวอร์ช้า จึง sleep ระหว่างคำขอ
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser

import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from core import env as _env  # noqa: E402,F401  (โหลด .env)
from core.db import connect   # noqa: E402

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)-7s | %(message)s")
log = logging.getLogger("led_results")

REPORT_URL = "https://asset.led.go.th/report/report.asp"
ACTION_VALUE = " ดูผลการขาย "        # ต้องส่งเป็น cp874 (หน้าเป็น windows-874)
FRESH_DAYS = 45                       # วันขายภายใน N วันล่าสุด = ดึงซ้ำเสมอ (ผลอาจยังเปลี่ยนรอบถัดไป)

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _num(s):
    if s is None:
        return None
    s = s.replace(",", "").strip()
    if s in ("", "-", "0.00"):
        return 0 if s == "0.00" else (None if s in ("", "-") else None)
    try:
        return float(s)
    except ValueError:
        return None


def _bud_to_date(yyyymmdd: str):
    """'25690807' (พ.ศ.) -> date(2026,8,7)"""
    try:
        y = int(yyyymmdd[0:4]) - 543
        m = int(yyyymmdd[4:6])
        d = int(yyyymmdd[6:8])
        return dt.date(y, m, d)
    except (ValueError, IndexError):
        return None


def _date_to_saledate(d: dt.date) -> str:
    """date -> 'dd/mm/yyyy' (พ.ศ.) สำหรับส่งฟอร์ม"""
    return f"{d.day:02d}/{d.month:02d}/{d.year + 543}"


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._cur = None
        self._incell = False
        self._buf = ""

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._cur = []
        elif tag in ("td", "th"):
            self._incell = True
            self._buf = ""

    def handle_data(self, data):
        if self._incell:
            self._buf += data

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cur is not None:
            self._cur.append(re.sub(r"\s+", " ", self._buf).strip())
            self._incell = False
        elif tag == "tr" and self._cur is not None:
            self.rows.append(self._cur)
            self._cur = None


def _fetch_report(opener, office_id: str, d: dt.date):
    """คืน list ของ dict ผลการขายสำหรับ (office, วัน) — [] ถ้าไม่พบ/error"""
    body = (
        "PROVINCE_ID=" + urllib.parse.quote(office_id)
        + "&saledate=" + urllib.parse.quote(_date_to_saledate(d))
        + "&Action=" + urllib.parse.quote_from_bytes(ACTION_VALUE.encode("cp874"))
    )
    req = urllib.request.Request(
        REPORT_URL, data=body.encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Referer": REPORT_URL,
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126",
                 "Accept-Language": "th"})
    try:
        raw = opener.open(req, timeout=90).read()
    except Exception as exc:                                   # noqa: BLE001
        log.warning("  ยิง %s %s ไม่สำเร็จ: %s", office_id, d, str(exc)[:100])
        return None
    txt = raw.decode("utf-8", "replace")
    if "Error Pages" in txt or len(txt) < 500:
        return None                                            # error/ไม่ใช่หน้าผล
    # หมายเหตุ: หน้ารายงานมี template "ไม่พบข้อมูล" ซ่อนอยู่เสมอ — ห้ามใช้เป็นตัวตัดสิน
    # ต้องดูจากจำนวนแถวที่ parse ได้จริง (out ว่าง = ไม่มีผลของวันนั้น)
    p = _TableParser()
    p.feed(txt)
    out = []
    for r in p.rows:
        if len(r) < 9:
            continue
        seq, court, case_no, deed, plaintiff, ptype, appr, result, sold = r[:9]
        if seq in ("", "ลำดับที่") or "ลำดับ" in seq:
            continue                                           # header
        result = result.strip()
        out.append({
            "seq": seq, "court": court, "case_no": case_no, "deed": deed,
            "plaintiff": plaintiff, "property_type_th": ptype,
            "appraised_price": _num(appr), "result": result,
            "sold_price": _num(sold),
            "is_sold": ("ขายได้" in result and "งด" not in result),
        })
    return out


def _targets_and_index(conn, days_back: int):
    """คืน (targets, match_index)
       targets = set{(office_id, date)} วันนัดที่เลยแล้วและอยู่ในกรอบ
       match_index = {(office_id, date): {appraised_int: external_ref}}
    """
    today = dt.date.today()
    lo = today - dt.timedelta(days=days_back)
    rows = conn.execute("""
        select external_ref,
               raw_fields->'_open_post'->>'province_id' as office_id,
               raw_fields->>'appraised_price'           as appr,
               raw_fields->'_open_post'->>'biddate1' b1,
               raw_fields->'_open_post'->>'biddate2' b2,
               raw_fields->'_open_post'->>'biddate3' b3,
               raw_fields->'_open_post'->>'biddate4' b4,
               raw_fields->'_open_post'->>'biddate5' b5,
               raw_fields->'_open_post'->>'biddate6' b6,
               raw_fields->'_open_post'->>'biddate7' b7,
               raw_fields->'_open_post'->>'biddate8' b8
          from listing_snapshots
         where source_code = 'led_auction'
           and raw_fields->'_open_post'->>'province_id' is not null
    """).fetchall()
    targets: set = set()
    index: dict = {}
    for r in rows:
        office = r["office_id"]
        if not office:
            continue
        appr_i = None
        if r["appr"]:
            try:
                appr_i = int(float(r["appr"]))
            except ValueError:
                appr_i = None
        for k in ("b1", "b2", "b3", "b4", "b5", "b6", "b7", "b8"):
            bd = r[k]
            if not bd or not bd.isdigit() or len(bd) != 8:
                continue
            d = _bud_to_date(bd)
            if not d or d >= today or d < lo:
                continue                                       # เอาเฉพาะที่เลยวันแล้ว+ในกรอบ
            targets.add((office, d))
            if appr_i is not None:
                index.setdefault((office, d), {}).setdefault(appr_i, r["external_ref"])
    return targets, index


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-back", type=int, default=185,
                    help="ดึงผลของวันนัดย้อนหลังกี่วัน (LED เก็บ ~180)")
    ap.add_argument("--sleep", type=float, default=1.2, help="พักระหว่างคำขอ (วินาที)")
    ap.add_argument("--refetch", action="store_true", help="ดึงซ้ำแม้เคยดึงแล้ว")
    ap.add_argument("--limit", type=int, default=0, help="จำกัดจำนวน (office,วัน) ต่อรอบ (0=ไม่จำกัด)")
    args = ap.parse_args()

    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_CTX))
    opener.addheaders = [("User-Agent", "Mozilla/5.0 Chrome/126"), ("Accept-Language", "th")]
    ok = False
    for _try in range(3):
        try:
            opener.open(REPORT_URL, timeout=60).read()         # อุ่น session/cookie
            ok = True
            break
        except Exception as exc:                                # noqa: BLE001
            log.warning("เปิด report.asp ครั้งที่ %s ไม่สำเร็จ: %s", _try + 1, str(exc)[:100])
            time.sleep(3)
    if not ok:
        log.error("เปิด report.asp ไม่ได้หลังลอง 3 ครั้ง — เครือข่ายนี้เข้า LED ไม่ได้ "
                  "(GitHub Actions/US มักโดนบล็อก ให้รันบนเครื่อง IP ไทย) — ข้ามรอบนี้")
        return 0   # ไม่ทำให้ job แดง

    today = dt.date.today()
    with connect() as conn:
        targets, index = _targets_and_index(conn, args.days_back)
        done = {(r["office_id"], r["sale_date"]) for r in conn.execute(
            "select office_id, sale_date from led_result_fetchlog").fetchall()}
        todo = []
        for (office, d) in sorted(targets, key=lambda x: x[1], reverse=True):
            fresh = (today - d).days <= FRESH_DAYS
            if (office, d) in done and not fresh and not args.refetch:
                continue
            todo.append((office, d))
        if args.limit:
            todo = todo[:args.limit]
        log.info("เป้าหมาย (office,วัน) ที่ต้องดึง: %d จากทั้งหมด %d", len(todo), len(targets))

        n_rows = n_matched = n_pages = 0
        for i, (office, d) in enumerate(todo, 1):
            rows = _fetch_report(opener, office, d)
            if rows is None:
                time.sleep(args.sleep)
                continue
            n_pages += 1
            idx = index.get((office, d), {})
            for row in rows:
                appr_i = int(row["appraised_price"]) if row["appraised_price"] else None
                ref = idx.get(appr_i) if appr_i is not None else None
                if ref:
                    n_matched += 1
                row_key = f"{(row['case_no'] or '')}|{appr_i if appr_i is not None else ''}"
                conn.execute("""
                    insert into led_auction_results
                      (office_id, sale_date, row_key, case_no, seq, court, deed, plaintiff,
                       property_type_th, appraised_price, result, sold_price, is_sold, matched_ref, fetched_at)
                    values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                    on conflict (office_id, sale_date, row_key) do update set
                       case_no=excluded.case_no, seq=excluded.seq, court=excluded.court,
                       deed=excluded.deed, plaintiff=excluded.plaintiff,
                       property_type_th=excluded.property_type_th,
                       appraised_price=excluded.appraised_price, result=excluded.result,
                       sold_price=excluded.sold_price, is_sold=excluded.is_sold,
                       matched_ref=coalesce(excluded.matched_ref, led_auction_results.matched_ref),
                       fetched_at=now()
                """, (office, d, row_key, row["case_no"], row["seq"], row["court"],
                      row["deed"], row["plaintiff"], row["property_type_th"],
                      row["appraised_price"], row["result"], row["sold_price"],
                      row["is_sold"], ref))
                n_rows += 1
            conn.execute("""insert into led_result_fetchlog (office_id, sale_date, rows_found, fetched_at)
                            values (%s,%s,%s, now())
                            on conflict (office_id, sale_date)
                            do update set rows_found=excluded.rows_found, fetched_at=now()""",
                         (office, d, len(rows)))
            conn.commit()
            if i % 20 == 0:
                log.info("  ...%d/%d (rows=%d matched=%d)", i, len(todo), n_rows, n_matched)
            time.sleep(args.sleep)

        log.info("เสร็จ: ดึง %d หน้า | เก็บผล %d แถว | จับคู่ทรัพย์เราได้ %d", n_pages, n_rows, n_matched)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
