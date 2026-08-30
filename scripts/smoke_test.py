#!/usr/bin/env python3
"""smoke_test.py — ยิงทุก GET route เช็คว่าไม่มี Internal Server Error (>=500) ก่อน deploy

รัน (จากโฟลเดอร์ ingest, เครือข่ายที่ต่อ DB ได้ — เหมือน led_results):
    python scripts/smoke_test.py

- Auto-discover ทุก GET route ที่ไม่มี path param (รวม /upcoming, /auction-results, admin ฯลฯ)
- เพิ่ม curated URL: หน้าที่มี query filter + หน้า /p/led_auction/{ref} (ดึง ref จริงจาก DB)
- ผ่าน = ทุก route คืน < 500 · exit code 0 = ผ่านหมด, 1 = มี route พัง (ใช้เป็น gate ก่อน git push ได้)

หมายเหตุ: 4xx (เช่น 401/403/404 ของหน้า admin/auth ที่ไม่ได้ล็อกอิน) ถือว่า "ไม่พัง"
เพราะเป็นพฤติกรรมปกติ — เราจับเฉพาะ 500 ที่เป็นบั๊กจริง
"""
from __future__ import annotations

import pathlib
import sys
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent.parent          # ingest/
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient                       # noqa: E402
import web                                                      # noqa: E402


def _sample_led_ref() -> str | None:
    try:
        from core.db import connect
        with connect() as c:
            r = c.execute("select external_ref from listing_snapshots "
                          "where source_code='led_auction' limit 1").fetchone()
        return r["external_ref"] if r else None
    except Exception:                                           # noqa: BLE001
        return None


def _curated() -> list[str]:
    urls = [
        "/upcoming",
        "/upcoming?round=5",
        "/upcoming?ptype=condo",
        "/upcoming?province=" + urllib.parse.quote("กรุงเทพมหานคร"),
        "/upcoming?date=2026-09-03",
        "/auction-results",
        "/auction-results?result=sold",
        "/auction-results?result=nobid",
        "/auction-results?round=5",
        "/auction-results?date=2026-08-15",
        "/auction-stats",
        "/auction-stats?days=30",
        "/auction-stats?days=90",
    ]
    ref = _sample_led_ref()
    if ref:
        urls.append("/p/led_auction/" + urllib.parse.quote(ref, safe=""))
    return urls


# route ที่ขึ้นกับ config ภายนอก (OAuth) — พฤติกรรมต่างกันตาม env จึงข้าม
# บนเครื่อง dev ที่ไม่ได้ตั้ง LINE/Google Login จะคืน 503 โดยตั้งใจ ไม่ใช่บั๊ก
SKIP_PREFIXES = ("/auth/",)


def _discover_get_routes() -> list[str]:
    out = []
    for r in web.app.routes:
        methods = getattr(r, "methods", None) or set()
        path = getattr(r, "path", "") or ""
        if "GET" not in methods:
            continue
        if "{" in path:                # มี path param — ใช้ curated แทน
            continue
        if any(path.startswith(p) for p in SKIP_PREFIXES):
            continue
        out.append(path)
    return out


def main() -> int:
    client = TestClient(web.app, raise_server_exceptions=False)
    urls = list(dict.fromkeys(_discover_get_routes() + _curated()))
    fails: list[tuple] = []
    ok = 0
    print(f"ยิง {len(urls)} route...\n")
    for u in urls:
        try:
            resp = client.get(u, follow_redirects=False)
            code = resp.status_code
        except Exception as exc:                               # noqa: BLE001
            fails.append((u, "EXC", str(exc)[:160]))
            print(f"  [EXC ] {u} — {str(exc)[:100]}")
            continue
        if code >= 500:
            body = resp.text[:200].replace("\n", " ")
            fails.append((u, code, body))
            print(f"  [{code} ] ✗ {u}")
        else:
            ok += 1
            print(f"  [{code} ]   {u}")
    print(f"\nสรุป: ผ่าน {ok} · พัง {len(fails)}")
    for u, c, e in fails:
        print(f"  ✗ {u} -> {c}")
        if e:
            print(f"      {e}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
