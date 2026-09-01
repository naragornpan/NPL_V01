#!/usr/bin/env python3
"""Runner — จุดเข้าเดียวของทุก adapter

    python src/run.py led_auction                # ดึงจริง
    python src/run.py led_auction --dry-run      # ดึงแล้วพิมพ์ ไม่เขียน DB
    python src/run.py led_auction --reparse      # ไม่ดึงใหม่ ใช้ raw เดิม parse ซ้ำ

--reparse คือเหตุผลที่เราเก็บ raw ไว้: พอ parser เก่งขึ้น
ย้อนกลับไปสกัดข้อมูลจากของเดิมได้โดยไม่ต้องรบกวนเว็บต้นทางอีก
"""
from __future__ import annotations

import argparse
import logging
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from core import env as _env  # noqa: E402,F401  (โหลด .env ก่อนใครเพื่อน)

from adapters.bam import BamAdapter  # noqa: E402
from adapters.ktb import KtbAdapter  # noqa: E402
from adapters.sam import SamAdapter  # noqa: E402
from adapters.ghb import GhbAdapter  # noqa: E402
from adapters.ttb import TtbAdapter  # noqa: E402
from adapters.led_auction import LedAuctionAdapter  # noqa: E402
from adapters.krungsri import KrungsriAdapter  # noqa: E402
from adapters.gsb import GsbAdapter  # noqa: E402
from core.db import Repo, connect  # noqa: E402
from core.http import Fetcher  # noqa: E402
from core.zones import (estimated_runtime_minutes, office_filter_for_tier,  # noqa: E402
                        provinces_for_tier)

ADAPTERS = {
    BamAdapter.source_code: BamAdapter,
    KtbAdapter.source_code: KtbAdapter,
    GhbAdapter.source_code: GhbAdapter,
    TtbAdapter.source_code: TtbAdapter,
    LedAuctionAdapter.source_code: LedAuctionAdapter,
    KrungsriAdapter.source_code: KrungsriAdapter,
    GsbAdapter.source_code: GsbAdapter,
    SamAdapter.source_code: SamAdapter,
    # เพิ่ม adapter ใหม่ที่นี่
}

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("run")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", choices=sorted(ADAPTERS))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reparse", action="store_true")
    ap.add_argument("--tier", type=int, default=int(os.environ.get("TARGET_TIER", "1")),
                    help="1=กทม+ปริมณฑล 2=+EEC 3=+หัวเมืองภาค")
    ap.add_argument("--provinces", default=os.environ.get("TARGET_PROVINCES", ""),
                    help="ระบุเองเพื่อ override tier")
    ap.add_argument("--all-offices", action="store_true",
                    help="LED: ดึงทุกสำนักงานทั่วประเทศ (ไม่กรอง tier) — ปริมาณมาก "
                         "เหมาะกับ backfill/กวาดเต็ม ควบคู่กับ --days-ahead ที่พอเหมาะ")
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--full", action="store_true",
                    help="ไล่ครบทุกหน้า ไม่หยุดแม้ไม่เจอของใหม่ (ใช้ตอนกวาดเต็มรอบ)")
    ap.add_argument("--days-ahead", type=int,
                    help="LED: ดึงล่วงหน้ากี่วัน (ค่าเริ่มต้น adapter = 45) "
                         "ใส่ 0 = วันนี้อย่างเดียว เหมาะกับทดสอบ")
    ap.add_argument("--days-back", type=int,
                    help="LED: ดึงย้อนหลังกี่วัน (ค่าเริ่มต้น adapter = 3)")
    args = ap.parse_args()

    provinces = [p.strip() for p in args.provinces.split(",") if p.strip()]
    if not provinces:
        provinces = provinces_for_tier(args.tier)
    # office_filter ว่าง ([]) = adapter LED ดึง "ทุกสำนักงาน" (follow_up ไม่กรอง)
    office_filter = [] if args.all_offices else office_filter_for_tier(args.tier)
    config = {"provinces": provinces, "max_pages": args.max_pages,
              "office_filter": office_filter}
    # ส่งช่วงวันให้ LED (adapter อื่นไม่สนใจคีย์นี้)
    if args.days_ahead is not None:
        config["days_ahead"] = args.days_ahead
    if args.days_back is not None:
        config["days_back"] = args.days_back

    if args.all_offices:
        log.info("โหมด --all-offices | ดึงทุกสำนักงานทั่วประเทศ (ปริมาณมาก) "
                 "| days_ahead=%s days_back=%s", config.get("days_ahead", 45),
                 config.get("days_back", 3))
    est = estimated_runtime_minutes(args.tier, args.max_pages, 3.0)
    if not args.all_offices:
        log.info("tier %s | %s จังหวัด | ประเมินเวลารัน ~%.0f นาที",
                 args.tier, len(provinces), est)
    if est > 25 and not args.all_offices:
        log.warning("เกิน timeout ของ GitHub Actions (30 นาที) — "
                    "ลด --max-pages หรือแตก workflow เป็นหลาย job")

    with connect() as conn:
        repo = Repo(conn)
        source = repo.get_source(args.source)
        fetcher = Fetcher(
            encoding=source["encoding"],
            rate_limit_s=float(source["rate_limit_s"]),
        )
        adapter = ADAPTERS[args.source](fetcher, config)

        run_id = repo.start_run(args.source)
        stats = {"pages_fetched": 0, "rows_parsed": 0, "rows_new": 0, "error_count": 0}
        error_sample = None

        try:
            queue = list(adapter.discover())
            stale_streak: dict[str, int] = {}   # นับหน้าที่ไม่เจอของใหม่ แยกตามกลุ่ม
            skipped_pages = 0

            while queue:
                task = queue.pop(0)

                # ข้ามหน้าถัดไปของกลุ่มที่ไม่เจอของใหม่ติดกันแล้ว
                # ประหยัดเวลามาก เพราะรันซ้ำทุกวันจะเจอของเดิมเป็นส่วนใหญ่
                group = task.get("meta", {}).get("province") or task.get("meta", {}).get("office_name")
                limit = getattr(adapter, "STOP_AFTER_STALE_PAGES", 0)
                if (limit and group and stale_streak.get(group, 0) >= limit
                        and not args.full):
                    skipped_pages += 1
                    continue
                try:
                    resp = adapter.fetch(task)
                except Exception as exc:                       # noqa: BLE001
                    stats["error_count"] += 1
                    error_sample = error_sample or f"fetch: {exc}"
                    log.warning("ดึงไม่สำเร็จ %s — %s", task.get("url"), exc)
                    continue

                stats["pages_fetched"] += 1
                raw_id = None if args.dry_run else repo.save_raw(args.source, run_id, resp)

                # adapter บางตัวต้องเดินสองขั้น (หน้าสรุป -> หน้ารายการ)
                if hasattr(adapter, "follow_up"):
                    extra = list(adapter.follow_up(resp, task))
                    if extra:
                        log.info("พบงานต่อเนื่อง %s รายการจาก %s",
                                 len(extra), task["meta"].get("bid_date"))
                        queue.extend(extra)

                rows = list(adapter.parse(resp, task))
                page_new = 0
                if not rows:
                    continue

                for fields in rows:
                    stats["rows_parsed"] += 1
                    try:
                        snap = adapter.to_snapshot(fields, raw_id)
                    except Exception as exc:                   # noqa: BLE001
                        stats["error_count"] += 1
                        error_sample = error_sample or f"parse: {exc}"
                        if not args.dry_run:
                            repo.log_parse_failure(run_id, raw_id, str(exc))
                        continue

                    if args.dry_run:
                        log.info("DRY %s | %s | %s",
                                 snap.get("external_ref"), snap.get("property_type"),
                                 snap.get("opening_price"))
                    elif repo.save_snapshot(snap):
                        stats["rows_new"] += 1
                        page_new += 1

                # อัปเดตสถิติ "หน้านี้ได้ของใหม่ไหม"
                if group:
                    if page_new == 0 and not args.dry_run:
                        stale_streak[group] = stale_streak.get(group, 0) + 1
                    else:
                        stale_streak[group] = 0

            if skipped_pages:
                log.info("ข้าม %s หน้าที่ไม่น่าจะมีของใหม่ (ใช้ --full เพื่อไล่ครบทุกหน้า)",
                         skipped_pages)
            status = "ok" if stats["error_count"] == 0 else "partial"
        except Exception as exc:                               # noqa: BLE001
            log.exception("รันล้มเหลว")
            status, error_sample = "failed", str(exc)

        if not args.dry_run:
            repo.finish_run(run_id, status, error_sample=error_sample, **stats)
            purged = repo.purge_expired_raw()
            log.info("ลบ raw ที่หมดอายุ %s รายการ", purged)

        mb = fetcher.bytes_downloaded / 1024 / 1024
        log.info("เสร็จ [%s] %s | ใช้ดาต้า %.1f MB", status, stats, mb)
        return 0 if status != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
