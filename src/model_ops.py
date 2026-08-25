"""วงจรชีวิตโมเดล — รีเฟรชพารามิเตอร์ ประเมิน และเลื่อนขั้นอย่างปลอดภัย

รันเดือนละครั้ง:
    python src/model_ops.py monthly

สิ่งที่เกิดขึ้นตามลำดับ
  1. คำนวณ uplift observation ใหม่จากดีลที่ปิดในเดือนที่ผ่านมา
  2. snapshot เส้นโค้งไว้แบบ point-in-time
  3. ตรวจ drift — ถ้าเส้นโค้งกระโดดผิดปกติ หยุดและแจ้งเตือน ไม่อัปเดตเอง
  4. ประเมินโมเดลทุกเวอร์ชันที่มี outcome ใหม่
  5. เช็คว่ามี challenger ตัวไหนผ่านเกณฑ์เลื่อนขั้น -> แจ้งให้คนตัดสินใจ

ข้อที่ 3 กับ 5 จบที่ "แจ้งเตือน" ไม่ใช่ "ทำเลย" โดยตั้งใจ
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from core.db import connect

log = logging.getLogger("model_ops")


# ---------------------------------------------------------------------
def refresh_parameters(conn, as_of: date) -> dict:
    """ประเภท A — รีเฟรชพารามิเตอร์ ปลอดภัยพอที่จะทำอัตโนมัติ

    ไม่แตะโครงสร้างโมเดล แค่ให้ตัวเลขสะท้อนข้อมูลล่าสุด
    """
    n = conn.execute("select snapshot_uplift_curves(%s) as n", (as_of,)).fetchone()["n"]
    conn.commit()
    log.info("snapshot เส้นโค้ง %s รายการ ณ %s", n, as_of)
    return {"snapshots": n}


def check_drift(conn) -> list[dict]:
    """หยุดทันทีถ้าเส้นโค้งเปลี่ยนผิดปกติ

    p50 กระโดดเกิน 30% ในเดือนเดียว มักไม่ใช่ตลาดเปลี่ยน
    แต่เป็น parser พัง หรือมีดีลผิดปกติหลุดเข้ามา
    """
    rows = conn.execute(
        "select * from v_curve_drift where verdict <> 'ปกติ' order by drift_pct desc"
    ).fetchall()
    for r in rows:
        log.warning(
            "DRIFT %s/%s/%s: p50 %s -> %s (%s%%) — %s",
            r["project_type"], r["event_type"], r["distance_band"],
            r["prev_p50"], r["cur_p50"], r["drift_pct"], r["verdict"],
        )
    return rows


def evaluate_all(conn) -> list[dict]:
    versions = conn.execute(
        "select version from model_versions where status in ('champion','challenger')"
    ).fetchall()
    results = []
    for v in versions:
        for horizon in (12, 24, 36, 60):
            row = conn.execute(
                """select count(*) as n from property_forecasts f
                   join forecast_outcomes o on o.forecast_id = f.id
                   where f.model_version = %s and f.horizon_months = %s""",
                (v["version"], horizon),
            ).fetchone()
            if row["n"] < 5:
                continue          # ตัวอย่างน้อยเกินกว่าจะประเมิน
            conn.execute("select evaluate_model(%s, %s)", (v["version"], horizon))
            conn.commit()
            results.append({"version": v["version"], "horizon": horizon, "n": row["n"]})
    return results


def promotion_candidates(conn) -> list[dict]:
    return conn.execute(
        """select * from v_promotion_check
           where eligible and model_version in
             (select version from model_versions where status = 'challenger')"""
    ).fetchall()


def promote(conn, version: str, approved_by: str) -> None:
    """เลื่อนขั้นด้วยมือเท่านั้น — ต้องมีชื่อคนอนุมัติ

    ไม่ทำอัตโนมัติแม้จะผ่านเกณฑ์ทุกข้อ เพราะเกณฑ์เชิงตัวเลขจับไม่ได้ว่า
    โมเดลใหม่ทำพลาดในเคสสำคัญหรือเปล่า ต้องมีคนไล่ดูตัวอย่างจริงก่อน
    """
    ok = conn.execute(
        "select eligible from v_promotion_check where model_version = %s", (version,)
    ).fetchone()
    if not ok or not ok["eligible"]:
        raise RuntimeError(f"{version} ยังไม่ผ่านเกณฑ์ใน v_promotion_check")

    conn.execute(
        """update model_versions
           set status = 'retired', retired_at = now(),
               retire_reason = 'ถูกแทนที่โดย ' || %s
           where status = 'champion'""",
        (version,),
    )
    conn.execute(
        """update model_versions set status = 'champion',
               promoted_at = now(), promoted_by = %s
           where version = %s""",
        (approved_by, version),
    )
    conn.commit()
    log.info("เลื่อนขั้น %s เป็น champion โดย %s", version, approved_by)


# ---------------------------------------------------------------------
def monthly(conn) -> str:
    """งานรายเดือน คืนข้อความสรุปสำหรับส่งเข้า LINE"""
    today = date.today()
    lines = [f"รายงานสุขภาพโมเดล {today:%Y-%m-%d}", ""]

    stats = refresh_parameters(conn, today)
    lines.append(f"• snapshot เส้นโค้ง: {stats['snapshots']} รายการ")

    drift = check_drift(conn)
    if drift:
        lines.append(f"• เส้นโค้งผิดปกติ {len(drift)} รายการ — ต้องตรวจสอบก่อนใช้")
        for d in drift[:3]:
            lines.append(f"    {d['distance_band']} {d['event_type']}: "
                         f"เปลี่ยน {d['drift_pct']}%")
    else:
        lines.append("• ไม่พบเส้นโค้งผิดปกติ")

    evals = evaluate_all(conn)
    lines.append(f"• ประเมินโมเดล {len(evals)} ชุด")
    for e in evals:
        row = conn.execute(
            """select median_ape, band_coverage, bias_pct
               from forecast_evaluations
               where model_version = %s and horizon_months = %s
               order by evaluated_at desc limit 1""",
            (e["version"], e["horizon"]),
        ).fetchone()
        if row:
            lines.append(
                f"    {e['version']} @{e['horizon']}ด. n={e['n']} "
                f"ape={row['median_ape']}% cover={row['band_coverage']}% "
                f"bias={row['bias_pct']}%"
            )

    cands = promotion_candidates(conn)
    if cands:
        names = ", ".join(c["model_version"] for c in cands)
        lines.append(f"• มี challenger ผ่านเกณฑ์: {names} — รอคนตรวจและอนุมัติ")
    else:
        lines.append("• ไม่มี challenger ที่พร้อมเลื่อนขั้น")

    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(level="INFO", format="%(levelname)-7s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["monthly", "evaluate", "drift", "promote"])
    ap.add_argument("--version")
    ap.add_argument("--approved-by")
    args = ap.parse_args()

    with connect() as conn:
        if args.command == "monthly":
            print(monthly(conn))
        elif args.command == "evaluate":
            print(evaluate_all(conn))
        elif args.command == "drift":
            for r in check_drift(conn):
                print(r)
        elif args.command == "promote":
            if not args.version or not args.approved_by:
                ap.error("promote ต้องระบุ --version และ --approved-by")
            promote(conn, args.version, args.approved_by)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    raise SystemExit(main())
