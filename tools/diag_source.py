#!/usr/bin/env python3
"""ดูสาเหตุ error ของแหล่งหนึ่ง — จาก ingest_runs + parse_failures

    python tools/diag_source.py ttb
    python tools/diag_source.py ttb --runs 8

ใช้ตอนเห็น WARN/partial ใน run_all --health-only ว่ารายการไหน/เพราะอะไร
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from core import env as _env  # noqa: E402,F401
from core.db import connect  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="รหัสแหล่ง เช่น ttb, ghb, bam")
    ap.add_argument("--runs", type=int, default=5, help="ดูกี่รอบล่าสุด")
    args = ap.parse_args()

    with connect() as conn:
        print(f"=== ingest_runs {args.runs} รอบล่าสุดของ '{args.source}' ===")
        runs = conn.execute(
            """select started_at, status, pages_fetched, rows_parsed,
                      rows_new, error_count, error_sample
                 from ingest_runs
                where source_code = %s
                order by started_at desc limit %s""",
            (args.source, args.runs)).fetchall()
        if not runs:
            print("  (ยังไม่เคยรัน)")
        for r in runs:
            print(f"[{r['status']:7}] {str(r['started_at'])[:19]}  "
                  f"pages={r['pages_fetched']} parsed={r['rows_parsed']} "
                  f"new={r['rows_new']} err={r['error_count']}")
            if r["error_sample"]:
                print(f"        └ error_sample: {r['error_sample'][:300]}")

        print(f"\n=== parse_failures (เหตุผลที่พบบ่อย, {args.runs} รอบล่าสุด) ===")
        fails = conn.execute(
            """select reason, count(*) as n
                 from parse_failures
                where run_id in (select id from ingest_runs
                                  where source_code = %s
                                  order by started_at desc limit %s)
                group by reason order by n desc limit 10""",
            (args.source, args.runs)).fetchall()
        if not fails:
            print("  (ไม่มี parse_failure — WARN อาจมาจาก fetch timeout บางหน้า)")
        for f in fails:
            print(f"  {f['n']:4} ×  {f['reason'][:180]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
