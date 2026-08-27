#!/usr/bin/env python3
"""เว็บแสดงผลทรัพย์ — รันบนเครื่องตัวเอง

    python src/web.py

ถ้ายังไม่ตั้ง DATABASE_URL จะเข้าโหมดตัวอย่างอัตโนมัติ

โหมดการแสดงรูป
    PUBLIC_MODE=1  แสดงเฉพาะรูปที่มีสิทธิ์เผยแพร่ (สำหรับตอนเปิดให้คนอื่นดู)
    ค่าปกติ        แสดงทุกรูป (ใช้ภายในเพื่อวิเคราะห์)
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import sys
from datetime import date, timedelta
from urllib.parse import quote

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from core import env as _env  # noqa: E402,F401  (โหลด .env ก่อนใครเพื่อน)

from fastapi import FastAPI, HTTPException, Query, Request  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from jinja2 import DictLoader, Environment  # noqa: E402

from core import settings as st  # noqa: E402
from core.gallery import fetch_bam_detail  # noqa: E402
from core.grading import GRADE_STYLE  # noqa: E402
from core.images import hidden_image_count, resolve_images  # noqa: E402
from core.source_links import all_links  # noqa: E402

NO_DB = not os.environ.get("DATABASE_URL")
DEMO_MODE = os.environ.get("DEMO_MODE") == "1" or NO_DB
PUBLIC_MODE = os.environ.get("PUBLIC_MODE") == "1"
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
# รหัสผ่านสำหรับ login หลังบ้าน (ถ้าไม่ตั้ง ใช้ ADMIN_TOKEN แทน เพื่อความเข้ากันได้)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "") or ADMIN_TOKEN
# กุญแจเซ็น cookie — ตั้ง SECRET_KEY บน production; ถ้าไม่ตั้ง อนุมานจาก token
SECRET_KEY = os.environ.get("SECRET_KEY") or ADMIN_TOKEN or "dev-insecure-key"
# URL ฐานสำหรับลิงก์ absolute (OG/แชร์) — เช่น https://plaengdee.onrender.com
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
SESSION_COOKIE = "npa_admin"
SESSION_DAYS = 7
# Google Analytics 4 — ตั้ง GA_MEASUREMENT_ID (เช่น G-XXXXXXX) บน production เพื่อเปิดใช้
GA_ID = os.environ.get("GA_MEASUREMENT_ID", "").strip()
# LINE Login (ผู้ใช้ทั่วไป — ทรัพย์โปรด/แจ้งเตือน) — ตั้งค่าจาก LINE Developers Console
LINE_LOGIN_CHANNEL_ID = os.environ.get("LINE_LOGIN_CHANNEL_ID", "").strip()
LINE_LOGIN_CHANNEL_SECRET = os.environ.get("LINE_LOGIN_CHANNEL_SECRET", "").strip()
LINE_LOGIN_ENABLED = bool(LINE_LOGIN_CHANNEL_ID and LINE_LOGIN_CHANNEL_SECRET)
USER_COOKIE = "npa_user"        # cookie session ของผู้ใช้ LINE (แยกจาก admin)

log = logging.getLogger("web")
app = FastAPI(title="แปลงดี — NPA Deal Finder")


# ---------------------------------------------------------------------
# Auth หลังบ้าน — cookie เซ็นด้วย HMAC (ไม่พึ่ง lib เพิ่ม)
# แทน token ใน URL (ซึ่งรั่วผ่าน log/referrer/ประวัติ)
# ยังรับ ?token=ADMIN_TOKEN ได้อยู่ เพื่อความเข้ากันได้กับลิงก์เดิม
# ---------------------------------------------------------------------
def _sign(msg: str) -> str:
    import hashlib
    import hmac
    return hmac.new(SECRET_KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]


def make_session_cookie() -> str:
    import time
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    return f"{exp}.{_sign(exp)}"


def _valid_cookie(value: str | None) -> bool:
    import hmac
    import time
    if not value or "." not in value:
        return False
    exp, sig = value.split(".", 1)
    if not hmac.compare_digest(sig, _sign(exp)):
        return False
    try:
        return int(exp) > int(time.time())
    except ValueError:
        return False


def admin_ok(request: "Request", token: str = "") -> bool:
    """True ถ้าล็อกอินแล้ว (cookie) หรือส่ง token ถูก (เข้ากันได้กับลิงก์เดิม)"""
    if _valid_cookie(request.cookies.get(SESSION_COOKIE)):
        return True
    return bool(ADMIN_TOKEN) and token == ADMIN_TOKEN


def guard(request: "Request", token: str = ""):
    """คืน RedirectResponse ไปหน้า login ถ้ายังไม่ได้สิทธิ์ admin, ไม่งั้นคืน None"""
    if admin_ok(request, token):
        return None
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/admin/login", status_code=303)


# ---------------------------------------------------------------------
# Session ผู้ใช้ LINE (แยกจาก admin) — cookie เซ็น HMAC เช่นกัน
# ค่าใน cookie = "<line_user_id>|<exp>.<sig>"
# ---------------------------------------------------------------------
def make_user_cookie(uid: str) -> str:
    import time
    exp = str(int(time.time()) + 30 * 86400)          # อยู่ได้ 30 วัน
    payload = f"{uid}|{exp}"
    return f"{payload}.{_sign(payload)}"


def current_user(request: "Request") -> str | None:
    """คืน line_user_id ถ้า cookie ผู้ใช้ยังใช้ได้ ไม่งั้น None"""
    import hmac
    import time
    v = request.cookies.get(USER_COOKIE)
    if not v or "." not in v:
        return None
    payload, sig = v.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(payload)):
        return None
    uid, _, exp = payload.partition("|")
    try:
        if not uid or int(exp) < int(time.time()):
            return None
    except ValueError:
        return None
    return uid


def user_fav_pairs(uid: str | None) -> set:
    """เซ็ตของ (source_code, external_ref) ที่ผู้ใช้กดโปรด — ใช้ทำหัวใจ/กรอง"""
    if not uid or DEMO_MODE:
        return set()
    try:
        from core.db import connect
        with connect() as conn:
            return {(r["source_code"], r["external_ref"]) for r in conn.execute(
                "select source_code, external_ref from user_favorites "
                "where line_user_id=%s", (uid,)).fetchall()}
    except Exception as exc:                                        # noqa: BLE001
        log.warning("โหลดทรัพย์โปรดไม่สำเร็จ (รัน migration 037?): %s", str(exc)[:100])
        return set()


def _abs_url(request: "Request", path_or_url: str | None) -> str | None:
    """ทำให้เป็น URL แบบเต็ม (absolute) สำหรับ OG/แชร์ — data: ไม่เอา"""
    if not path_or_url or path_or_url.startswith("data:"):
        return None
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    base = BASE_URL or str(request.base_url).rstrip("/")
    return base + (path_or_url if path_or_url.startswith("/") else "/" + path_or_url)


def _jsonld(obj) -> str:
    """แปลงเป็นสตริง JSON-LD ที่ฝังใน <script> ได้ปลอดภัย (กัน </script> injection)"""
    return json.dumps(obj, ensure_ascii=False).replace("<", "\\u003c")


@app.exception_handler(RuntimeError)
async def db_error_handler(request: Request, exc: RuntimeError):
    """หน้า error ที่เป็นมิตรเมื่อต่อฐานข้อมูลไม่ได้

    เจอบ่อยตอน Supabase pooler ไม่เสถียรชั่วขณะ (หลุดกลางเซสชัน)
    ซึ่งแก้ไม่ได้ด้วยการตั้งค่าใหม่ — ลองซ้ำมักจะผ่าน
    แสดงหน้านี้แทน traceback ดิบที่อ่านไม่รู้เรื่องสำหรับคนทั่วไป
    """
    from fastapi.responses import HTMLResponse
    msg = str(exc)
    is_db = any(k in msg for k in ("ฐานข้อมูล", "DNS", "timeout", "Supabase"))
    if not is_db:
        raise exc
    return HTMLResponse(status_code=503, content=f"""
<!doctype html><html lang="th"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>เชื่อมต่อฐานข้อมูลไม่ได้ชั่วคราว · แปลงดี</title></head>
<body style="font-family:-apple-system,'Segoe UI','Noto Sans Thai',sans-serif;
             background:#FBFAF7;color:#0F2434;display:flex;min-height:100vh;
             align-items:center;justify-content:center;margin:0;padding:20px">
  <div style="max-width:480px;background:#fff;border:1px solid #DCE2E5;
              border-radius:12px;padding:32px;text-align:center">
    <div style="font-size:15px;font-weight:600;margin-bottom:10px">
      เชื่อมต่อฐานข้อมูลไม่ได้ชั่วคราว</div>
    <p style="font-size:13px;color:#64757F;line-height:1.6;margin:0 0 18px">
      มักเกิดจาก Supabase (ผู้ให้บริการฐานข้อมูล) ไม่เสถียรชั่วขณะ<br>
      ไม่ใช่ปัญหาจากการตั้งค่าของคุณ — ลองใหม่อีกครั้งมักจะผ่าน</p>
    <a href="javascript:location.reload()"
       style="display:inline-block;background:#0F2434;color:#fff;
              padding:9px 22px;border-radius:8px;font-size:13px;
              text-decoration:none">ลองใหม่</a>
    <p style="font-size:11px;color:#94A3B0;margin-top:20px">
      รายละเอียด: {msg[:150]}</p>
  </div>
</body></html>""")


_static = pathlib.Path(__file__).resolve().parents[1] / "static"
if _static.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static)), name="static")

TYPE_LABELS = {
    "land": "ที่ดิน", "house": "บ้านเดี่ยว", "townhouse": "ทาวน์เฮาส์",
    "condo": "ห้องชุด", "commercial": "อาคารพาณิชย์",
    "industrial": "โรงงาน/โกดัง", "movable": "สังหาริมทรัพย์",
    "common_area": "พื้นที่ส่วนกลาง", "special": "ทรัพย์เฉพาะทาง",
    "other": "อื่น ๆ",
}

SEVERITY_STYLE = {
    "critical": ("bg-red-50 text-red-800 border-red-200", "อันตราย"),
    "caution": ("bg-amber-50 text-amber-800 border-amber-200", "ระวัง"),
    "info": ("bg-slate-50 text-slate-700 border-slate-200", "ข้อมูล"),
    "positive": ("bg-emerald-50 text-emerald-800 border-emerald-200", "บวก"),
}


# ---------------------------------------------------------------------
DEMO_ROWS = [
    {
        "external_ref": "LED-2569-00412", "source_code": "led_auction",
        "province": "นนทบุรี", "district": "บางบัวทอง", "subdistrict": "โสนลอย",
        "property_type": "townhouse", "land_area_sqwa": 21.5, "usable_area_sqm": 96,
        "opening_price": 1_180_000, "appraised_price": 1_950_000,
        "auction_round": 3, "auction_date": date.today() + timedelta(days=18),
        "office_name": "สนง.บังคับคดีนนทบุรี", "mortgage_carried": False,
        "occupancy_note": "มีผู้อยู่อาศัย", "lat": 13.9160, "lng": 100.4210,
        "deal_score": 72, "bedrooms": 3, "bathrooms": 2, "images": [],
        "institution_name": "กรมบังคับคดี", "institution_kind": "government",
        "grade": "B", "grade_score": 99.8,
        "flags": [
            {"code": "PRICE_BELOW_GOV", "severity": "positive",
             "evidence": "ราคาเปิดต่ำกว่าราคาประเมิน 39%"},
            {"code": "MULTI_ROUND", "severity": "info",
             "evidence": "นัดขายครั้งที่ 3 ราคาลดจากนัดแรก 21%"},
            {"code": "OCCUPIED", "severity": "caution",
             "evidence": "ประกาศระบุว่ามีผู้อยู่อาศัย ต้องเผื่อเวลาและค่าใช้จ่ายขับไล่"},
            {"code": "TRANSIT_PROXIMITY", "severity": "positive",
             "evidence": "ห่างสถานีสายสีชมพู ประมาณ 780 ม."},
        ],
        "forecast": {"horizon_months": 60, "bear": 1_420_000, "mid": 1_680_000,
                     "bull": 2_050_000, "confidence": "medium",
                     "confidence_reason": "อ้างอิงจากเหตุการณ์เทียบเคียง 14 รายการ"},
    },
    {
        "external_ref": "LED-2569-00518", "source_code": "led_auction",
        "province": "กรุงเทพมหานคร", "district": "ทุ่งครุ", "subdistrict": "บางมด",
        "property_type": "land", "land_area_sqwa": 68.0, "usable_area_sqm": None,
        "opening_price": 2_400_000, "appraised_price": 3_100_000,
        "auction_round": 1, "auction_date": date.today() + timedelta(days=31),
        "office_name": "สนง.บังคับคดีแพ่งกรุงเทพ 3", "mortgage_carried": True,
        "occupancy_note": "ว่าง", "lat": 13.6480, "lng": 100.4980,
        "deal_score": 34, "bedrooms": None, "bathrooms": None, "images": [],
        "institution_name": "กรมบังคับคดี", "institution_kind": "government",
        "grade": "E", "grade_score": 43.3,
        "flags": [
            {"code": "EXPROPRIATION_RISK", "severity": "critical",
             "evidence": "อยู่ในแนวเขตเวนคืนโครงการสายสีม่วงใต้ ต้องตรวจแผนที่ท้าย พ.ร.ฎ."},
            {"code": "NO_ACCESS_ROAD", "severity": "critical",
             "evidence": "ไม่พบทางเข้าออกสาธารณะจากผังแปลง"},
        ],
        "forecast": None,
    },
    {
        "external_ref": "BAM-NPA-77120", "source_code": "bam",
        "province": "สมุทรปราการ", "district": "บางพลี", "subdistrict": "บางแก้ว",
        "property_type": "condo", "land_area_sqwa": None, "usable_area_sqm": 34,
        "opening_price": 890_000, "appraised_price": 1_150_000,
        "auction_round": None, "auction_date": None,
        "office_name": "BAM สาขาสมุทรปราการ", "mortgage_carried": False,
        "occupancy_note": "ว่าง", "lat": 13.6420, "lng": 100.6810,
        "deal_score": 58, "bedrooms": 1, "bathrooms": 1,
        "institution_name": "BAM", "institution_kind": "amc",
        "grade": "C", "grade_score": 53.0,
        "images": [
            {"origin_url": None, "usage_scope": "internal_only",
             "caption": "ตัวอย่างรูปจากแหล่งภายนอก", "attribution": "BAM"},
        ],
        "flags": [
            {"code": "CONDO_FEE_RISK", "severity": "caution",
             "evidence": "อาคารอายุเกิน 15 ปี ต้องเช็คยอดค่าส่วนกลางค้างกับนิติก่อนเคาะ"},
            {"code": "APPRAISAL_LAG", "severity": "positive",
             "evidence": "ราคาตลาดในเขตวิ่งนำราคาประเมินราชการ 18 จุด"},
        ],
        "forecast": {"horizon_months": 60, "bear": 940_000, "mid": 1_090_000,
                     "bull": 1_310_000, "confidence": "low",
                     "confidence_reason": "เหตุการณ์เทียบเคียงยังน้อยกว่า 10 รายการ"},
    },
    {
        "external_ref": "KTB-NPA-40218", "source_code": "ktb",
        "province": "ปทุมธานี", "district": "ลำลูกกา", "subdistrict": "บึงคำพร้อย",
        "property_type": "house", "land_area_sqwa": 54.1, "usable_area_sqm": 125,
        "opening_price": 2_980_000, "appraised_price": 3_600_000,
        "auction_round": None, "auction_date": None,
        "office_name": "กรุงไทย", "mortgage_carried": False,
        "occupancy_note": "ว่าง", "lat": 13.9880, "lng": 100.7420,
        "deal_score": 66, "bedrooms": 3, "bathrooms": 2, "images": [],
        "institution_name": "กรุงไทย", "institution_kind": "bank",
        "grade": "B", "grade_score": 71.2,
        "flags": [{"code": "PRICE_BELOW_GOV", "severity": "positive",
                   "evidence": "ต่ำกว่าราคาประเมิน 17%"}],
        "forecast": None,
    },
    {
        "external_ref": "GSB-NPA-11907", "source_code": "gsb",
        "province": "กรุงเทพมหานคร", "district": "หนองจอก", "subdistrict": "กระทุ่มราย",
        "property_type": "land", "land_area_sqwa": 210.0, "usable_area_sqm": None,
        "opening_price": 1_650_000, "appraised_price": None,
        "auction_round": None, "auction_date": None,
        "office_name": "ออมสิน", "mortgage_carried": False,
        "occupancy_note": None, "lat": None, "lng": None,
        "deal_score": None, "bedrooms": None, "bathrooms": None, "images": [],
        "institution_name": "ออมสิน", "institution_kind": "bank",
        "grade": None, "grade_score": None,
        "flags": [], "forecast": None,
    },
]


DEMO_COMPS = [
    {"province": "นนทบุรี", "district": "บางบัวทอง", "property_type": "townhouse",
     "auction_median": 1_240_000, "n_auction": 11, "rows": [
        {"source_label": "เว็บประกาศขาย A", "price_kind": "asking",
         "market_median": 2_150_000, "n_listings": 84, "days": 6, "freshness": "สด"},
        {"source_label": "เว็บประกาศขาย B", "price_kind": "asking",
         "market_median": 2_290_000, "n_listings": 51, "days": 21, "freshness": "เริ่มเก่า"},
        {"source_label": "REIC โอนกรรมสิทธิ์", "price_kind": "closed",
         "market_median": 1_880_000, "n_listings": 402, "days": 38, "freshness": "เริ่มเก่า"},
     ]},
    {"province": "สมุทรปราการ", "district": "บางพลี", "property_type": "condo",
     "auction_median": 905_000, "n_auction": 7, "rows": [
        {"source_label": "เว็บประกาศขาย A", "price_kind": "asking",
         "market_median": 1_320_000, "n_listings": 137, "days": 9, "freshness": "สด"},
        {"source_label": "เว็บประกาศขาย C", "price_kind": "asking",
         "market_median": 1_395_000, "n_listings": 62, "days": 63, "freshness": "เก่าเกินไป"},
     ]},
]
DEMO_HOT_PROPS = [
    {"external_ref": "LED-2569-00412", "source_code": "led_auction",
     "province": "นนทบุรี", "district": "บางบัวทอง", "property_type": "townhouse",
     "opening_price": 1_180_000, "sessions": 148, "views": 231,
     "saves": 22, "inquiries": 9, "source_clicks": 41, "interest_score": 597},
    {"external_ref": "BAM-NPA-77120", "source_code": "bam",
     "province": "สมุทรปราการ", "district": "บางพลี", "property_type": "condo",
     "opening_price": 890_000, "sessions": 96, "views": 140,
     "saves": 14, "inquiries": 5, "source_clicks": 18, "interest_score": 387},
    {"external_ref": "LED-2569-00518", "source_code": "led_auction",
     "province": "กรุงเทพมหานคร", "district": "ทุ่งครุ", "property_type": "land",
     "opening_price": 2_400_000, "sessions": 61, "views": 88,
     "saves": 3, "inquiries": 0, "source_clicks": 7, "interest_score": 106},
]
DEMO_HOT_ZONES = [
    {"province": "นนทบุรี", "district": "บางบัวทอง", "sessions": 210,
     "views": 361, "inquiries": 14, "listings": 12, "demand_supply_ratio": 17.5},
    {"province": "สมุทรปราการ", "district": "บางพลี", "sessions": 158,
     "views": 240, "inquiries": 8, "listings": 31, "demand_supply_ratio": 5.1},
    {"province": "กรุงเทพมหานคร", "district": "ทุ่งครุ", "sessions": 74,
     "views": 108, "inquiries": 1, "listings": 26, "demand_supply_ratio": 2.8},
    {"province": "ปทุมธานี", "district": "ลำลูกกา", "sessions": 66,
     "views": 91, "inquiries": 4, "listings": 3, "demand_supply_ratio": 22.0},
]
DEMO_HEALTH = [
    {"code": "led_auction", "name": "กรมบังคับคดี - ประกาศขายทอดตลาด",
     "verdict": "ปกติ", "hours_since_run": 6, "new_7d": 84, "runs_7d": 7,
     "failed_7d": 0, "rows_parsed": 120, "error_count": 0, "error_sample": None},
    {"code": "led_result", "name": "กรมบังคับคดี - รายงานผลการขาย",
     "verdict": "รันผ่านแต่ไม่ได้ข้อมูลเลย", "hours_since_run": 7, "new_7d": 0,
     "runs_7d": 7, "failed_7d": 0, "rows_parsed": 0, "error_count": 0,
     "error_sample": "parse ได้ 0 แถวติดกัน 7 วัน — เว็บอาจเปลี่ยนโครงสร้าง"},
    {"code": "bam", "name": "BAM - ทรัพย์ NPA", "verdict": "เลยกำหนด",
     "hours_since_run": 31, "new_7d": 12, "runs_7d": 5, "failed_7d": 1,
     "rows_parsed": 40, "error_count": 2, "error_sample": "timeout 2 หน้า"},
]
DEMO_TRAFFIC = [{"day": f"2026-08-{d:02d}", "sessions": v, "inquiries": i}
                for d, v, i in [(17, 41, 1), (18, 58, 2), (19, 66, 3),
                                (20, 52, 1), (21, 79, 4), (22, 94, 5), (23, 88, 3)]]

HAIRCUT_PCT = 8.0
HAIRCUT_BASIS = "ค่าตั้งต้นสมมติ — ยังไม่ได้สอบเทียบกับดีลจริง"


def compute_gaps(block: dict) -> dict:
    """คำนวณส่วนต่างทั้งแบบดิบและแบบปรับส่วนลดต่อรอง

    ราคาตั้งขายต้องหักส่วนลดต่อรองก่อนเทียบ ไม่งั้นส่วนต่างจะดูใหญ่เกินจริง
    ราคาปิด (closed) ไม่ต้องหัก เพราะเป็นเงินจริงอยู่แล้ว
    """
    out = dict(block, rows=[])
    a = block.get("auction_median")
    for row in block["rows"]:
        m = row["market_median"]
        r = dict(row)
        r["raw_gap"] = round((1 - a / m) * 100, 1) if a and m else None
        if row["price_kind"] == "asking" and a and m:
            adj = m * (1 - HAIRCUT_PCT / 100)
            r["adj_gap"] = round((1 - a / adj) * 100, 1)
            r["adj_note"] = f"หักส่วนลดต่อรอง {HAIRCUT_PCT:.0f}%"
        else:
            r["adj_gap"] = r["raw_gap"]
            r["adj_note"] = "ราคาปิดจริง ไม่ต้องหัก"
        out["rows"].append(r)
    return out


def load_comps(province=None, district=None, ptype=None) -> list[dict]:
    if DEMO_MODE:
        blocks = DEMO_COMPS
    else:
        from core.db import connect
        with connect() as conn:
            rows = [dict(x) for x in conn.execute(
                "select * from v_price_gap order by province, district, property_type"
            ).fetchall()]
        grouped: dict[tuple, dict] = {}
        for x in rows:
            key = (x["province"], x["district"], x["property_type"])
            g = grouped.setdefault(key, {
                "province": x["province"], "district": x["district"],
                "property_type": x["property_type"],
                "auction_median": x["auction_median"], "n_auction": x["n_auction"],
                "rows": []})
            g["rows"].append({
                "source_label": x["source_label"], "price_kind": x["price_kind"],
                "market_median": x["market_median"], "n_listings": x["n_listings"],
                "days": x["days_since_update"], "freshness": x["freshness"],
                "search_url": x.get("search_url")})
        blocks = list(grouped.values())

    def keep(b):
        return ((not province or b["province"] == province)
                and (not district or b["district"] == district)
                and (not ptype or b["property_type"] == ptype))

    return [compute_gaps(b) for b in blocks if keep(b)]


# ---------------------------------------------------------------------
def enrich(r: dict, settings: dict | None = None, is_admin: bool = False) -> dict:
    settings = settings or st.DEFAULTS
    r["links"] = all_links(r)

    # ลิงก์ไปทรัพย์ต้นทาง — admin เห็นเสมอ ผู้ใช้ทั่วไปขึ้นกับการตั้งค่า
    # เหตุผลที่ปิดเป็นค่าเริ่มต้น: ผู้ซื้ออาจติดต่อสถาบันเองแล้วเราไม่ได้ค่าคอม
    allowed = is_admin or st.link_allowed(settings, r.get("allow_source_link"))
    r["source_link_visible"] = allowed
    # เก็บ URL ต้นทางไว้ใช้ภายในเสมอ (ดึงแกลเลอรี ตรวจสอบ)
    # แยกจาก detail_url ที่อาจถูกซ่อนจากผู้ใช้
    r["_source_url"] = r.get("detail_url")
    if not allowed:
        # ซ่อนเฉพาะลิงก์ที่พาไปหาเจ้าของทรัพย์ ลิงก์แผนที่ยังต้องอยู่
        r["links"] = [l for l in r["links"] if l.get("kind") != "source"]
        r["detail_url"] = None
    if not is_admin and not st.as_bool(settings, "show_institution_name"):
        r["institution_name"] = None
    r["show_market_code"] = is_admin or st.as_bool(settings, "show_market_code")
    # รูปจากต้นทางใช้แสดงได้เฉพาะโหมดใช้ภายใน
    # โหมดเผยแพร่ต้องใช้รูปที่เราถ่ายเองใน listing_images เท่านั้น
    if r.get("image_url") and not r.get("images"):
        r["images"] = [{"origin_url": r["image_url"], "cached_path": None,
                        "usage_scope": "internal_only", "caption": None,
                        "attribution": r.get("institution_name")}]
    r["images_view"] = resolve_images(r, PUBLIC_MODE)
    r["hidden_images"] = hidden_image_count(r, PUBLIC_MODE)
    r["discount_pct"] = (
        round((1 - r["opening_price"] / r["appraised_price"]) * 100)
        if r.get("opening_price") and r.get("appraised_price") else None
    )
    # ส่วนลดจากราคาตั้งขาย (ทรัพย์ธนาคารที่ไม่มีราคาประเมิน) -> badge "ลดแรง"
    _sp, _lp = r.get("special_price"), r.get("list_price")
    r["special_discount_pct"] = (
        round((1 - _sp / _lp) * 100) if _sp and _lp and _sp < _lp else None
    )
    area = r.get("land_area_sqwa")
    r["price_per_sqwa"] = (
        round(r["opening_price"] / area) if area and r.get("opening_price") else None
    )
    r["type_label"] = TYPE_LABELS.get(r.get("property_type"), "อื่น ๆ")
    r["title"] = (f"{r['type_label']} {r.get('subdistrict') or ''} "
                  f"{r.get('district') or ''} {r.get('province') or ''}").strip()
    r["has_critical"] = any(f.get("severity") == "critical" for f in r.get("flags", []))
    r["grade_style"], r["grade_label"] = GRADE_STYLE.get(r.get("grade"),
                                                         GRADE_STYLE[None])
    return r


async def read_form(request: Request) -> dict[str, str]:
    """อ่านข้อมูลจากฟอร์มโดยไม่พึ่ง python-multipart

    ฟอร์ม HTML ธรรมดาส่งมาเป็น application/x-www-form-urlencoded
    ซึ่ง parse เองได้ด้วย urllib ไม่ต้องลง library เพิ่ม

    เดิมใช้ request.form() ซึ่งบังคับให้ต้องมี python-multipart
    ถ้าไม่มีจะพังเฉพาะตอน POST ส่วน GET ยังปกติ ทำให้หาสาเหตุยาก
    """
    ctype = request.headers.get("content-type", "")
    body = await request.body()

    if "application/x-www-form-urlencoded" in ctype:
        from urllib.parse import parse_qs
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        return {k: v[-1] for k, v in parsed.items()}

    if "multipart/form-data" in ctype:
        try:
            form = await request.form()
            return {k: str(v) for k, v in form.items()}
        except Exception as exc:                              # noqa: BLE001
            log.error("อ่านฟอร์มแบบ multipart ไม่ได้: %s", exc)
            raise HTTPException(
                400, "อ่านข้อมูลฟอร์มไม่ได้ — ติดตั้ง python-multipart แล้วลองใหม่")

    return {}


def _num(value) -> float | None:
    """แปลงค่าจากฟอร์มเป็นตัวเลข — ช่องว่างหรือค่าที่ไม่ใช่ตัวเลขให้เป็น None"""
    if value in (None, "", "None"):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def current_settings() -> dict:
    if DEMO_MODE:
        return dict(st.DEFAULTS)
    from core.db import connect
    with connect() as conn:
        return st.load(conn)


GRADE_ORDER = {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1, None: 0}

# ประเภทที่ไม่ใช่อสังหาที่ซื้อไปอยู่หรือลงทุนได้ตามปกติ
# เก็บไว้ในฐานเสมอ แต่ซ่อนจากรายการหลักเพราะทำให้หาของจริงยากขึ้น
# ตัวอย่างจริงที่เจอ: เครื่องจักรโรงน้ำแข็ง · พื้นที่ส่วนกลางของหมู่บ้าน
SPECIAL_TYPES = {"movable", "common_area", "special"}

# รูปจากต้นทางใบละ 170-440 KB ถ้าโหลด 500 การ์ดพร้อมกันคือ 100+ MB
# ต่อการเปิดหนึ่งครั้ง จึงต้องแบ่งหน้าและใช้ lazy loading
PAGE_SIZE = 24


# subquery ระยะถึงสถานีรถไฟฟ้า/รถไฟ (จาก infra engine) — join แบบ USING กัน column ชนกัน
# ใช้ตารางจริง (property_links + property_infra_features จาก migration 002/030) ไม่พึ่ง view ใหม่
# LEFT JOIN: ทรัพย์ที่ยังไม่ถูกคำนวณ infra จะได้ระยะเป็น null (ไม่พังทั้ง query)
_TRANSIT_JOIN = (
    " left join (select pl.source_code, pl.external_ref,"
    " pif.nearest_station_m, pif.nearest_station_name"
    " from property_links pl"
    " join property_infra_features pif on pif.property_id = pl.property_id"
    " ) t using (source_code, external_ref) "
)


def load_rows(where: str = "", params: tuple = (), limit: int | None = None,
              offset: int = 0, order: str | None = None) -> list[dict]:
    """อ่านทรัพย์จากฐาน — กรองที่ SQL ไม่ใช่ในหน่วยความจำ

    เดิมดึงมา 500 แถวแล้วค่อยกรองใน Python ซึ่งผิด เพราะทรัพย์ที่เข้าเงื่อนไข
    แต่อยู่นอก 500 แถวแรกจะไม่มีวันปรากฏ พอข้อมูลโตเป็นหลักพันจึงเห็นปัญหาชัด
    """
    if DEMO_MODE:
        return [dict(r) for r in DEMO_ROWS]

    from core.db import connect
    default_order = ("""case grade when 'A' then 5 when 'B' then 4 when 'C' then 3
                             when 'D' then 2 when 'E' then 1 else 0 end desc,
                  score desc nulls last, opening_price asc""")
    # order รับเฉพาะสตริงคงที่จากในโค้ด (ไม่ใช่ค่าจากผู้ใช้) — ปลอดภัยจาก injection
    sql = f"""
        select source_code, external_ref, institution_code, institution_name,
               institution_kind, allow_source_link,
               title, detail_url, image_url, property_type,
               province, district, subdistrict, lat, lng, geo_precision,
               land_area_sqwa, usable_area_sqm, bedrooms, bathrooms, parking,
               opening_price, list_price, special_price, appraised_price,
               renovated, auction_date, auction_round, occupancy_note,
               grade, score as grade_score, completeness, reasons,
               recommend_score, first_seen, is_fresh,
               nearest_station_m, nearest_station_name
          from v_recommended {_TRANSIT_JOIN}
         {where}
         order by {order or default_order}
    """
    if limit is not None:
        sql += f" limit {int(limit)} offset {int(offset)}"

    with connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        if not rows:
            return rows

        srcs = [r["source_code"] for r in rows]
        refs = [r["external_ref"] for r in rows]
        images: dict[tuple, list] = {}
        img_rows = conn.execute(
            """select v.source_code, v.external_ref, v.origin_url, v.cached_path,
                      v.caption, v.attribution, v.usage_scope
               from v_primary_image v
               join unnest(%s::text[], %s::text[]) as k(sc, er)
                 on k.sc = v.source_code and k.er = v.external_ref""",
            (srcs, refs)).fetchall()
        for i in img_rows:
            images.setdefault((i["source_code"], i["external_ref"]), []).append(dict(i))

        for r in rows:
            r["images"] = images.get((r["source_code"], r["external_ref"]), [])
            r["flags"] = _reasons_to_flags(r.get("reasons"))
            r.setdefault("deal_score", None)
            r.setdefault("forecast", None)
    return rows


def _reasons_to_flags(reasons) -> list[dict]:
    """แปลงเหตุผลของเกรดให้อยู่ในรูปเดียวกับ flag เพื่อแสดงบนหน้าเว็บ

    เหตุผลที่ไม่มีผลต่อคะแนน (impact = 0) ไม่ต้องแสดง เพราะเป็นแค่หมายเหตุ
    """
    if not reasons:
        return []
    if isinstance(reasons, str):
        try:
            reasons = json.loads(reasons)
        except (TypeError, ValueError):
            return []
    out = []
    for x in reasons:
        impact = x.get("impact") or 0
        if impact == 0 and "เพดาน" not in str(x.get("factor", "")):
            continue
        out.append({
            "code": x.get("factor", ""),
            "severity": ("positive" if impact > 0
                         else "critical" if impact <= -30
                         else "caution"),
            "evidence": x.get("detail", ""),
        })
    return out


def build_filter(province=None, district=None, ptype=None, max_price=None,
                 min_price=None, institution=None, min_grade=None,
                 show_special=False, hide_critical=False,
                 near_transit=None) -> tuple[str, tuple]:
    """สร้าง WHERE ให้ฐานข้อมูล — คืน (sql, params)"""
    conds, params = [], []
    if province:
        conds.append("province = %s"); params.append(province)
    if district:
        conds.append("district = %s"); params.append(district)
    if ptype:
        conds.append("property_type = %s"); params.append(ptype)
    elif not show_special:
        conds.append("(property_type is null or property_type <> all(%s))")
        params.append(list(SPECIAL_TYPES))
    if max_price:
        conds.append("opening_price <= %s"); params.append(max_price)
    if min_price:
        conds.append("opening_price >= %s"); params.append(min_price)
    if institution:
        # รองรับทั้งเลือกแหล่งเดียว (str) และหลายแหล่ง (list) — ใช้บนหน้าแผนที่
        if isinstance(institution, (list, tuple, set)):
            names = [i for i in institution if i]
            if names:
                conds.append("institution_name = any(%s)"); params.append(list(names))
        else:
            conds.append("institution_name = %s"); params.append(institution)
    if min_grade:
        allowed = [g for g, v in GRADE_ORDER.items()
                   if g and v >= GRADE_ORDER[min_grade]]
        conds.append("grade = any(%s)"); params.append(allowed)
    if hide_critical:
        # flag ระดับ critical กดเกรดเป็น E เสมอ จึงกรองด้วยเกรดได้
        conds.append("(grade is null or grade <> 'E')")
    if near_transit:
        # ระยะถึงสถานีรถไฟฟ้า/รถไฟ ไม่เกิน N เมตร (จาก _TRANSIT_JOIN)
        conds.append("nearest_station_m is not null and nearest_station_m <= %s")
        params.append(near_transit)
    return ("where " + " and ".join(conds)) if conds else "", tuple(params)


def count_rows(where: str, params: tuple) -> int:
    if DEMO_MODE:
        return len(DEMO_ROWS)
    from core.db import connect
    with connect() as conn:
        return conn.execute(
            f"select count(*) as n from v_listings_with_grade {_TRANSIT_JOIN} {where}",
            params).fetchone()["n"]


def filter_options(province: str | None = None) -> dict:
    """ตัวเลือกในฟอร์มกรอง — ดึงจากข้อมูลจริงที่มีอยู่"""
    if DEMO_MODE:
        by_prov: dict[str, list[str]] = {}
        for r in DEMO_ROWS:
            if r.get("province") and r.get("district"):
                by_prov.setdefault(r["province"], []).append(r["district"])
        return {
            "provinces": sorted({r["province"] for r in DEMO_ROWS if r.get("province")}),
            "districts": sorted({r["district"] for r in DEMO_ROWS
                                 if r.get("district") and (not province or r["province"] == province)}),
            "districts_by_province": {k: sorted(set(v)) for k, v in by_prov.items()},
            "institutions": sorted({r.get("institution_name") for r in DEMO_ROWS
                                    if r.get("institution_name")}),
            "special_count": 0,
        }
    from core.db import connect
    with connect() as conn:
        provinces = [r["province"] for r in conn.execute(
            "select distinct province from v_listings_with_grade "
            "where province is not null order by province").fetchall()]

        # แผนที่จังหวัด -> อำเภอ ส่งไปให้ JS กรองในหน้าได้ทันที
        # ไม่ต้องรีเฟรชทั้งหน้าเมื่อเปลี่ยนจังหวัด
        by_prov: dict[str, list[str]] = {}
        for r in conn.execute(
                "select distinct province, district from v_listings_with_grade "
                "where district is not null order by province, district").fetchall():
            by_prov.setdefault(r["province"], []).append(r["district"])
        # แยกเป็นสอง query แทนการใช้ %s เทียบ null
        # เพราะ Postgres เดาชนิดข้อมูลของพารามิเตอร์ไม่ได้ในบริบทนั้น
        if province:
            district_rows = conn.execute(
                "select distinct district from v_listings_with_grade "
                "where district is not null and province = %s order by district",
                (province,)).fetchall()
        else:
            district_rows = conn.execute(
                "select distinct district from v_listings_with_grade "
                "where district is not null order by district").fetchall()
        districts = [r["district"] for r in district_rows]
        institutions = [r["institution_name"] for r in conn.execute(
            "select distinct institution_name from v_listings_with_grade "
            "where institution_name is not null order by institution_name").fetchall()]
        special = conn.execute(
            "select count(*) as n from v_listings_with_grade where property_type = any(%s)",
            (list(SPECIAL_TYPES),)).fetchone()["n"]
    return {"provinces": provinces, "districts": districts,
            "districts_by_province": by_prov,
            "institutions": institutions, "special_count": special}



def fetch_rows(province=None, ptype=None, max_price=None, hide_critical=False,
               institution=None, min_grade=None, show_special=False,
               is_admin=False, district=None, min_price=None,
               page=None, page_size=None, order=None, near_transit=None):
    """คืน (rows, total) — กรองและแบ่งหน้าที่ SQL"""
    settings = current_settings()
    where, params = build_filter(
        province=province, district=district, ptype=ptype,
        max_price=max_price, min_price=min_price, institution=institution,
        min_grade=min_grade, show_special=show_special, hide_critical=hide_critical,
        near_transit=near_transit)

    total = count_rows(where, params)
    limit = page_size
    offset = ((page or 1) - 1) * (page_size or 0) if page_size else 0
    raw = load_rows(where, params, limit, offset, order=order)

    if DEMO_MODE:
        # โหมดตัวอย่างยังกรองในหน่วยความจำ เพราะไม่มีฐานข้อมูลให้ query
        def keep(r):
            if province and r.get("province") != province: return False
            if district and r.get("district") != district: return False
            if ptype and r.get("property_type") != ptype: return False
            if max_price and (r.get("opening_price") or 0) > max_price: return False
            if institution and r.get("institution_name") != institution: return False
            if min_grade and GRADE_ORDER.get(r.get("grade"), 0) < GRADE_ORDER[min_grade]:
                return False
            if not show_special and not ptype and r.get("property_type") in SPECIAL_TYPES:
                return False
            if near_transit and (r.get("nearest_station_m") is None
                                 or r.get("nearest_station_m") > near_transit):
                return False
            return True
        raw = [r for r in raw if keep(r)]
        total = len(raw)
        if page_size:
            raw = raw[offset:offset + page_size]

    rows = [enrich(r, settings, is_admin) for r in raw]
    if DEMO_MODE and hide_critical:
        rows = [r for r in rows if not r["has_critical"]]
        total = len(rows)
    return rows, total


def top_recommended(is_admin: bool = False, n: int = 8) -> list[dict]:
    """ทรัพย์แนะนำ — เกรด A/B เรียงตาม recommend_score

    recommend_score = คุณภาพ (เกรด/ส่วนลด) + โบนัสพิกัดแปลงจริง/มีรูป/มาใหม่
    ทรัพย์เสี่ยง (เกรด E) ถูกกันออกด้วย min_grade=B + hide_critical อยู่แล้ว
    """
    settings = current_settings()
    if DEMO_MODE:
        rows = [enrich(dict(r), settings, is_admin) for r in DEMO_ROWS]
        return [r for r in rows if r.get("grade") in ("A", "B")][:n]
    where, params = build_filter(min_grade="B", hide_critical=True)
    raw = load_rows(where, params, limit=n, offset=0,
                    order="recommend_score desc nulls last, score desc nulls last")
    return [enrich(r, settings, is_admin) for r in raw]


def featured_by_hot_zone(is_admin: bool = False, n: int = 8) -> list[dict]:
    """ทรัพย์แนะนำ — สุ่มหมุนเวียนจาก "โซนที่คนสนใจ" (analytics)

    ดึงโซน demand สูงจาก v_hot_zones แล้วสุ่มทรัพย์เกรด A/B ในโซนนั้น
    ให้ชุดต่างกันทุกครั้งที่โหลด (สดใหม่ + ดันทรัพย์ในทำเลที่คนกำลังมองหา)
    ถ้ายังไม่มี traffic หรือทรัพย์ในโซนไม่พอ n ตัว → เติมด้วยทรัพย์แนะนำทั่วเว็บ
    """
    settings = current_settings()
    if DEMO_MODE:
        import random
        rows = [enrich(dict(r), settings, is_admin) for r in DEMO_ROWS
                if r.get("grade") in ("A", "B")]
        random.shuffle(rows)
        return rows[:n]

    zones: list[tuple] = []
    try:
        from core.db import connect
        with connect() as conn:
            zones = [(z["province"], z["district"]) for z in conn.execute(
                "select province, district from v_hot_zones "
                "where district is not null and province is not null "
                "limit 8").fetchall()]
    except Exception as exc:                                    # noqa: BLE001
        log.warning("โหลดโซนฮิตไม่สำเร็จ (ใช้ทรัพย์แนะนำทั่วเว็บแทน): %s", str(exc)[:100])

    picked: list[dict] = []
    seen: set[tuple] = set()

    def _take(raw):
        for r in raw:
            key = (r["source_code"], r["external_ref"])
            if key not in seen:
                seen.add(key)
                picked.append(r)
                if len(picked) >= n:
                    break

    # 1) สุ่มจากทรัพย์เกรด A/B ในโซนที่คนสนใจ
    if zones:
        base_where, base_params = build_filter(min_grade="B", hide_critical=True)
        zone_pred = " or ".join(["(province = %s and district = %s)"] * len(zones))
        where = (f"{base_where} and ({zone_pred})" if base_where
                 else f"where ({zone_pred})")
        params = tuple(base_params) + tuple(v for pair in zones for v in pair)
        _take(load_rows(where, params, limit=n, offset=0, order="random()"))

    # 2) ยังไม่ครบ → เติมด้วยทรัพย์แนะนำทั่วเว็บ (สุ่มเช่นกัน)
    if len(picked) < n:
        gw, gp = build_filter(min_grade="B", hide_critical=True)
        _take(load_rows(gw, gp, limit=n * 3, offset=0, order="random()"))

    return [enrich(r, settings, is_admin) for r in picked[:n]]


def promoted_list(is_admin: bool = False, n: int = 6) -> list[dict]:
    """ทรัพย์ที่ "ดันโปรโมท" — โชว์ใน rail ด้านขวาของหน้าแรก (จอกว้าง)

    ดึงจากตาราง promoted_properties ที่ active เรียงตาม rank
    ยังไม่มีการโปรโมท/ไม่พอ → เติมด้วยเกรด A ส่วนลดสูง (คุณภาพ) เป็น placeholder
    * การจัดการรายการโปรโมท (เพิ่ม/ลบ) ทำในหลังบ้าน Phase ถัดไป (พร้อมหน้า add ทรัพย์)
    """
    settings = current_settings()
    if DEMO_MODE:
        rows = [enrich(dict(r), settings, is_admin) for r in DEMO_ROWS
                if r.get("grade") in ("A", "B")]
        return rows[:n]

    picked: list[dict] = []
    seen: set[tuple] = set()
    pairs: list[tuple] = []
    try:
        from core.db import connect
        with connect() as conn:
            pairs = [(p["source_code"], p["external_ref"]) for p in conn.execute(
                "select source_code, external_ref from promoted_properties "
                "where active is true order by rank asc, created_at desc limit %s",
                (n,)).fetchall()]
    except Exception as exc:                                    # noqa: BLE001
        log.warning("โหลดทรัพย์โปรโมทไม่สำเร็จ (รัน migration 034?): %s", str(exc)[:100])

    if pairs:
        conds = " or ".join(["(source_code = %s and external_ref = %s)"] * len(pairs))
        params = tuple(v for pair in pairs for v in pair)
        raw = load_rows(f"where {conds}", params, limit=n)
        rank = {pair: i for i, pair in enumerate(pairs)}
        raw.sort(key=lambda r: rank.get((r["source_code"], r["external_ref"]), 999))
        for r in raw:
            seen.add((r["source_code"], r["external_ref"]))
            picked.append(r)

    # เติมให้ครบด้วยเกรด A คุณภาพสูง (placeholder จนกว่าจะมีการโปรโมทจริง)
    if len(picked) < n:
        gw, gp = build_filter(min_grade="A", hide_critical=True)
        for r in load_rows(gw, gp, limit=n * 3, order="recommend_score desc nulls last"):
            key = (r["source_code"], r["external_ref"])
            if key not in seen:
                seen.add(key)
                picked.append(r)
                if len(picked) >= n:
                    break

    return [enrich(r, settings, is_admin) for r in picked[:n]]


def ensure_gallery(source_code: str, ref: str, detail_url: str | None) -> list[str]:
    """คืน URL รูปทั้งหมดของทรัพย์ ดึงจากต้นทางครั้งแรกที่มีคนเปิดดู

    บันทึกผลทุกครั้งแม้ไม่เจอรูป ไม่งั้นทรัพย์ที่ไม่มีรูป
    จะถูกยิงซ้ำทุกครั้งที่มีคนกดเข้ามา
    """
    # ตรวจโดเมนก่อนเสมอ เคยพลาดยิงไปที่ Google Maps เพราะรับ URL ผิดตัวมา
    if DEMO_MODE or not detail_url or source_code != "bam":
        return []
    if "bam.co.th/th/npa/property/" not in detail_url:
        log.warning("ข้ามการดึงแกลเลอรี URL ไม่ใช่หน้าทรัพย์ BAM: %s", detail_url[:80])
        return []

    from core.db import connect
    with connect() as conn:
        rows = conn.execute(
            """select image_url from property_gallery
                where source_code = %s and external_ref = %s
                order by sort_order""", (source_code, ref)).fetchall()
        if rows:
            return [r["image_url"] for r in rows]

        done = conn.execute(
            "select 1 as x from gallery_fetch_log where source_code = %s and external_ref = %s",
            (source_code, ref)).fetchone()
        if done:
            return []

        detail = fetch_bam_detail(detail_url)
        urls = detail["images"]
        addr = detail.get("address")

        # เก็บที่อยู่เต็มไปพร้อมกัน ไม่ต้องยิงเว็บเพิ่ม
        if addr:
            conn.execute(
                """insert into property_details
                     (source_code, external_ref, address_full, street,
                      subdistrict, district, province)
                   values (%s,%s,%s,%s,%s,%s,%s)
                   on conflict (source_code, external_ref) do update set
                     address_full = excluded.address_full, street = excluded.street,
                     subdistrict = excluded.subdistrict, fetched_at = now()""",
                (source_code, ref, addr.get("full"), addr.get("street"),
                 addr.get("subdistrict"), addr.get("district"), addr.get("province")))
            if addr.get("subdistrict"):
                conn.execute(
                    """update listing_snapshots set subdistrict = %s
                        where source_code = %s and external_ref = %s
                          and subdistrict is distinct from %s""",
                    (addr["subdistrict"], source_code, ref, addr["subdistrict"]))

        for i, u in enumerate(urls):
            conn.execute(
                """insert into property_gallery (source_code, external_ref, image_url, sort_order)
                   values (%s,%s,%s,%s) on conflict do nothing""",
                (source_code, ref, u, i))
        conn.execute(
            """insert into gallery_fetch_log (source_code, external_ref, image_count, status)
               values (%s,%s,%s,%s)
               on conflict (source_code, external_ref) do update set
                 fetched_at = now(), image_count = excluded.image_count""",
            (source_code, ref, len(urls), "ok" if urls else "not_found"))
        conn.commit()
        return urls


def find_row(source_code: str, ref: str, is_admin: bool = False) -> dict | None:
    settings = current_settings()
    if DEMO_MODE:
        for r in DEMO_ROWS:
            if r["source_code"] == source_code and r["external_ref"] == ref:
                return enrich(dict(r), settings, is_admin)
        return None
    rows = load_rows("where source_code = %s and external_ref = %s",
                     (source_code, ref), limit=1)
    return enrich(rows[0], settings, is_admin) if rows else None


def duplicates_for(row: dict, is_admin: bool = False) -> list[dict]:
    """ทรัพย์ "แปลงเดียวกัน" ที่พบในแหล่งอื่น — สำหรับ dedupe/เทียบราคาข้ามแหล่ง

    จับคู่แบบระมัดระวัง (กันจับผิดคู่): เฉพาะพิกัดระดับแปลง (parcel) ที่ตรงกันถึง
    ~1 เมตร (ปัดทศนิยม 5 ตำแหน่ง) + ประเภททรัพย์ + จังหวัด/อำเภอเดียวกัน
    เป็น read-only ไม่แตะ property_links/properties เดิม (ปลอดภัย/ย้อนกลับได้)
    """
    if DEMO_MODE:
        return []
    if row.get("geo_precision") != "parcel" or row.get("lat") is None \
            or row.get("lng") is None:
        return []
    show_inst = is_admin or st.as_bool(current_settings(), "show_institution_name")
    try:
        from core.db import connect
        with connect() as conn:
            raw = conn.execute(
                """select source_code, external_ref, institution_name, property_type,
                          opening_price, grade, province, district, subdistrict
                     from v_listings_with_grade
                    where geo_precision = 'parcel'
                      and property_type is not distinct from %s
                      and province is not distinct from %s
                      and district is not distinct from %s
                      and round(lat::numeric, 5) = round(%s::numeric, 5)
                      and round(lng::numeric, 5) = round(%s::numeric, 5)
                      and not (source_code = %s and external_ref = %s)
                    order by opening_price asc nulls last
                    limit 8""",
                (row.get("property_type"), row.get("province"), row.get("district"),
                 row.get("lat"), row.get("lng"),
                 row.get("source_code"), row.get("external_ref"))).fetchall()
    except Exception as exc:                                       # noqa: BLE001
        log.warning("หาทรัพย์ซ้ำข้ามแหล่งไม่สำเร็จ: %s", str(exc)[:100])
        return []
    out = []
    for r in raw:
        d = dict(r)
        d["type_label"] = TYPE_LABELS.get(d.get("property_type"), "อื่น ๆ")
        if not show_inst:
            d["institution_name"] = None
        out.append(d)
    return out


# ---------------------------------------------------------------------
TEMPLATES = {
"layout.html": """
<!doctype html><html lang="th"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ (og_title or title) ~ ' | แปลงดี' }}</title>
<meta name="description" content="{{ og_desc or 'แปลงดี — รวมทรัพย์ NPA บ้านหลุดจำนอง ที่ดิน คอนโด อาคารพาณิชย์ จากธนาคาร AMC และกรมบังคับคดี (ขายทอดตลาด) ราคาต่ำกว่าตลาด พร้อมจัดเกรดคุณภาพ วิเคราะห์ส่วนลด ทำเล และแนวรถไฟฟ้า' }}">
<meta name="keywords" content="ทรัพย์ NPA, บ้านหลุดจำนอง, บ้านมือสอง, ขายทอดตลาด, กรมบังคับคดี, ที่ดินราคาถูก, คอนโดมือสอง, บ้านธนาคาร, NPA, แปลงดี">
{% if canonical %}<link rel="canonical" href="{{ canonical }}">{% endif %}
<link rel="icon" href="/static/logo.svg" type="image/svg+xml">
<!-- OG/Twitter — พรีวิวตอนแชร์ลิงก์ใน LINE/FB -->
<meta property="og:type" content="{{ og_type or 'website' }}">
<meta property="og:site_name" content="แปลงดี">
<meta property="og:title" content="{{ og_title or title }}">
<meta property="og:description" content="{{ og_desc or 'รวมทรัพย์ NPA/ขายทอดตลาดหลายแหล่งไว้ที่เดียว จัดเกรดคุณภาพ วิเคราะห์ทำเลและราคาให้เห็นชัด' }}">
{% if og_url %}<meta property="og:url" content="{{ og_url }}">{% endif %}
{% if og_image %}<meta property="og:image" content="{{ og_image }}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="{{ og_image }}">{% else %}
<meta name="twitter:card" content="summary">{% endif %}
<meta name="twitter:title" content="{{ og_title or title }}">
<meta name="twitter:description" content="{{ og_desc or 'รวมทรัพย์ NPA/ขายทอดตลาด จัดเกรด วิเคราะห์ทำเล' }}">
{% if jsonld %}<script type="application/ld+json">{{ jsonld|safe }}</script>{% endif %}
{% if ga_id %}<!-- Google Analytics 4 -->
<script async src="https://www.googletagmanager.com/gtag/js?id={{ ga_id }}"></script>
<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments)}
gtag('js',new Date());gtag('config','{{ ga_id }}',{anonymize_ip:true});</script>{% endif %}
<!-- เก็บ Tailwind ไว้ในเครื่อง ไม่พึ่ง CDN ตอนรัน
     ถ้าพึ่ง CDN แล้วเน็ตช้าหรือ CDN ล่ม หน้าเว็บจะไม่มีสไตล์เลย
     ซึ่งเกิดขึ้นจริงตอนทดสอบ -->
<script src="/static/tailwind.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bai+Jamjuree:wght@500;600;700&family=IBM+Plex+Sans+Thai:wght@400;500;600&display=swap" rel="stylesheet">
<style>
/* ─────────────────────────────────────────────────────────────
   แนวทางออกแบบ: ยืมภาษาจากเอกสารสิทธิ์ที่ดินและแผนที่รังวัด
   หมึกน้ำเงินเข้มของช่างรังวัด · กระดาษเอกสาร · ตราประทับสีชาด
   ตัวเลขใช้ tabular figures เพื่อให้เทียบราคาในคอลัมน์ได้ด้วยตา
   ───────────────────────────────────────────────────────────── */
:root{
  --ink:#12283A;        /* หมึกช่างรังวัด */
  --sheet:#E7EEEC;      /* พื้นหน้า - กระดาษอมเขียวเทา ให้การ์ดขาวเด้งขึ้น */
  --card:#FFFFFF;       /* การ์ด */
  --seal:#E24637;       /* ตราประทับ - แดงส้มสด */
  --survey:#0BA07A;     /* เส้นรังวัด - เขียวมรกตสด (สีแบรนด์หลัก) */
  --survey-deep:#0A8062;
  --sky:#1C86C9;        /* ฟ้าสด - ใช้เน้นข้อมูล/กราф */
  --pencil:#4C5E6C;     /* ดินสอ - ข้อความรอง (เข้มขึ้น อ่านชัด) */
  --rule:#CFDADC;       /* เส้นตาราง */
}
*{-webkit-font-smoothing:antialiased}
body{
  font-family:"IBM Plex Sans Thai",-apple-system,"Segoe UI",sans-serif;
  background:var(--sheet); color:var(--ink);
  /* ตารางรังวัดจาง ๆ เป็นพื้นหลัง */
  background-image:
    linear-gradient(var(--rule) 1px, transparent 1px),
    linear-gradient(90deg, var(--rule) 1px, transparent 1px);
  background-size:48px 48px; background-position:-1px -1px;
}
body::before{content:"";position:fixed;inset:0;background:var(--sheet);
  opacity:.88;pointer-events:none;z-index:-1}
/* แถบสีแบรนด์บนสุด — เพิ่มความสดทันทีที่เปิดหน้า */
.brandbar{height:4px;background:linear-gradient(90deg,var(--survey),var(--sky) 55%,var(--seal))}
h1,h2,.display{font-family:"Bai Jamjuree","IBM Plex Sans Thai",sans-serif;
  font-weight:600;letter-spacing:-.01em}
.num{font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1}
.wordmark{font-family:"Bai Jamjuree",sans-serif;font-weight:700;letter-spacing:-.02em;
  color:var(--ink)}
.eyebrow{font-size:11px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--survey);font-weight:600}
.sheet{background:var(--card);border:1px solid var(--rule);border-radius:12px;
  box-shadow:0 1px 3px rgba(18,40,58,.05)}
.sheet:hover{border-color:var(--survey);box-shadow:0 4px 14px rgba(11,160,122,.10)}
a.brandlink{color:var(--survey);font-weight:600}
a.brandlink:hover{color:var(--survey-deep)}
/* เนื้อหาบทความ/หน้าเนื้อหา — คืน default ที่ Tailwind reset เอาออก */
.prose-npa h2{font-family:"Bai Jamjuree",sans-serif;font-weight:600;font-size:1.25rem;margin:1.4em 0 .4em;color:var(--ink)}
.prose-npa h3{font-weight:600;font-size:1.05rem;margin:1.1em 0 .3em;color:var(--ink)}
.prose-npa p{margin:.5em 0}
.prose-npa ul{list-style:disc;padding-left:1.4em;margin:.5em 0}
.prose-npa ol{list-style:decimal;padding-left:1.4em;margin:.5em 0}
.prose-npa li{margin:.25em 0}
.prose-npa a{color:var(--survey);font-weight:600}
.prose-npa strong,.prose-npa b{color:var(--ink);font-weight:700}
.prose-npa blockquote{border-left:3px solid var(--survey);padding-left:1em;color:var(--pencil);margin:.8em 0}
.btn-primary{background:var(--ink);color:#fff}
.btn-primary:hover{background:#0B1B29}
.chip{border-radius:999px}
a.navlink{color:var(--pencil);position:relative;padding-bottom:2px}
a.navlink:hover{color:var(--ink)}
a.navlink[aria-current="page"]{color:var(--ink);font-weight:600;
  box-shadow:inset 0 -2px 0 var(--seal)}

/* ตราประทับเกรด — องค์ประกอบหลักของหน้า
   เอียงเล็กน้อยให้เหมือนถูกประทับลงบนเอกสารจริง */
.seal{
  font-family:"Bai Jamjuree",sans-serif;font-weight:700;
  display:inline-flex;align-items:center;justify-content:center;
  border:2px solid currentColor;border-radius:50%;
  transform:rotate(-8deg);letter-spacing:0;line-height:1;
}
.seal-sm{width:32px;height:32px;font-size:16px;border-width:2px}
.seal-lg{width:60px;height:60px;font-size:29px;border-width:3px}
/* ถ้ารูปโหลดไม่ได้ แสดงลายผังแปลงแทนช่องว่างเปล่า */
.imgwrap{min-height:160px}
.imgwrap.noimg{
  background:
    repeating-linear-gradient(0deg,#E3E8EB 0 1px,transparent 1px 22px),
    repeating-linear-gradient(90deg,#E3E8EB 0 1px,transparent 1px 22px),#F1F4F6;
}
.imgwrap.noimg::after{content:"ไม่มีรูป";position:absolute;inset:0;
  display:flex;align-items:center;justify-content:center;
  color:#9AA7AF;font-size:12px;letter-spacing:.08em}
.g-A{color:#0F9E70} .g-B{color:#5AA62E} .g-C{color:#C6871A}
.g-D{color:#DB6A26} .g-E{color:var(--seal)} .g-none{color:#94A3B0}

/* ตัวกรองพับบนมือถือ — ใช้ปุ่ม + hidden/sm:block (เลี่ยง <details>
   ที่ Chrome ใหม่ซ่อนเนื้อหาผ่าน ::details-content ทำให้ CSS force-open ไม่ติด) */
.chev{transition:transform .18s ease}
[aria-expanded="true"] .chev{transform:rotate(180deg)}

@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head><body class="text-[15px]">
<div class="brandbar"></div>
<header class="sticky top-0 z-20 bg-white/90 backdrop-blur border-b" style="border-color:var(--rule)">
  <div class="max-w-6xl mx-auto px-4 py-2.5 flex items-center gap-5">
    <a href="/" class="flex items-center gap-2.5 shrink-0" title="แปลงดี">
      <svg width="32" height="32" viewBox="0 0 32 32" fill="none" aria-hidden="true">
        <!-- ผังแปลงที่ดิน + แปลงที่ "ใช่" ถูกเลือก (เครื่องหมายถูก) = แปลงดี -->
        <defs><linearGradient id="pdlogo" x1="4" y1="4" x2="28" y2="28" gradientUnits="userSpaceOnUse">
          <stop stop-color="#12B98C"/><stop offset="1" stop-color="#0A7D60"/></linearGradient></defs>
        <rect x="2.5" y="2.5" width="27" height="27" rx="7.5" fill="url(#pdlogo)"/>
        <path d="M16 5V27M5 16H27" stroke="#fff" stroke-width="1.3" opacity=".45" stroke-linecap="round"/>
        <rect x="17" y="17" width="9.5" height="9.5" rx="2.4" fill="#E24637"/>
        <path d="M19.3 21.9l1.9 1.9 3.2-3.7" stroke="#fff" stroke-width="1.9"
              stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <span class="wordmark text-[20px] leading-none">แปลง<span style="color:var(--survey)">ดี</span></span>
    </a>
    <nav class="flex gap-4 text-[13px] overflow-x-auto min-w-0">
      {% set tk = '?token=' ~ admin_token if admin_token else '' %}
      <a href="/{{ tk }}" class="navlink whitespace-nowrap">ทรัพย์</a>
      <a href="/map{{ tk }}" class="navlink whitespace-nowrap">แผนที่</a>
      <a href="/compare" class="navlink whitespace-nowrap">เทียบราคา</a>
      <a href="/articles" class="navlink whitespace-nowrap">บทความ</a>
      {% if line_login %}
        {% if user_logged_in %}
        <a href="/favorites" class="navlink whitespace-nowrap">❤️ ทรัพย์โปรด</a>
        <a href="/auth/logout" class="navlink whitespace-nowrap text-slate-400">ออกจากระบบ</a>
        {% else %}
        <a href="/auth/line/login" class="navlink whitespace-nowrap" style="color:var(--survey)">เข้าสู่ระบบ</a>
        {% endif %}
      {% endif %}
      {% if is_admin %}
      <span class="w-px bg-slate-300 my-0.5 shrink-0" aria-hidden="true"></span>
      <a href="/admin/add{{ tk }}" class="navlink whitespace-nowrap" style="color:var(--survey)">+ เพิ่มทรัพย์</a>
      <a href="/admin{{ tk }}" class="navlink whitespace-nowrap">สถิติ</a>
      <a href="/admin/monitor{{ tk }}" class="navlink whitespace-nowrap">ยอดดู</a>
      <a href="/admin/inquiries{{ tk }}" class="navlink whitespace-nowrap">คำขอติดต่อ</a>
      <a href="/admin/feedback{{ tk }}" class="navlink whitespace-nowrap">ความเห็น</a>
      <a href="/admin/promoted{{ tk }}" class="navlink whitespace-nowrap">โปรโมท</a>
      <a href="/admin/settings{{ tk }}" class="navlink whitespace-nowrap">ตั้งค่า</a>
      <a href="/health{{ tk }}" class="navlink whitespace-nowrap">สุขภาพระบบ</a>
      <a href="/admin/logout" class="navlink whitespace-nowrap text-slate-400">ออกจากระบบ</a>
      {% endif %}
    </nav>
    <span class="ml-auto flex items-center gap-1.5 shrink-0">
      {% if is_admin %}<span class="text-[11px] px-2 py-0.5 rounded border"
        style="border-color:var(--seal);color:var(--seal)"
        title="เห็นลิงก์ต้นทางและรหัสตลาดเสมอ">admin</span>{% endif %}
      {% if public_mode %}<span class="text-[11px] px-2 py-0.5 rounded border"
        style="border-color:var(--survey);color:var(--survey)">เผยแพร่</span>{% endif %}
      {% if demo %}<span class="text-[11px] px-2 py-0.5 rounded border"
        style="border-color:#B98A2E;color:#8A6516">ตัวอย่าง</span>{% endif %}
    </span>
  </div>
</header>
<main class="{{ maxw or 'max-w-6xl' }} mx-auto px-4 py-6">
{% if demo %}
<div class="mb-4 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
  <b>กำลังแสดงข้อมูลตัวอย่าง</b> — {{ demo_reason }}
</div>
{% endif %}
{% block body %}{% endblock %}
</main>
<script>
(function(){
  var k='npa_sid', sid=sessionStorage.getItem(k);
  if(!sid){ sid=Math.random().toString(36).slice(2)+Date.now().toString(36);
            sessionStorage.setItem(k,sid); }
  window.npaTrack=function(type,extra){
    try{
      fetch('/api/track',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(Object.assign({event_type:type,sid:sid,
          device_class: matchMedia('(max-width:640px)').matches?'mobile':'desktop',
          referrer_kind: document.referrer.includes('line')?'line'
            :(document.referrer?'search':'direct')}, extra||{}))});
    }catch(e){}
  };
  document.addEventListener('click',function(e){
    var a=e.target.closest('a[data-track-source]');
    if(a) npaTrack('click_source',{source_code:a.dataset.trackSource,
                                   external_ref:a.dataset.trackRef});
  });
})();
</script>
<script>
/* ทรัพย์โปรด — กดหัวใจ (toggle) ผ่าน /api/favorite; ยังไม่ล็อกอิน → พาไป LINE Login */
window.toggleFav=function(btn){
  var sc=btn.dataset.sc, ref=btn.dataset.ref;
  btn.disabled=true;
  fetch('/api/favorite',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({source_code:sc,external_ref:ref})})
  .then(function(r){
    if(r.status===401){ location.href='/auth/line/login?next='+encodeURIComponent(location.pathname); return null; }
    return r.json();
  })
  .then(function(d){
    btn.disabled=false;
    if(!d||!d.ok) return;
    var on=d.favorited;
    btn.setAttribute('aria-pressed', on?'true':'false');
    if(btn.dataset.label!=='0'){ btn.innerHTML = on ? '❤️ บันทึกแล้ว' : '🤍 บันทึกทรัพย์นี้'; }
    else { btn.innerHTML = on ? '❤️' : '🤍'; }
    if(!on && btn.dataset.removeCard){ var c=btn.closest('[data-fav-card]'); if(c) c.remove(); }
  })
  .catch(function(){ btn.disabled=false; });
};
</script>
{% block track %}{% endblock %}

<!-- ปุ่ม feedback ลอยมุมจอ — เก็บความเห็นช่วง beta (มีทุกหน้า) -->
<button id="fb-open" onclick="fbOpen()" aria-label="ส่งความเห็น"
  class="fixed z-30 bottom-4 right-4 flex items-center gap-2 text-white
         rounded-full shadow-lg px-4 py-3 text-sm font-medium"
  style="background:var(--survey)">
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.4A8 8 0 1 1 21 12Z"
          stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>
  <span class="hidden sm:inline">ส่งความเห็น</span>
</button>

<div id="fb-modal" class="fixed inset-0 z-40 hidden items-end sm:items-center justify-center
     bg-black/40 p-0 sm:p-4" onclick="if(event.target===this)fbClose()">
  <div class="bg-white w-full sm:max-w-md rounded-t-2xl sm:rounded-2xl p-5 shadow-xl">
    <div class="flex items-start justify-between gap-3">
      <div>
        <h3 class="text-base font-semibold">บอกเราหน่อย 🙏</h3>
        <p class="text-xs text-slate-500 mt-0.5">เว็บนี้กำลังทดลองใช้ — ความเห็นของคุณช่วยให้ดีขึ้น</p>
      </div>
      <button onclick="fbClose()" class="text-slate-400 hover:text-slate-700 text-xl leading-none">&times;</button>
    </div>
    <div id="fb-form" class="mt-3 space-y-3">
      <div>
        <div class="text-xs text-slate-500 mb-1">รู้สึกยังไงกับเว็บ</div>
        <div id="fb-rate" class="flex gap-1.5 text-2xl">
          {% for r in [1,2,3,4,5] %}
          <button type="button" data-r="{{ r }}" onclick="fbSetRate({{ r }})"
            class="fb-star opacity-40 hover:opacity-100 transition">★</button>
          {% endfor %}
        </div>
      </div>
      <textarea id="fb-msg" rows="4" placeholder="ชอบ/ไม่ชอบตรงไหน เจอปัญหาอะไร อยากได้อะไรเพิ่ม…"
        class="w-full border rounded-lg px-3 py-2 text-sm"></textarea>
      <input id="fb-contact" placeholder="ชื่อหรือ LINE (ถ้าอยากให้เราติดต่อกลับ — ไม่บังคับ)"
        class="w-full border rounded-lg px-3 py-2 text-sm">
      <input id="fb-website" class="hidden" tabindex="-1" autocomplete="off">
      <button onclick="fbSend()" id="fb-send"
        class="w-full text-white rounded-lg px-4 py-2.5 text-sm font-medium"
        style="background:var(--ink)">ส่งความเห็น</button>
      <p class="text-[11px] text-slate-400">เราเก็บเฉพาะข้อความที่คุณพิมพ์และหน้าที่เปิดอยู่ ไม่เก็บข้อมูลระบุตัวตนอื่น</p>
    </div>
    <div id="fb-done" class="hidden py-8 text-center">
      <div class="text-4xl">🎉</div>
      <div class="mt-2 font-medium">ขอบคุณมากครับ!</div>
      <div class="text-sm text-slate-500 mt-1">รับความเห็นแล้ว เราจะเอาไปปรับปรุงต่อ</div>
      <button onclick="fbClose()" class="mt-4 text-sm brandlink">ปิด</button>
    </div>
  </div>
</div>
<script>
let fbRate=0;
function fbOpen(){var m=document.getElementById('fb-modal');m.classList.remove('hidden');m.classList.add('flex');}
function fbClose(){var m=document.getElementById('fb-modal');m.classList.add('hidden');m.classList.remove('flex');
  document.getElementById('fb-form').classList.remove('hidden');
  document.getElementById('fb-done').classList.add('hidden');}
function fbSetRate(r){fbRate=r;document.querySelectorAll('#fb-rate .fb-star').forEach(function(b){
  b.classList.toggle('opacity-40', +b.dataset.r>r);b.classList.toggle('opacity-100', +b.dataset.r<=r);});}
function fbSend(){
  var msg=document.getElementById('fb-msg').value.trim();
  if(!msg && !fbRate){document.getElementById('fb-msg').focus();return;}
  if(document.getElementById('fb-website').value) return;   // honeypot
  var btn=document.getElementById('fb-send');btn.disabled=true;btn.textContent='กำลังส่ง…';
  fetch('/api/feedback',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({message:msg,rating:fbRate||null,
      contact:document.getElementById('fb-contact').value.trim()||null,
      page_url:location.pathname+location.search,
      sid:(function(){try{return sessionStorage.getItem('npa_sid')}catch(e){return null}})(),
      device_class: matchMedia('(max-width:640px)').matches?'mobile':'desktop'})})
   .then(function(r){if(!r.ok)throw 0;
     document.getElementById('fb-form').classList.add('hidden');
     document.getElementById('fb-done').classList.remove('hidden');})
   .catch(function(){btn.disabled=false;btn.textContent='ส่งความเห็น';alert('ส่งไม่สำเร็จ ลองอีกครั้งนะครับ');});
}
</script>

<footer class="mt-8 border-t" style="border-color:var(--rule)">
  <div class="max-w-6xl mx-auto px-4 py-8 text-sm">
    <div class="flex flex-wrap gap-x-6 gap-y-2 mb-4">
      <a href="/about" class="brandlink">เกี่ยวกับแปลงดี</a>
      <a href="/zone" class="brandlink">ทรัพย์ตามทำเล</a>
      <a href="/articles" class="brandlink">บทความ/คู่มือ</a>
      <a href="/contact" class="brandlink">ติดต่อเรา</a>
      <a href="/privacy" class="brandlink">นโยบายความเป็นส่วนตัว (PDPA)</a>
      <a href="/terms" class="brandlink">เงื่อนไขการใช้งาน</a>
    </div>
    <p class="text-xs text-slate-500 leading-relaxed max-w-3xl">
      แปลงดี — รวมทรัพย์ NPA/ขายทอดตลาดจากประกาศสาธารณะเพื่อการวิเคราะห์ <b>ไม่ใช่คำแนะนำการลงทุน</b>
      ต้องตรวจสอบเอกสารสิทธิ์ ภาระผูกพัน แนวเขต และสภาพทรัพย์กับหน่วยงานที่เกี่ยวข้องก่อนตัดสินใจทุกครั้ง</p>
  </div>
</footer></body></html>
""",

"list.html": """
{% extends "layout.html" %}{% block body %}
{% set filter_on = province or district or ptype or min_price or max_price or institution or min_grade or hide_critical or show_special or near_transit %}

{% if landing_h1 %}
<!-- Landing SEO (โซน/ประเภท) — H1 + เกริ่นนำคีย์เวิร์ด + ลิงก์ภายใน -->
<nav class="text-xs mb-2" style="color:var(--pencil)" aria-label="breadcrumb">
  <a href="/" class="brandlink">หน้าแรก</a> ›
  <a href="/zone" class="brandlink">ทำเล</a>
  {% if landing_crumb %} › <a href="{{ landing_crumb.href }}" class="brandlink">{{ landing_crumb.label }}</a>{% endif %}
</nav>
<section class="mb-5 rounded-2xl overflow-hidden relative"
  style="background:linear-gradient(135deg,var(--ink) 0%,var(--survey-deep) 100%)">
  <div class="absolute inset-0 opacity-15" aria-hidden="true"
    style="background-image:linear-gradient(#fff 1px,transparent 1px),linear-gradient(90deg,#fff 1px,transparent 1px);background-size:38px 38px"></div>
  <div class="relative px-5 py-7 sm:px-9 sm:py-9 text-white">
    <h1 class="display text-2xl sm:text-[1.9rem] font-bold leading-tight text-white">{{ landing_h1 }}</h1>
    {% if landing_intro %}<p class="text-white/85 text-sm mt-2.5 leading-relaxed max-w-3xl">{{ landing_intro }}</p>{% endif %}
    {% if landing_links %}
    <div class="mt-4 flex flex-wrap gap-2">
      {% for l in landing_links %}
      <a href="{{ l.href }}" class="chip px-3.5 py-1.5 text-[13px] font-medium bg-white/15 text-white border border-white/25 hover:bg-white/25 transition">{{ l.label }}</a>
      {% endfor %}
    </div>{% endif %}
    {% if landing_links2 %}
    <div class="mt-3">
      {% if landing_links2_label %}<div class="text-[11px] text-white/60 mb-1.5">{{ landing_links2_label }}</div>{% endif %}
      <div class="flex flex-wrap gap-2">
        {% for l in landing_links2 %}
        <a href="{{ l.href }}" class="chip px-3 py-1 text-[12px] font-medium bg-white/10 text-white/90 border border-white/20 hover:bg-white/20 transition">{{ l.label }}</a>
        {% endfor %}
      </div>
    </div>{% endif %}
  </div>
</section>
{% endif %}

{% if not filter_on and not landing_h1 %}
<!-- Hero search แบบ portal — โชว์เฉพาะหน้าแรกที่ยังไม่กรอง -->
<section class="-mt-2 mb-5 rounded-2xl overflow-hidden relative"
  style="background:linear-gradient(135deg,var(--ink) 0%,var(--survey-deep) 100%)">
  <div class="absolute inset-0 opacity-15" aria-hidden="true"
    style="background-image:linear-gradient(#fff 1px,transparent 1px),linear-gradient(90deg,#fff 1px,transparent 1px);background-size:38px 38px"></div>
  <div class="relative px-5 py-8 sm:px-9 sm:py-11">
    <div class="text-white max-w-2xl">
      <div class="text-[11px] tracking-widest uppercase font-semibold" style="color:rgba(255,255,255,.75)">แปลงดี · ทรัพย์ NPA / ขายทอดตลาด</div>
      <h1 class="display text-2xl sm:text-[2rem] font-bold mt-1.5 leading-tight text-white">
        หาบ้าน–ที่ดิน ราคาต่ำกว่าตลาด<br class="hidden sm:block"> จัดเกรดคุณภาพ ดูทำเลชัด</h1>
      <p class="text-white/80 text-sm mt-2 leading-relaxed">
        รวมจากธนาคาร · AMC · กรมบังคับคดี กว่า <b class="num text-white">{{ "{:,}".format(count) }}</b> รายการ
        พร้อมวิเคราะห์ส่วนลด แนวรถไฟฟ้า และความเสี่ยง ในที่เดียว</p>
    </div>
    <form class="mt-5 bg-white rounded-xl p-2.5 grid gap-2 sm:grid-cols-[1.3fr_1fr_1fr_auto] items-end shadow-xl">
      {% if admin_token %}<input type="hidden" name="token" value="{{ admin_token }}">{% endif %}
      <label class="text-[11px] text-slate-500 font-medium">จังหวัด
        <select name="province" class="mt-0.5 w-full border rounded-lg px-3 py-2.5 text-sm">
          <option value="">ทุกจังหวัด</option>
          {% for p in provinces %}<option value="{{ p }}">{{ p }}</option>{% endfor %}
        </select></label>
      <label class="text-[11px] text-slate-500 font-medium">ประเภท
        <select name="ptype" class="mt-0.5 w-full border rounded-lg px-3 py-2.5 text-sm">
          <option value="">ทุกประเภท</option>
          {% for k,v in type_labels.items() %}<option value="{{ k }}">{{ v }}</option>{% endfor %}
        </select></label>
      <label class="text-[11px] text-slate-500 font-medium">ราคาไม่เกิน (บาท)
        <input name="max_price" type="number" placeholder="เช่น 2000000"
          class="mt-0.5 w-full border rounded-lg px-3 py-2.5 text-sm"></label>
      <button class="rounded-lg px-6 py-2.5 text-sm font-semibold text-white whitespace-nowrap"
        style="background:var(--seal)">ค้นหาทรัพย์</button>
    </form>
    <div class="mt-3 flex flex-wrap gap-2">
      {% set chip_types = ['house','townhouse','condo','land','commercial'] %}
      {% for k in chip_types %}{% if type_labels.get(k) %}
      <a href="/?ptype={{ k }}"
         class="chip px-3.5 py-1.5 text-[13px] font-medium bg-white/15 text-white border border-white/25 hover:bg-white/25 transition">{{ type_labels.get(k) }}</a>
      {% endif %}{% endfor %}
      <a href="/map" class="chip px-3.5 py-1.5 text-[13px] font-medium bg-white/15 text-white border border-white/25 hover:bg-white/25 transition">🗺️ ดูบนแผนที่</a>
    </div>
  </div>
</section>
{% endif %}

{% if institutions %}
<!-- แถบเลือกแหล่งทรัพย์ (source) — กดสลับแหล่งได้ตลอด -->
<div class="mb-4 flex flex-wrap items-center gap-2">
  <span class="text-[13px] font-medium text-slate-500 mr-0.5">แหล่ง:</span>
  <a href="/{% if admin_token %}?token={{ admin_token }}{% endif %}"
     class="chip px-3 py-1.5 text-[13px] font-medium border transition"
     style="{% if not institution %}background:var(--survey);border-color:var(--survey);color:#fff{% else %}background:#fff;border-color:var(--rule);color:var(--pencil){% endif %}">ทุกแหล่ง</a>
  {% for i in institutions %}
  <a href="/?institution={{ i }}{% if admin_token %}&token={{ admin_token }}{% endif %}"
     class="chip px-3 py-1.5 text-[13px] font-medium border transition hover:border-slate-400"
     style="{% if i==institution %}background:var(--survey);border-color:var(--survey);color:#fff{% else %}background:#fff;border-color:var(--rule);color:var(--pencil){% endif %}">{{ i }}</a>
  {% endfor %}
</div>
{% endif %}

<div id="filterbox" class="mb-5">
<button type="button" onclick="fltToggle(this)" aria-expanded="{{ 'true' if filter_on else 'false' }}"
  class="sheet w-full px-4 py-3 flex items-center justify-between text-sm font-medium">
  <span class="flex items-center gap-2">ตัวกรองเพิ่มเติม
    <span class="hidden sm:inline text-xs font-normal text-slate-400">เขต · ช่วงราคา · แหล่ง · เกรด</span>
    {% if filter_on %}<span class="text-[11px] px-1.5 py-0.5 rounded-full text-white"
      style="background:var(--seal)">กำลังกรอง</span>{% endif %}</span>
  <svg class="chev" width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M4 6l4 4 4-4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
</button>
<div id="filterwrap" class="{{ 'block' if filter_on else 'hidden' }}">
<form class="sheet p-4 mt-2 grid gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 items-end">
  {% if admin_token %}<input type="hidden" name="token" value="{{ admin_token }}">{% endif %}
  <label class="text-sm">จังหวัด
    <select name="province" id="f-province" onchange="syncDistricts()"
            class="mt-1 w-full border rounded-lg px-2 py-1.5">
      <option value="">ทั้งหมด</option>
      {% for p in provinces %}<option value="{{ p }}" {% if p==province %}selected{% endif %}>{{ p }}</option>{% endfor %}
    </select></label>
  <label class="text-sm">เขต/อำเภอ
    <select name="district" id="f-district" class="mt-1 w-full border rounded-lg px-2 py-1.5">
      <option value="">ทั้งหมด</option>
      {% for d in districts %}<option value="{{ d }}" {% if d==district %}selected{% endif %}>{{ d }}</option>{% endfor %}
    </select></label>
  <label class="text-sm">ประเภท
    <select name="ptype" class="mt-1 w-full border rounded-lg px-2 py-1.5">
      <option value="">ทั้งหมด</option>
      {% for k,v in type_labels.items() %}<option value="{{ k }}" {% if k==ptype %}selected{% endif %}>{{ v }}</option>{% endfor %}
    </select></label>
  <label class="text-sm">ราคาตั้งแต่
    <input name="min_price" type="number" value="{{ min_price or '' }}"
      class="mt-1 w-full border rounded-lg px-2 py-1.5" placeholder="500000"></label>
  <label class="text-sm">ราคาไม่เกิน
    <input name="max_price" type="number" value="{{ max_price or '' }}"
      class="mt-1 w-full border rounded-lg px-2 py-1.5" placeholder="2000000"></label>
  <label class="text-sm">แหล่ง
    <select name="institution" class="mt-1 w-full border rounded-lg px-2 py-1.5">
      <option value="">ทุกแหล่ง</option>
      {% for i in institutions %}<option value="{{ i }}" {% if i==institution %}selected{% endif %}>{{ i }}</option>{% endfor %}
    </select></label>
  <label class="text-sm">เกรดขั้นต่ำ
    <select name="min_grade" class="mt-1 w-full border rounded-lg px-2 py-1.5">
      <option value="">ทุกเกรด</option>
      {% for g in ['A','B','C','D'] %}<option value="{{ g }}" {% if g==min_grade %}selected{% endif %}>{{ g }} ขึ้นไป</option>{% endfor %}
    </select></label>
  <label class="text-sm">🚉 ใกล้รถไฟฟ้า
    <select name="near_transit" class="mt-1 w-full border rounded-lg px-2 py-1.5">
      {% for val,lbl in [('','ไม่จำกัด'),('500','ในรัศมี 500 ม.'),('1000','ในรัศมี 1 กม.'),('2000','ในรัศมี 2 กม.'),('3000','ในรัศมี 3 กม.')] %}
      <option value="{{ val }}" {% if val and near_transit and val|int==near_transit %}selected{% endif %}>{{ lbl }}</option>{% endfor %}
    </select></label>
  <label class="text-sm flex items-center gap-2 pb-1.5">
    <input type="checkbox" name="hide_critical" value="1" {% if hide_critical %}checked{% endif %}>
    ซ่อนรายการเสี่ยงสูง</label>
  <label class="text-sm flex items-center gap-2 pb-1.5"
         title="เครื่องจักร พื้นที่ส่วนกลาง และทรัพย์เฉพาะทาง">
    <input type="checkbox" name="show_special" value="1" {% if show_special %}checked{% endif %}>
    รวมทรัพย์ประเภทพิเศษ{% if special_count %} ({{ special_count }}){% endif %}</label>
  <label class="text-sm">เรียงตาม
    <select name="sort" onchange="this.form.submit()"
            class="mt-1 w-full border rounded-lg px-2 py-1.5">
      {% for k,v in [('','แนะนำ (เกรด)'),('reco','คะแนนแนะนำ'),('price_asc','ราคาต่ำ→สูง'),('price_desc','ราคาสูง→ต่ำ'),('new','มาใหม่ล่าสุด')] %}
      <option value="{{ k }}" {% if k==sort %}selected{% endif %}>{{ v }}</option>{% endfor %}
    </select></label>
  <div class="flex gap-2">
    <button class="flex-1 bg-slate-900 text-white rounded-lg px-4 py-2 text-sm">กรอง</button>
    <a href="/{% if admin_token %}?token={{ admin_token }}{% endif %}"
       class="px-3 py-2 text-sm border rounded-lg text-slate-600 hover:bg-slate-50">ล้าง</a>
  </div>
</form>
</div>
</div>

<div class="xl:grid xl:grid-cols-[1fr_320px] xl:gap-6 xl:items-start">
<div class="min-w-0">
<div class="flex items-center justify-between mb-3">
  <{{ 'h2' if landing_h1 else 'h1' }} class="text-xl font-semibold">พบ <span class="num">{{ "{:,}".format(count) }}</span> รายการ
    {% if district %}<span class="text-sm font-normal text-slate-500">ใน {{ district }}</span>
    {% elif province %}<span class="text-sm font-normal text-slate-500">ใน {{ province }}</span>{% endif %}</{{ 'h2' if landing_h1 else 'h1' }}>
  <a href="/map{{ qs }}" class="text-sm brandlink">ดูบนแผนที่ →</a>
</div>

{% if featured %}
<section class="mb-6 p-5 rounded-2xl"
         style="background:linear-gradient(180deg,rgba(11,160,122,.09),rgba(11,160,122,0))">
  <div class="flex items-center gap-2 mb-3">
    <h2 class="text-lg font-semibold" style="color:var(--survey-deep)">⭐ ทรัพย์แนะนำ</h2>
    <span class="text-xs" style="color:var(--pencil)">หมุนเวียนจากโซนที่คนสนใจ · เกรด A/B</span>
  </div>
  <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
  {% for r in featured %}
    <a href="/p/{{ r.source_code }}/{{ r.external_ref }}"
       class="sheet overflow-hidden block ring-1 ring-amber-200 hover:ring-amber-300 transition">
      <div class="relative imgwrap" style="background:#EEF1F3">
        <img src="{{ r.images_view[0].url }}" alt="{{ r.title }}" loading="lazy"
             decoding="async" referrerpolicy="no-referrer"
             onerror="this.parentNode.classList.add('noimg');this.remove()"
             class="w-full h-36 object-cover">
        <span class="absolute top-2 left-2 seal seal-sm bg-white/95 g-{{ r.grade or 'none' }}"
              title="{{ r.grade_label }}">{{ r.grade or '—' }}</span>
      </div>
      <div class="p-2.5">
        <div class="text-lg font-semibold">{{ "{:,.0f}".format(r.opening_price or 0) }}
          <span class="text-xs font-normal text-slate-500">บาท</span></div>
        <div class="mt-0.5 text-xs text-slate-600 line-clamp-1">{{ r.title }}</div>
        <div class="mt-1.5 flex flex-wrap gap-1">
          {% if r.discount_pct %}<span class="text-[11px] px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700 font-medium">ต่ำกว่าประเมิน {{ r.discount_pct }}%</span>
          {% elif r.special_discount_pct %}<span class="text-[11px] px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700 font-medium">ลดแรง {{ r.special_discount_pct }}%</span>{% endif %}
          {% if r.geo_precision=='parcel' %}<span class="text-[11px] px-1.5 py-0.5 rounded bg-sky-50 text-sky-700">พิกัดจริง</span>{% endif %}
          {% if r.is_fresh %}<span class="text-[11px] px-1.5 py-0.5 rounded bg-amber-50 text-amber-700">มาใหม่</span>{% endif %}
        </div>
      </div>
    </a>
  {% endfor %}
  </div>
</section>
{% endif %}

{% if not rows %}
<div class="sheet p-10 text-center text-slate-500">ไม่มีรายการตรงเงื่อนไข</div>
{% endif %}

<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
{% for r in rows %}
<a href="/p/{{ r.source_code }}/{{ r.external_ref }}"
   class="sheet overflow-hidden transition block">
  <div class="relative imgwrap" style="background:#EEF1F3">
    <img src="{{ r.images_view[0].url }}" alt="{{ r.title }}"
         loading="lazy" decoding="async" referrerpolicy="no-referrer"
         onerror="this.parentNode.classList.add('noimg');this.remove()"
         class="w-full h-48 object-cover">
    <span class="absolute top-2.5 left-2.5 flex items-center gap-2">
      <span class="seal seal-sm bg-white/95 g-{{ r.grade or 'none' }}"
            title="{{ r.grade_label }}">{{ r.grade or '—' }}</span>
      {% if r.institution_name %}
      <span class="text-[11px] px-2 py-1 rounded bg-white/95 font-medium tracking-wide">
        {{ r.institution_name }}</span>{% endif %}
    </span>
    {% if r.discount_pct %}
    <span class="absolute top-2 right-2 text-xs px-2 py-1 rounded bg-white/95 text-emerald-700 font-medium">
      ต่ำกว่าประเมิน {{ r.discount_pct }}%</span>
    {% endif %}
  </div>
  <div class="p-3">
    <div class="text-xl font-semibold">{{ "{:,.0f}".format(r.opening_price or 0) }} <span class="text-sm font-normal text-slate-500">บาท</span></div>
    {% if r.price_per_sqwa %}<div class="text-xs text-slate-500">{{ "{:,.0f}".format(r.price_per_sqwa) }} บาท/ตร.ว.</div>{% endif %}
    <div class="mt-1.5 font-medium text-sm line-clamp-2">{{ r.title }}</div>
    <div class="mt-1 text-xs text-slate-600 flex flex-wrap gap-x-3 gap-y-0.5">
      {% if r.land_area_sqwa %}<span>{{ r.land_area_sqwa }} ตร.ว.</span>{% endif %}
      {% if r.usable_area_sqm %}<span>{{ r.usable_area_sqm }} ตร.ม.</span>{% endif %}
      {% if r.bedrooms %}<span>{{ r.bedrooms }} นอน</span>{% endif %}
      {% if r.bathrooms %}<span>{{ r.bathrooms }} น้ำ</span>{% endif %}
    </div>
    {% if (not r.discount_pct and r.special_discount_pct) or r.geo_precision=='parcel' or r.is_fresh %}
    <div class="mt-2 flex flex-wrap gap-1">
      {% if not r.discount_pct and r.special_discount_pct %}<span class="text-[11px] px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700 font-medium">ลดแรง {{ r.special_discount_pct }}%</span>{% endif %}
      {% if r.geo_precision=='parcel' %}<span class="text-[11px] px-1.5 py-0.5 rounded bg-sky-50 text-sky-700">พิกัดจริง</span>{% endif %}
      {% if r.is_fresh %}<span class="text-[11px] px-1.5 py-0.5 rounded bg-amber-50 text-amber-700">มาใหม่</span>{% endif %}
    </div>
    {% endif %}
    {% if r.nearest_station_m and r.nearest_station_m <= 3000 %}
    <div class="mt-2">
      <span class="text-[11px] px-1.5 py-0.5 rounded font-medium" style="background:#E7F1FB;color:var(--sky)">🚉 ใกล้รถไฟฟ้า {% if r.nearest_station_m < 1000 %}{{ r.nearest_station_m|round|int }} ม.{% else %}{{ (r.nearest_station_m/1000)|round(1) }} กม.{% endif %}{% if r.nearest_station_name %} · {{ r.nearest_station_name }}{% endif %}</span>
    </div>
    {% endif %}
    {% if r.auction_date %}
    <div class="mt-2 text-xs text-slate-500">ขาย {{ r.auction_date }}{% if r.auction_round %} · นัดที่ {{ r.auction_round }}{% endif %}</div>
    {% endif %}
    {% if r.flags %}
    <div class="mt-2 flex flex-wrap gap-1">
      {% for f in r.flags[:3] %}
      {% set st = severity_style.get(f.severity, severity_style['info']) %}
      <span class="text-[11px] px-1.5 py-0.5 rounded border {{ st[0] }}">{{ f.code }}</span>
      {% endfor %}
    </div>
    {% endif %}
  </div>
</a>
{% endfor %}
</div>
<script>
// ปุ่มพับตัวกรองบนมือถือ (เดสก์ท็อปโชว์เสมอด้วย sm:block)
function fltToggle(btn){
  var w=document.getElementById('filterwrap');
  var open=w.classList.toggle('hidden')===false;
  btn.setAttribute('aria-expanded', open?'true':'false');
}
// อำเภอต้องสัมพันธ์กับจังหวัดที่เลือก
// ทำในหน้าเลย ไม่ต้องรีเฟรช จะได้ไม่เสียค่าที่กรอกไว้ช่องอื่น
const DISTRICTS = {{ districts_by_province | tojson }};
function syncDistricts() {
  const prov = document.getElementById('f-province').value;
  const sel  = document.getElementById('f-district');
  const keep = sel.value;
  const list = prov ? (DISTRICTS[prov] || []) : Object.values(DISTRICTS).flat().sort();
  sel.innerHTML = '<option value="">ทั้งหมด</option>';
  list.forEach(d => {
    const o = document.createElement('option');
    o.value = d; o.textContent = d;
    if (d === keep) o.selected = true;
    sel.appendChild(o);
  });
}
document.addEventListener('DOMContentLoaded', syncDistricts);
</script>

{% if pages > 1 %}
<nav class="mt-6 flex items-center justify-center gap-1 flex-wrap">
  {% set sep = '&' if qs else '?' %}
  {% if page > 1 %}
  <a href="/{{ qs }}{{ sep }}page={{ page - 1 }}"
     class="px-3 py-1.5 text-sm border rounded-lg bg-white hover:bg-slate-50">ก่อนหน้า</a>
  {% endif %}
  {% for p in range(1, pages + 1) %}
    {% if p == 1 or p == pages or (p >= page - 2 and p <= page + 2) %}
    <a href="/{{ qs }}{{ sep }}page={{ p }}"
       class="px-3 py-1.5 text-sm border rounded-lg
              {% if p == page %}bg-slate-900 text-white{% else %}bg-white hover:bg-slate-50{% endif %}">{{ p }}</a>
    {% elif p == page - 3 or p == page + 3 %}
    <span class="px-1 text-slate-400">...</span>
    {% endif %}
  {% endfor %}
  {% if page < pages %}
  <a href="/{{ qs }}{{ sep }}page={{ page + 1 }}"
     class="px-3 py-1.5 text-sm border rounded-lg bg-white hover:bg-slate-50">ถัดไป</a>
  {% endif %}
</nav>
{% endif %}
</div>{# /main column #}

{% if promoted %}
<aside class="hidden xl:block">
  <div class="sticky top-16">
    <div class="flex items-center gap-2 mb-3">
      <span class="text-sm font-semibold" style="color:var(--seal)">🔥 ทรัพย์โปรโมท</span>
      <span class="text-[11px] text-slate-400">แนะนำพิเศษ</span>
    </div>
    <div class="space-y-3">
    {% for r in promoted %}
      <a href="/p/{{ r.source_code }}/{{ r.external_ref }}"
         class="sheet flex gap-3 p-2 hover:no-underline"
         style="box-shadow:0 1px 3px rgba(226,70,55,.10);border-color:#F1C9C4">
        <div class="relative w-24 h-20 shrink-0 imgwrap rounded-lg overflow-hidden" style="background:#EEF1F3">
          <img src="{{ r.images_view[0].url }}" alt="{{ r.title }}" loading="lazy"
               referrerpolicy="no-referrer"
               onerror="this.parentNode.classList.add('noimg');this.remove()"
               class="w-24 h-20 object-cover">
          <span class="absolute top-1 left-1 seal seal-sm bg-white/95 g-{{ r.grade or 'none' }}"
                style="width:24px;height:24px;font-size:12px">{{ r.grade or '—' }}</span>
        </div>
        <div class="min-w-0 py-0.5">
          <div class="font-semibold text-sm num">{{ "{:,.0f}".format(r.opening_price or 0) }}
            <span class="text-[11px] font-normal text-slate-500">บาท</span></div>
          <div class="text-xs text-slate-500 line-clamp-2 leading-snug mt-0.5">{{ r.title }}</div>
          {% if r.discount_pct %}<span class="inline-block mt-1 text-[10px] px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700">ต่ำกว่าประเมิน {{ r.discount_pct }}%</span>{% endif %}
        </div>
      </a>
    {% endfor %}
    </div>
    <div class="text-[11px] text-slate-400 mt-3 leading-relaxed">
      พื้นที่โปรโมททรัพย์ · จัดการรายการได้จากหลังบ้าน (เร็วๆ นี้)
    </div>
  </div>
</aside>
{% endif %}
</div>{# /xl grid #}
{% endblock %}
{% block track %}<script>npaTrack('view_list',{province:{{ (province or '')|tojson }}});</script>{% endblock %}
""",

"detail.html": """
{% extends "layout.html" %}{% block body %}
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>

<a href="/" class="text-sm text-slate-600 hover:text-slate-900">← กลับไปรายการ</a>

<!-- สรุปหัวเรื่องบนมือถือ — โชว์ชื่อ/ราคา/เกรดทันทีก่อนเลื่อน (เดสก์ท็อปใช้ sidebar แทน) -->
<div class="mt-3 sheet p-4 flex items-center gap-3 lg:hidden">
  <span class="seal seal-lg g-{{ r.grade or 'none' }} shrink-0">{{ r.grade or '—' }}</span>
  <div class="min-w-0 flex-1">
    <h1 class="text-base font-semibold leading-snug line-clamp-2">{{ r.title }}</h1>
    <div class="text-xl font-semibold num mt-0.5">{{ "{:,.0f}".format(r.opening_price or 0) }}
      <span class="text-xs font-normal text-slate-500">บาท</span></div>
    {% if r.discount_pct %}<div class="text-xs text-emerald-700">ต่ำกว่าประเมิน {{ r.discount_pct }}%</div>{% endif %}
  </div>
  <a href="#contact" class="shrink-0 text-xs px-3 py-2 rounded-lg text-white font-medium"
     style="background:var(--survey)">สนใจ</a>
</div>

<div class="mt-3 grid gap-5 lg:grid-cols-3">
<div class="lg:col-span-2 space-y-5">

  <div class="sheet overflow-hidden">
    <div class="relative imgwrap" style="min-height:20rem">
      <img id="hero" src="{{ r.images_view[0].url }}" alt="{{ r.title }}"
           referrerpolicy="no-referrer" onerror="this.parentNode.classList.add('noimg');this.remove()"
           class="w-full h-80 object-cover">
    </div>
    {% if r.images_view|length > 1 %}
    <div class="p-2 flex gap-2 overflow-x-auto">
      {% for im in r.images_view %}
      <img src="{{ im.url }}" onclick="document.getElementById('hero').src=this.src"
           class="h-16 w-24 object-cover rounded cursor-pointer border">
      {% endfor %}
    </div>
    {% endif %}
    <div class="px-3 pb-3 text-xs text-slate-500">
      {% if r.images_view[0].is_placeholder %}ยังไม่มีรูปของทรัพย์นี้ในระบบ
      {% elif r.images_view[0].attribution %}ที่มาของรูป: {{ r.images_view[0].attribution }}{% endif %}
      {% if r.hidden_images %}
      · ซ่อนรูปไว้ {{ r.hidden_images }} ใบ เพราะยังไม่มีสิทธิ์เผยแพร่
      {% endif %}
    </div>
  </div>

  <div class="sheet p-4">
    <h2 class="font-semibold mb-3">รายละเอียดทรัพย์</h2>
    <dl class="grid grid-cols-2 sm:grid-cols-3 gap-y-3 gap-x-4 text-sm">
      {% for k, v in specs %}
      <div><dt class="text-slate-500 text-xs">{{ k }}</dt><dd class="font-medium">{{ v }}</dd></div>
      {% endfor %}
    </dl>
  </div>

  {% if r.flags %}
  <div class="sheet p-4" id="why">
    <h2 class="font-semibold mb-3">ทำไมได้เกรดนี้ — ข้อดีข้อเสียที่ระบบตรวจพบ</h2>
    <div class="grid gap-2">
      {% for f in r.flags %}
      {% set st = severity_style.get(f.severity, severity_style['info']) %}
      <div class="border rounded-lg px-3 py-2 text-sm {{ st[0] }}">
        <span class="font-medium">{{ st[1] }}</span>
        <span class="text-xs opacity-70">· {{ f.code }}</span>
        <div class="mt-0.5">{{ f.evidence }}</div>
      </div>
      {% endfor %}
    </div>
    <p class="mt-3 text-xs text-slate-500">
      ทุกข้อมาจากกฎที่ตรวจสอบได้ ไม่ใช่การคาดเดา แต่ยังต้องยืนยันกับเอกสารจริงก่อนตัดสินใจ
    </p>
  </div>
  {% endif %}

  {% if r.lat and r.lng %}
  <div class="sheet p-4">
    <div class="flex items-center justify-between mb-3">
      <h2 class="font-semibold">ทำเล</h2>
      <span id="nearstation" class="text-xs" style="color:var(--survey)"></span>
    </div>
    <div id="minimap" class="rounded-lg border" style="height:300px"></div>
    <p class="mt-2 text-xs text-slate-500">
      {% if r.geo_precision == 'parcel' %}พิกัดระดับแปลงจากต้นทาง
      {% elif r.geo_precision == 'subdistrict' %}พิกัดโดยประมาณระดับตำบล — ตรวจตำแหน่งจริงกับต้นทางอีกครั้ง
      {% else %}พิกัดโดยประมาณระดับอำเภอ/จังหวัด — ยังไม่ใช่ตำแหน่งจริงของทรัพย์{% endif %}
      · จุดวงกลม = สถานีรถไฟฟ้า เส้น = แนวเส้นทาง/เวนคืน
    </p>
  </div>
  {% endif %}
</div>

<aside class="space-y-5">
  {% if is_admin %}
  <form method="post" action="/admin/promoted/add"
        class="sheet p-3 flex items-center gap-2 text-sm"
        style="border-color:#F1C9C4;background:#FFF7F6">
    <input type="hidden" name="token" value="{{ admin_token }}">
    <input type="hidden" name="source_code" value="{{ r.source_code }}">
    <input type="hidden" name="external_ref" value="{{ r.external_ref }}">
    <input type="hidden" name="rank" value="10">
    <input type="hidden" name="next" value="/p/{{ r.source_code }}/{{ r.external_ref }}?token={{ admin_token }}">
    <span class="text-slate-500">แอดมิน:</span>
    <button class="text-white rounded-lg px-3 py-1.5 text-xs font-medium" style="background:var(--seal)">📌 ดันโปรโมท</button>
    {% if r.source_code == 'manual' %}<a href="/admin/add?ref={{ r.external_ref }}&token={{ admin_token }}" class="text-xs brandlink">✏️ แก้ไข</a>{% endif %}
    <a href="/admin/promoted?token={{ admin_token }}" class="text-xs brandlink ml-auto">โปรโมท →</a>
  </form>
  {% endif %}
  <div class="sheet p-4">
    <div class="text-xs text-slate-500">
      {% if r.show_market_code %}{{ r.external_ref }}{% else %}รหัสภายใน{% endif %}
      {% if is_admin and not r.source_link_visible %}
      <span class="ml-1 text-violet-600">· ลิงก์ต้นทางซ่อนจากผู้ใช้ทั่วไป</span>
      {% endif %}
    </div>
    <h1 class="text-lg font-semibold mt-0.5">{{ r.title }}</h1>
    <div class="text-3xl font-semibold mt-2">{{ "{:,.0f}".format(r.opening_price or 0) }}</div>
    <div class="text-sm text-slate-500">บาท (ราคาเปิด)</div>
    {% if r.appraised_price %}
    <div class="mt-2 text-sm">ราคาประเมิน {{ "{:,.0f}".format(r.appraised_price) }} บาท
      {% if r.discount_pct %}<span class="text-emerald-700">· ต่ำกว่า {{ r.discount_pct }}%</span>{% endif %}
    </div>
    {% endif %}
    <div class="mt-4 pt-4 border-t flex items-center gap-4" style="border-color:var(--rule)">
      <span class="seal seal-lg g-{{ r.grade or 'none' }}">{{ r.grade or '—' }}</span>
      <div class="text-sm">
        <div class="font-medium display text-base">{{ r.grade_label }}</div>
        <div class="text-xs text-slate-500">
          {% if r.grade_score %}คะแนน {{ r.grade_score }}/100{% else %}ยังให้เกรดไม่ได้{% endif %}
          · แหล่ง {{ r.institution_name or '-' }}</div>
      </div>
    </div>
    {% if r.grade_score %}
    <div class="mt-3 h-2 rounded-full bg-slate-100 overflow-hidden">
      <div class="h-full rounded-full g-{{ r.grade or 'none' }}"
           style="width:{{ r.grade_score }}%;background:currentColor"></div>
    </div>
    {% endif %}
    {% if r.flags %}
    <a href="#why" class="mt-3 inline-flex items-center gap-1 text-xs font-medium"
       style="color:var(--survey)">ทำไมได้เกรดนี้ →</a>
    {% endif %}
  </div>

  <!-- แชร์ -->
  <div class="sheet p-3 flex items-center gap-2 flex-wrap text-sm">
    <span class="font-medium text-slate-600">แชร์:</span>
    <a href="#" onclick="return shareLine()" class="px-3 py-1.5 rounded-lg text-white text-xs font-medium" style="background:#06C755">LINE</a>
    <a href="#" onclick="return shareFb()" class="px-3 py-1.5 rounded-lg text-white text-xs font-medium" style="background:#1877F2">Facebook</a>
    <button onclick="copyLink(this)" class="px-3 py-1.5 rounded-lg border text-xs font-medium text-slate-600 hover:bg-slate-50">คัดลอกลิงก์</button>
    {% if line_login %}
    <button type="button" onclick="toggleFav(this)" data-sc="{{ r.source_code }}" data-ref="{{ r.external_ref }}"
      aria-pressed="{{ 'true' if is_fav else 'false' }}"
      class="ml-auto px-3 py-1.5 rounded-lg border text-xs font-medium hover:bg-slate-50"
      style="border-color:var(--seal);color:var(--seal)">{{ '❤️ บันทึกแล้ว' if is_fav else '🤍 บันทึกทรัพย์นี้' }}</button>
    {% endif %}
  </div>

  {% if r.opening_price %}
  <!-- เครื่องคำนวณผ่อน (ประมาณการ) -->
  <div class="sheet p-4">
    <h2 class="font-semibold text-sm mb-2">คำนวณค่าผ่อนคร่าว ๆ</h2>
    <div class="grid grid-cols-3 gap-2 text-[11px] text-slate-500">
      <label>เงินดาวน์ %<input id="m-dp" type="number" value="10" min="0" max="100"
        oninput="calcMortgage()" class="mt-0.5 w-full border rounded-lg px-2 py-1.5 text-sm text-slate-800"></label>
      <label>ดอกเบี้ย %/ปี<input id="m-rate" type="number" value="5" step="0.1" min="0"
        oninput="calcMortgage()" class="mt-0.5 w-full border rounded-lg px-2 py-1.5 text-sm text-slate-800"></label>
      <label>ระยะเวลา (ปี)<input id="m-yr" type="number" value="30" min="1" max="40"
        oninput="calcMortgage()" class="mt-0.5 w-full border rounded-lg px-2 py-1.5 text-sm text-slate-800"></label>
    </div>
    <div class="mt-3 flex items-baseline justify-between">
      <span class="text-sm text-slate-500">ผ่อน/เดือน</span>
      <span class="text-xl font-bold num" style="color:var(--survey-deep)"><span id="m-out">-</span>
        <span class="text-xs font-normal text-slate-500">บาท</span></span>
    </div>
    <div class="text-[11px] text-slate-400 mt-1">วงเงินกู้ ~<span id="m-loan">-</span> บาท ·
      ประมาณการเบื้องต้น ไม่รวมค่าธรรมเนียม/ประกัน — ตรวจสอบกับธนาคารจริงอีกครั้ง</div>
  </div>
  <script>
    var PRICE0 = {{ r.opening_price or 0 }};
    function _fmt(n){return Math.round(n).toLocaleString('th-TH');}
    function calcMortgage(){
      var dp=+document.getElementById('m-dp').value||0;
      var rate=+document.getElementById('m-rate').value||0;
      var yr=+document.getElementById('m-yr').value||1;
      var loan=PRICE0*(1-dp/100); if(loan<0)loan=0;
      var r=rate/100/12, n=yr*12;
      var m=r>0?loan*r/(1-Math.pow(1+r,-n)):loan/n;
      document.getElementById('m-out').textContent = m>0?_fmt(m):'-';
      document.getElementById('m-loan').textContent = _fmt(loan);
    }
    function shareLine(){window.open('https://social-plugins.line.me/lineit/share?url='+encodeURIComponent(location.href),'_blank');return false;}
    function shareFb(){window.open('https://www.facebook.com/sharer/sharer.php?u='+encodeURIComponent(location.href),'_blank');return false;}
    function copyLink(b){navigator.clipboard.writeText(location.href).then(function(){var t=b.textContent;b.textContent='คัดลอกแล้ว ✓';setTimeout(function(){b.textContent=t;},1500);});}
    calcMortgage();
  </script>
  {% endif %}

  {% if r.forecast %}
  <div class="sheet p-4">
    <h2 class="font-semibold text-sm">มูลค่าเทียบเคียงใน {{ (r.forecast.horizon_months/12)|int }} ปี</h2>
    <div class="mt-2 space-y-1 text-sm">
      <div class="flex justify-between"><span class="text-slate-500">แย่</span><span>{{ "{:,.0f}".format(r.forecast.bear) }}</span></div>
      <div class="flex justify-between font-semibold"><span>กลาง</span><span>{{ "{:,.0f}".format(r.forecast.mid) }}</span></div>
      <div class="flex justify-between"><span class="text-slate-500">ดี</span><span>{{ "{:,.0f}".format(r.forecast.bull) }}</span></div>
    </div>
    <div class="mt-3 text-xs px-2 py-1 rounded inline-block
      {% if r.forecast.confidence == 'high' %}bg-emerald-50 text-emerald-800
      {% elif r.forecast.confidence == 'medium' %}bg-amber-50 text-amber-800
      {% else %}bg-slate-100 text-slate-600{% endif %}">
      ความเชื่อมั่น: {{ r.forecast.confidence }}
    </div>
    <p class="mt-2 text-xs text-slate-500">{{ r.forecast.confidence_reason }}</p>
    <p class="mt-2 text-xs text-slate-500">
      เป็นการเทียบกับเหตุการณ์ในอดีต ไม่ใช่การพยากรณ์ราคาและไม่ใช่คำแนะนำการลงทุน
    </p>
  </div>
  {% else %}
  <div class="sheet p-4 text-sm text-slate-500">
    ยังไม่มีข้อมูลเทียบเคียงพอสำหรับประเมินมูลค่าอนาคตของทรัพย์นี้
  </div>
  {% endif %}

  {% if comps %}
  <div class="sheet p-4">
    <h2 class="font-semibold text-sm">เทียบราคาตลาดในเขตนี้</h2>
    <div class="mt-2 space-y-2 text-sm">
      {% for row in comps.rows %}
      <div class="flex items-baseline justify-between gap-2
                  {% if row.freshness == 'เก่าเกินไป' %}opacity-50{% endif %}">
        <div>
          <div>{{ row.source_label }}</div>
          <div class="text-[11px] text-slate-500">
            {% if row.price_kind == 'asking' %}ตั้งขาย{% else %}ปิดจริง{% endif %}
            · n={{ row.n_listings }} · {{ row.days }} วันที่แล้ว</div>
        </div>
        <div class="text-right">
          <div class="font-medium">{{ "{:,.0f}".format(row.market_median) }}</div>
          {% if row.adj_gap %}<div class="text-[11px] text-emerald-700">ถูกกว่า {{ row.adj_gap }}%</div>{% endif %}
        </div>
      </div>
      {% endfor %}
    </div>
    <a href="/compare?province={{ r.province }}&district={{ r.district }}"
       class="mt-3 inline-block text-xs text-slate-600 hover:text-slate-900">ดูตารางเต็ม →</a>
  </div>
  {% endif %}

  {% if dupes %}
  <div class="sheet p-4">
    <div class="flex items-center gap-2">
      <h2 class="font-semibold text-sm">🔁 ทรัพย์แปลงเดียวกันในแหล่งอื่น</h2>
      <span class="text-[11px] px-1.5 py-0.5 rounded-full text-white" style="background:var(--sky)">{{ dupes|length }} แหล่ง</span>
    </div>
    <p class="mt-1 text-[11px] text-slate-500">พิกัดระดับแปลงตรงกัน — น่าจะเป็นทรัพย์เดียวกันที่ถูกประกาศหลายที่ เทียบราคาก่อนตัดสินใจ</p>
    {% if dupe_cheapest and r.opening_price and r.opening_price > dupe_cheapest %}
    <div class="mt-2 text-xs px-2 py-1.5 rounded bg-amber-50 text-amber-800">⚠️ แหล่งอื่นมีราคาต่ำกว่า — ถูกสุด {{ "{:,.0f}".format(dupe_cheapest) }} บาท</div>
    {% elif dupe_cheapest and r.opening_price and r.opening_price <= dupe_cheapest %}
    <div class="mt-2 text-xs px-2 py-1.5 rounded bg-emerald-50 text-emerald-800">✓ รายการนี้ราคาต่ำสุดในบรรดาแหล่งที่พบ</div>
    {% endif %}
    <div class="mt-2 space-y-1.5 text-sm">
      {% for d in dupes %}
      <a href="/p/{{ d.source_code }}/{{ d.external_ref }}{% if admin_token %}?token={{ admin_token }}{% endif %}"
         class="flex items-baseline justify-between gap-2 py-1 border-b last:border-0 hover:bg-slate-50 -mx-1 px-1 rounded">
        <div>
          <div class="font-medium">{{ d.institution_name or 'อีกแหล่ง' }}
            {% if d.grade %}<span class="text-[10px] px-1 rounded bg-slate-100 g-{{ d.grade }}">{{ d.grade }}</span>{% endif %}</div>
          <div class="text-[11px] text-slate-500">{{ d.type_label }}{% if d.district %} · {{ d.district }}{% endif %}</div>
        </div>
        <div class="text-right whitespace-nowrap">
          <div class="font-semibold">{{ "{:,.0f}".format(d.opening_price or 0) }}</div>
          {% if r.opening_price and d.opening_price and d.opening_price < r.opening_price %}
          <div class="text-[11px] text-emerald-700">ถูกกว่า {{ "{:,.0f}".format(r.opening_price - d.opening_price) }}</div>
          {% elif r.opening_price and d.opening_price and d.opening_price > r.opening_price %}
          <div class="text-[11px] text-slate-400">แพงกว่า {{ "{:,.0f}".format(d.opening_price - r.opening_price) }}</div>
          {% endif %}
        </div>
      </a>
      {% endfor %}
    </div>
  </div>
  {% endif %}

  <div class="sheet p-4" id="contact">
    <h2 class="font-semibold text-sm">สนใจทรัพย์นี้</h2>
    <p class="mt-1 text-xs text-slate-500">ทิ้งข้อมูลไว้ เดี๋ยวติดต่อกลับพร้อมข้อมูลเพิ่มเติม</p>

    {% if contact_line_url %}
    <a href="{{ contact_line_url }}" target="_blank" rel="noopener noreferrer"
       onclick="npaTrack('inquire',{source_code:'{{ r.source_code }}',external_ref:'{{ r.external_ref }}',meta:{via:'line'}})"
       class="mt-3 flex items-center justify-center gap-2 bg-[#06C755] text-white
              rounded-lg px-4 py-2.5 text-sm font-medium hover:opacity-90">
      คุยผ่าน LINE (เร็วที่สุด)
    </a>
    <div class="my-3 flex items-center gap-2 text-xs text-slate-400">
      <span class="flex-1 border-t"></span>หรือกรอกฟอร์ม<span class="flex-1 border-t"></span>
    </div>
    {% endif %}

    <form method="post" action="/api/inquire" class="mt-2 space-y-2">
      <input type="hidden" name="source_code" value="{{ r.source_code }}">
      <input type="hidden" name="external_ref" value="{{ r.external_ref }}">
      <input type="text" name="website" class="hidden" tabindex="-1" autocomplete="off">

      <input name="contact_name" required placeholder="ชื่อที่ให้เรียก"
             class="w-full border rounded-lg px-3 py-2 text-sm">
      <input name="phone" required placeholder="เบอร์โทร"
             class="w-full border rounded-lg px-3 py-2 text-sm">
      <input name="line_id" placeholder="LINE ID (ถ้ามี)"
             class="w-full border rounded-lg px-3 py-2 text-sm">

      <select name="funding_source" class="w-full border rounded-lg px-3 py-2 text-sm">
        <option value="">แหล่งเงินที่จะใช้ (เลือกได้)</option>
        <option value="cash">เงินสด</option>
        <option value="loan">กู้ธนาคาร</option>
        <option value="unsure">ยังไม่แน่ใจ</option>
      </select>

      <select name="preferred_time" class="w-full border rounded-lg px-3 py-2 text-sm">
        <option value="">ช่วงเวลาที่สะดวก</option>
        <option value="morning">เช้า (9-12)</option>
        <option value="afternoon">บ่าย (13-17)</option>
        <option value="evening">เย็น (17-20)</option>
        <option value="anytime">เวลาไหนก็ได้</option>
      </select>

      <textarea name="message" rows="2" placeholder="อยากถามอะไรเป็นพิเศษ"
                class="w-full border rounded-lg px-3 py-2 text-sm"></textarea>

      <label class="flex items-start gap-2 text-xs text-slate-600">
        <input type="checkbox" name="consent_service" value="1" required class="mt-0.5">
        <span>ยินยอมให้เก็บและใช้ข้อมูลเพื่อติดต่อกลับเรื่องทรัพย์นี้
          <span class="text-red-600">*</span></span>
      </label>
      <label class="flex items-start gap-2 text-xs text-slate-600">
        <input type="checkbox" name="consent_marketing" value="1" class="mt-0.5">
        <span>ยินดีรับข่าวทรัพย์ใหม่ที่ตรงความต้องการ (ยกเลิกได้ทุกเมื่อ)</span>
      </label>

      <button class="w-full bg-slate-900 text-white rounded-lg px-4 py-2.5 text-sm font-medium">
        ส่งข้อมูลติดต่อกลับ</button>
      <p class="text-[11px] text-slate-400 leading-relaxed">
        เราเก็บเฉพาะข้อมูลที่จำเป็นต่อการติดต่อกลับ ไม่ส่งต่อให้บุคคลที่สาม
        โดยไม่ได้รับความยินยอมแยกต่างหาก</p>
    </form>
  </div>

  <div class="sheet p-4">
    <h2 class="font-semibold text-sm mb-2">ลิงก์ที่เกี่ยวข้อง</h2>
    <div class="grid gap-2">
      {% for l in r.links %}
      <a href="{{ l.url }}" target="_blank" rel="noopener noreferrer"
         data-track-source="{{ r.source_code }}" data-track-ref="{{ r.external_ref }}"
         class="text-sm border rounded-lg px-3 py-2 hover:bg-slate-50
                {% if l.confidence=='search' %}border-dashed text-slate-600{% endif %}">
        {{ l.label }}{% if l.confidence=='search' %} ↗{% endif %}
        {% if l.hint %}<div class="text-xs text-slate-500 mt-0.5">{{ l.hint }}</div>{% endif %}
      </a>
      {% endfor %}
    </div>
  </div>

  <div class="sheet p-4 text-xs text-slate-500 leading-relaxed">
    <div class="font-semibold text-slate-700 text-sm mb-1">ข้อมูลนี้มาจากไหน</div>
    {% if r.show_market_code %}แหล่ง: {{ r.source_code }} · เลขอ้างอิง {{ r.external_ref }}
    {% else %}ข้อมูลรวบรวมจากประกาศสาธารณะ{% endif %}<br>
    ระบบเก็บเฉพาะข้อมูลของทรัพย์ ไม่เก็บชื่อคู่ความหรือเลขคดีตาม PDPA
  </div>
</aside>
</div>

{% block track %}<script>
npaTrack('view_detail',{source_code:{{ r.source_code|tojson }},
  external_ref:{{ r.external_ref|tojson }}, province:{{ (r.province or '')|tojson }},
  district:{{ (r.district or '')|tojson }}, property_type:{{ (r.property_type or '')|tojson }}});
</script>{% endblock %}

{% if r.lat and r.lng %}
<script>
const PLAT={{ r.lat }}, PLNG={{ r.lng }};
const m = L.map('minimap').setView([PLAT, PLNG], 15);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom:19, attribution:'&copy; OpenStreetMap contributors'}).addTo(m);
const propMk = L.circleMarker([PLAT, PLNG], {radius:10, color:'#fff', weight:2,
  fillColor:'{{ "#ef4444" if r.has_critical else "#12977A" }}', fillOpacity:0.95})
  .addTo(m).bindTooltip('ตำแหน่งทรัพย์',{direction:'top'});

// ระยะแบบ haversine (เมตร) — ใช้หาสถานีใกล้สุด
function distM(aLat,aLng,bLat,bLng){
  const R=6371000, toR=x=>x*Math.PI/180;
  const dLat=toR(bLat-aLat), dLng=toR(bLng-aLng);
  const s=Math.sin(dLat/2)**2+Math.cos(toR(aLat))*Math.cos(toR(bLat))*Math.sin(dLng/2)**2;
  return 2*R*Math.asin(Math.sqrt(s));
}
// วางซ้อนแนวเส้นทาง + สถานีรถไฟฟ้าจากข้อมูล infra จริง แล้วบอกสถานีใกล้สุด
fetch('/api/infra.geojson').then(r=>r.json()).then(gj=>{
  if(!gj.features) return;
  let best=null;
  gj.features.forEach(f=>{
    if(!f.geometry) return;
    const t=f.geometry.type, col=f.properties.color||'#7c3aed';
    if(t==='Point'||t==='MultiPoint'){
      const cs = t==='Point'?[f.geometry.coordinates]:f.geometry.coordinates;
      cs.forEach(c=>{
        L.circleMarker([c[1],c[0]],{radius:4,color:col,weight:2,fillColor:'#fff',fillOpacity:1})
          .addTo(m).bindTooltip(f.properties.name||'สถานี',{direction:'top'});
        const d=distM(PLAT,PLNG,c[1],c[0]);
        if(!best||d<best.d) best={d,name:f.properties.name||'สถานี'};
      });
    } else {
      L.geoJSON(f,{style:{color:col,weight:3,opacity:.75,
        dashArray:f.properties.certainty>=3?null:'5 4'}}).addTo(m);
    }
  });
  const el=document.getElementById('nearstation');
  if(el&&best&&best.d<=4000){
    const km=best.d>=1000?(best.d/1000).toFixed(1)+' กม.':Math.round(best.d/10)*10+' ม.';
    el.textContent='สถานีใกล้สุด: '+best.name+' ~'+km;
  } else if(el&&best){
    el.textContent='ไม่มีสถานีรถไฟฟ้าในรัศมี 4 กม.';
    el.style.color='var(--pencil)';
  }
});
</script>
{% endif %}
{% endblock %}
""",

"map.html": """
{% extends "layout.html" %}{% block body %}
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css">
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>

<div class="sheet p-3 mb-3 flex flex-wrap gap-4 items-center text-sm">
  <span class="font-medium">สีหมุด</span>
  <span class="flex items-center gap-1.5"><i class="w-3 h-3 rounded-full bg-red-500 inline-block"></i> เสี่ยงสูง</span>
  <span class="flex items-center gap-1.5"><i class="w-3 h-3 rounded-full bg-emerald-500 inline-block"></i> เกรด A-B</span>
  <span class="flex items-center gap-1.5"><i class="w-3 h-3 rounded-full bg-amber-500 inline-block"></i> เกรด C</span>
  <span class="flex items-center gap-1.5"><i class="w-3 h-3 rounded-full bg-slate-400 inline-block"></i> เกรด D-E</span>
  {% if exact %}<span class="ml-auto text-xs" style="color:var(--survey)">
    {{ exact }} รายการมีพิกัดจริงของแปลง</span>{% endif %}
  <a href="/" class="ml-auto text-slate-600 hover:text-slate-900">← กลับไปรายการ</a>
</div>
<!-- ป้ายสีสายรถไฟฟ้า — สร้างจากข้อมูลจริงที่โหลดได้ (โชว์เฉพาะสายที่มีบนแผนที่) -->
<div id="linelegend" class="sheet p-3 mb-3 flex flex-wrap gap-x-4 gap-y-2 items-center text-sm hidden"></div>
{% if with_geo == 0 and total > 0 %}
<div class="mb-3 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
  <b>มีทรัพย์ {{ total }} รายการ แต่ยังไม่มีพิกัดสักตัว</b> จึงยังไม่มีหมุดขึ้นแผนที่<br>
  รันคำสั่งนี้เพื่อหาพิกัดจากชื่ออำเภอ (ใช้เวลาราว 1 วินาทีต่อโซน)
  <code class="mt-1 block bg-white border rounded px-2 py-1">python src/enrich.py geocode</code>
</div>
{% elif with_geo < total %}
<div class="mb-3 rounded-lg border border-slate-200 bg-white px-4 py-2 text-xs text-slate-600">
  แสดง {{ with_geo }} จาก {{ total }} รายการ — ที่เหลือยังไม่มีพิกัด
  รัน <code class="bg-slate-100 px-1 rounded">python src/enrich.py geocode</code> เพื่อเติมให้ครบ
</div>
{% endif %}

{% if coarse %}
<div class="mb-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
  <b>{{ coarse }} รายการยังเป็นพิกัดระดับอำเภอ</b> ทรัพย์ในอำเภอเดียวกันจึงซ้อนกันที่จุดเดียว
  ซึ่งไม่ได้สะท้อนทำเลจริง<br>
  ที่อยู่เต็มอยู่ในหน้ารายละเอียดของต้นทาง ดึงมาแล้วจะได้พิกัดระดับตำบล
  <code class="mt-1 block bg-white border rounded px-2 py-1">python src/enrich.py details --limit 200
&#10;python src/enrich.py geocode</code>
</div>
{% endif %}

{% if institutions %}
<div class="sheet p-3 mb-3 flex flex-wrap gap-3 items-center text-sm">
  <span class="font-medium">แหล่ง</span>
  {% for inst in institutions %}
  <label class="flex items-center gap-1.5 cursor-pointer">
    <input type="checkbox" class="srcflt" value="{{ inst }}" checked onchange="loadProps()">
    {{ inst }}
  </label>
  {% endfor %}
  <button type="button" class="text-xs text-slate-500 underline ml-1"
    onclick="document.querySelectorAll('.srcflt').forEach(b=>b.checked=true);loadProps()">เลือกทั้งหมด</button>
  <span id="srccount" class="ml-auto text-xs text-slate-500"></span>
</div>
{% endif %}

<div id="map" class="rounded-xl border" style="height:72vh"></div>
<div class="mt-3 text-xs text-slate-500 leading-relaxed">
  เส้นทึบ = โครงการถึงขั้น พ.ร.ฎ. แล้ว · เส้นประ = ยังไม่ถึง ·
  แนวเขตเป็นค่าที่เราวาดเอง <b>ต้องยืนยันกับแผนที่ท้าย พ.ร.ฎ. เสมอ</b>
</div>

<script>
const map = L.map('map').setView([13.75, 100.52], 10);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom:19, attribution:'&copy; OpenStreetMap contributors'}).addTo(map);
const colorFor = p => p.critical ? '#ef4444'
  : (p.grade === 'A' || p.grade === 'B') ? '#10b981'
  : p.grade === 'C' ? '#f59e0b' : '#94a3b8';
const baht = n => n ? Number(n).toLocaleString('th-TH',{maximumFractionDigits:0}) : '-';
const cluster = L.markerClusterGroup({maxClusterRadius:45});
// สร้าง layer control ครั้งเดียว (ไม่ผูกกับ zones) แล้วเติมชั้นเมื่อโหลดเสร็จ
// ทุกชั้นมี checkbox ติ๊กออก/เปิดได้
const layerCtrl = L.control.layers(null, {}, {collapsed:false}).addTo(map);

// สีมาตรฐานสายรถไฟฟ้าไทย → ชื่อสาย (ตรงกับ LINE_COLORS ใน load_infra.py)
// ใช้ทำป้ายสี ให้คนอ่านแผนที่รู้ว่าสีไหนคือสายอะไร
const LINE_NAMES = {
  '#69BE28':'สายสุขุมวิท','#0B6E3B':'สายสีลม','#1E52A0':'สายสีน้ำเงิน',
  '#8E258D':'สายสีม่วง','#B01116':'แอร์พอร์ตลิงก์','#E4002B':'สายสีแดง',
  '#CBA63C':'สายสีทอง','#FDD100':'สายสีเหลือง','#E5007D':'สายสีชมพู',
  '#4CAF50':'สายสีเขียว'
};
function buildLineLegend(lineF){
  const seen = new Map();               // สี(ตัวใหญ่) -> ชื่อที่จะโชว์
  lineF.forEach(f=>{
    let col = (f.properties.color||'').toUpperCase();
    if(!col) return;
    if(!seen.has(col)) seen.set(col, LINE_NAMES[col] || f.properties.name || 'แนวเส้นทาง');
  });
  if(!seen.size) return;
  const box = document.getElementById('linelegend');
  if(!box) return;
  box.innerHTML = '<span class="font-medium">สายรถไฟฟ้า</span>' +
    [...seen].map(([c,l])=>`<span class="flex items-center gap-1.5">`+
      `<i style="display:inline-block;width:18px;height:4px;border-radius:2px;background:${c}"></i>`+
      `${l}</span>`).join('') +
    `<span class="ml-auto text-xs text-slate-400">เส้นทึบ = พ.ร.ฎ.แล้ว · เส้นประ = ยังไม่ถึง</span>`;
  box.classList.remove('hidden');
}

fetch('/api/zones.geojson').then(r=>r.json()).then(gj=>{
  if(!gj.features||!gj.features.length) return;
  const lyr = L.geoJSON(gj,{style:{color:'#475569',weight:1,fillOpacity:0.04,dashArray:'4 3'},
    onEachFeature:(f,l)=>{const n=f.properties&&(f.properties.name||f.properties.NAME_2);if(n)l.bindTooltip(n,{sticky:true});}}).addTo(map);
  layerCtrl.addOverlay(lyr,'ขอบเขตโซน');
});

fetch('/api/infra.geojson').then(r=>r.json()).then(gj=>{
  if(!gj.features||!gj.features.length) return;
  const lineF = gj.features.filter(f=>f.geometry && f.geometry.type!=='Point' && f.geometry.type!=='MultiPoint');
  const ptF   = gj.features.filter(f=>f.geometry && (f.geometry.type==='Point' || f.geometry.type==='MultiPoint'));
  // แนวเส้นทาง/เวนคืน = เส้น (สีม่วงทึบ = พ.ร.ฎ.แล้ว, ม่วงอ่อนประ = ยังไม่ถึง)
  if(lineF.length){
    const lines = L.geoJSON({type:'FeatureCollection',features:lineF},{
      style:f=>({color:f.properties.color||(f.properties.certainty>=3?'#7c3aed':'#a78bfa'),
        weight:4,opacity:0.9,dashArray:f.properties.certainty>=3?null:'6 5'}),
      onEachFeature:(f,l)=>l.bindPopup(`<b>${f.properties.name}</b><br>${f.properties.status_label||''}`)
    }).addTo(map);
    layerCtrl.addOverlay(lines,'🚆 แนวเส้นทาง/เวนคืน');
    buildLineLegend(lineF);
  }
  // สถานี = จุด (วงกลม)
  if(ptF.length){
    const pts = L.geoJSON({type:'FeatureCollection',features:ptF},{
      pointToLayer:(f,ll)=>L.circleMarker(ll,{radius:5,color:f.properties.color||'#7c3aed',weight:3,fillColor:'#ffffff',fillOpacity:1}),
      onEachFeature:(f,l)=>l.bindTooltip(f.properties.name,{direction:'top'})
    }).addTo(map);
    layerCtrl.addOverlay(pts,'🚉 สถานีรถไฟฟ้า');
  }
});

map.addLayer(cluster);

// สร้าง query string ตามแหล่งที่ติ๊ก — ติ๊กครบหรือไม่ติ๊กเลย = ทุกแหล่ง
function selectedQS(){
  const all=[...document.querySelectorAll('.srcflt')];
  const on=all.filter(b=>b.checked).map(b=>b.value);
  const p=new URLSearchParams(location.search);
  p.delete('institution');
  if(on.length && on.length < all.length) on.forEach(v=>p.append('institution',v));
  const c=document.getElementById('srccount');
  if(c) c.textContent = (on.length && on.length<all.length)
    ? ('กรอง '+on.length+' แหล่ง') : 'ทุกแหล่ง';
  return p.toString();
}

function loadProps(){
  cluster.clearLayers();
  fetch('/api/properties.geojson?'+selectedQS()).then(r=>r.json()).then(gj=>{
  const bounds=[];
  gj.features.forEach(f=>{
    const p=f.properties, c=f.geometry.coordinates;
    const mk=L.circleMarker([c[1],c[0]],{radius:8,color:'#fff',weight:2,fillColor:colorFor(p),fillOpacity:0.95});
    const gradeColor = {A:'#059669',B:'#65a30d',C:'#f59e0b',D:'#ea580c',E:'#dc2626'};
    mk.bindPopup(`<div style="min-width:250px;max-width:280px">
      <img src="${p.image}" loading="lazy" referrerpolicy="no-referrer"
           onerror="this.style.display='none'"
           style="width:100%;height:120px;object-fit:cover;border-radius:6px">

      <div style="display:flex;align-items:center;gap:6px;margin-top:7px">
        ${p.grade?`<span style="background:${gradeColor[p.grade]||'#94a3b8'};color:#fff;
          width:22px;height:22px;border-radius:4px;display:inline-flex;align-items:center;
          justify-content:center;font-weight:700;font-size:12px">${p.grade}</span>`:''}
        ${p.institution?`<span style="font-size:11px;background:#f1f5f9;padding:2px 7px;
          border-radius:4px;color:#334155">${p.institution}</span>`:''}
        ${p.type_label?`<span style="font-size:11px;color:#64748b">${p.type_label}</span>`:''}
      </div>

      <div style="font-weight:600;margin-top:5px;line-height:1.35">${p.title}</div>

      <div style="font-size:19px;font-weight:600;margin:5px 0 2px">${baht(p.price)}
        <span style="font-size:12px;font-weight:400;color:#64748b">บาท</span></div>
      ${p.price_per_sqwa?`<div style="font-size:11px;color:#64748b">${baht(p.price_per_sqwa)} บาท/ตร.ว.</div>`:''}
      ${p.discount?`<div style="color:#047857;font-size:12px;margin-top:2px">ต่ำกว่าประเมิน ${p.discount}%</div>`:''}

      <div style="font-size:11px;color:#64748b;margin-top:5px">
        ${p.area?`${p.area} ตร.ว.`:''}${p.area&&p.usable?' · ':''}${p.usable?`${p.usable} ตร.ม.`:''}
      </div>

      ${p.geo_precision==='parcel'
        ?`<div style="font-size:10px;color:#1F6F5C;margin-top:4px">พิกัดจริงของแปลง</div>`
        :p.geo_precision==='subdistrict'
        ?`<div style="font-size:10px;color:#94a3b8;margin-top:4px">พิกัดระดับตำบล</div>`
        :(p.geo_precision?`<div style="font-size:10px;color:#b45309;margin-top:4px">
           พิกัดระดับ${p.geo_precision==='province'?'จังหวัด':'อำเภอ'} — ทรัพย์ในพื้นที่เดียวกันซ้อนกัน</div>`:'')}
      ${p.address?`<div style="font-size:11px;color:#475569;margin-top:4px">${p.address}</div>`:''}

      <div style="margin-top:9px;display:flex;gap:6px;flex-wrap:wrap">
        <a href="${p.detail_url}" style="flex:1;text-align:center;padding:6px 10px;
           background:#0f172a;color:#fff;border-radius:6px;font-size:12px;
           text-decoration:none">ดูรายละเอียด</a>
        ${p.source_url?`<a href="${p.source_url}" target="_blank" rel="noopener noreferrer"
           style="padding:6px 10px;border:1px solid #cbd5e1;border-radius:6px;
           font-size:12px;text-decoration:none;color:#0f172a">ต้นทาง</a>`:''}
      </div>
      ${p.ref?`<div style="font-size:10px;color:#94a3b8;margin-top:6px">${p.ref}</div>`:''}
    </div>`);
    cluster.addLayer(mk); bounds.push([c[1],c[0]]);
  });
  if(bounds.length) map.fitBounds(bounds,{padding:[40,40],maxZoom:14});
  // แสดงจำนวนผลลัพธ์จริงหลังกรอง — ทำให้เห็นว่าตัวกรองทำงาน
  const c=document.getElementById('srccount');
  if(c){
    const all=[...document.querySelectorAll('.srcflt')];
    const on=all.filter(b=>b.checked).length;
    const flt=(on && on<all.length) ? (' · กรอง '+on+' แหล่ง') : ' · ทุกแหล่ง';
    c.textContent='แสดง '+(gj.features.length).toLocaleString('th-TH')+' ทรัพย์'+flt;
  }
  });
}

loadProps();
</script>
{% endblock %}
""",

"compare.html": """
{% extends "layout.html" %}{% block body %}
<h1 class="text-lg font-semibold">ราคาตลาด เทียบ ราคาประมูล</h1>
<p class="mt-1 text-sm text-slate-600 max-w-3xl">
  ราคาจากเว็บประกาศขายเป็น <b>ราคาตั้งขาย</b> ไม่ใช่ราคาปิด
  การเทียบตรง ๆ จะทำให้ส่วนต่างดูใหญ่เกินจริง ตารางนี้จึงแสดงทั้งสองค่า
</p>

{% for b in blocks %}
<section class="mt-5 bg-white border rounded-xl overflow-hidden">
  <div class="px-4 py-3 border-b flex flex-wrap items-baseline gap-x-3 gap-y-1">
    <h2 class="font-semibold">{{ b.district or b.province }} · {{ type_labels.get(b.property_type, b.property_type) }}</h2>
    {% if b.auction_median %}
    <span class="text-sm text-slate-600">ราคาปิดประมูลกลาง
      <b class="text-slate-900">{{ "{:,.0f}".format(b.auction_median) }}</b> บาท
      <span class="text-xs text-slate-500">(n={{ b.n_auction }})</span></span>
    {% else %}
    <span class="text-sm text-amber-700">ยังไม่มีราคาปิดประมูลในพื้นที่นี้</span>
    {% endif %}
  </div>
  <div class="overflow-x-auto">
  <table class="w-full text-sm min-w-[680px]">
    <thead class="bg-slate-50 text-left text-xs text-slate-600"><tr>
      <th class="px-4 py-2">แหล่ง</th><th class="px-4 py-2">ชนิดราคา</th>
      <th class="px-4 py-2 text-right">ราคากลาง</th><th class="px-4 py-2 text-right">n</th>
      <th class="px-4 py-2 text-right">ส่วนต่างดิบ</th>
      <th class="px-4 py-2 text-right">ส่วนต่างหลังปรับ</th>
      <th class="px-4 py-2">อัปเดตล่าสุด</th>
    </tr></thead>
    <tbody>
    {% for row in b.rows %}
    <tr class="border-t {% if row.freshness == 'เก่าเกินไป' %}opacity-50{% endif %}">
      <td class="px-4 py-2">
        {% if row.search_url %}<a href="{{ row.search_url }}" target="_blank"
          rel="noopener noreferrer" class="underline decoration-dotted">{{ row.source_label }}</a>
        {% else %}{{ row.source_label }}{% endif %}
      </td>
      <td class="px-4 py-2">
        {% if row.price_kind == 'asking' %}
        <span class="text-xs px-1.5 py-0.5 rounded bg-slate-100">ตั้งขาย</span>
        {% else %}
        <span class="text-xs px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-800">ปิดจริง</span>
        {% endif %}
      </td>
      <td class="px-4 py-2 text-right font-medium">{{ "{:,.0f}".format(row.market_median) }}</td>
      <td class="px-4 py-2 text-right text-slate-500">{{ row.n_listings }}</td>
      <td class="px-4 py-2 text-right">{% if row.raw_gap %}{{ row.raw_gap }}%{% else %}-{% endif %}</td>
      <td class="px-4 py-2 text-right font-semibold text-emerald-700">
        {% if row.adj_gap %}{{ row.adj_gap }}%{% else %}-{% endif %}
        <div class="text-[11px] font-normal text-slate-500">{{ row.adj_note }}</div>
      </td>
      <td class="px-4 py-2">
        <span class="text-xs px-1.5 py-0.5 rounded
          {% if row.freshness == 'สด' %}bg-emerald-50 text-emerald-800
          {% elif row.freshness == 'เริ่มเก่า' %}bg-amber-50 text-amber-800
          {% else %}bg-red-50 text-red-800{% endif %}">{{ row.freshness }}</span>
        <div class="text-[11px] text-slate-500">{{ row.days }} วันที่แล้ว</div>
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
</section>
{% else %}
<div class="mt-5 bg-white border rounded-xl p-10 text-center text-slate-500">
  ยังไม่มีข้อมูลเปรียบเทียบ — บันทึกด้วย <code class="bg-slate-100 px-1 rounded">python tools/add_comp.py</code>
</div>
{% endfor %}

<div class="mt-6 bg-white border rounded-xl p-4 text-xs text-slate-600 leading-relaxed">
  <div class="font-semibold text-slate-800 text-sm mb-1">อ่านตารางนี้อย่างไร</div>
  <b>ส่วนต่างดิบ</b> คือเทียบราคาปิดประมูลกับราคาตั้งขายตรง ๆ ตัวเลขนี้สวยแต่หลอก
  เพราะราคาตั้งขายยังไม่ผ่านการต่อรอง<br>
  <b>ส่วนต่างหลังปรับ</b> หักส่วนลดต่อรอง {{ haircut_pct }}% ออกจากราคาตั้งขายก่อนเทียบ
  ค่านี้ใกล้ความจริงกว่า<br>
  <span class="text-amber-700">สมมติฐานส่วนลดต่อรองตอนนี้: {{ haircut_basis }}</span>
  ต้องแทนที่ด้วยค่าจริงจากดีลที่ปิดเอง อย่าปล่อยให้ค่าตั้งต้นค้างเกิน 6 เดือน<br>
  แถวที่จางลงคือข้อมูลเก่าเกิน 45 วัน อย่าใช้ตัดสินใจ<br><br>
  ระบบเก็บเฉพาะ<b>สถิติรวมรายเขต</b> ไม่เก็บประกาศรายชิ้น ไม่เก็บรูปหรือข้อความจากเว็บอื่น
  และลิงก์กลับไปหน้าค้นหาต้นทางเสมอ
</div>
{% endblock %}
""",

"admin.html": """
{% extends "layout.html" %}{% block body %}
<h1 class="text-lg font-semibold">Dashboard หลังบ้าน</h1>
<p class="text-sm text-slate-600 mt-1">ข้อมูล 30 วันล่าสุด</p>
<div class="mt-2 flex flex-wrap gap-2 text-sm">
  <a href="/admin/settings?token={{ admin_token }}" class="rounded border px-3 py-1 hover:bg-slate-50">⚙️ ตั้งค่าระบบ</a>
  <a href="/admin/parcels?token={{ admin_token }}" class="rounded border px-3 py-1 hover:bg-slate-50">📍 กรอกพิกัดแปลง LED</a>
  <a href="/admin/monitor?token={{ admin_token }}" class="rounded border px-3 py-1 hover:bg-slate-50">📊 สถิติคนดู (7/30/90 วัน)</a>
</div>

<section class="mt-5">
  <h2 class="font-semibold mb-2">สุขภาพการดึงข้อมูล</h2>
  <div class="grid gap-2">
  {% for h in health %}
  {% set bad = h.verdict in ['รันล้มเหลว','เงียบนานผิดปกติ','รันผ่านแต่ไม่ได้ข้อมูลเลย'] %}
  <div class="sheet p-3 flex flex-wrap items-center gap-3
    {% if bad %}border-red-300 bg-red-50{% elif h.verdict != 'ปกติ' %}border-amber-300 bg-amber-50{% endif %}">
    <span class="text-xs px-2 py-1 rounded font-medium
      {% if bad %}bg-red-600 text-white
      {% elif h.verdict == 'ปกติ' %}bg-emerald-600 text-white
      {% else %}bg-amber-500 text-white{% endif %}">{{ h.verdict }}</span>
    <div class="min-w-48">
      <div class="font-medium text-sm">{{ h.code }}</div>
      <div class="text-xs text-slate-600">{{ h.name }}</div>
    </div>
    <div class="text-sm text-slate-600">รันล่าสุด {{ h.hours_since_run }} ชม.ที่แล้ว</div>
    <div class="text-sm">ได้ใหม่ 7 วัน <b>{{ h.new_7d }}</b></div>
    <div class="text-sm text-slate-500">รัน {{ h.runs_7d }} ครั้ง · ล้มเหลว {{ h.failed_7d }}</div>
    {% if h.error_sample %}
    <div class="w-full text-xs text-red-700 border-t pt-2 mt-1">{{ h.error_sample }}</div>
    {% endif %}
  </div>
  {% endfor %}
  </div>
  <p class="mt-2 text-xs text-slate-500">
    <b>"รันผ่านแต่ไม่ได้ข้อมูลเลย" อันตรายกว่า "รันล้มเหลว"</b> —
    รันล้มเหลวเห็นชัด แต่รันผ่านแล้ว parse ไม่ได้จะเงียบไปเป็นเดือนโดยไม่มีใครรู้
  </p>
</section>

<section class="mt-6 grid gap-5 lg:grid-cols-2">
  <div>
    <h2 class="font-semibold mb-2">ทรัพย์ที่คนสนใจมากที่สุด</h2>
    <div class="sheet overflow-x-auto">
    <table class="w-full text-sm min-w-[420px]">
      <thead class="bg-slate-50 text-left text-xs text-slate-600"><tr>
        <th class="px-3 py-2">ทรัพย์</th><th class="px-3 py-2 text-right">ผู้ชม</th>
        <th class="px-3 py-2 text-right">บันทึก</th><th class="px-3 py-2 text-right">ทักถาม</th>
        <th class="px-3 py-2 text-right">คะแนน</th>
      </tr></thead><tbody>
      {% for p in hot_props %}
      <tr class="border-t">
        <td class="px-3 py-2">
          <a href="/p/{{ p.source_code }}/{{ p.external_ref }}" class="underline decoration-dotted">
            {{ p.external_ref }}</a>
          <div class="text-xs text-slate-500">{{ p.district }} · {{ "{:,.0f}".format(p.opening_price or 0) }}</div>
        </td>
        <td class="px-3 py-2 text-right">{{ p.sessions }}</td>
        <td class="px-3 py-2 text-right">{{ p.saves }}</td>
        <td class="px-3 py-2 text-right font-semibold
          {% if p.inquiries > 0 %}text-emerald-700{% else %}text-slate-400{% endif %}">
          {{ p.inquiries }}</td>
        <td class="px-3 py-2 text-right">{{ p.interest_score }}</td>
      </tr>
      {% endfor %}
      </tbody></table>
    </div>
    <p class="mt-2 text-xs text-slate-500">
      คะแนนถ่วงน้ำหนัก: ทักถาม ×25 · บันทึก ×8 · กดไปดูต้นทาง ×3 · ผู้ชม ×1
      <br>ทรัพย์ที่คนดูเยอะแต่ไม่ทักเลย มักแปลว่าราคาน่าสนใจแต่มีอะไรน่ากลัวในรายละเอียด
    </p>
  </div>

  <div>
    <h2 class="font-semibold mb-2">โซนที่คนดูเยอะ</h2>
    <div class="sheet overflow-x-auto">
    <table class="w-full text-sm min-w-[420px]">
      <thead class="bg-slate-50 text-left text-xs text-slate-600"><tr>
        <th class="px-3 py-2">โซน</th><th class="px-3 py-2 text-right">ผู้ชม</th>
        <th class="px-3 py-2 text-right">ทรัพย์ที่มี</th>
        <th class="px-3 py-2 text-right">ดีมานด์/ซัพพลาย</th>
      </tr></thead><tbody>
      {% for z in hot_zones %}
      <tr class="border-t">
        <td class="px-3 py-2">{{ z.district }}
          <div class="text-xs text-slate-500">{{ z.province }}</div></td>
        <td class="px-3 py-2 text-right">{{ z.sessions }}</td>
        <td class="px-3 py-2 text-right {% if z.listings < 5 %}text-red-600 font-semibold{% endif %}">
          {{ z.listings }}</td>
        <td class="px-3 py-2 text-right">
          <span class="px-2 py-0.5 rounded text-xs
            {% if z.demand_supply_ratio and z.demand_supply_ratio >= 10 %}bg-red-100 text-red-800
            {% elif z.demand_supply_ratio and z.demand_supply_ratio >= 5 %}bg-amber-100 text-amber-800
            {% else %}bg-slate-100 text-slate-700{% endif %}">
            {{ z.demand_supply_ratio }}</span>
        </td>
      </tr>
      {% endfor %}
      </tbody></table>
    </div>
    <p class="mt-2 text-xs text-slate-500">
      <b>ดีมานด์/ซัพพลายสูง = คนอยากได้แต่เราไม่มีของ</b>
      นี่คือตัวบอกว่าควรขยาย ingestion ไปโซนไหนต่อ ตรงกว่าการเดา
    </p>
  </div>
</section>

<section class="mt-6">
  <h2 class="font-semibold mb-2">ทราฟฟิกรายวัน</h2>
  <div class="sheet p-4">
    {% set maxv = traffic | map(attribute='sessions') | max %}
    <div class="flex items-end gap-2" style="height:140px">
      {% for t in traffic %}
      <div class="flex-1 flex flex-col items-center justify-end h-full">
        <div class="text-[10px] text-slate-500">{{ t.sessions }}</div>
        <div class="w-full bg-slate-800 rounded-t"
             style="height:{{ (t.sessions / maxv * 100) | round }}%"></div>
        {% if t.inquiries %}
        <div class="w-full bg-emerald-500" style="height:{{ (t.inquiries / maxv * 100) | round }}%"></div>
        {% endif %}
        <div class="text-[10px] text-slate-500 mt-1">{{ t.day[-2:] }}</div>
      </div>
      {% endfor %}
    </div>
    <div class="mt-2 text-xs text-slate-500">
      แท่งเข้ม = ผู้ชม · แท่งเขียว = ทักถาม
    </div>
  </div>
</section>
{% endblock %}
""",

"settings.html": """
{% extends "layout.html" %}{% block body %}
<h1 class="text-lg font-semibold">ตั้งค่าระบบ</h1>
<p class="mt-1 text-sm text-slate-600">การเปลี่ยนแปลงมีผลทันที ไม่ต้องรีสตาร์ต</p>

{% if saved %}
<div class="mt-3 rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-2 text-sm text-emerald-800">
  บันทึกแล้ว</div>
{% endif %}

<form method="post" action="/admin/settings" class="mt-4 space-y-3">
  <input type="hidden" name="token" value="{{ token }}">
  {% for s in items %}
  <div class="sheet p-4">
    {% if s.value_type == 'bool' %}
    <label class="flex items-start gap-3 cursor-pointer">
      <input type="checkbox" name="{{ s.key }}" value="true" class="mt-1"
             {% if s.value == 'true' %}checked{% endif %}>
      <span>
        <span class="font-medium text-sm">{{ s.label }}</span>
        <span class="block text-xs text-slate-500 mt-0.5 leading-relaxed">{{ s.description }}</span>
      </span>
    </label>
    {% else %}
    <label class="block">
      <span class="font-medium text-sm">{{ s.label }}</span>
      <span class="block text-xs text-slate-500 mt-0.5 mb-1.5">{{ s.description }}</span>
      <input type="text" name="{{ s.key }}" value="{{ s.value }}"
             class="w-full border rounded-lg px-3 py-1.5 text-sm">
    </label>
    {% endif %}
    {% if s.updated_at %}
    <div class="mt-2 text-[11px] text-slate-400">
      แก้ล่าสุด {{ s.updated_at }}{% if s.updated_by %} โดย {{ s.updated_by }}{% endif %}</div>
    {% endif %}
  </div>
  {% endfor %}
  <button class="bg-slate-900 text-white rounded-lg px-5 py-2 text-sm">บันทึก</button>
</form>

<section class="mt-8">
  <h2 class="font-semibold">ตั้งค่าแยกรายสถาบัน</h2>
  <p class="mt-1 text-sm text-slate-600">
    ทับค่ากลางได้ ใช้เมื่อสัญญานายหน้าของบางเจ้ากำหนดว่าต้องลิงก์กลับ</p>
  <div class="mt-3 bg-white border rounded-xl overflow-x-auto">
    <table class="w-full text-sm min-w-[520px]">
      <thead class="bg-slate-50 text-left text-xs text-slate-600"><tr>
        <th class="px-4 py-2">สถาบัน</th><th class="px-4 py-2">สิทธิ์ข้อมูล</th>
        <th class="px-4 py-2">ลิงก์ต้นทาง</th><th class="px-4 py-2">ทรัพย์ในระบบ</th>
      </tr></thead><tbody>
      {% for i in institutions_rows %}
      <tr class="border-t">
        <td class="px-4 py-2">{{ i.short_name }}
          <div class="text-xs text-slate-500">{{ i.full_name }}</div></td>
        <td class="px-4 py-2">
          <span class="text-xs px-2 py-0.5 rounded
            {% if i.legal_status == 'permitted' %}bg-emerald-50 text-emerald-800
            {% elif i.legal_status == 'restricted' %}bg-red-50 text-red-800
            {% else %}bg-slate-100 text-slate-600{% endif %}">{{ i.legal_status }}</span>
        </td>
        <td class="px-4 py-2">
          {% if i.allow_source_link is none %}<span class="text-slate-500">ตามค่ากลาง</span>
          {% elif i.allow_source_link %}<span class="text-emerald-700">บังคับเปิด</span>
          {% else %}<span class="text-red-700">บังคับปิด</span>{% endif %}
        </td>
        <td class="px-4 py-2 text-right">{{ i.n or 0 }}</td>
      </tr>
      {% endfor %}
      </tbody></table>
  </div>
  <p class="mt-2 text-xs text-slate-500">
    แก้ค่ารายสถาบันด้วย SQL:
    <code class="bg-slate-100 px-1 rounded">update institutions set allow_source_link = true where code = 'bam';</code>
  </p>
</section>
{% endblock %}
""",

"inquiries.html": """
{% extends "layout.html" %}{% block body %}
<h1 class="text-lg font-semibold">คำขอติดต่อกลับ</h1>
<p class="mt-1 text-sm text-slate-600">เรียงใหม่สุดก่อน · แสดง 200 รายการล่าสุด</p>

{% if not rows %}
<div class="mt-4 bg-white border rounded-xl p-10 text-center text-slate-500">
  ยังไม่มีคำขอติดต่อกลับ</div>
{% endif %}

<div class="mt-4 grid gap-3">
{% for r in rows %}
<div class="sheet p-4
  {% if r.status == 'new' %}border-amber-300 bg-amber-50{% endif %}">
  <div class="flex flex-wrap items-start gap-3">
    <div class="flex-1 min-w-56">
      <div class="font-medium">{{ r.contact_name }}
        <span class="text-sm font-normal text-slate-600">· {{ r.phone }}</span>
        {% if r.line_id %}<span class="text-sm text-slate-500">· LINE {{ r.line_id }}</span>{% endif %}
      </div>
      <div class="text-xs text-slate-500 mt-0.5">
        {{ r.created_at }}
        {% if r.preferred_time %}· สะดวก{{ r.preferred_time }}{% endif %}
        {% if r.funding_source %}· {{ r.funding_source }}{% endif %}
      </div>
      {% if r.message %}
      <div class="mt-2 text-sm bg-slate-50 border rounded-lg px-3 py-2">{{ r.message }}</div>
      {% endif %}
    </div>
    <div class="text-right">
      <a href="/p/{{ r.source_code }}/{{ r.external_ref }}?token={{ token }}"
         class="text-sm underline decoration-dotted">{{ r.external_ref }}</a>
      {% if r.grade %}<div class="text-xs text-slate-500 mt-0.5">เกรด {{ r.grade }}</div>{% endif %}
      <span class="mt-1 inline-block text-xs px-2 py-0.5 rounded
        {% if r.status == 'new' %}bg-amber-600 text-white{% else %}bg-slate-100{% endif %}">
        {{ r.status }}</span>
    </div>
  </div>
</div>
{% endfor %}
</div>

<div class="mt-5 bg-white border rounded-xl p-4 text-xs text-slate-600 leading-relaxed">
  <div class="font-semibold text-slate-800 text-sm mb-1">อัปเดตสถานะด้วย SQL</div>
  <code class="bg-slate-100 px-1 rounded">update property_inquiries set status='contacted',
  handled_by='me', handled_at=now() where id='...';</code><br><br>
  สถานะ: new · contacted · qualified · closed · spam
</div>
{% endblock %}
""",

"thanks.html": """
{% extends "layout.html" %}{% block body %}
<div class="max-w-md mx-auto bg-white border rounded-xl p-8 text-center mt-8">
  <div class="text-4xl">✓</div>
  <h1 class="mt-3 text-lg font-semibold">ได้รับข้อมูลแล้ว</h1>
  <p class="mt-2 text-sm text-slate-600">
    จะติดต่อกลับภายใน 1 วันทำการ{% if preferred %} ในช่วง{{ preferred }}{% endif %}</p>
  {% if contact_line_url %}
  <a href="{{ contact_line_url }}" target="_blank" rel="noopener noreferrer"
     class="mt-4 inline-block bg-[#06C755] text-white rounded-lg px-5 py-2.5 text-sm">
    หรือทักมาทาง LINE เลย</a>
  {% endif %}
  <div class="mt-5 pt-4 border-t">
    <a href="/" class="text-sm text-slate-600 hover:text-slate-900">← กลับไปดูทรัพย์อื่น</a>
  </div>
</div>
{% endblock %}
""",

"health.html": """
{% extends "layout.html" %}{% block body %}
<h1 class="text-xl font-semibold mb-4">สุขภาพระบบเก็บข้อมูล</h1>
<div class="overflow-x-auto">
<table class="w-full bg-white border rounded-xl overflow-hidden text-sm min-w-[600px]">
<thead class="bg-slate-50 text-left"><tr>
<th class="p-3">แหล่ง</th><th class="p-3">รันล่าสุด</th><th class="p-3">สถานะ</th>
<th class="p-3">หน้า</th><th class="p-3">แถวใหม่</th><th class="p-3">ผิดพลาด</th></tr></thead>
<tbody>{% for r in runs %}<tr class="border-t">
<td class="p-3">{{ r.source_code }}</td><td class="p-3">{{ r.started_at }}</td>
<td class="p-3">{{ r.status }}</td><td class="p-3">{{ r.pages_fetched }}</td>
<td class="p-3">{{ r.rows_new }}</td><td class="p-3">{{ r.error_count }}</td></tr>
{% else %}<tr><td class="p-6 text-slate-500" colspan="6">ยังไม่มีประวัติการรัน</td></tr>
{% endfor %}</tbody></table>
</div>
{% endblock %}
""",

"admin_parcels.html": """
{% extends "layout.html" %}{% block body %}
<div class="mb-4 flex items-center justify-between">
  <h1 class="text-xl font-semibold">พิกัดแปลง LED (กรอกจาก LandsMaps)</h1>
  <a href="/admin?token={{ token }}" class="text-sm text-slate-600 hover:text-slate-900">← หลังบ้าน</a>
</div>
<div class="sheet p-4 mb-4 text-sm text-slate-600 leading-relaxed">
  เปิด <a href="https://landsmaps.dol.go.th/" target="_blank" rel="noopener"
     class="text-blue-600 underline font-medium">LandsMaps</a>
  → ค้นด้วย <b>จังหวัด + อำเภอ + เลขโฉนด</b> ของแต่ละแถว → คัดลอก
  "ค่าพิกัดแปลง" (เช่น <code>13.78202850,100.58332907</code>) มาวางในช่องแล้วกดบันทึก<br>
  รอกรอก <b>{{ pending|length }}</b> รายการ · ลงพิกัดแปลงแล้ว <b>{{ done }}</b> รายการ
</div>
{% if saved %}<div class="mb-3 rounded-lg bg-emerald-50 border border-emerald-300 px-3 py-2 text-sm text-emerald-800">บันทึกพิกัดแล้ว ✓</div>{% endif %}
{% if err %}<div class="mb-3 rounded-lg bg-red-50 border border-red-300 px-3 py-2 text-sm text-red-800">{{ err }}</div>{% endif %}
<div class="sheet overflow-x-auto">
<table class="w-full text-sm">
  <thead><tr class="text-left border-b bg-slate-50">
    <th class="p-2">ref</th><th class="p-2">จังหวัด</th><th class="p-2">อำเภอ</th>
    <th class="p-2">เลขโฉนด</th><th class="p-2">พิกัดแปลง (lat,lng)</th></tr></thead>
  <tbody>
  {% for r in pending %}
    <tr class="border-b hover:bg-slate-50">
      <td class="p-2 font-mono text-xs">{{ r.external_ref }}</td>
      <td class="p-2">{{ r.province or '-' }}</td>
      <td class="p-2">{{ r.district or '-' }}</td>
      <td class="p-2 font-semibold">{{ r.deed_no }}</td>
      <td class="p-2">
        <form method="post" action="/admin/parcels/set" class="flex gap-2">
          <input type="hidden" name="token" value="{{ token }}">
          <input type="hidden" name="ref" value="{{ r.external_ref }}">
          <input name="coord" placeholder="13.7820,100.5833" autocomplete="off"
            class="border rounded px-2 py-1 w-56">
          <button class="bg-slate-900 text-white rounded px-3 py-1 hover:bg-slate-700">บันทึก</button>
        </form>
      </td>
    </tr>
  {% else %}
    <tr><td class="p-6 text-slate-500" colspan="5">ครบแล้ว — ไม่มีทรัพย์ที่รอพิกัดแปลง 🎉</td></tr>
  {% endfor %}
  </tbody>
</table>
</div>
{% endblock %}
""",

"monitor.html": """
{% extends "layout.html" %}{% block body %}
<div class="flex items-center justify-between flex-wrap gap-2">
  <h1 class="text-lg font-semibold">📊 สถิติคนดูทรัพย์</h1>
  <div class="flex gap-1 text-sm">
    {% for d in [7,30,90] %}
    <a href="/admin/monitor?days={{ d }}&token={{ admin_token }}"
       class="rounded border px-3 py-1 {% if days==d %}bg-slate-900 text-white{% else %}hover:bg-slate-50{% endif %}">{{ d }} วัน</a>
    {% endfor %}
    <a href="/admin?token={{ admin_token }}" class="rounded border px-3 py-1 hover:bg-slate-50">← Dashboard</a>
  </div>
</div>
<p class="text-sm text-slate-600 mt-1">ข้อมูล {{ days }} วันล่าสุด (สดถึงวันนี้ · ไม่เก็บข้อมูลระบุตัวตนตาม PDPA)</p>

<div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2 mt-4">
  {% set cards = [('เปิดดูทรัพย์', totals.views),('ผู้ชม (คน)', totals.sessions),('บันทึก', totals.saves),('ทักถาม', totals.inquiries),('กดไปต้นทาง', totals.source_clicks),('ดูแผนที่', totals.map_views)] %}
  {% for label, val in cards %}
  <div class="sheet p-3">
    <div class="text-2xl font-semibold">{{ "{:,}".format(val or 0) }}</div>
    <div class="text-xs text-slate-500">{{ label }}</div>
  </div>
  {% endfor %}
</div>

{% if daily %}
{% set vmax = (daily|map(attribute='views')|max) or 1 %}
<section class="mt-6">
  <h2 class="font-semibold mb-2 text-sm">แนวโน้มการเปิดดูรายวัน</h2>
  <div class="sheet p-3 overflow-x-auto">
    <div class="flex items-end gap-[3px]" style="height:8rem;min-width:{{ daily|length * 10 }}px">
    {% for d in daily %}
    <div class="flex flex-col items-center justify-end" style="height:100%;flex:1 0 7px"
         title="{{ d.day }}: เปิดดู {{ d.views }} · {{ d.sessions }} คน">
      <div class="w-full rounded-t" style="height:{{ (100*d.views/vmax)|round|int }}%;background:var(--sky)"></div>
    </div>
    {% endfor %}
    </div>
  </div>
</section>
{% endif %}

<section class="mt-6">
  <h2 class="font-semibold mb-2">ทรัพย์ที่คนสนใจมากสุด</h2>
  {% if not props %}<div class="sheet p-6 text-center text-slate-400 text-sm">ยังไม่มีข้อมูลการดูในช่วงนี้</div>{% endif %}
  {% if props %}
  <div class="sheet overflow-x-auto">
  <table class="w-full text-sm whitespace-nowrap">
    <thead class="text-left text-slate-500 border-b">
      <tr><th class="p-2">#</th><th class="p-2">ทรัพย์</th><th class="p-2">โซน</th>
      <th class="p-2 text-right">ดู</th><th class="p-2 text-right">คน</th>
      <th class="p-2 text-right">บันทึก</th><th class="p-2 text-right">ทัก</th>
      <th class="p-2 text-right">ต้นทาง</th><th class="p-2 text-right">สนใจ</th></tr>
    </thead>
    <tbody>
    {% for p in props %}
    <tr class="border-b last:border-0 hover:bg-slate-50">
      <td class="p-2 text-slate-400">{{ loop.index }}</td>
      <td class="p-2">
        <a href="/p/{{ p.source_code }}/{{ p.external_ref }}?token={{ admin_token }}" class="text-sky-700 hover:underline">
          {% if p.grade %}<span class="text-[10px] px-1 rounded bg-slate-200">{{ p.grade }}</span>{% endif %}
          {{ type_labels.get(p.property_type, 'ทรัพย์') }}{% if p.opening_price %} · {{ "{:,.0f}".format(p.opening_price) }}฿{% endif %}
        </a>
        <div class="text-[11px] text-slate-400">{{ p.source_code }}:{{ p.external_ref }}</div>
      </td>
      <td class="p-2 text-slate-600">{{ p.district or '-' }}{% if p.province %}, {{ p.province }}{% endif %}</td>
      <td class="p-2 text-right">{{ p.views }}</td>
      <td class="p-2 text-right">{{ p.sessions }}</td>
      <td class="p-2 text-right">{{ p.saves }}</td>
      <td class="p-2 text-right font-medium {% if p.inquiries %}text-emerald-700{% endif %}">{{ p.inquiries }}</td>
      <td class="p-2 text-right">{{ p.source_clicks }}</td>
      <td class="p-2 text-right font-semibold">{{ p.interest }}</td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
  {% endif %}
</section>

<section class="mt-6">
  <h2 class="font-semibold mb-1">โซนที่คนดูเยอะ</h2>
  <p class="text-xs text-slate-500 mb-2">demand/supply สูง = คนสนใจเยอะแต่ทรัพย์น้อย → ควรหาทรัพย์เพิ่มโซนนั้น</p>
  {% if not zones %}<div class="sheet p-6 text-center text-slate-400 text-sm">ยังไม่มีข้อมูล</div>{% endif %}
  {% if zones %}
  <div class="sheet overflow-x-auto">
  <table class="w-full text-sm whitespace-nowrap">
    <thead class="text-left text-slate-500 border-b">
      <tr><th class="p-2">#</th><th class="p-2">โซน</th>
      <th class="p-2 text-right">เปิดดู</th><th class="p-2 text-right">คน</th>
      <th class="p-2 text-right">ทัก</th><th class="p-2 text-right">ทรัพย์ในระบบ</th>
      <th class="p-2 text-right">demand/supply</th></tr>
    </thead>
    <tbody>
    {% for z in zones %}
    <tr class="border-b last:border-0 hover:bg-slate-50">
      <td class="p-2 text-slate-400">{{ loop.index }}</td>
      <td class="p-2 font-medium">{{ z.district or '-' }}<span class="text-slate-400 font-normal">, {{ z.province }}</span></td>
      <td class="p-2 text-right">{{ z.views }}</td>
      <td class="p-2 text-right">{{ z.sessions }}</td>
      <td class="p-2 text-right">{{ z.inquiries }}</td>
      <td class="p-2 text-right">{{ z.listings }}</td>
      <td class="p-2 text-right font-semibold {% if z.demand_supply and z.demand_supply >= 1 %}text-amber-700{% endif %}">{{ z.demand_supply or '-' }}</td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
  {% endif %}
</section>
{% endblock %}
""",

"feedback.html": """
{% extends "layout.html" %}{% block body %}
<div class="flex items-center justify-between flex-wrap gap-2">
  <h1 class="text-lg font-semibold">💬 ความเห็นจากผู้ใช้</h1>
  <a href="/admin?token={{ admin_token }}" class="rounded border px-3 py-1 text-sm hover:bg-slate-50">← Dashboard</a>
</div>
<div class="grid grid-cols-2 sm:grid-cols-4 gap-2 mt-4">
  {% set cards = [('ทั้งหมด', stats.total),('มีข้อความ', stats.with_msg),('คะแนนเฉลี่ย', stats.avg_rating),('7 วันล่าสุด', stats.last7)] %}
  {% for label, val in cards %}
  <div class="sheet p-3"><div class="text-2xl font-semibold num">{{ val if val is not none else '-' }}</div>
    <div class="text-xs text-slate-500">{{ label }}</div></div>
  {% endfor %}
</div>

{% if not rows %}
<div class="sheet p-10 text-center text-slate-400 text-sm mt-5">ยังไม่มีความเห็นเข้ามา</div>
{% endif %}
<div class="mt-5 grid gap-3">
{% for f in rows %}
<div class="sheet p-4">
  <div class="flex items-center gap-2 text-sm">
    {% if f.rating %}<span class="text-amber-500">{% for i in range(f.rating) %}★{% endfor %}<span class="text-slate-300">{% for i in range(5 - f.rating) %}★{% endfor %}</span></span>{% endif %}
    <span class="text-xs text-slate-400">{{ f.created_at }}</span>
    {% if f.device_class %}<span class="text-[11px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-600">{{ f.device_class }}</span>{% endif %}
    {% if f.page_url %}<a href="{{ f.page_url }}?token={{ admin_token }}" class="text-[11px] brandlink truncate max-w-[45%]">{{ f.page_url }}</a>{% endif %}
  </div>
  {% if f.message %}<p class="mt-2 text-sm whitespace-pre-wrap">{{ f.message }}</p>{% endif %}
  {% if f.contact %}<div class="mt-2 text-xs text-slate-500">ติดต่อกลับ: <b>{{ f.contact }}</b></div>{% endif %}
</div>
{% endfor %}
</div>
{% endblock %}
""",

"promoted_admin.html": """
{% extends "layout.html" %}{% block body %}
<div class="flex items-center justify-between flex-wrap gap-2">
  <h1 class="text-lg font-semibold">🔥 จัดการทรัพย์โปรโมท</h1>
  <a href="/admin?token={{ admin_token }}" class="rounded border px-3 py-1 text-sm hover:bg-slate-50">← Dashboard</a>
</div>
<p class="text-sm text-slate-600 mt-1">ทรัพย์ในนี้จะโชว์ใน rail ด้านขวาของหน้าแรก (จอกว้าง) เรียงตาม “ลำดับ” (น้อย=อยู่บน)</p>

<form method="post" action="/admin/promoted/add" class="sheet p-4 mt-4 grid gap-3 sm:grid-cols-5 items-end">
  <input type="hidden" name="token" value="{{ admin_token }}">
  <label class="text-sm sm:col-span-1">แหล่ง
    <select name="source_code" class="mt-1 w-full border rounded-lg px-2 py-1.5">
      {% for s in sources %}<option value="{{ s }}">{{ s }}</option>{% endfor %}
    </select></label>
  <label class="text-sm sm:col-span-2">รหัสทรัพย์ (external_ref)
    <input name="external_ref" required placeholder="เช่น 12345 หรือ ttb:abc"
      class="mt-1 w-full border rounded-lg px-2 py-1.5"></label>
  <label class="text-sm">ลำดับ
    <input name="rank" type="number" value="10" class="mt-1 w-full border rounded-lg px-2 py-1.5"></label>
  <button class="text-white rounded-lg px-4 py-2 text-sm" style="background:var(--ink)">เพิ่ม/อัปเดต</button>
  <label class="text-sm sm:col-span-5">โน้ตภายใน (ไม่โชว์หน้าเว็บ)
    <input name="note" class="mt-1 w-full border rounded-lg px-2 py-1.5"></label>
</form>
<p class="text-xs text-slate-500 mt-2">เคล็ดลับ: เปิดหน้าทรัพย์ที่อยากดัน แล้วกดปุ่ม “📌 ดันโปรโมท” ในหน้านั้นก็ได้ (รหัสจะถูกกรอกให้อัตโนมัติ)</p>

<h2 class="font-semibold mt-6 mb-2">รายการที่โปรโมทอยู่ ({{ rows|length }})</h2>
{% if not rows %}<div class="sheet p-8 text-center text-slate-400 text-sm">ยังไม่มีทรัพย์โปรโมท — ระบบจะโชว์เกรด A คุณภาพสูงแทนไปก่อน</div>{% endif %}
<div class="grid gap-2">
{% for r in rows %}
<div class="sheet p-3 flex items-center gap-3 {% if not r.active %}opacity-50{% endif %}">
  <span class="text-xs w-10 text-center text-slate-400">#{{ r.rank }}</span>
  <div class="w-16 h-12 shrink-0 rounded overflow-hidden bg-slate-100">
    {% if r.image %}<img src="{{ r.image }}" referrerpolicy="no-referrer" class="w-16 h-12 object-cover" onerror="this.remove()">{% endif %}
  </div>
  <div class="min-w-0 flex-1">
    <div class="text-sm font-medium truncate">
      {% if r.grade %}<span class="text-[11px] px-1 rounded bg-slate-200">{{ r.grade }}</span>{% endif %}
      {{ r.title or (r.source_code ~ ':' ~ r.external_ref) }}</div>
    <div class="text-xs text-slate-500">{{ r.source_code }}:{{ r.external_ref }}{% if r.opening_price %} · {{ "{:,.0f}".format(r.opening_price) }} บาท{% endif %}{% if r.note %} · <span class="italic">{{ r.note }}</span>{% endif %}</div>
  </div>
  <a href="/p/{{ r.source_code }}/{{ r.external_ref }}?token={{ admin_token }}" class="text-xs brandlink shrink-0">ดู</a>
  <form method="post" action="/admin/promoted/remove" class="shrink-0">
    <input type="hidden" name="token" value="{{ admin_token }}">
    <input type="hidden" name="source_code" value="{{ r.source_code }}">
    <input type="hidden" name="external_ref" value="{{ r.external_ref }}">
    <button class="text-xs px-2.5 py-1 rounded border text-red-600 border-red-200 hover:bg-red-50">เอาออก</button>
  </form>
</div>
{% endfor %}
</div>
{% endblock %}
""",

"login.html": """
{% extends "layout.html" %}{% block body %}
<div class="max-w-sm mx-auto mt-10">
  <div class="sheet p-6">
    <h1 class="text-lg font-semibold">เข้าสู่ระบบหลังบ้าน</h1>
    <p class="text-sm text-slate-500 mt-1">สำหรับผู้ดูแลระบบเท่านั้น</p>
    {% if error %}<div class="mt-3 text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg px-3 py-2">{{ error }}</div>{% endif %}
    <form method="post" action="/admin/login" class="mt-4 space-y-3">
      <input type="hidden" name="next" value="{{ next or '/admin' }}">
      <input type="text" name="website" class="hidden" tabindex="-1" autocomplete="off">
      <label class="text-sm block">รหัสผ่าน
        <input name="password" type="password" required autofocus
          class="mt-1 w-full border rounded-lg px-3 py-2">
      </label>
      <button class="w-full text-white rounded-lg px-4 py-2.5 text-sm font-medium"
        style="background:var(--ink)">เข้าสู่ระบบ</button>
    </form>
  </div>
</div>
{% endblock %}
""",

"add_property.html": """
{% extends "layout.html" %}{% block body %}
<div class="max-w-3xl mx-auto">
  <div class="flex items-center justify-between mb-3">
    <h1 class="text-lg font-semibold">{% if edit %}แก้ไขทรัพย์{% else %}เพิ่มทรัพย์ใหม่{% endif %}</h1>
    <a href="/admin{{ '?token=' ~ admin_token if admin_token else '' }}" class="text-sm brandlink">← หลังบ้าน</a>
  </div>
  {% if saved %}<div class="mb-3 text-sm text-emerald-800 bg-emerald-50 border border-emerald-200 rounded-lg px-3 py-2">บันทึกแล้ว</div>{% endif %}

  <form method="post" action="/admin/add" class="sheet p-5 grid gap-4 sm:grid-cols-2">
    <input type="hidden" name="token" value="{{ admin_token }}">
    <input type="hidden" name="external_ref" value="{{ f.external_ref or '' }}">

    <label class="text-sm sm:col-span-2">หัวข้อ/ชื่อทรัพย์ <span class="text-red-500">*</span>
      <input name="title" required value="{{ f.title or '' }}"
        placeholder="เช่น บ้านเดี่ยว 2 ชั้น หมู่บ้าน..." class="mt-1 w-full border rounded-lg px-3 py-2"></label>

    <label class="text-sm">ประเภท <span class="text-red-500">*</span>
      <select name="property_type" required class="mt-1 w-full border rounded-lg px-3 py-2">
        {% for k,v in type_labels.items() %}<option value="{{ k }}" {% if k==f.property_type %}selected{% endif %}>{{ v }}</option>{% endfor %}
      </select></label>
    <label class="text-sm">ชนิดเอกสารสิทธิ์
      <input name="title_deed_type" value="{{ f.title_deed_type or '' }}" placeholder="โฉนด / นส.3ก / อช.2" class="mt-1 w-full border rounded-lg px-3 py-2"></label>

    <label class="text-sm">ราคาเปิด/ราคาขาย (บาท) <span class="text-red-500">*</span>
      <input name="opening_price" type="number" required value="{{ f.opening_price or '' }}" class="mt-1 w-full border rounded-lg px-3 py-2"></label>
    <label class="text-sm">ราคาประเมิน (บาท)
      <input name="appraised_price" type="number" value="{{ f.appraised_price or '' }}" class="mt-1 w-full border rounded-lg px-3 py-2"></label>

    <label class="text-sm">จังหวัด <span class="text-red-500">*</span>
      <input name="province" required value="{{ f.province or '' }}" class="mt-1 w-full border rounded-lg px-3 py-2"></label>
    <label class="text-sm">อำเภอ/เขต
      <input name="district" value="{{ f.district or '' }}" class="mt-1 w-full border rounded-lg px-3 py-2"></label>
    <label class="text-sm">ตำบล/แขวง
      <input name="subdistrict" value="{{ f.subdistrict or '' }}" class="mt-1 w-full border rounded-lg px-3 py-2"></label>
    <label class="text-sm">ที่อยู่ (ไม่ต้องใส่บ้านเลขที่)
      <input name="address_raw" value="{{ f.address_raw or '' }}" class="mt-1 w-full border rounded-lg px-3 py-2"></label>

    <label class="text-sm">เนื้อที่ดิน (ตร.ว.)
      <input name="land_area_sqwa" type="number" step="0.1" value="{{ f.land_area_sqwa or '' }}" class="mt-1 w-full border rounded-lg px-3 py-2"></label>
    <label class="text-sm">พื้นที่ใช้สอย (ตร.ม.)
      <input name="usable_area_sqm" type="number" step="0.1" value="{{ f.usable_area_sqm or '' }}" class="mt-1 w-full border rounded-lg px-3 py-2"></label>

    <div class="grid grid-cols-3 gap-3 sm:col-span-2">
      <label class="text-sm">นอน<input name="bedrooms" type="number" value="{{ f.bedrooms or '' }}" class="mt-1 w-full border rounded-lg px-3 py-2"></label>
      <label class="text-sm">น้ำ<input name="bathrooms" type="number" value="{{ f.bathrooms or '' }}" class="mt-1 w-full border rounded-lg px-3 py-2"></label>
      <label class="text-sm">จอดรถ<input name="parking" type="number" value="{{ f.parking or '' }}" class="mt-1 w-full border rounded-lg px-3 py-2"></label>
    </div>

    <label class="text-sm">พิกัด lat (เช่น 13.7563)
      <input name="lat" value="{{ f.lat or '' }}" class="mt-1 w-full border rounded-lg px-3 py-2"></label>
    <label class="text-sm">พิกัด lng (เช่น 100.5018)
      <input name="lng" value="{{ f.lng or '' }}" class="mt-1 w-full border rounded-lg px-3 py-2"></label>

    <label class="text-sm sm:col-span-2">ลิงก์รูป (URL ละบรรทัด — บรรทัดแรก = รูปหลัก)
      <textarea name="image_urls" rows="3" placeholder="https://...jpg&#10;https://...jpg"
        class="mt-1 w-full border rounded-lg px-3 py-2">{{ f.image_urls or '' }}</textarea>
      <span class="text-xs text-slate-400">ใช้รูปที่มีสิทธิ์เผยแพร่ (ถ่ายเอง/ได้รับอนุญาต) — จะถูกทำเครื่องหมายว่าเผยแพร่ได้</span></label>

    <label class="text-sm sm:col-span-2">หมายเหตุผู้อยู่อาศัย/สภาพ
      <input name="occupancy_note" value="{{ f.occupancy_note or '' }}" class="mt-1 w-full border rounded-lg px-3 py-2"></label>

    <div class="sm:col-span-2 flex items-center gap-3">
      <button class="text-white rounded-lg px-5 py-2.5 text-sm font-medium" style="background:var(--ink)">{% if edit %}บันทึกการแก้ไข{% else %}เพิ่มทรัพย์{% endif %}</button>
      <span class="text-xs text-slate-400">หลังบันทึก รัน <code class="bg-slate-100 px-1 rounded">python src\\enrich.py grade</code> เพื่อให้ระบบจัดเกรด (ถ้ายังไม่ขึ้นเกรด)</span>
    </div>
  </form>
</div>
{% endblock %}
""",

"staticpage.html": """
{% extends "layout.html" %}{% block body %}
<article class="max-w-3xl mx-auto sheet p-6 sm:p-8">
  <h1 class="display text-2xl font-bold mb-4">{{ heading }}</h1>
  <div class="prose-npa space-y-3 text-[15px] leading-relaxed text-slate-700">{{ page_body|safe }}</div>
  {% if updated %}<p class="mt-6 text-xs text-slate-400">อัปเดตล่าสุด: {{ updated }}</p>{% endif %}
</article>
{% endblock %}
""",

"articles.html": """
{% extends "layout.html" %}{% block body %}
<h1 class="display text-2xl font-bold mb-1">บทความ & คู่มือ</h1>
<p class="text-sm text-slate-500 mb-5">ความรู้เรื่องทรัพย์ NPA ประมูลทรัพย์ และการตรวจสอบก่อนซื้อ</p>
<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
  {% for a in articles %}
  <a href="/article/{{ a.slug }}" class="sheet p-5 block hover:no-underline">
    <div class="text-3xl">{{ a.emoji }}</div>
    <h2 class="font-semibold mt-2 leading-snug">{{ a.title }}</h2>
    <p class="text-sm text-slate-500 mt-1 line-clamp-3">{{ a.excerpt }}</p>
    <span class="brandlink text-sm mt-3 inline-block">อ่านต่อ →</span>
  </a>
  {% endfor %}
</div>
{% endblock %}
""",

"article.html": """
{% extends "layout.html" %}{% block body %}
<article class="max-w-3xl mx-auto">
  <a href="/articles" class="text-sm brandlink">← บทความทั้งหมด</a>
  <div class="sheet p-6 sm:p-8 mt-3">
    <div class="text-4xl">{{ a.emoji }}</div>
    <h1 class="display text-2xl sm:text-3xl font-bold mt-2 leading-tight">{{ a.title }}</h1>
    {% if a.updated %}<p class="text-xs text-slate-400 mt-2">อัปเดต: {{ a.updated }}</p>{% endif %}
    <div class="prose-npa mt-5 space-y-3 text-[15px] leading-relaxed text-slate-700">{{ a.body|safe }}</div>
  </div>
  <div class="sheet p-5 mt-4 flex items-center justify-between flex-wrap gap-3">
    <div class="text-sm text-slate-600">อยากดูทรัพย์จริง? เริ่มค้นหาได้เลย</div>
    <a href="/" class="rounded-lg px-4 py-2 text-sm font-medium text-white" style="background:var(--survey)">ดูทรัพย์ทั้งหมด →</a>
  </div>
</article>
{% endblock %}
""",
"favorites.html": """
{% extends "layout.html" %}{% block body %}
<div class="flex items-center justify-between mb-4">
  <h1 class="text-xl font-semibold">❤️ ทรัพย์โปรดของฉัน</h1>
  <a href="/" class="text-sm brandlink">← ดูทรัพย์ทั้งหมด</a>
</div>
{% if not rows %}
<div class="sheet p-10 text-center text-slate-500 leading-relaxed">
  ยังไม่มีทรัพย์ที่บันทึกไว้<br>
  <span class="text-sm">เปิดหน้าทรัพย์ที่สนใจ แล้วกด <b style="color:var(--seal)">🤍 บันทึกทรัพย์นี้</b> ไว้ดูทีหลังได้เลย</span>
</div>
{% else %}
<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
{% for r in rows %}
<div class="relative" data-fav-card>
  <a href="/p/{{ r.source_code }}/{{ r.external_ref }}" class="sheet overflow-hidden transition block">
    <div class="relative imgwrap" style="background:#EEF1F3">
      <img src="{{ r.images_view[0].url }}" alt="{{ r.title }}" loading="lazy" decoding="async"
           referrerpolicy="no-referrer" onerror="this.parentNode.classList.add('noimg');this.remove()"
           class="w-full h-44 object-cover">
      <span class="absolute top-2.5 left-2.5 seal seal-sm bg-white/95 g-{{ r.grade or 'none' }}"
            title="{{ r.grade_label }}">{{ r.grade or '—' }}</span>
    </div>
    <div class="p-3">
      <div class="text-lg font-semibold">{{ "{:,.0f}".format(r.opening_price or 0) }}
        <span class="text-xs font-normal text-slate-500">บาท</span></div>
      <div class="mt-0.5 text-xs text-slate-600 line-clamp-2">{{ r.title }}</div>
      {% if r.auction_date %}<div class="mt-1 text-[11px] text-slate-500">ขาย {{ r.auction_date }}</div>{% endif %}
    </div>
  </a>
  <button type="button" onclick="toggleFav(this)" data-sc="{{ r.source_code }}" data-ref="{{ r.external_ref }}"
    data-label="0" data-remove-card="1" aria-pressed="true" title="เอาออกจากทรัพย์โปรด"
    class="absolute top-2 right-2 w-9 h-9 rounded-full bg-white/95 shadow flex items-center justify-center text-lg">❤️</button>
</div>
{% endfor %}
</div>
{% endif %}
{% endblock %}
""",
}

env = Environment(loader=DictLoader(TEMPLATES), autoescape=True)

# ─────────────────────────────────────────────────────────────
# เนื้อหาหน้า static + บทความ — แก้ไขได้ภายหลัง (ผู้ใช้ปรับเนื้อหาเองได้)
# ─────────────────────────────────────────────────────────────
STATIC_PAGES = {
    "about": {
        "title": "เกี่ยวกับแปลงดี — รวมทรัพย์ NPA/ขายทอดตลาด",
        "heading": "เกี่ยวกับแปลงดี",
        "updated": "2026-08",
        "body": """
<p><b>แปลงดี</b> คือเว็บรวมทรัพย์ NPA (ทรัพย์รอการขายของสถาบันการเงิน) และทรัพย์ขายทอดตลาด
จากหลายแหล่งไว้ที่เดียว — ธนาคาร, บริษัทบริหารสินทรัพย์ (AMC) และกรมบังคับคดี —
พร้อม <b>จัดเกรดคุณภาพ วิเคราะห์ส่วนลด ทำเล และแนวรถไฟฟ้า</b> ให้เห็นชัดในหน้าเดียว</p>
<h2>เราช่วยอะไรคุณ</h2>
<ul>
<li>รวมทรัพย์จากหลายแหล่งให้ค้นหาที่เดียว ไม่ต้องเปิดทีละเว็บ</li>
<li>จัดเกรด A–E จากความครบของข้อมูล ส่วนลดจากราคาประเมิน และราคาต่อพื้นที่เทียบโซน</li>
<li>ดูตำแหน่งบนแผนที่ + ระยะถึงสถานีรถไฟฟ้า และเตือนความเสี่ยง เช่น แนวเวนคืน</li>
</ul>
<h2>ข้อมูลมาจากไหน</h2>
<p>รวบรวมจากประกาศสาธารณะของแต่ละแหล่ง เพื่อการวิเคราะห์และเปรียบเทียบ
เราไม่เก็บชื่อคู่ความหรือเลขคดีตาม PDPA และลิงก์กลับไปยังต้นทางเสมอ</p>
<blockquote>ข้อมูลบนเว็บนี้ใช้เพื่อการวิเคราะห์เบื้องต้น <b>ไม่ใช่คำแนะนำการลงทุน</b>
ก่อนตัดสินใจทุกครั้งควรตรวจสอบเอกสารสิทธิ์ ภาระผูกพัน และสภาพทรัพย์กับหน่วยงานที่เกี่ยวข้อง</blockquote>
""",
    },
    "contact": {
        "title": "ติดต่อแปลงดี",
        "heading": "ติดต่อเรา",
        "updated": "2026-08",
        "body": """
<p>มีคำถาม อยากเสนอทรัพย์ ลงโฆษณา หรือแจ้งปัญหาการใช้งาน ติดต่อได้ตามช่องทางนี้</p>
<ul>
<li><b>LINE:</b> เพิ่มเพื่อนแล้วทักได้เลย (ลิงก์อยู่ในปุ่ม “คุยผ่าน LINE” หน้าทรัพย์)</li>
<li><b>อีเมล:</b> [ใส่อีเมลของคุณที่นี่]</li>
<li><b>ส่งความเห็น:</b> กดปุ่ม “ส่งความเห็น” มุมขวาล่างของทุกหน้า</li>
</ul>
<p>สนใจทรัพย์ไหนเป็นพิเศษ กรอกฟอร์ม “สนใจทรัพย์นี้” ในหน้ารายละเอียด เดี๋ยวเราติดต่อกลับพร้อมข้อมูลเพิ่มเติม</p>
""",
    },
    "privacy": {
        "title": "นโยบายความเป็นส่วนตัว (PDPA) — แปลงดี",
        "heading": "นโยบายความเป็นส่วนตัว (PDPA)",
        "updated": "2026-08",
        "body": """
<p>แปลงดีเคารพความเป็นส่วนตัวของคุณ และปฏิบัติตาม พ.ร.บ.คุ้มครองข้อมูลส่วนบุคคล (PDPA)
หน้านี้อธิบายว่าเราเก็บข้อมูลอะไร ใช้ทำอะไร และคุณมีสิทธิ์อย่างไร</p>
<h2>ข้อมูลที่เราเก็บ</h2>
<ul>
<li><b>เมื่อคุณกรอกฟอร์มติดต่อ/สนใจทรัพย์:</b> ชื่อ เบอร์โทร LINE ID (ถ้าให้) และข้อความ — เพื่อติดต่อกลับเรื่องทรัพย์นั้น</li>
<li><b>ความเห็น (feedback):</b> ข้อความและคะแนนที่คุณส่ง</li>
<li><b>สถิติการใช้งานแบบไม่ระบุตัวตน:</b> หน้าที่เปิด/โซนที่สนใจ โดยใช้รหัสชั่วคราวที่หมุนทุกวัน ไม่เก็บ IP หรือชื่อ</li>
</ul>
<h2>เราใช้ข้อมูลอย่างไร</h2>
<ul>
<li>ติดต่อกลับตามที่คุณร้องขอ</li>
<li>ปรับปรุงบริการและคัดทรัพย์ให้ตรงความต้องการ (เฉพาะเมื่อคุณยินยอมรับข่าวสาร)</li>
</ul>
<h2>การเปิดเผยและระยะเวลาเก็บ</h2>
<p>เราไม่ขาย/ส่งต่อข้อมูลให้บุคคลที่สามโดยไม่ได้รับความยินยอมแยกต่างหาก
และเก็บข้อมูลเท่าที่จำเป็นต่อวัตถุประสงค์ข้างต้น</p>
<h2>สิทธิ์ของคุณ</h2>
<p>คุณมีสิทธิ์ขอเข้าถึง แก้ไข ลบ หรือถอนความยินยอมได้ทุกเมื่อ โดยติดต่อผ่าน
<a href="/contact">หน้าติดต่อเรา</a></p>
<p class="text-xs text-slate-400">* เนื้อหานี้เป็นฉบับร่างเพื่อความโปร่งใส ควรให้ที่ปรึกษากฎหมายตรวจก่อนใช้จริง</p>
""",
    },
    "terms": {
        "title": "เงื่อนไขการใช้งาน — แปลงดี",
        "heading": "เงื่อนไขการใช้งาน",
        "updated": "2026-08",
        "body": """
<p>การใช้เว็บไซต์แปลงดีถือว่าคุณยอมรับเงื่อนไขต่อไปนี้</p>
<h2>1. ลักษณะของข้อมูล</h2>
<p>ข้อมูลทรัพย์รวบรวมจากประกาศสาธารณะของธนาคาร AMC และกรมบังคับคดี เพื่อการวิเคราะห์และเปรียบเทียบเบื้องต้น
อาจมีความคลาดเคลื่อน ล้าสมัย หรือทรัพย์ถูกขาย/ถอนไปแล้ว</p>
<h2>2. ไม่ใช่คำแนะนำการลงทุน</h2>
<p>เกรด การวิเคราะห์ราคา และการประเมินต่าง ๆ เป็นข้อมูลประกอบการพิจารณา <b>ไม่ใช่คำแนะนำการลงทุนหรือการรับประกันใด ๆ</b>
ก่อนตัดสินใจต้องตรวจสอบเอกสารสิทธิ์ ภาระผูกพัน แนวเขต และสภาพทรัพย์กับหน่วยงานที่เกี่ยวข้องด้วยตนเอง</p>
<h2>3. ความรับผิด</h2>
<p>แปลงดีไม่รับผิดต่อความเสียหายที่เกิดจากการนำข้อมูลไปใช้ตัดสินใจ ผู้ใช้ต้องตรวจสอบกับต้นทางเสมอ</p>
<h2>4. ทรัพย์สินทางปัญญา</h2>
<p>รูปภาพและข้อมูลบางส่วนเป็นลิขสิทธิ์ของต้นทาง แสดงเพื่อการอ้างอิง เราลิงก์กลับต้นทางเสมอ</p>
<p class="text-xs text-slate-400">* เนื้อหานี้เป็นฉบับร่าง ควรให้ที่ปรึกษากฎหมายตรวจก่อนใช้จริง</p>
""",
    },
}

ARTICLES = [
    {
        "slug": "auction-led-guide",
        "emoji": "⚖️",
        "title": "ประมูลทรัพย์กรมบังคับคดี ทำยังไง? คู่มือมือใหม่",
        "updated": "2026-08",
        "excerpt": "ตั้งแต่หาทรัพย์ วางเงินประกัน ไปจนถึงวันประมูลและโอนกรรมสิทธิ์ — สรุปให้เข้าใจใน 5 นาที",
        "body": """
<p>การขายทอดตลาดของกรมบังคับคดีเป็นช่องทางซื้อทรัพย์ราคาต่ำกว่าตลาดที่ได้รับความนิยม
แต่มีขั้นตอนและความเสี่ยงที่ต้องเข้าใจก่อน</p>
<h2>ขั้นตอนโดยสรุป</h2>
<ol>
<li><b>หาทรัพย์:</b> ดูประกาศขายทอดตลาดตามวันนัด (บนแปลงดีก็รวมไว้แล้ว) จดเลขคดี/สำนักงานที่รับผิดชอบ</li>
<li><b>ตรวจสอบก่อน:</b> ไปดูทรัพย์จริง ตรวจโฉนด ภาระผูกพัน ผู้อยู่อาศัย และการจำนองว่าติดไปกับทรัพย์หรือไม่</li>
<li><b>วางหลักประกัน:</b> วันประมูลต้องนำแคชเชียร์เช็ค/เงินสดตามที่ประกาศกำหนดไปวางเป็นหลักประกันการเข้าสู้ราคา</li>
<li><b>สู้ราคา:</b> เริ่มจากราคาเริ่มต้น (นัดหลัง ๆ ราคามักลดลง) ผู้ให้ราคาสูงสุดชนะ</li>
<li><b>ชำระเงินส่วนที่เหลือ + โอน:</b> ตามกำหนดเวลา แล้วไปจดทะเบียนโอนที่สำนักงานที่ดิน</li>
</ol>
<h2>ข้อควรระวัง</h2>
<ul>
<li>ทรัพย์ขายตาม “สภาพที่เป็นอยู่” — อาจมีผู้อยู่อาศัยที่ต้องขับไล่เอง</li>
<li>ตรวจว่า <b>จำนองติดไปกับทรัพย์</b> หรือไม่ (ถ้าติดไป ผู้ซื้อรับภาระ)</li>
<li>เผื่อค่าใช้จ่ายโอน ค่าภาษี และค่าดำเนินการอื่น ๆ</li>
</ul>
<blockquote>เคล็ดลับ: ใช้เกรดและส่วนลดบนแปลงดีคัดกรองเบื้องต้น แล้วค่อยลงลึกตรวจเอกสารกับสำนักงานบังคับคดีก่อนวันประมูล</blockquote>
""",
    },
    {
        "slug": "buy-bank-npa",
        "emoji": "🏦",
        "title": "ซื้อทรัพย์ NPA จากธนาคาร/AMC ต่างจากประมูลยังไง?",
        "updated": "2026-08",
        "excerpt": "NPA ธนาคารมักซื้อง่ายกว่าประมูล ต่อรองได้ ผ่อนได้ — แต่ก็มีจุดที่ต้องดูให้ดี",
        "body": """
<p>ทรัพย์ NPA (Non-Performing Asset) คือทรัพย์ที่ธนาคารหรือบริษัทบริหารสินทรัพย์ (AMC) ยึดมาและนำออกขาย
ต่างจากการประมูลของกรมบังคับคดีตรงที่ <b>ซื้อขายกับสถาบันโดยตรง</b></p>
<h2>ข้อดีของ NPA ธนาคาร</h2>
<ul>
<li>ขั้นตอนชัดเจน มีเจ้าหน้าที่ดูแล ต่อรองราคาได้</li>
<li>หลายรายการ <b>ขอสินเชื่อกับธนาคารเจ้าของทรัพย์ได้เลย</b> (ผ่อนได้)</li>
<li>สถานะกรรมสิทธิ์มักชัดเจนกว่า (ธนาคารเป็นเจ้าของแล้ว)</li>
</ul>
<h2>สิ่งที่ต้องดู</h2>
<ul>
<li>ราคาตั้งขายอาจสูงกว่าประมูล — ดู <b>ส่วนลดจากราคาประเมิน</b> และเทียบราคาตลาดโซนนั้น (แปลงดีคำนวณให้)</li>
<li>สภาพทรัพย์จริง มีผู้อยู่อาศัยไหม ต้องซ่อมเท่าไร</li>
<li>เงื่อนไขการโอน/ค่าใช้จ่าย และโปรโมชันสินเชื่อ</li>
</ul>
<blockquote>บนแปลงดี ทรัพย์ธนาคารที่ “ลดแรง” จะมีป้ายบอก และเกรดจะสะท้อนทั้งส่วนลดและความครบของข้อมูล</blockquote>
""",
    },
    {
        "slug": "checklist-before-buy",
        "emoji": "✅",
        "title": "7 อย่างต้องเช็คก่อนซื้อทรัพย์มือสอง/หลุดจำนอง",
        "updated": "2026-08",
        "excerpt": "เช็กลิสต์กันพลาด ตั้งแต่โฉนด ภาระผูกพัน ผู้อยู่อาศัย ไปจนถึงทำเลและแนวเวนคืน",
        "body": """
<p>ทรัพย์ราคาถูกน่าสนใจ แต่ “ถูกเพราะมีเหตุ” เสมอ เช็ก 7 ข้อนี้ก่อนตัดสินใจ</p>
<ol>
<li><b>โฉนด/เอกสารสิทธิ์:</b> ตรงกับทรัพย์จริงไหม ประเภทโฉนด (นส.4จ/นส.3ก) เนื้อที่ตรงหรือไม่</li>
<li><b>ภาระผูกพัน:</b> มีจำนอง อายัด ภาระจำยอม หรือคดีค้างอยู่หรือไม่</li>
<li><b>ผู้อยู่อาศัย:</b> มีคนอยู่ไหม ต้องขับไล่เองหรือเปล่า (มีต้นทุน/เวลา)</li>
<li><b>สภาพจริง:</b> ไปดูของจริง เช็กโครงสร้าง น้ำ ไฟ ความชื้น ค่าซ่อม</li>
<li><b>ทำเล & การเข้าถึง:</b> ทางเข้า-ออก น้ำท่วมไหม ใกล้รถไฟฟ้า/สิ่งอำนวยความสะดวก</li>
<li><b>แนวเวนคืน/ผังเมือง:</b> อยู่ในแนวเวนคืนหรือข้อจำกัดการใช้ที่ดินหรือไม่</li>
<li><b>ราคาเทียบตลาด:</b> ถูกจริงหรือแค่ดูถูก เทียบราคาต่อ ตร.ว./ตร.ม. กับทรัพย์ใกล้เคียง</li>
</ol>
<blockquote>แปลงดีช่วยข้อ 5–7 ให้ (ระยะรถไฟฟ้า เตือนแนวเวนคืน เทียบราคาโซน) แต่ข้อ 1–4 ต้องตรวจเอกสารและดูของจริงเสมอ</blockquote>
""",
    },
]
ARTICLES_BY_SLUG = {a["slug"]: a for a in ARTICLES}

# บทความจาก DB (site_articles) — agent นักเขียน SEO เขียนแล้วเผยแพร่อัตโนมัติ
# ไม่ต้อง deploy; รวมกับบทความ hardcoded (DB ชนะถ้า slug ซ้ำ) แล้ว cache 5 นาที
_ARTICLES_CACHE: dict = {"items": None, "ts": 0.0}


def load_articles() -> list[dict]:
    """คืนบทความทั้งหมด (DB + hardcoded) เรียงใหม่→เก่า — cache 5 นาที"""
    import time
    now = time.time()
    if _ARTICLES_CACHE["items"] is not None and now - _ARTICLES_CACHE["ts"] < 300:
        return _ARTICLES_CACHE["items"]
    db_items: list[dict] = []
    if not DEMO_MODE:
        try:
            from core.db import connect
            with connect() as conn:
                rows = conn.execute(
                    "select slug, title, excerpt, body_html, emoji, updated "
                    "from site_articles where published is true "
                    "order by updated desc, created_at desc").fetchall()
            for r in rows:
                db_items.append({
                    "slug": r["slug"], "title": r["title"],
                    "excerpt": r["excerpt"] or "", "body": r["body_html"],
                    "emoji": r["emoji"] or "📝",
                    "updated": str(r["updated"]) if r["updated"] else None})
        except Exception as exc:                                   # noqa: BLE001
            log.warning("โหลดบทความจาก DB ไม่สำเร็จ (รัน migration 036?): %s",
                        str(exc)[:100])
    db_slugs = {a["slug"] for a in db_items}
    merged = db_items + [a for a in ARTICLES if a["slug"] not in db_slugs]
    _ARTICLES_CACHE.update(items=merged, ts=now)
    return merged


def article_by_slug(slug: str) -> dict | None:
    for a in load_articles():
        if a["slug"] == slug:
            return a
    return None


DEMO_REASON = (
    "ยังไม่ได้ตั้งค่า DATABASE_URL จึงใช้ข้อมูลตัวอย่างไปก่อน"
    if NO_DB else "เปิด DEMO_MODE=1 ไว้"
)

BASE = {"demo": DEMO_MODE, "demo_reason": DEMO_REASON, "public_mode": PUBLIC_MODE,
        "type_labels": TYPE_LABELS, "severity_style": SEVERITY_STYLE, "ga_id": GA_ID,
        "line_login": LINE_LOGIN_ENABLED, "user_logged_in": False, "fav_pairs": set()}


def base(**kw) -> dict:
    """ค่าที่ทุกหน้าต้องมี — route ที่ต้องการทับค่าไหนก็ส่งเข้ามา"""
    return {**BASE, "is_admin": False, "admin_token": "", **kw}


def ubase(request: "Request", **kw) -> dict:
    """เหมือน base() แต่เติมสถานะผู้ใช้ LINE (nav login/logout + หัวใจ) ให้อัตโนมัติ"""
    uid = current_user(request)
    return base(user_logged_in=bool(uid), **kw)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, province: str | None = Query(None), district: str | None = Query(None),
          ptype: str | None = Query(None),
          max_price: str = Query(""), min_price: str = Query(""),
          hide_critical: bool = Query(False),
          institution: str | None = Query(None), min_grade: str | None = Query(None),
          show_special: bool = Query(False), page: int = Query(1, ge=1),
          sort: str = Query(""), near_transit: str = Query(""), token: str = Query("")):
    # รับเป็นสตริงแล้วแปลงเอง — ฟอร์ม HTML ส่งช่องว่างมาเป็น "" เสมอ
    # ถ้าประกาศเป็น float FastAPI จะปฏิเสธทั้งคำขอด้วย error ที่ผู้ใช้อ่านไม่รู้เรื่อง
    max_price_v = _num(max_price)
    min_price_v = _num(min_price)
    # ระยะใกล้รถไฟฟ้า (เมตร) — รับเฉพาะค่าที่กำหนดไว้ กันค่ามั่ว
    near_transit_v = int(near_transit) if near_transit.isdigit() and \
        int(near_transit) in (500, 1000, 2000, 3000) else None
    province = province or None
    district = district or None
    ptype = ptype or None
    institution = institution or None
    min_grade = min_grade or None
    # การเรียง — รับเฉพาะคีย์คงที่ในโค้ด แล้วแปลงเป็น ORDER BY ที่ปลอดภัย
    ORDER_MAP = {
        "reco": "recommend_score desc nulls last, opening_price asc nulls last",
        "price_asc": "opening_price asc nulls last",
        "price_desc": "opening_price desc nulls last",
        "new": "first_seen desc nulls last",
    }
    if sort not in ORDER_MAP:
        sort = ""
    order = ORDER_MAP.get(sort)
    is_admin = admin_ok(request, token)
    rows, total = fetch_rows(
        province=province, district=district, ptype=ptype, max_price=max_price_v,
        min_price=min_price_v, hide_critical=hide_critical, institution=institution,
        min_grade=min_grade, show_special=show_special, is_admin=is_admin,
        page=page, page_size=PAGE_SIZE, order=order, near_transit=near_transit_v)
    pages = max(1, -(-total // PAGE_SIZE))
    opts = filter_options(province)

    # "ทรัพย์แนะนำ" โชว์เฉพาะหน้าแรกที่ไม่ได้กรองอะไร (หน้าโฮมจริง ๆ)
    featured = []
    if page == 1 and not any([province, district, ptype, max_price_v, min_price_v,
                              institution, min_grade, hide_critical, show_special,
                              near_transit_v]):
        try:
            featured = featured_by_hot_zone(is_admin, n=8)
        except Exception as exc:                               # noqa: BLE001
            log.warning("ทรัพย์แนะนำ(โซนฮิต)โหลดไม่สำเร็จ ลองแบบทั่วเว็บ: %s",
                        str(exc)[:100])
            try:
                featured = top_recommended(is_admin, n=8)
            except Exception as exc2:                          # noqa: BLE001
                log.warning("ทรัพย์แนะนำโหลดไม่สำเร็จ (รัน migration 028?): %s",
                            str(exc2)[:100])
                featured = []

    # ทรัพย์โปรโมท (rail ขวา บนจอกว้าง) — โชว์บนหน้าแรกที่ไม่ได้กรอง
    promoted = []
    if page == 1 and not any([province, district, ptype, max_price_v, min_price_v,
                              institution, min_grade, hide_critical, show_special,
                              near_transit_v]):
        try:
            promoted = promoted_list(is_admin, n=6)
        except Exception as exc:                               # noqa: BLE001
            log.warning("ทรัพย์โปรโมทโหลดไม่สำเร็จ: %s", str(exc)[:100])
            promoted = []

    qs_parts = {"province": province, "district": district, "ptype": ptype,
                "max_price": max_price_v, "min_price": min_price_v,
                "institution": institution, "min_grade": min_grade,
                "hide_critical": 1 if hide_critical else None,
                "show_special": 1 if show_special else None,
                "sort": sort or None,
                "near_transit": near_transit_v,
                "token": token or None}
    qs = "?" + "&".join(f"{k}={v}" for k, v in qs_parts.items() if v) \
        if any(qs_parts.values()) else ""

    # SEO title — ใส่ keyword + ทำเล/ประเภทที่กำลังดู เพื่อให้ค้นเจอง่าย
    type_word = TYPE_LABELS.get(ptype) if ptype else "บ้าน ที่ดิน คอนโด"
    loc_word = f"ใน{district or province}" if (district or province) else "ทั่วไทย"
    seo_title = (f"{type_word} หลุดจำนอง/ขายทอดตลาด {loc_word} ราคาต่ำกว่าตลาด — ทรัพย์ NPA")
    canonical = _abs_url(request, "/")
    home_jsonld = _jsonld([
        {"@context": "https://schema.org", "@type": "Organization",
         "name": "แปลงดี", "url": _abs_url(request, "/"),
         "logo": _abs_url(request, "/static/logo.svg"),
         "description": "รวมทรัพย์ NPA บ้านหลุดจำนอง ที่ดิน คอนโด จากธนาคาร AMC และกรมบังคับคดี พร้อมจัดเกรดคุณภาพ"},
        {"@context": "https://schema.org", "@type": "WebSite",
         "name": "แปลงดี", "url": _abs_url(request, "/")},
    ]) if page == 1 else None

    return env.get_template("list.html").render(
        title=seo_title, rows=rows, count=total, page=page, pages=pages,
        canonical=canonical, jsonld=home_jsonld,
        featured=featured, promoted=promoted,
        maxw="max-w-7xl" if promoted else "max-w-6xl",
        provinces=opts["provinces"], districts=opts["districts"],
        institutions=opts["institutions"], special_count=opts["special_count"],
        districts_by_province=opts.get("districts_by_province", {}),
        province=province, district=district, ptype=ptype,
        max_price=max_price_v, min_price=min_price_v,
        hide_critical=hide_critical, qs=qs, sort=sort,
        institution=institution, min_grade=min_grade, show_special=show_special,
        near_transit=near_transit_v,
        **base(is_admin=is_admin, admin_token=token))


@app.get("/p/{source_code}/{ref}", response_class=HTMLResponse)
def detail(request: Request, source_code: str, ref: str, token: str = Query("")):
    # ใส่ ?token= เพื่อดูแบบ admin — เห็นลิงก์ต้นทางเสมอไม่ว่าตั้งค่าไว้อย่างไร
    is_admin = admin_ok(request, token)
    r = find_row(source_code, ref, is_admin)
    if not r:
        raise HTTPException(404, "ไม่พบทรัพย์รายการนี้")
    specs = [(k, v) for k, v in [
        ("ประเภท", r["type_label"]),
        ("เนื้อที่ดิน", f"{r['land_area_sqwa']} ตร.ว." if r.get("land_area_sqwa") else None),
        ("พื้นที่ใช้สอย", f"{r['usable_area_sqm']} ตร.ม." if r.get("usable_area_sqm") else None),
        ("ราคา/ตร.ว.", f"{r['price_per_sqwa']:,.0f} บาท" if r.get("price_per_sqwa") else None),
        ("นัดขายครั้งที่", r.get("auction_round")),
        ("วันขายทอดตลาด", r.get("auction_date")),
        ("สำนักงาน", r.get("office_name")),
        ("การจำนอง", "ติดไปกับทรัพย์" if r.get("mortgage_carried") else "ไม่ติดไป"),
        ("ผู้อยู่อาศัย", r.get("occupancy_note")),
        ("จังหวัด", r.get("province")),
        ("อำเภอ/เขต", r.get("district")),
        ("ตำบล/แขวง", r.get("subdistrict")),
    ] if v is not None]
    # ดึงแกลเลอรีเฉพาะโหมดใช้ภายใน — โหมดเผยแพร่ต้องใช้รูปที่เราถ่ายเอง
    if not PUBLIC_MODE:
        gallery = ensure_gallery(source_code, ref, r.get("_source_url"))
        if gallery:
            r["images_view"] = [{"url": u, "caption": None,
                                 "attribution": r.get("institution_name"),
                                 "is_placeholder": False} for u in gallery]

    comp_blocks = load_comps(r.get("province"), r.get("district"),
                             r.get("property_type"))

    # ทรัพย์แปลงเดียวกันในแหล่งอื่น (dedupe/เทียบราคาข้ามแหล่ง)
    dupes = duplicates_for(r, is_admin)
    dupe_cheapest = None
    if dupes:
        prices = [d["opening_price"] for d in ([r] + dupes) if d.get("opening_price")]
        dupe_cheapest = min(prices) if prices else None

    # OG สำหรับแชร์ลิงก์ทรัพย์ใน LINE/FB — ราคา + ทำเล + รูปจริง
    price = r.get("opening_price")
    og_title = (f"{price:,.0f} บาท · " if price else "") + (r.get("title") or "ทรัพย์")
    og_bits = [r.get("type_label")]
    if r.get("land_area_sqwa"):
        og_bits.append(f"{r['land_area_sqwa']} ตร.ว.")
    if r.get("usable_area_sqm"):
        og_bits.append(f"{r['usable_area_sqm']} ตร.ม.")
    loc = " ".join(x for x in [r.get("district"), r.get("province")] if x)
    if loc:
        og_bits.append(loc)
    if r.get("grade"):
        og_bits.append(f"เกรด {r['grade']}")
    og_desc = " · ".join(x for x in og_bits if x)
    first_img = (r.get("images_view") or [{}])[0].get("url")
    og_image = _abs_url(request, first_img)
    page_url = _abs_url(request, f"/p/{source_code}/{ref}")

    # JSON-LD structured data — ให้ Google แสดงราคา/รูปในผลค้นหา (rich result)
    addr = {"@type": "PostalAddress", "addressCountry": "TH"}
    if r.get("province"):
        addr["addressRegion"] = r["province"]
    if r.get("district"):
        addr["addressLocality"] = r["district"]
    product = {"@context": "https://schema.org", "@type": "Product",
               "name": r["title"], "description": og_desc, "category": r.get("type_label"),
               "url": page_url}
    if og_image:
        product["image"] = og_image
    if price:
        product["offers"] = {"@type": "Offer", "price": int(price), "priceCurrency": "THB",
                             "availability": "https://schema.org/InStock", "url": page_url}
    if len(addr) > 2:
        product["areaServed"] = addr
    crumbs = [{"@type": "ListItem", "position": 1, "name": "ทรัพย์ทั้งหมด",
               "item": _abs_url(request, "/")}]
    if r.get("province"):
        crumbs.append({"@type": "ListItem", "position": 2, "name": r["province"],
                       "item": _abs_url(request, f"/?province={r['province']}")})
    crumbs.append({"@type": "ListItem", "position": len(crumbs) + 1,
                   "name": r["title"], "item": page_url})
    breadcrumb = {"@context": "https://schema.org", "@type": "BreadcrumbList",
                  "itemListElement": crumbs}
    jsonld = _jsonld([product, breadcrumb])

    uid = current_user(request)
    is_fav = (source_code, ref) in user_fav_pairs(uid)
    return env.get_template("detail.html").render(
        title=r["title"], r=r, specs=specs,
        comps=comp_blocks[0] if comp_blocks else None,
        dupes=dupes, dupe_cheapest=dupe_cheapest, is_fav=is_fav,
        contact_line_url=current_settings().get("contact_line_url"),
        og_title=og_title, og_desc=og_desc, og_image=og_image, og_type="product",
        og_url=page_url, canonical=page_url, jsonld=jsonld,
        **base(is_admin=is_admin, admin_token=token, user_logged_in=bool(uid)))


@app.get("/map", response_class=HTMLResponse)
def map_view(request: Request, token: str = Query("")):
    is_admin = admin_ok(request, token)
    rows, total_rows = fetch_rows(is_admin=is_admin)
    with_geo = sum(1 for r in rows if r.get("lat") and r.get("lng"))
    coarse = sum(1 for r in rows
                 if r.get("lat") and r.get("geo_precision") in ("district", "province"))
    exact = sum(1 for r in rows if r.get("geo_precision") == "parcel")
    institutions = filter_options().get("institutions", [])
    return env.get_template("map.html").render(
        title="แผนที่ทรัพย์", total=total_rows, with_geo=with_geo, coarse=coarse,
        exact=exact, institutions=institutions,
        **base(is_admin=is_admin, admin_token=token))


@app.get("/api/properties.geojson")
def properties_geojson(request: Request,
                       province: str | None = Query(None),
                       district: str | None = Query(None),
                       ptype: str | None = Query(None),
                       max_price: str = Query(""),
                       hide_critical: bool = Query(False),
                       institution: list[str] | None = Query(None),
                       min_grade: str | None = Query(None),
                       show_special: bool = Query(False),
                       token: str = Query("")):
    is_admin = admin_ok(request, token)
    rows, _ = fetch_rows(province=province or None, ptype=ptype or None,
                         max_price=_num(max_price),
                         hide_critical=hide_critical, institution=institution,
                         min_grade=min_grade, show_special=show_special,
                         is_admin=is_admin, district=district)
    feats = []
    for r in rows:
        if r.get("lat") is None or r.get("lng") is None:
            continue
        source = next((l for l in r.get("links", []) if l.get("kind") == "source"), None)
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [float(r["lng"]), float(r["lat"])]},
            "properties": {
                "ref": r["external_ref"] if r.get("show_market_code") else None,
                "title": r["title"],
                "type_label": r.get("type_label"),
                "price": r.get("opening_price"),
                "price_per_sqwa": r.get("price_per_sqwa"),
                "discount": r.get("discount_pct"),
                "area": r.get("land_area_sqwa"),
                "usable": r.get("usable_area_sqm"),
                "grade": r.get("grade"),
                "grade_label": r.get("grade_label"),
                "score": r.get("grade_score") or 0,
                "critical": r["has_critical"],
                # ชื่อสถาบันขึ้นกับ show_institution_name
                "institution": r.get("institution_name"),
                # ลิงก์ต้นทางขึ้นกับ show_source_link (admin เห็นเสมอ)
                "source_url": source["url"] if source else None,
                "source_label": source["label"] if source else None,
                "geo_precision": r.get("geo_precision"),
                "address": r.get("subdistrict"),
                "image": r["images_view"][0]["url"],
                "detail_url": f"/p/{r['source_code']}/{r['external_ref']}",
            }})
    return {"type": "FeatureCollection", "features": feats}


@app.get("/api/zones.geojson")
def zones_geojson():
    path = pathlib.Path(__file__).resolve().parents[1] / "data" / "geo" / "zones.geojson"
    if not path.exists():
        return {"type": "FeatureCollection", "features": []}
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/api/infra.geojson")
def infra_geojson():
    labels = {1: "ผลการศึกษา", 2: "มติ ครม.", 3: "พ.ร.ฎ.เวนคืน",
              4: "กำลังก่อสร้าง", 5: "เปิดใช้แล้ว"}
    if DEMO_MODE:
        return {"type": "FeatureCollection", "features": [{
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [
                [100.4880, 13.6720], [100.4960, 13.6520], [100.5040, 13.6320]]},
            "properties": {"name": "ตัวอย่าง: สายสีม่วงใต้ (ข้อมูลสมมติ)",
                           "certainty": 3, "status_label": labels[3]}}]}
    from core.db import connect
    with connect() as conn:
        rows = conn.execute(
            """select p.name, p.project_type, p.certainty_level,
                      coalesce(p.line_color,
                        (select l.line_color from infra_projects l
                          where l.line_color is not null
                            and l.project_type in ('rail','road','expressway')
                            and st_dwithin(p.geom::geography, l.geom::geography, 500)
                          order by p.geom <-> l.geom limit 1)) as color,
                      st_asgeojson(p.geom) as gj
               from infra_projects p where p.geom is not null""").fetchall()
    return {"type": "FeatureCollection", "features": [{
        "type": "Feature", "geometry": json.loads(r["gj"]),
        "properties": {"name": r["name"], "type": r["project_type"],
                       "certainty": r["certainty_level"], "color": r["color"],
                       "status_label": labels.get(r["certainty_level"], "-")}}
        for r in rows]}


@app.get("/compare", response_class=HTMLResponse)
def compare(province: str | None = Query(None), district: str | None = Query(None),
            ptype: str | None = Query(None)):
    return env.get_template("compare.html").render(
        title="เทียบราคาตลาด", blocks=load_comps(province, district, ptype),
        haircut_pct=HAIRCUT_PCT, haircut_basis=HAIRCUT_BASIS, **base())


@app.get("/robots.txt")
def robots(request: Request):
    from fastapi.responses import PlainTextResponse
    base_u = BASE_URL or str(request.base_url).rstrip("/")
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "Disallow: /api/\n"
        "Disallow: /auth/\n"
        "Disallow: /favorites\n"
        f"Sitemap: {base_u}/sitemap.xml\n"
    )
    return PlainTextResponse(body)


# cache sitemap ในหน่วยความจำ (สร้างใหม่ทุก 1 ชม.) — กันคิว DB 11k แถวทุกครั้งที่ถูกเรียก
_SITEMAP_CACHE: dict = {"xml": None, "ts": 0.0}


@app.get("/sitemap.xml")
def sitemap(request: Request):
    import time
    from xml.sax.saxutils import escape
    from fastapi.responses import Response

    base_u = BASE_URL or str(request.base_url).rstrip("/")
    now = time.time()
    if _SITEMAP_CACHE["xml"] and now - _SITEMAP_CACHE["ts"] < 3600:
        return Response(_SITEMAP_CACHE["xml"], media_type="application/xml")

    urls = [(f"{base_u}/", None, "daily", "1.0"),
            (f"{base_u}/map", None, "weekly", "0.6"),
            (f"{base_u}/compare", None, "weekly", "0.5"),
            (f"{base_u}/zone", None, "weekly", "0.7"),
            (f"{base_u}/articles", None, "weekly", "0.5"),
            (f"{base_u}/about", None, "monthly", "0.3")]

    # บทความ (DB + hardcoded) — ให้ Google เก็บหน้าบทความที่ agent เขียนด้วย
    try:
        for a in load_articles():
            urls.append((f"{base_u}/article/{a['slug']}", a.get("updated"),
                         "monthly", "0.5"))
    except Exception as exc:                                       # noqa: BLE001
        log.warning("sitemap articles ล้มเหลว: %s", str(exc)[:100])

    # ดึงทุกอย่างในคอนเนกชันเดียว (2 query) — เร็วพอสำหรับ free tier
    # กัน Googlebot fetch timeout: เดิมเปิด DB ทีละจังหวัด ~77 รอบ ทำให้ช้าจนดึงไม่ได้
    if not DEMO_MODE:
        try:
            from core.db import connect
            with connect() as conn:
                # หน้า landing โซน (programmatic SEO) — query เดียว group จังหวัด×เขต×ประเภท
                # แล้วรวมยอดใน Python: จังหวัด / จังหวัด×ประเภท / เขต / เขต×ประเภท
                zrows = conn.execute(
                    "select province, district, property_type, count(*) as n "
                    "from v_listings_with_grade where province is not null "
                    "group by province, district, property_type "
                    "order by province").fetchall()
                seen_prov: set = set()
                seen_ptype: set = set()
                dist_total: dict = {}          # (prov,dist) -> จำนวนรวม
                dist_rows: dict = {}           # (prov,dist) -> [(ptype,n)]
                for zr in zrows:
                    prov, dist, t, n = (zr["province"], zr["district"],
                                        zr["property_type"], zr["n"])
                    if not _real_place(prov):                       # ตัดจังหวัดขยะ
                        continue
                    if prov not in seen_prov:
                        seen_prov.add(prov)
                        urls.append((f"{base_u}/zone/{quote(prov)}", None, "weekly", "0.7"))
                    if t and TYPE_LABELS.get(t) and (prov, t) not in seen_ptype:
                        seen_ptype.add((prov, t))
                        urls.append((f"{base_u}/zone/{quote(prov)}/{t}", None, "weekly", "0.6"))
                    if dist and _real_place(dist):
                        dist_total[(prov, dist)] = dist_total.get((prov, dist), 0) + n
                        dist_rows.setdefault((prov, dist), []).append((t, n))
                # เขต/อำเภอ: ใส่เฉพาะที่มีทรัพย์พอ (≥5) กันหน้าบางเกินไป; ประเภทในเขต ≥3
                for (prov, dist), tot in dist_total.items():
                    if tot < 5:
                        continue
                    urls.append((f"{base_u}/zone/{quote(prov)}/d/{quote(dist)}",
                                 None, "weekly", "0.6"))
                    for t, n in dist_rows[(prov, dist)]:
                        if t and TYPE_LABELS.get(t) and n >= 3:
                            urls.append((f"{base_u}/zone/{quote(prov)}/d/{quote(dist)}/{t}",
                                         None, "weekly", "0.55"))
                # หน้าทรัพย์รายรายการ
                rows = conn.execute(
                    """select source_code, external_ref, max(observed_at)::date as lastmod
                         from listing_snapshots
                        group by source_code, external_ref
                        order by max(observed_at) desc
                        limit 45000""").fetchall()
            for r in rows:
                loc = f"{base_u}/p/{escape(r['source_code'])}/{escape(str(r['external_ref']))}"
                urls.append((loc, r["lastmod"], "weekly", "0.8"))
        except Exception as exc:                               # noqa: BLE001
            log.warning("สร้าง sitemap ไม่สำเร็จ: %s", str(exc)[:120])

    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, lastmod, freq, prio in urls:
        parts.append("<url><loc>" + escape(loc) + "</loc>"
                     + (f"<lastmod>{lastmod}</lastmod>" if lastmod else "")
                     + f"<changefreq>{freq}</changefreq>"
                     + f"<priority>{prio}</priority></url>")
    parts.append("</urlset>")
    xml = "\n".join(parts)
    _SITEMAP_CACHE.update(xml=xml, ts=now)
    return Response(xml, media_type="application/xml")


@app.get("/about", response_class=HTMLResponse)
@app.get("/contact", response_class=HTMLResponse)
@app.get("/privacy", response_class=HTMLResponse)
@app.get("/terms", response_class=HTMLResponse)
def static_page(request: Request):
    """หน้าเนื้อหา: /about /contact /privacy /terms (route ชัดเจน ไม่ชนหน้าอื่น)"""
    slug = request.url.path.strip("/")
    pg = STATIC_PAGES.get(slug)
    if not pg:
        raise HTTPException(404, "ไม่พบหน้านี้")
    return env.get_template("staticpage.html").render(
        title=pg["title"], heading=pg["heading"], page_body=pg["body"],
        updated=pg.get("updated"), canonical=_abs_url(request, f"/{slug}"),
        og_desc=pg["heading"] + " — แปลงดี", **base())


@app.get("/articles", response_class=HTMLResponse)
def articles_list(request: Request):
    return env.get_template("articles.html").render(
        title="บทความ & คู่มือ ทรัพย์ NPA ประมูลทรัพย์", articles=load_articles(),
        canonical=_abs_url(request, "/articles"),
        og_desc="รวมบทความและคู่มือเรื่องทรัพย์ NPA ประมูลกรมบังคับคดี และการตรวจสอบก่อนซื้อ",
        **base())


@app.get("/article/{slug}", response_class=HTMLResponse)
def article_page(request: Request, slug: str):
    a = article_by_slug(slug)
    if not a:
        raise HTTPException(404, "ไม่พบบทความนี้")
    page_url = _abs_url(request, f"/article/{slug}")
    jsonld = _jsonld({
        "@context": "https://schema.org", "@type": "Article",
        "headline": a["title"], "description": a["excerpt"],
        "datePublished": a.get("updated"), "author": {"@type": "Organization", "name": "แปลงดี"},
        "publisher": {"@type": "Organization", "name": "แปลงดี"},
        "mainEntityOfPage": page_url})
    return env.get_template("article.html").render(
        title=a["title"], a=a, og_title=a["title"], og_desc=a["excerpt"],
        og_type="article", og_url=page_url, canonical=page_url, jsonld=jsonld,
        **base())


# ---------------------------------------------------------------------
# LINE Login (ผู้ใช้ทั่วไป) — OAuth 2.0 + session cookie แยกจาก admin
# ---------------------------------------------------------------------
@app.get("/auth/line/login")
def line_login(request: Request, next: str = Query("/favorites")):
    import secrets
    import urllib.parse
    from fastapi.responses import RedirectResponse
    if not LINE_LOGIN_ENABLED:
        raise HTTPException(503, "ยังไม่ได้เปิดใช้ LINE Login (ตั้งค่า env ก่อน)")
    if not next.startswith("/"):
        next = "/favorites"
    state = secrets.token_urlsafe(16)
    redirect_uri = (BASE_URL or str(request.base_url).rstrip("/")) + "/auth/line/callback"
    params = {"response_type": "code", "client_id": LINE_LOGIN_CHANNEL_ID,
              "redirect_uri": redirect_uri, "state": state, "scope": "profile openid"}
    url = "https://access.line.me/oauth2/v2.1/authorize?" + urllib.parse.urlencode(params)
    resp = RedirectResponse(url, status_code=303)
    payload = f"{state}|{next}"
    resp.set_cookie("npa_oauth", f"{payload}.{_sign(payload)}",
                    max_age=600, httponly=True, samesite="lax")
    return resp


@app.get("/auth/line/callback")
def line_callback(request: Request, code: str = Query(""), state: str = Query("")):
    import hmac
    import json as _json
    import urllib.parse
    import urllib.request
    from fastapi.responses import RedirectResponse
    if not LINE_LOGIN_ENABLED:
        raise HTTPException(503, "ยังไม่ได้เปิดใช้ LINE Login")
    raw = request.cookies.get("npa_oauth", "")
    if "." not in raw:
        raise HTTPException(400, "เซสชันหมดอายุ ลองเข้าสู่ระบบใหม่")
    payload, sig = raw.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(payload)):
        raise HTTPException(400, "state ไม่ถูกต้อง (อาจถูกปลอม)")
    saved_state, _, next_url = payload.partition("|")
    if not code or not state or state != saved_state:
        raise HTTPException(400, "ยืนยันตัวตนไม่สำเร็จ ลองใหม่อีกครั้ง")
    redirect_uri = (BASE_URL or str(request.base_url).rstrip("/")) + "/auth/line/callback"
    try:
        data = urllib.parse.urlencode({
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri, "client_id": LINE_LOGIN_CHANNEL_ID,
            "client_secret": LINE_LOGIN_CHANNEL_SECRET}).encode()
        treq = urllib.request.Request(
            "https://api.line.me/oauth2/v2.1/token", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(treq, timeout=10) as r:
            tok = _json.loads(r.read())
        access_token = tok.get("access_token")
        if not access_token:
            raise ValueError("no access_token")
        preq = urllib.request.Request(
            "https://api.line.me/v2/profile",
            headers={"Authorization": "Bearer " + access_token})
        with urllib.request.urlopen(preq, timeout=10) as r:
            prof = _json.loads(r.read())
    except Exception as exc:                                        # noqa: BLE001
        log.warning("LINE OAuth ล้มเหลว: %s", str(exc)[:150])
        raise HTTPException(502, "เชื่อมต่อ LINE ไม่สำเร็จ ลองใหม่อีกครั้ง")
    uid = prof.get("userId")
    if not uid:
        raise HTTPException(502, "ไม่ได้ข้อมูลผู้ใช้จาก LINE")
    if not DEMO_MODE:
        try:
            from core.db import connect
            with connect() as conn:
                conn.execute(
                    """insert into app_users
                           (line_user_id, display_name, picture_url, last_login)
                         values (%s, %s, %s, now())
                       on conflict (line_user_id) do update set
                           display_name = excluded.display_name,
                           picture_url = excluded.picture_url, last_login = now()""",
                    (uid, prof.get("displayName"), prof.get("pictureUrl")))
                conn.commit()
        except Exception as exc:                                    # noqa: BLE001
            log.warning("บันทึกผู้ใช้ LINE ล้มเหลว (รัน migration 037?): %s", str(exc)[:120])
    resp = RedirectResponse(next_url if next_url.startswith("/") else "/favorites",
                            status_code=303)
    resp.set_cookie(USER_COOKIE, make_user_cookie(uid), max_age=30 * 86400,
                    httponly=True, samesite="lax", secure=True)
    resp.delete_cookie("npa_oauth")
    return resp


@app.get("/auth/logout")
def user_logout(request: Request, next: str = Query("/")):
    from fastapi.responses import RedirectResponse
    resp = RedirectResponse(next if next.startswith("/") else "/", status_code=303)
    resp.delete_cookie(USER_COOKIE)
    return resp


# ---------------------------------------------------------------------
# ทรัพย์โปรด (favorites) — กดหัวใจ (toggle) + หน้ารวมของฉัน
# ---------------------------------------------------------------------
@app.post("/api/favorite")
async def api_favorite(request: Request):
    from fastapi.responses import JSONResponse
    uid = current_user(request)
    if not uid:
        return JSONResponse({"ok": False, "need_login": True,
                             "login_url": "/auth/line/login"}, status_code=401)
    if DEMO_MODE:
        return JSONResponse({"ok": False, "error": "demo"}, status_code=400)
    try:
        data = await request.json()
    except Exception:                                              # noqa: BLE001
        data = dict(await request.form())
    sc = (data.get("source_code") or "").strip()
    ref = (data.get("external_ref") or "").strip()
    if not (sc and ref):
        raise HTTPException(422, "ต้องมี source_code + external_ref")
    from fastapi.responses import JSONResponse
    from core.db import connect
    with connect() as conn:
        exists = conn.execute(
            "select 1 from user_favorites "
            "where line_user_id=%s and source_code=%s and external_ref=%s",
            (uid, sc, ref)).fetchone()
        if exists:
            conn.execute("delete from user_favorites where line_user_id=%s "
                         "and source_code=%s and external_ref=%s", (uid, sc, ref))
            fav = False
        else:
            conn.execute("insert into user_favorites (line_user_id, source_code, "
                         "external_ref) values (%s,%s,%s) on conflict do nothing",
                         (uid, sc, ref))
            fav = True
        conn.commit()
    return JSONResponse({"ok": True, "favorited": fav})


@app.get("/favorites", response_class=HTMLResponse)
def favorites_page(request: Request):
    from fastapi.responses import RedirectResponse
    uid = current_user(request)
    if not uid:
        return RedirectResponse("/auth/line/login?next=/favorites", status_code=303)
    settings = current_settings()
    rows: list = []
    if not DEMO_MODE:
        try:
            from core.db import connect
            with connect() as conn:
                pairs = [(r["source_code"], r["external_ref"]) for r in conn.execute(
                    "select source_code, external_ref from user_favorites "
                    "where line_user_id=%s order by created_at desc limit 100",
                    (uid,)).fetchall()]
            for sc, ref in pairs:
                r = find_row(sc, ref, is_admin=False)
                if r:
                    rows.append(r)
        except Exception as exc:                                   # noqa: BLE001
            log.warning("โหลดหน้าทรัพย์โปรดไม่สำเร็จ: %s", str(exc)[:120])
    fav_pairs = {(r["source_code"], r["external_ref"]) for r in rows}
    return env.get_template("favorites.html").render(
        title="ทรัพย์โปรดของฉัน", rows=rows,
        canonical=_abs_url(request, "/favorites"),
        og_desc="ทรัพย์ NPA/ขายทอดตลาดที่คุณบันทึกไว้บนแปลงดี",
        **ubase(request, user_logged_in=True, fav_pairs=fav_pairs))


# ---------------------------------------------------------------------
# API เผยแพร่บทความ — สำหรับ agent นักเขียน SEO (เขียนแล้ว POST เข้ามา เผยแพร่ทันที)
# ยืนยันตัวตนด้วย ADMIN_TOKEN (?token=) หรือ cookie แอดมิน
# ---------------------------------------------------------------------
@app.get("/admin/articles")
def admin_articles_list(request: Request, token: str = Query("")):
    """ลิสต์ slug บทความที่มีอยู่ (JSON) — ให้ agent เช็กกันเขียนซ้ำ"""
    token = token or request.headers.get("x-admin-token", "")
    if not admin_ok(request, token):
        raise HTTPException(403, "ต้องยืนยันตัวตนแอดมิน")
    items = [{"slug": a["slug"], "title": a["title"], "updated": a.get("updated")}
             for a in load_articles()]
    from fastapi.responses import JSONResponse
    return JSONResponse({"count": len(items), "articles": items})


@app.post("/admin/articles")
async def admin_article_publish(request: Request, token: str = Query("")):
    """เผยแพร่/อัปเดตบทความ (upsert by slug) — รับ JSON หรือฟอร์ม
    ต้องมี: slug, title, body_html (หรือ body) · ทางเลือก: excerpt, emoji
    """
    from fastapi.responses import JSONResponse
    token = token or request.headers.get("x-admin-token", "")
    if not admin_ok(request, token):
        raise HTTPException(403, "ต้องยืนยันตัวตนแอดมิน (ส่ง header X-Admin-Token หรือ ?token=)")
    if DEMO_MODE:
        raise HTTPException(400, "โหมดตัวอย่าง: ไม่มีฐานข้อมูลให้บันทึก")
    try:
        data = await request.json()
    except Exception:                                              # noqa: BLE001
        data = dict(await request.form())
    import re as _re
    slug = _re.sub(r"[^a-z0-9-]", "", (data.get("slug") or "").strip().lower())
    title = (data.get("title") or "").strip()
    body_html = (data.get("body_html") or data.get("body") or "").strip()
    excerpt = (data.get("excerpt") or "").strip()[:400]
    emoji = (data.get("emoji") or "📝").strip()[:8]
    if not (slug and title and body_html):
        raise HTTPException(422, "ต้องมี slug (a-z0-9-), title และ body_html")
    from core.db import connect
    with connect() as conn:
        conn.execute(
            """insert into site_articles
                   (slug, title, excerpt, body_html, emoji, updated, published)
                 values (%s, %s, %s, %s, %s, current_date, true)
               on conflict (slug) do update set
                   title = excluded.title, excerpt = excluded.excerpt,
                   body_html = excluded.body_html, emoji = excluded.emoji,
                   updated = current_date, published = true""",
            (slug, title, excerpt, body_html, emoji))
        conn.commit()
    _ARTICLES_CACHE.update(items=None, ts=0.0)                     # ล้าง cache ให้ขึ้นทันที
    return JSONResponse({"ok": True, "slug": slug,
                         "url": _abs_url(request, f"/article/{slug}")})


# ---------------------------------------------------------------------
# Programmatic SEO — หน้าโซน/ทำเล (/zone, /zone/{province}, /zone/{province}/{ptype})
# สร้างหน้า landing คีย์เวิร์ดแยกตามจังหวัด+ประเภท ให้ Google เก็บได้เยอะ
# ใช้ fetch_rows + list.html เดิม เพิ่มแค่ H1/เกริ่นนำ/ลิงก์ภายใน
# ---------------------------------------------------------------------
_ZONE_CACHE: dict = {"prov": None, "ts": 0.0}

# ค่าจังหวัด/อำเภอที่ parse ไม่ได้ — ไม่สร้างหน้า zone / ไม่ใส่ sitemap (กันหน้าขยะ SEO)
_PLACE_SKIP = {"", "-", "--", "ไม่ระบุ", "ไม่ทราบ", "n/a", "na", "none", "null"}


def _real_place(name) -> bool:
    return bool(name) and str(name).strip().lower() not in _PLACE_SKIP


def zone_provinces():
    """[(province, n)] เรียงตามจำนวนทรัพย์มาก→น้อย — cache 1 ชม."""
    import time
    now = time.time()
    if _ZONE_CACHE["prov"] is not None and now - _ZONE_CACHE["ts"] < 3600:
        return _ZONE_CACHE["prov"]
    out: list = []
    if DEMO_MODE:
        from collections import Counter
        out = Counter(r["province"] for r in DEMO_ROWS if r.get("province")).most_common()
    else:
        try:
            from core.db import connect
            with connect() as conn:
                out = [(r["province"], r["n"]) for r in conn.execute(
                    "select province, count(*) as n from v_listings_with_grade "
                    "where province is not null group by province "
                    "order by n desc, province").fetchall()]
        except Exception as exc:                                   # noqa: BLE001
            log.warning("zone_provinces ล้มเหลว: %s", str(exc)[:100])
            out = []
    out = [(p, n) for p, n in out if _real_place(p)]               # ตัดค่าขยะ
    _ZONE_CACHE.update(prov=out, ts=now)
    return out


def zone_types(province: str):
    """[(property_type, n)] ของจังหวัดนั้น เรียงตามจำนวน"""
    if DEMO_MODE:
        from collections import Counter
        return Counter(r.get("property_type") for r in DEMO_ROWS
                       if r.get("province") == province and r.get("property_type")).most_common()
    try:
        from core.db import connect
        with connect() as conn:
            return [(r["property_type"], r["n"]) for r in conn.execute(
                "select property_type, count(*) as n from v_listings_with_grade "
                "where province=%s and property_type is not null "
                "group by property_type order by n desc", (province,)).fetchall()]
    except Exception as exc:                                       # noqa: BLE001
        log.warning("zone_types ล้มเหลว: %s", str(exc)[:100])
        return []


def zone_districts(province: str):
    """[(district, n)] ของจังหวัดนั้น เรียงตามจำนวน (สำหรับหน้าเขต/อำเภอ)"""
    if DEMO_MODE:
        from collections import Counter
        return Counter(r.get("district") for r in DEMO_ROWS
                       if r.get("province") == province and r.get("district")).most_common()
    try:
        from core.db import connect
        with connect() as conn:
            rows = [(r["district"], r["n"]) for r in conn.execute(
                "select district, count(*) as n from v_listings_with_grade "
                "where province=%s and district is not null "
                "group by district order by n desc", (province,)).fetchall()]
        return [(d, n) for d, n in rows if _real_place(d)]         # ตัดค่าขยะ
    except Exception as exc:                                       # noqa: BLE001
        log.warning("zone_districts ล้มเหลว: %s", str(exc)[:100])
        return []


def zone_district_types(province: str, district: str):
    """[(property_type, n)] ของเขต/อำเภอนั้น"""
    if DEMO_MODE:
        from collections import Counter
        return Counter(r.get("property_type") for r in DEMO_ROWS
                       if r.get("province") == province and r.get("district") == district
                       and r.get("property_type")).most_common()
    try:
        from core.db import connect
        with connect() as conn:
            return [(r["property_type"], r["n"]) for r in conn.execute(
                "select property_type, count(*) as n from v_listings_with_grade "
                "where province=%s and district=%s and property_type is not null "
                "group by property_type order by n desc", (province, district)).fetchall()]
    except Exception as exc:                                       # noqa: BLE001
        log.warning("zone_district_types ล้มเหลว: %s", str(exc)[:100])
        return []


def _render_landing(request, *, province=None, district=None, ptype=None, page=1,
                    landing_h1="", landing_intro="", landing_links=None,
                    landing_links2=None, landing_links2_label="",
                    landing_crumb=None, seo_title="", canonical="", jsonld=None):
    """เรนเดอร์ list.html แบบหน้า landing — ใช้ fetch_rows เดิม"""
    is_admin = admin_ok(request)
    rows, total = fetch_rows(province=province, district=district, ptype=ptype,
                             is_admin=is_admin, page=page, page_size=PAGE_SIZE,
                             order="recommend_score desc nulls last, opening_price asc nulls last")
    pages = max(1, -(-total // PAGE_SIZE))
    opts = filter_options(province)
    return env.get_template("list.html").render(
        title=seo_title, rows=rows, count=total, page=page, pages=pages,
        canonical=canonical, jsonld=jsonld,
        featured=[], promoted=[], maxw="max-w-6xl",
        provinces=opts["provinces"], districts=opts["districts"],
        institutions=opts["institutions"], special_count=opts["special_count"],
        districts_by_province=opts.get("districts_by_province", {}),
        province=province, district=district, ptype=ptype,
        max_price=None, min_price=None, hide_critical=False, qs="", sort="",
        institution=None, min_grade=None, show_special=False,
        landing_h1=landing_h1, landing_intro=landing_intro,
        landing_links=landing_links or [], landing_crumb=landing_crumb,
        landing_links2=landing_links2 or [], landing_links2_label=landing_links2_label,
        **base(is_admin=is_admin, admin_token=""))


@app.get("/zone", response_class=HTMLResponse)
def zone_hub(request: Request):
    provs = zone_provinces()
    total = sum(n for _, n in provs)
    links = [{"label": f"{p} ({n:,})", "href": f"/zone/{quote(p)}"} for p, n in provs[:60]]
    intro = (f"เลือกดูทรัพย์ NPA บ้านหลุดจำนอง ที่ดิน คอนโด และทรัพย์ขายทอดตลาด "
             f"แยกตามจังหวัด รวมกว่า {total:,} รายการจากธนาคาร AMC และกรมบังคับคดี "
             f"เปรียบเทียบราคาและส่วนลดในแต่ละทำเลได้ในที่เดียว")
    jsonld = _jsonld({
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "หน้าแรก", "item": _abs_url(request, "/")},
            {"@type": "ListItem", "position": 2, "name": "ทำเล", "item": _abs_url(request, "/zone")}]})
    return _render_landing(
        request, page=1,
        landing_h1="ทรัพย์ NPA / ขายทอดตลาด แยกตามทำเล (จังหวัด)",
        landing_intro=intro, landing_links=links,
        seo_title="ทรัพย์ NPA ขายทอดตลาด แยกตามจังหวัด/ทำเล — บ้านหลุดจำนอง ที่ดิน คอนโด",
        canonical=_abs_url(request, "/zone"), jsonld=jsonld)


@app.get("/zone/{province}", response_class=HTMLResponse)
def zone_province(request: Request, province: str):
    known = {p for p, _ in zone_provinces()}
    if known and province not in known:
        raise HTTPException(404, "ไม่พบทำเลนี้")
    types = zone_types(province)
    n = sum(c for _, c in types)
    links = [{"label": f"{TYPE_LABELS.get(t, t)} ({c:,})",
              "href": f"/zone/{quote(province)}/{t}"} for t, c in types if TYPE_LABELS.get(t)]
    # ลิงก์ไปหน้าเขต/อำเภอ (โชว์เฉพาะเขตที่มีทรัพย์พอสมควร) — internal linking + SEO เจาะทำเล
    dists = [(d, c) for d, c in zone_districts(province) if c >= 3]
    dist_links = [{"label": f"{d} ({c:,})", "href": f"/zone/{quote(province)}/d/{quote(d)}"}
                  for d, c in dists[:24]]
    intro = (f"รวมทรัพย์ NPA และทรัพย์ขายทอดตลาดใน{province} {n:,} รายการ — "
             f"บ้านหลุดจำนอง ที่ดิน คอนโด อาคารพาณิชย์ จากธนาคาร AMC และกรมบังคับคดี "
             f"จัดเกรดคุณภาพ วิเคราะห์ส่วนลดและทำเลให้เทียบง่าย เลือกประเภทหรือเขต/อำเภอที่สนใจได้ด้านล่าง")
    jsonld = _jsonld({
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "หน้าแรก", "item": _abs_url(request, "/")},
            {"@type": "ListItem", "position": 2, "name": "ทำเล", "item": _abs_url(request, "/zone")},
            {"@type": "ListItem", "position": 3, "name": province,
             "item": _abs_url(request, f"/zone/{quote(province)}")}]})
    return _render_landing(
        request, province=province, page=1,
        landing_h1=f"ทรัพย์ NPA / ขายทอดตลาด ใน{province}",
        landing_intro=intro, landing_links=links,
        landing_links2=dist_links,
        landing_links2_label="ดูตามเขต/อำเภอ" if dist_links else "",
        landing_crumb={"label": province, "href": f"/zone/{quote(province)}"},
        seo_title=f"ทรัพย์ NPA {province} — บ้านหลุดจำนอง ที่ดิน คอนโด ขายทอดตลาด ราคาต่ำกว่าตลาด",
        canonical=_abs_url(request, f"/zone/{quote(province)}"), jsonld=jsonld)


@app.get("/zone/{province}/{ptype}", response_class=HTMLResponse)
def zone_province_type(request: Request, province: str, ptype: str):
    if ptype not in TYPE_LABELS:
        raise HTTPException(404, "ไม่พบประเภททรัพย์นี้")
    known = {p for p, _ in zone_provinces()}
    if known and province not in known:
        raise HTTPException(404, "ไม่พบทำเลนี้")
    tword = TYPE_LABELS.get(ptype, "ทรัพย์")
    # ลิงก์ไปประเภทอื่นในจังหวัดเดียวกัน + กลับหน้าจังหวัด
    links = [{"label": f"{TYPE_LABELS.get(t, t)}", "href": f"/zone/{quote(province)}/{t}"}
             for t, _ in zone_types(province) if TYPE_LABELS.get(t) and t != ptype][:8]
    links.append({"label": f"ดูทรัพย์ทุกประเภทใน{province}", "href": f"/zone/{quote(province)}"})
    intro = (f"{tword}หลุดจำนอง/ขายทอดตลาดใน{province} — คัดจากธนาคาร AMC และกรมบังคับคดี "
             f"ราคาต่ำกว่าตลาด พร้อมจัดเกรดคุณภาพ วิเคราะห์ส่วนลด ทำเล และแนวรถไฟฟ้า "
             f"อัปเดตต่อเนื่อง เทียบราคาก่อนตัดสินใจได้ทันที")
    jsonld = _jsonld({
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "หน้าแรก", "item": _abs_url(request, "/")},
            {"@type": "ListItem", "position": 2, "name": "ทำเล", "item": _abs_url(request, "/zone")},
            {"@type": "ListItem", "position": 3, "name": province,
             "item": _abs_url(request, f"/zone/{quote(province)}")},
            {"@type": "ListItem", "position": 4, "name": tword,
             "item": _abs_url(request, f"/zone/{quote(province)}/{ptype}")}]})
    return _render_landing(
        request, province=province, ptype=ptype, page=1,
        landing_h1=f"{tword}หลุดจำนอง / ขายทอดตลาด ใน{province}",
        landing_intro=intro, landing_links=links,
        landing_crumb={"label": province, "href": f"/zone/{quote(province)}"},
        seo_title=f"{tword} {province} หลุดจำนอง/ขายทอดตลาด ราคาต่ำกว่าตลาด — ทรัพย์ NPA",
        canonical=_abs_url(request, f"/zone/{quote(province)}/{ptype}"), jsonld=jsonld)


@app.get("/zone/{province}/d/{district}", response_class=HTMLResponse)
def zone_district(request: Request, province: str, district: str):
    if not _real_place(province) or not _real_place(district):
        raise HTTPException(404, "ไม่พบทำเลนี้")
    known = {p for p, _ in zone_provinces()}
    if known and province not in known:
        raise HTTPException(404, "ไม่พบทำเลนี้")
    dtypes = zone_district_types(province, district)
    if not dtypes:
        raise HTTPException(404, "ไม่พบเขต/อำเภอนี้")
    n = sum(c for _, c in dtypes)
    # ลิงก์ประเภทในเขตนี้
    type_links = [{"label": f"{TYPE_LABELS.get(t, t)} ({c:,})",
                   "href": f"/zone/{quote(province)}/d/{quote(district)}/{t}"}
                  for t, c in dtypes if TYPE_LABELS.get(t)]
    # ลิงก์เขต/อำเภออื่นในจังหวัดเดียวกัน (ช่วย crawl ต่อ)
    sib = [{"label": d, "href": f"/zone/{quote(province)}/d/{quote(d)}"}
           for d, c in zone_districts(province) if d != district and c >= 3][:12]
    sib.append({"label": f"↑ ทุกเขตใน{province}", "href": f"/zone/{quote(province)}"})
    intro = (f"ทรัพย์ NPA และทรัพย์ขายทอดตลาดใน{district} {province} {n:,} รายการ — "
             f"บ้านหลุดจำนอง ที่ดิน คอนโด อาคารพาณิชย์ จากธนาคาร AMC และกรมบังคับคดี "
             f"จัดเกรดคุณภาพ วิเคราะห์ส่วนลดและทำเล เทียบราคาก่อนตัดสินใจได้ทันที")
    jsonld = _jsonld({
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "หน้าแรก", "item": _abs_url(request, "/")},
            {"@type": "ListItem", "position": 2, "name": "ทำเล", "item": _abs_url(request, "/zone")},
            {"@type": "ListItem", "position": 3, "name": province,
             "item": _abs_url(request, f"/zone/{quote(province)}")},
            {"@type": "ListItem", "position": 4, "name": district,
             "item": _abs_url(request, f"/zone/{quote(province)}/d/{quote(district)}")}]})
    return _render_landing(
        request, province=province, district=district, page=1,
        landing_h1=f"ทรัพย์ NPA / ขายทอดตลาด ใน{district} {province}",
        landing_intro=intro, landing_links=type_links,
        landing_links2=sib, landing_links2_label="เขต/อำเภออื่น",
        landing_crumb={"label": province, "href": f"/zone/{quote(province)}"},
        seo_title=f"ทรัพย์ NPA {district} {province} — บ้านหลุดจำนอง ที่ดิน คอนโด ขายทอดตลาด ราคาต่ำกว่าตลาด",
        canonical=_abs_url(request, f"/zone/{quote(province)}/d/{quote(district)}"), jsonld=jsonld)


@app.get("/zone/{province}/d/{district}/{ptype}", response_class=HTMLResponse)
def zone_district_type(request: Request, province: str, district: str, ptype: str):
    if ptype not in TYPE_LABELS:
        raise HTTPException(404, "ไม่พบประเภททรัพย์นี้")
    if not _real_place(province) or not _real_place(district):
        raise HTTPException(404, "ไม่พบทำเลนี้")
    known = {p for p, _ in zone_provinces()}
    if known and province not in known:
        raise HTTPException(404, "ไม่พบทำเลนี้")
    dtypes = zone_district_types(province, district)
    if not dtypes:
        raise HTTPException(404, "ไม่พบเขต/อำเภอนี้")
    tword = TYPE_LABELS.get(ptype, "ทรัพย์")
    # ประเภทอื่นในเขตนี้ + กลับหน้าเขต
    links = [{"label": TYPE_LABELS.get(t, t),
              "href": f"/zone/{quote(province)}/d/{quote(district)}/{t}"}
             for t, _ in dtypes if TYPE_LABELS.get(t) and t != ptype][:8]
    links.append({"label": f"ทุกประเภทใน{district}",
                  "href": f"/zone/{quote(province)}/d/{quote(district)}"})
    intro = (f"{tword}หลุดจำนอง/ขายทอดตลาดใน{district} {province} — คัดจากธนาคาร AMC "
             f"และกรมบังคับคดี ราคาต่ำกว่าตลาด พร้อมจัดเกรดคุณภาพ วิเคราะห์ส่วนลด ทำเล "
             f"และแนวรถไฟฟ้า เทียบราคาก่อนตัดสินใจได้ทันที")
    jsonld = _jsonld({
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "หน้าแรก", "item": _abs_url(request, "/")},
            {"@type": "ListItem", "position": 2, "name": "ทำเล", "item": _abs_url(request, "/zone")},
            {"@type": "ListItem", "position": 3, "name": province,
             "item": _abs_url(request, f"/zone/{quote(province)}")},
            {"@type": "ListItem", "position": 4, "name": district,
             "item": _abs_url(request, f"/zone/{quote(province)}/d/{quote(district)}")},
            {"@type": "ListItem", "position": 5, "name": tword,
             "item": _abs_url(request, f"/zone/{quote(province)}/d/{quote(district)}/{ptype}")}]})
    return _render_landing(
        request, province=province, district=district, ptype=ptype, page=1,
        landing_h1=f"{tword}หลุดจำนอง / ขายทอดตลาด ใน{district} {province}",
        landing_intro=intro, landing_links=links,
        landing_crumb={"label": district,
                       "href": f"/zone/{quote(province)}/d/{quote(district)}"},
        seo_title=f"{tword} {district} {province} หลุดจำนอง/ขายทอดตลาด ราคาต่ำกว่าตลาด — ทรัพย์ NPA",
        canonical=_abs_url(request, f"/zone/{quote(province)}/d/{quote(district)}/{ptype}"),
        jsonld=jsonld)


def _admin_data():
    if DEMO_MODE:
        return DEMO_HEALTH, DEMO_HOT_PROPS, DEMO_HOT_ZONES, DEMO_TRAFFIC
    from core.db import connect
    with connect() as conn:
        health = [dict(r) for r in conn.execute("select * from v_source_health").fetchall()]
        props = [dict(r) for r in conn.execute(
            "select * from v_hot_properties limit 15").fetchall()]
        zones = [dict(r) for r in conn.execute(
            "select * from v_hot_zones limit 15").fetchall()]
        traffic = [{"day": str(r["day"]), "sessions": r["sessions"],
                    "inquiries": r["inquiries"]}
                   for r in conn.execute(
                       "select * from v_daily_traffic order by day desc limit 14").fetchall()]
    return health, props, zones, list(reversed(traffic))


@app.get("/admin/stats")
def admin_stats(request: Request, token: str = Query("")):
    """สถิติภายในแอป (JSON) — ให้ agent monitor อ่านทำรายงาน SEO/marketing รายสัปดาห์
    รวม: traffic 14 วัน, โซนฮิต, ทรัพย์ฮิต, สุขภาพแหล่งข้อมูล, ยอดรวม/แหล่ง, จำนวนบทความ
    """
    from fastapi.responses import JSONResponse
    token = token or request.headers.get("x-admin-token", "")
    if not admin_ok(request, token):
        raise HTTPException(403, "ต้องยืนยันตัวตนแอดมิน")
    health, props, zones, traffic = _admin_data()
    totals: dict = {"listings": None, "by_source": [], "articles": len(load_articles())}
    if not DEMO_MODE:
        try:
            from core.db import connect
            with connect() as conn:
                totals["listings"] = conn.execute(
                    "select count(*) as n from v_listings_with_grade").fetchone()["n"]
                totals["by_source"] = [dict(r) for r in conn.execute(
                    "select source_code, count(*) as n from v_listings_with_grade "
                    "group by source_code order by n desc").fetchall()]
        except Exception as exc:                                   # noqa: BLE001
            log.warning("stats totals ล้มเหลว: %s", str(exc)[:100])
    payload = {"traffic_14d": traffic, "hot_zones": zones,
               "hot_properties": props, "source_health": health, "totals": totals}
    return JSONResponse(json.loads(json.dumps(payload, default=str)))


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request, next: str = Query("/admin")):
    if admin_ok(request):
        from fastapi.responses import RedirectResponse
        return RedirectResponse(next or "/admin", status_code=303)
    return env.get_template("login.html").render(title="เข้าสู่ระบบ", next=next, error=None, **base())


@app.post("/admin/login")
async def admin_login(request: Request):
    import hmac
    from fastapi.responses import RedirectResponse
    form = await read_form(request)
    if form.get("website"):                       # honeypot
        return RedirectResponse("/admin/login", status_code=303)
    pw = form.get("password", "")
    nxt = form.get("next") or "/admin"
    if ADMIN_PASSWORD and hmac.compare_digest(pw, ADMIN_PASSWORD):
        resp = RedirectResponse(nxt, status_code=303)
        resp.set_cookie(SESSION_COOKIE, make_session_cookie(), max_age=SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", secure=bool(BASE_URL.startswith("https")))
        return resp
    return HTMLResponse(env.get_template("login.html").render(
        title="เข้าสู่ระบบ", next=nxt, error="รหัสผ่านไม่ถูกต้อง", **base()), status_code=401)


@app.get("/admin/logout")
def admin_logout():
    from fastapi.responses import RedirectResponse
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, token: str = Query("")):
    g = guard(request, token)
    if g:
        return g
    health, props, zones, traffic = _admin_data()
    return env.get_template("admin.html").render(
        title="Dashboard", health=health, hot_props=props,
        hot_zones=zones, traffic=traffic,
        **base(is_admin=True, admin_token=token))


@app.post("/api/track")
async def track(request: Request):
    """บันทึก event — ไม่เก็บ IP ไม่เก็บ user agent เต็ม

    session_hash หมุนทุกวันเพื่อนับ unique แบบหยาบโดยไม่ตามรอยข้ามวัน
    """
    if DEMO_MODE:
        return {"ok": True, "demo": True}
    body = await request.json()
    allowed = {"view_list", "view_detail", "view_map", "click_source",
               "save", "inquire", "filter"}
    if body.get("event_type") not in allowed:
        raise HTTPException(400, "event_type ไม่ถูกต้อง")

    import hashlib
    from datetime import date as _date
    raw = f"{body.get('sid', '')}|{_date.today().isoformat()}"
    session_hash = hashlib.sha256(raw.encode()).hexdigest()[:32]

    from core.db import connect
    with connect() as conn:
        conn.execute(
            """insert into page_events
                 (event_type, source_code, external_ref, province, district,
                  property_type, session_hash, device_class, referrer_kind)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (body["event_type"], body.get("source_code"), body.get("external_ref"),
             body.get("province"), body.get("district"), body.get("property_type"),
             session_hash, body.get("device_class"), body.get("referrer_kind")))
        conn.commit()
    return {"ok": True}


@app.post("/api/feedback")
async def api_feedback(request: Request):
    """รับความเห็นจากปุ่ม feedback (ช่วง beta) — เก็บลง site_feedback"""
    body = await request.json()
    if body.get("website"):                       # honeypot — bot กรอกช่องซ่อน
        return {"ok": True}
    msg = (body.get("message") or "").strip()[:4000]
    rating = body.get("rating")
    try:
        rating = int(rating) if rating else None
        if rating is not None and not (1 <= rating <= 5):
            rating = None
    except (TypeError, ValueError):
        rating = None
    if not msg and rating is None:
        raise HTTPException(400, "ต้องมีข้อความหรือให้คะแนนอย่างน้อยอย่างใดอย่างหนึ่ง")
    if DEMO_MODE:
        return {"ok": True}
    from core.db import connect
    with connect() as conn:
        conn.execute(
            """insert into site_feedback
                 (message, rating, contact, page_url, device_class, sid)
               values (%s,%s,%s,%s,%s,%s)""",
            (msg or None, rating, (body.get("contact") or None),
             (body.get("page_url") or None)[:300] if body.get("page_url") else None,
             body.get("device_class"), (body.get("sid") or None)))
        conn.commit()
    return {"ok": True}


def _require_admin(request: Request, token: str = "") -> None:
    """redirect ไปหน้า login ถ้ายังไม่ได้สิทธิ์ (ใช้กับทั้ง GET page และ POST)"""
    if not admin_ok(request, token):
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})


@app.get("/admin/settings", response_class=HTMLResponse)
def admin_settings(request: Request, token: str = Query(""), saved: bool = Query(False)):
    _require_admin(request, token)
    if DEMO_MODE:
        items = [{"key": k, "value": v, "value_type": "bool" if v in ("true", "false") else "text",
                  "label": k, "description": "โหมดตัวอย่าง แก้ไม่ได้",
                  "updated_at": None, "updated_by": None}
                 for k, v in st.DEFAULTS.items()]
        inst = []
    else:
        from core.db import connect
        with connect() as conn:
            items = [dict(r) for r in conn.execute(
                "select * from app_settings order by key").fetchall()]
            inst = [dict(r) for r in conn.execute(
                """select i.code, i.short_name, i.full_name, i.legal_status,
                          i.allow_source_link,
                          (select count(*) from listing_snapshots s
                            join sources src on src.code = s.source_code
                           where src.institution_code = i.code) as n
                     from institutions i order by i.sort_order""").fetchall()]
    return env.get_template("settings.html").render(
        title="ตั้งค่าระบบ", items=items, institutions_rows=inst,
        token=token, saved=saved, **base())


@app.post("/admin/settings")
async def admin_settings_save(request: Request):
    form = await read_form(request)
    _require_admin(request, form.get("token", ""))
    if DEMO_MODE:
        raise HTTPException(400, "โหมดตัวอย่างแก้ค่าไม่ได้ ต้องต่อฐานข้อมูลจริงก่อน")

    from fastapi.responses import RedirectResponse

    from core.db import connect
    with connect() as conn:
        rows = conn.execute("select key, value_type from app_settings").fetchall()
        for r in rows:
            key, vtype = r["key"], r["value_type"]
            if vtype == "bool":
                # checkbox ที่ไม่ติ๊กจะไม่ถูกส่งมาเลย จึงต้องตั้งเป็น false เอง
                st.save(conn, key, "true" if form.get(key) == "true" else "false")
            elif key in form:
                st.save(conn, key, form.get(key, ""))
    return RedirectResponse(
        f"/admin/settings?token={form.get('token','')}&saved=1", status_code=303)


# ---------------------------------------------------------------------
# พิกัดแปลง LED — กรอกด้วยมือจาก LandsMaps
#   (ดึงอัตโนมัติไม่ได้ เพราะ LandsMaps มี WAF กันบอท + ToS ห้ามทำซ้ำ
#    หน้านี้ให้ "คน" ค้นเองแล้วบันทึกพิกัดที่ได้)
# ---------------------------------------------------------------------
def _parse_latlng(text: str) -> tuple[float, float] | None:
    m = re.search(r"(-?\d{1,2}\.\d+)\s*[,\s]\s*(-?\d{2,3}\.\d+)", text or "")
    if not m:
        return None
    lat, lng = float(m.group(1)), float(m.group(2))
    if 5.5 <= lat <= 20.5 and 97.0 <= lng <= 106.0:
        return lat, lng
    return None


def _parcel_pending(limit: int = 300):
    if DEMO_MODE:
        return [], 0
    from core.db import connect
    with connect() as conn:
        pending = [dict(r) for r in conn.execute(
            """select external_ref, province, district,
                      raw_fields->>'deed_no' as deed_no
                 from (select distinct on (source_code, external_ref)
                              external_ref, province, district,
                              geo_precision, raw_fields, observed_at
                         from listing_snapshots
                        where source_code = 'led_auction'
                        order by source_code, external_ref, observed_at desc) s
                where s.raw_fields->>'deed_no' is not null
                  and s.raw_fields->>'deed_no' not in ('-', '0', '')
                  and coalesce(s.geo_precision, '') <> 'parcel'
                order by external_ref
                limit %s""", (limit,)).fetchall()]
        done = conn.execute(
            """select count(*) as n from
                 (select distinct on (source_code, external_ref) geo_precision
                    from listing_snapshots where source_code = 'led_auction'
                   order by source_code, external_ref, observed_at desc) s
                where geo_precision = 'parcel'""").fetchone()["n"]
    return pending, done


@app.get("/admin/parcels", response_class=HTMLResponse)
def admin_parcels(request: Request, token: str = Query(""), saved: bool = Query(False),
                  err: str = Query("")):
    _require_admin(request, token)
    pending, done = _parcel_pending()
    return env.get_template("admin_parcels.html").render(
        title="พิกัดแปลง LED", pending=pending, done=done,
        token=token, saved=saved, err=err, **base())


@app.post("/admin/parcels/set")
async def admin_parcels_set(request: Request):
    from fastapi.responses import RedirectResponse
    form = await read_form(request)
    _require_admin(request, form.get("token", ""))
    tok = form.get("token", "")
    ref = (form.get("ref") or "").strip()
    c = _parse_latlng(form.get("coord") or "")
    if not (ref and c):
        return RedirectResponse(
            f"/admin/parcels?token={tok}&err=พิกัดไม่ถูกต้อง+(รูปแบบ+13.7820,100.5833)",
            status_code=303)
    from core.db import connect
    with connect() as conn:
        conn.execute(
            """update listing_snapshots
                  set lat = %s, lng = %s, geo_precision = 'parcel'
                where source_code = 'led_auction' and external_ref = %s""",
            (c[0], c[1], ref))
        conn.commit()
    return RedirectResponse(f"/admin/parcels?token={tok}&saved=true", status_code=303)


@app.post("/api/inquire")
async def inquire(request: Request):
    """รับคำขอติดต่อกลับ

    สร้าง lead + บันทึก consent แยกวัตถุประสงค์ตาม PDPA
    ถ้าเคยมี lead ที่เบอร์เดียวกันแล้ว ใช้ตัวเดิม ไม่สร้างซ้ำ
    """
    form = await read_form(request)

    # กับดักบอท: ฟิลด์ที่คนมองไม่เห็น ถ้ามีค่าแปลว่าเป็นบอทกรอก
    if form.get("website"):
        raise HTTPException(400, "ไม่สามารถส่งข้อมูลได้")

    if not form.get("consent_service"):
        raise HTTPException(400, "ต้องยินยอมให้เก็บข้อมูลเพื่อติดต่อกลับ")

    name = (form.get("contact_name") or "").strip()[:120]
    phone = (form.get("phone") or "").strip()[:40]
    if not name or not phone:
        raise HTTPException(400, "กรุณากรอกชื่อและเบอร์โทร")

    if DEMO_MODE:
        return HTMLResponse(env.get_template("thanks.html").render(
            title="ได้รับข้อมูลแล้ว",
            contact_line_url=st.DEFAULTS.get("contact_line_url"),
            preferred=None, **base()))

    from core.db import connect
    with connect() as conn:
        settings = st.load(conn)
        lead = conn.execute(
            "select id from leads where phone = %s limit 1", (phone,)).fetchone()
        if lead:
            lead_id = lead["id"]
            conn.execute("update leads set last_contact_at = now() where id = %s",
                         (lead_id,))
        else:
            lead_id = conn.execute(
                """insert into leads (display_name, phone, line_user_id, source,
                                      source_detail, last_contact_at)
                   values (%s,%s,%s,'web_form',%s, now()) returning id""",
                (name, phone, (form.get("line_id") or "").strip()[:80] or None,
                 f"{form.get('source_code')}:{form.get('external_ref')}")).fetchone()["id"]

        policy = "2026-08"
        for purpose, given in (("service", True),
                               ("marketing", bool(form.get("consent_marketing")))):
            conn.execute(
                """insert into lead_consents (lead_id, purpose, granted,
                                              policy_version, channel, evidence)
                   values (%s,%s,%s,%s,'web_form',%s)""",
                (lead_id, purpose, given, policy,
                 f"ฟอร์มติดต่อกลับ ทรัพย์ {form.get('external_ref')}"))

        conn.execute(
            """insert into property_inquiries
                 (lead_id, source_code, external_ref, contact_name, phone, line_id,
                  message, preferred_time, funding_source)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (lead_id, form.get("source_code"), form.get("external_ref"), name, phone,
             (form.get("line_id") or "").strip()[:80] or None,
             (form.get("message") or "").strip()[:2000] or None,
             form.get("preferred_time") or None,
             form.get("funding_source") or None))

        conn.execute(
            """insert into page_events (event_type, source_code, external_ref, session_hash)
               values ('inquire', %s, %s, 'form')""",
            (form.get("source_code"), form.get("external_ref")))
        conn.commit()

    labels = {"morning": "เช้า", "afternoon": "บ่าย",
              "evening": "เย็น", "anytime": "เวลาไหนก็ได้"}
    return HTMLResponse(env.get_template("thanks.html").render(
        title="ได้รับข้อมูลแล้ว",
        contact_line_url=settings.get("contact_line_url"),
        preferred=labels.get(form.get("preferred_time")), **base()))


@app.get("/admin/inquiries", response_class=HTMLResponse)
def admin_inquiries(request: Request, token: str = Query("")):
    _require_admin(request, token)
    if DEMO_MODE:
        rows = []
    else:
        from core.db import connect
        with connect() as conn:
            rows = [dict(r) for r in conn.execute(
                """select i.*, g.grade
                     from property_inquiries i
                     left join property_grades g
                       on g.source_code = i.source_code and g.external_ref = i.external_ref
                    order by i.created_at desc limit 200""").fetchall()]
    return env.get_template("inquiries.html").render(
        title="คำขอติดต่อกลับ", rows=rows, token=token,
        **base(is_admin=True, admin_token=token))


def monitor_data(days: int):
    """สถิติคนดูช่วง N วัน — ดึงจาก page_events ตรง ๆ (สดถึงวันนี้)"""
    from core.db import connect
    with connect() as conn:
        totals = dict(conn.execute("""
            select
              count(*) filter (where event_type='view_detail')   as views,
              count(distinct session_hash)                       as sessions,
              count(*) filter (where event_type='save')          as saves,
              count(*) filter (where event_type='inquire')       as inquiries,
              count(*) filter (where event_type='click_source')  as source_clicks,
              count(*) filter (where event_type='view_map')      as map_views
            from page_events
            where occurred_at >= now() - make_interval(days => %s)""",
            (days,)).fetchone())

        props = [dict(r) for r in conn.execute("""
            with agg as (
              select source_code, external_ref,
                count(*) filter (where event_type='view_detail')  as views,
                count(distinct session_hash)                      as sessions,
                count(*) filter (where event_type='save')         as saves,
                count(*) filter (where event_type='inquire')      as inquiries,
                count(*) filter (where event_type='click_source') as source_clicks
              from page_events
              where occurred_at >= now() - make_interval(days => %s)
                and external_ref is not null
              group by 1,2)
            select a.*,
              (a.inquiries*25 + a.saves*8 + a.source_clicks*3 + a.sessions) as interest,
              s.province, s.district, s.property_type, s.opening_price, g.grade
            from agg a
            left join lateral (
              select province, district, property_type, opening_price
              from listing_snapshots ls
              where ls.source_code=a.source_code and ls.external_ref=a.external_ref
              order by ls.observed_at desc limit 1) s on true
            left join property_grades g
              on g.source_code=a.source_code and g.external_ref=a.external_ref
            order by interest desc, views desc limit 25""", (days,)).fetchall()]

        zones = [dict(r) for r in conn.execute("""
            with demand as (
              select province, district,
                count(*) filter (where event_type in ('view_detail','view_list')) as views,
                count(distinct session_hash) as sessions,
                count(*) filter (where event_type='inquire') as inquiries
              from page_events
              where occurred_at >= now() - make_interval(days => %s)
                and province is not null
              group by 1,2),
            supply as (
              select province, district, count(distinct external_ref) as listings
              from listing_snapshots
              where auction_date is null or auction_date >= current_date
              group by 1,2)
            select d.province, d.district, d.views, d.sessions, d.inquiries,
              coalesce(s.listings,0) as listings,
              round(d.sessions::numeric / nullif(s.listings,0), 2) as demand_supply
            from demand d
            left join supply s on s.province=d.province
                              and coalesce(s.district,'')=coalesce(d.district,'')
            order by d.sessions desc, d.views desc limit 25""", (days,)).fetchall()]

        daily = [{"day": str(r["day"]), "views": r["views"], "sessions": r["sessions"]}
                 for r in conn.execute("""
            select occurred_at::date as day,
              count(*) filter (where event_type='view_detail') as views,
              count(distinct session_hash) as sessions
            from page_events
            where occurred_at >= now() - make_interval(days => %s)
            group by 1 order by 1""", (days,)).fetchall()]
    return totals, props, zones, daily


@app.get("/admin/monitor", response_class=HTMLResponse)
def admin_monitor(request: Request, token: str = Query(""), days: int = Query(7)):
    _require_admin(request, token)
    days = 30 if days == 30 else 90 if days == 90 else 7
    if DEMO_MODE:
        totals, props, zones, daily = {}, [], [], []
    else:
        totals, props, zones, daily = monitor_data(days)
    return env.get_template("monitor.html").render(
        title="สถิติคนดูทรัพย์", days=days, totals=totals or {},
        props=props, zones=zones, daily=daily,
        **base(is_admin=True, admin_token=token))


@app.get("/admin/feedback", response_class=HTMLResponse)
def admin_feedback(request: Request, token: str = Query("")):
    _require_admin(request, token)
    rows, stats = [], {"total": 0, "with_msg": 0, "avg_rating": None, "last7": 0}
    if not DEMO_MODE:
        from core.db import connect
        with connect() as conn:
            rows = [dict(r) for r in conn.execute(
                """select id, created_at, message, rating, contact,
                          page_url, device_class
                   from site_feedback order by created_at desc limit 300""").fetchall()]
            s = conn.execute(
                """select count(*) as total,
                          count(*) filter (where message is not null) as with_msg,
                          round(avg(rating)::numeric, 1) as avg_rating,
                          count(*) filter (where created_at >= now() - interval '7 days') as last7
                     from site_feedback""").fetchone()
            if s:
                stats = dict(s)
    return env.get_template("feedback.html").render(
        title="ความเห็นจากผู้ใช้", rows=rows, stats=stats,
        **base(is_admin=True, admin_token=token))


@app.get("/admin/promoted", response_class=HTMLResponse)
def admin_promoted(request: Request, token: str = Query("")):
    _require_admin(request, token)
    rows, sources = [], ["bam", "sam", "ktb", "ghb", "ttb", "led_auction"]
    if not DEMO_MODE:
        from core.db import connect
        with connect() as conn:
            pp = [dict(r) for r in conn.execute(
                """select source_code, external_ref, rank, note, active
                     from promoted_properties order by active desc, rank asc, created_at desc"""
            ).fetchall()]
            srcs = [dict(r) for r in conn.execute(
                "select code from sources order by code").fetchall()]
            if srcs:
                sources = [s["code"] for s in srcs]
        # เติมข้อมูลทรัพย์ (ชื่อ/ราคา/เกรด/รูป) ให้แต่ละรายการ
        info = {}
        if pp:
            conds = " or ".join(
                ["(source_code = %s and external_ref = %s)"] * len(pp))
            params = tuple(v for p in pp for v in (p["source_code"], p["external_ref"]))
            for r in load_rows(f"where {conds}", params, limit=len(pp)):
                er = enrich(r, current_settings(), True)
                info[(r["source_code"], r["external_ref"])] = {
                    "title": er.get("title"), "opening_price": er.get("opening_price"),
                    "grade": er.get("grade"),
                    "image": (er.get("images_view") or [{}])[0].get("url")}
        for p in pp:
            p.update(info.get((p["source_code"], p["external_ref"]), {}))
            rows.append(p)
    return env.get_template("promoted_admin.html").render(
        title="จัดการทรัพย์โปรโมท", rows=rows, sources=sources,
        **base(is_admin=True, admin_token=token))


_ADD_FIELDS = ("title", "property_type", "opening_price", "appraised_price",
               "province", "district", "subdistrict", "address_raw",
               "land_area_sqwa", "usable_area_sqm", "bedrooms", "bathrooms",
               "parking", "lat", "lng", "title_deed_type", "occupancy_note")


@app.get("/admin/add", response_class=HTMLResponse)
def admin_add(request: Request, token: str = Query(""), ref: str = Query(""),
              saved: bool = Query(False)):
    _require_admin(request, token)
    f, edit = {}, False
    ref = ref.strip()
    if ref and not DEMO_MODE:
        r = find_row("manual", ref, True)
        if r:
            edit = True
            f = {k: r.get(k) for k in _ADD_FIELDS}
            f["external_ref"] = ref
    return env.get_template("add_property.html").render(
        title="เพิ่มทรัพย์", f=f, edit=edit, saved=saved,
        **base(is_admin=True, admin_token=token))


@app.post("/admin/add")
async def admin_add_save(request: Request):
    import hashlib
    import json as _json
    import secrets
    from fastapi.responses import RedirectResponse
    form = await read_form(request)
    _require_admin(request, form.get("token", ""))
    tok = form.get("token", "")
    if DEMO_MODE:
        return RedirectResponse(f"/admin/add?token={tok}", status_code=303)

    ref = (form.get("external_ref") or "").strip() or ("m-" + secrets.token_hex(4))

    def _f(key):
        v = (form.get(key) or "").strip().replace(",", "")
        if not v:
            return None
        try:
            return float(v)
        except ValueError:
            return None

    def _i(key):
        v = _f(key)
        return int(v) if v is not None else None

    lat, lng = _f("lat"), _f("lng")
    cols = {
        "province": (form.get("province") or "").strip() or None,
        "district": (form.get("district") or "").strip() or None,
        "subdistrict": (form.get("subdistrict") or "").strip() or None,
        "address_raw": (form.get("address_raw") or "").strip() or None,
        "lat": lat, "lng": lng,
        "geo_precision": "parcel" if (lat is not None and lng is not None) else "district",
        "property_type": (form.get("property_type") or "other").strip(),
        "title_deed_type": (form.get("title_deed_type") or "").strip() or None,
        "title": (form.get("title") or "").strip() or None,
        "opening_price": _f("opening_price"),
        "appraised_price": _f("appraised_price"),
        "land_area_sqwa": _f("land_area_sqwa"),
        "usable_area_sqm": _f("usable_area_sqm"),
        "bedrooms": _i("bedrooms"),
        "bathrooms": _i("bathrooms"),
        "parking": _i("parking"),
        "occupancy_note": (form.get("occupancy_note") or "").strip() or None,
    }
    content_hash = hashlib.sha256(_json.dumps(
        {**cols, "ref": ref}, sort_keys=True, ensure_ascii=False, default=str
    ).encode("utf-8")).hexdigest()
    raw_fields = _json.dumps({**cols, "external_ref": ref, "manual": True},
                             ensure_ascii=False, default=str)

    from core.db import connect
    with connect() as conn:
        conn.execute(
            """insert into listing_snapshots
                 (source_code, external_ref, content_hash, parser_version, observed_at,
                  province, district, subdistrict, address_raw, lat, lng, geo_precision,
                  property_type, title_deed_type, title, opening_price, appraised_price,
                  land_area_sqwa, usable_area_sqm, bedrooms, bathrooms, parking,
                  occupancy_note, raw_fields)
               values ('manual', %s, %s, 'manual', now(),
                  %s,%s,%s,%s,%s,%s,%s,
                  %s,%s,%s,%s,%s,
                  %s,%s,%s,%s,%s,
                  %s, %s::jsonb)
               on conflict (source_code, external_ref, content_hash) do nothing""",
            (ref, content_hash,
             cols["province"], cols["district"], cols["subdistrict"], cols["address_raw"],
             cols["lat"], cols["lng"], cols["geo_precision"],
             cols["property_type"], cols["title_deed_type"], cols["title"],
             cols["opening_price"], cols["appraised_price"],
             cols["land_area_sqwa"], cols["usable_area_sqm"],
             cols["bedrooms"], cols["bathrooms"], cols["parking"],
             cols["occupancy_note"], raw_fields))

        # รูป — ใส่ใหม่ก็แทนที่ของเดิม; เว้นว่างไว้ = คงรูปเดิม (ตอนแก้ไข)
        urls = [u.strip() for u in (form.get("image_urls") or "").splitlines() if u.strip()]
        if urls:
            conn.execute(
                "delete from listing_images where source_code = 'manual' and external_ref = %s",
                (ref,))
            for i, u in enumerate(urls):
                conn.execute(
                    """insert into listing_images
                         (source_code, external_ref, image_source, origin_url,
                          content_hash, is_primary, sort_order, attribution)
                       values ('manual', %s, 'own_survey', %s, %s, %s, %s, 'แปลงดี')
                       on conflict (source_code, external_ref, content_hash) do nothing""",
                    (ref, u, hashlib.sha256(u.encode()).hexdigest(), i == 0, i))
        conn.commit()

    return RedirectResponse(f"/p/manual/{ref}" + (f"?token={tok}" if tok else ""),
                            status_code=303)


@app.post("/admin/promoted/add")
async def admin_promoted_add(request: Request):
    form = await read_form(request)
    _require_admin(request, form.get("token", ""))
    from fastapi.responses import RedirectResponse
    sc = (form.get("source_code") or "").strip()
    ref = (form.get("external_ref") or "").strip()
    nxt = form.get("next") or f"/admin/promoted?token={form.get('token','')}"
    if sc and ref and not DEMO_MODE:
        try:
            rank = int(form.get("rank") or 100)
        except (TypeError, ValueError):
            rank = 100
        from core.db import connect
        with connect() as conn:
            conn.execute(
                """insert into promoted_properties (source_code, external_ref, rank, note, active)
                   values (%s,%s,%s,%s,true)
                   on conflict (source_code, external_ref) do update set
                     rank = excluded.rank, note = excluded.note, active = true""",
                (sc, ref, rank, (form.get("note") or None)))
            conn.commit()
    return RedirectResponse(nxt, status_code=303)


@app.post("/admin/promoted/remove")
async def admin_promoted_remove(request: Request):
    form = await read_form(request)
    _require_admin(request, form.get("token", ""))
    from fastapi.responses import RedirectResponse
    sc = (form.get("source_code") or "").strip()
    ref = (form.get("external_ref") or "").strip()
    if sc and ref and not DEMO_MODE:
        from core.db import connect
        with connect() as conn:
            conn.execute(
                "delete from promoted_properties where source_code = %s and external_ref = %s",
                (sc, ref))
            conn.commit()
    return RedirectResponse(f"/admin/promoted?token={form.get('token','')}", status_code=303)


@app.get("/health", response_class=HTMLResponse)
def health(request: Request, token: str = Query("")):
    _require_admin(request, token)
    runs = []
    if not DEMO_MODE:
        from core.db import connect
        with connect() as conn:
            runs = [dict(r) for r in conn.execute(
                """select source_code, started_at, status, pages_fetched,
                          rows_new, error_count
                   from ingest_runs order by started_at desc limit 30""").fetchall()]
    return env.get_template("health.html").render(title="สุขภาพระบบ", runs=runs,
                                                   **base(is_admin=True, admin_token=token))


if __name__ == "__main__":
    import uvicorn
    print("=" * 62)
    print(f"  ไฟล์ .env  : {_env.ENV_PATH or 'ไม่พบ'}")
    print(f"  {_env.describe()}")
    print(f"  โหมดข้อมูล : {'ตัวอย่าง — ' + DEMO_REASON if DEMO_MODE else 'ฐานข้อมูลจริง'}")
    print(f"  โหมดรูปภาพ : {'เผยแพร่ (ซ่อนรูปที่ไม่มีสิทธิ์)' if PUBLIC_MODE else 'ใช้ภายใน (แสดงทุกรูป)'}")
    # HOST=0.0.0.0 เมื่อ deploy ขึ้น cloud (ให้เข้าจากภายนอกได้) — ในเครื่องใช้ 127.0.0.1
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 8000))
    print(f"  เปิด http://{host}:{port}")
    print("=" * 62)
    uvicorn.run(app, host=host, port=port)
