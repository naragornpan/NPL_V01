#!/usr/bin/env python3
"""งานประจำวัน — รันทุก adapter, สรุปสถิติ, ตรวจสุขภาพ, แจ้งเตือน

    python src/run_all.py                  # รันทุกแหล่งที่เปิดใช้งาน
    python src/run_all.py --health-only    # ตรวจสุขภาพอย่างเดียว ไม่ดึงข้อมูล
    python src/run_all.py --dry-run

ตั้งเป็นงานประจำวันได้ 2 ทาง
    GitHub Actions  — ดู .github/workflows/ingest.yml (แนะนำ)
    Windows         — Task Scheduler เรียก run_daily.bat

หลักการ: แหล่งหนึ่งพังต้องไม่ทำให้แหล่งอื่นหยุด
"""
from __future__ import annotations

import argparse
import logging
import os
import pathlib
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from core import env as _env  # noqa: E402,F401  (โหลด .env ก่อนใครเพื่อน)

from core.db import connect  # noqa: E402

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("run_all")

VERDICT_ICON = {
    "ปกติ": "OK",
    "เลยกำหนด": "LATE",
    "เงียบนานผิดปกติ": "STALE",
    "รันล้มเหลว": "FAIL",
    "รันผ่านแต่ไม่ได้ข้อมูลเลย": "EMPTY",
    "มีข้อผิดพลาดบางส่วน": "WARN",
    "ยังไม่เคยรัน": "NEW",
    "ปิดใช้งาน": "OFF",
}

# verdict เหล่านี้ถือว่าต้องแจ้งเตือน
ALERT_VERDICTS = {"รันล้มเหลว", "เงียบนานผิดปกติ", "รันผ่านแต่ไม่ได้ข้อมูลเลย"}


def active_sources(conn) -> tuple[list[str], list[str]]:
    """คืน (แหล่งที่รันได้, แหล่งที่เปิดไว้แต่ยังไม่มี adapter)

    แหล่งที่ตั้ง is_active ไว้ล่วงหน้าแต่ยังไม่ได้เขียน adapter
    จะทำให้ log ขึ้น error ทุกวันจนคนเลิกอ่าน log ไปเลย
    ซึ่งอันตรายกว่า error จริงที่ซ่อนอยู่ในนั้น
    """
    import importlib

    codes = [r["code"] for r in conn.execute(
        "select code from sources where is_active order by code").fetchall()]

    runnable, pending = [], []
    for code in codes:
        try:
            importlib.import_module(f"adapters.{code}")
            runnable.append(code)
        except ModuleNotFoundError:
            pending.append(code)
    return runnable, pending


def run_one(source: str, dry_run: bool, tier: int | None = None,
            max_pages: int | None = None) -> tuple[str, int]:
    """รัน adapter หนึ่งตัวเป็น subprocess เพื่อไม่ให้มันพาตัวอื่นล้มไปด้วย"""
    cmd = [sys.executable, str(pathlib.Path(__file__).with_name("run.py")), source]
    if dry_run:
        cmd.append("--dry-run")
    if tier:
        cmd += ["--tier", str(tier)]
    if max_pages:
        cmd += ["--max-pages", str(max_pages)]
    log.info("เริ่ม %s", source)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        # แหล่งเดียว timeout ต้องไม่ทำให้ทั้ง run พัง — ข้ามไปแหล่งถัดไป
        log.error("%s เกินเวลา 1800s — ข้ามไปแหล่งถัดไป (ไม่ล้มทั้ง run)", source)
        return source, 124
    if proc.returncode != 0:
        log.error("%s ล้มเหลว: %s", source, (proc.stderr or "")[-500:])
    return source, proc.returncode


def health_report(conn) -> tuple[str, bool]:
    """คืนข้อความรายงาน และธงว่ามีอะไรต้องแจ้งเตือนไหม"""
    rows = conn.execute("select * from v_source_health").fetchall()
    lines = [f"สุขภาพระบบดึงข้อมูล {datetime.now():%Y-%m-%d %H:%M}", ""]
    needs_alert = False

    for r in rows:
        icon = VERDICT_ICON.get(r["verdict"], "?")
        hours = f"{r['hours_since_run']:.0f} ชม.ที่แล้ว" if r["hours_since_run"] else "-"
        lines.append(
            f"[{icon}] {r['code']:14} {r['verdict']:24} {hours:>14}  "
            f"ใหม่ 7 วัน {r['new_7d']:>5}")
        if r["verdict"] in ALERT_VERDICTS:
            needs_alert = True
            if r["error_sample"]:
                lines.append(f"       └ {r['error_sample'][:110]}")

    stats = conn.execute(
        """select count(distinct external_ref) as total,
                  count(distinct external_ref) filter
                    (where observed_at >= now() - interval '24 hours') as today
           from listing_snapshots""").fetchone()
    lines += ["", f"ทรัพย์ในระบบ {stats['total']:,} รายการ · เพิ่มใน 24 ชม. {stats['today']:,}"]

    hot = conn.execute("select * from v_hot_properties limit 3").fetchall()
    if hot:
        lines += ["", "ทรัพย์ที่คนสนใจสูงสุด 30 วัน"]
        for h in hot:
            lines.append(f"  {h['external_ref']:18} ดู {h['sessions']:>4} "
                         f"ทัก {h['inquiries']:>3}")
    return "\n".join(lines), needs_alert


def notify_line(message: str) -> None:
    """ส่งเข้า LINE ถ้าตั้งค่าไว้ ไม่ตั้งก็ข้าม ไม่ error"""
    token = os.environ.get("LINE_CHANNEL_TOKEN")
    to = os.environ.get("LINE_NOTIFY_TO")
    if not token or not to:
        log.info("ไม่ได้ตั้ง LINE_CHANNEL_TOKEN/LINE_NOTIFY_TO — ข้ามการแจ้งเตือน")
        return
    try:
        import requests
        resp = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={"to": to, "messages": [{"type": "text", "text": message[:4900]}]},
            timeout=15)
        log.info("ส่ง LINE: HTTP %s", resp.status_code)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("ส่ง LINE ไม่สำเร็จ: %s", exc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--health-only", action="store_true")
    ap.add_argument("--alert-always", action="store_true",
                    help="ส่ง LINE ทุกครั้ง ไม่ใช่เฉพาะตอนมีปัญหา")
    ap.add_argument("--tier", type=int,
                    help="1=กทม+ปริมณฑล 2=+EEC 3=+หัวเมืองภาค")
    ap.add_argument("--max-pages", type=int, help="จำนวนหน้าต่อจังหวัด")
    args = ap.parse_args()

    with connect() as conn:
        if not args.health_only:
            sources, pending = active_sources(conn)
            if pending:
                log.info("ข้ามแหล่งที่ยังไม่มี adapter: %s", ", ".join(pending))
            tier_txt = f" tier {args.tier}" if args.tier else ""
            log.info("จะรัน %s แหล่ง%s: %s", len(sources), tier_txt, ", ".join(sources))
            results = [run_one(s, args.dry_run, args.tier, args.max_pages)
                       for s in sources]
            failed = [s for s, rc in results if rc != 0]
            if failed:
                log.warning("แหล่งที่ล้มเหลว: %s", ", ".join(failed))

            if not args.dry_run:
                n = conn.execute("select rollup_events() as n").fetchone()["n"]
                conn.commit()
                log.info("สรุปสถิติรายวัน %s แถว", n)

        report, needs_alert = health_report(conn)
        print("\n" + report + "\n")

        if needs_alert or args.alert_always:
            notify_line(report)

    return 1 if needs_alert else 0


if __name__ == "__main__":
    raise SystemExit(main())
