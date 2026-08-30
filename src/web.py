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
# Google (Gmail) Login — สร้าง OAuth client ใน Google Cloud Console
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_LOGIN_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
USER_COOKIE = "npa_user"        # cookie session ของผู้ใช้ (LINE/Google) แยกจาก admin
# Supabase Storage — เก็บรูปประกาศที่ผู้ใช้อัปโหลด (marketplace)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "listings").strip()
STORAGE_ENABLED = bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)
# AI Renovate (Gemini image) — สร้างภาพจำลองรีโนเวท before/after
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL",
                                    "gemini-2.5-flash-image-preview").strip()
RENOVATE_ENABLED = bool(GEMINI_API_KEY and STORAGE_ENABLED)
try:
    RENO_FREE_LIMIT = int(os.environ.get("RENO_FREE_LIMIT", "5"))
except ValueError:
    RENO_FREE_LIMIT = 5

# ประกาศสมาชิก: "ยังไม่หมดอายุ" = ถูกดัน/ต่ออายุภายใน N วัน (ไม่ดันเกินให้ซ่อน)
try:
    MEMBER_ACTIVE_DAYS = int(os.environ.get("MEMBER_ACTIVE_DAYS", "60"))
except ValueError:
    MEMBER_ACTIVE_DAYS = 60
MEMBER_ACTIVE_SQL = f"last_bumped_at > now() - interval '{MEMBER_ACTIVE_DAYS} days'"
# อนุญาตให้กด "ดันประกาศ" ได้เมื่อดันล่าสุดเกินกี่ชั่วโมง (กันสแปมดัน)
MEMBER_BUMP_COOLDOWN_H = 20

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


def _finish_login(uid: str, name, pic, next_url: str):
    """upsert ผู้ใช้ + ตั้ง cookie + redirect — ใช้ร่วมทั้ง LINE และ Google
    uid เป็นคีย์รวม เช่น 'line:Uxxxx' หรือ 'google:1234' (เก็บใน app_users.line_user_id)
    """
    from fastapi.responses import RedirectResponse
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
                    (uid, name, pic))
                conn.commit()
        except Exception as exc:                                    # noqa: BLE001
            log.warning("บันทึกผู้ใช้ล้มเหลว (รัน migration 037?): %s", str(exc)[:120])
    resp = RedirectResponse(next_url if next_url.startswith("/") else "/favorites",
                            status_code=303)
    resp.set_cookie(USER_COOKIE, make_user_cookie(uid), max_age=30 * 86400,
                    httponly=True, samesite="lax", secure=True)
    resp.delete_cookie("npa_oauth")
    return resp


_IMG_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


_LAST_STORAGE_ERR = ""   # เก็บสาเหตุอัปโหลดล้มเหลวล่าสุด (สำหรับ /api/_diag/storage)


def upload_image_to_storage(data: bytes, content_type: str) -> str | None:
    """อัปโหลดรูปขึ้น Supabase Storage → คืน public URL (None ถ้าไม่ได้ตั้งค่า/ล้มเหลว)"""
    global _LAST_STORAGE_ERR
    if not STORAGE_ENABLED:
        _LAST_STORAGE_ERR = "STORAGE_ENABLED=false (ยังไม่ได้ตั้ง SUPABASE_URL/SUPABASE_SERVICE_KEY)"
        return None
    ext = _IMG_EXT.get((content_type or "").split(";")[0].strip().lower())
    if not ext:
        _LAST_STORAGE_ERR = f"unsupported content-type: {content_type!r}"
        return None
    import secrets
    import urllib.error
    import urllib.request
    path = f"member/{secrets.token_hex(16)}.{ext}"
    up_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{path}"
    try:
        req = urllib.request.Request(up_url, data=data, method="POST", headers={
            "Authorization": "Bearer " + SUPABASE_SERVICE_KEY,
            "apikey": SUPABASE_SERVICE_KEY,          # Supabase gateway ต้องมี header นี้ด้วย
            "Content-Type": content_type, "x-upsert": "true"})
        with urllib.request.urlopen(req, timeout=20) as r:
            if r.status not in (200, 201):
                _LAST_STORAGE_ERR = f"unexpected status {r.status}"
                return None
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:                                           # noqa: BLE001
            pass
        _LAST_STORAGE_ERR = f"HTTP {exc.code} จาก Supabase: {body}"
        log.warning("อัปโหลดรูปขึ้น Storage ล้มเหลว: %s", _LAST_STORAGE_ERR[:200])
        return None
    except Exception as exc:                                        # noqa: BLE001
        _LAST_STORAGE_ERR = f"{type(exc).__name__}: {str(exc)[:200]}"
        log.warning("อัปโหลดรูปขึ้น Storage ล้มเหลว: %s", _LAST_STORAGE_ERR)
        return None
    _LAST_STORAGE_ERR = ""
    return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{path}"


# AI Renovate — สไตล์ + prompt (คงโครงสร้างห้อง เปลี่ยนแค่ผิว/เฟอร์ฯ ไม่ over)
RENO_STYLES = {
    "clean": ("รีโนเวทสะอาดตา (ใกล้เดิม)", "a clean, freshly renovated version"),
    "modern": ("โมเดิร์น มินิมอล", "a modern minimalist style renovation"),
    "warm": ("โทนอบอุ่น ไม้ธรรมชาติ", "a warm cozy renovation with natural wood tones"),
    "muji": ("ญี่ปุ่น/มูจิ", "a Japanese Muji-style minimalist renovation"),
}


def _reno_prompt(style_en: str) -> str:
    return (
        "Renovate this real-estate room photo into " + style_en + ". "
        "CRITICAL RULES: keep the EXACT same room layout, wall positions, windows, doors, "
        "ceiling height, and camera viewpoint — do not move or remove structural elements. "
        "Only refresh finishes: wall paint, flooring, lighting, and add tasteful, realistic "
        "furniture and decor. Photorealistic, natural daylight, clean and believable as a real "
        "renovation of the SAME room. Do NOT exaggerate or add luxury elements that would "
        "misrepresent the property.")


def _fetch_image_bytes(url: str, max_bytes: int = 8 * 1024 * 1024):
    """โหลดรูปจาก URL (ฝั่งเซิร์ฟเวอร์) → (bytes, mime) หรือ (None, None)"""
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            data = r.read(max_bytes + 1)
            if len(data) > max_bytes:
                return None, None
            mime = (r.headers.get("Content-Type", "image/jpeg") or "image/jpeg").split(";")[0].strip()
            return data, mime
    except Exception as exc:                                        # noqa: BLE001
        log.warning("โหลดรูปต้นทางไม่สำเร็จ: %s", str(exc)[:120])
        return None, None


def gemini_renovate(img_bytes: bytes, mime: str, style_en: str):
    """เรียก Gemini image → (result_bytes, out_mime) หรือ None"""
    if not GEMINI_API_KEY:
        return None
    import base64
    import json as _json
    import urllib.request
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_IMAGE_MODEL}:generateContent?key={GEMINI_API_KEY}")
    if mime not in _IMG_EXT:
        mime = "image/jpeg"
    body = {"contents": [{"parts": [
        {"text": _reno_prompt(style_en)},
        {"inline_data": {"mime_type": mime,
                         "data": base64.b64encode(img_bytes).decode()}}]}],
        "generationConfig": {"responseModalities": ["IMAGE"]}}
    try:
        req = urllib.request.Request(
            url, data=_json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = _json.loads(r.read())
        for cand in resp.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                idata = part.get("inlineData") or part.get("inline_data")
                if idata and idata.get("data"):
                    out_mime = (idata.get("mimeType") or idata.get("mime_type")
                                or "image/png")
                    return base64.b64decode(idata["data"]), out_mime
        log.warning("Gemini ไม่คืนรูป: %s", str(resp)[:200])
    except Exception as exc:                                        # noqa: BLE001
        log.warning("Gemini renovate ล้มเหลว: %s", str(exc)[:200])
    return None


def _reno_used(uid: str) -> int:
    """จำนวนรูปที่ผู้ใช้เคยรีโนเวท (นับ quota ฟรี)"""
    if not uid or DEMO_MODE:
        return 0
    try:
        from core.db import connect
        with connect() as conn:
            return conn.execute(
                "select count(*) as n from member_renovations where created_by=%s",
                (uid,)).fetchone()["n"]
    except Exception:                                              # noqa: BLE001
        return 0


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

# ── ข้อมูลจังหวัด/อำเภอ/ตำบล (ทางการ 77 จังหวัด · 928 อำเภอ · 7,436 ตำบล) ──
# ใช้ทำ dropdown เชื่อมโยงกันในหน้า /sell — เก็บเป็น gzip+base64 ในซอร์ส (self-contained)
import base64 as _b64_geo
import gzip as _gz_geo
_TH_GEO_JSON = _gz_geo.decompress(_b64_geo.b64decode(
    "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"
)).decode("utf-8")

# รายชื่อ 77 จังหวัด (จาก _TH_GEO_JSON) — ใช้เดาจังหวัดจากชื่อศาล/สำนักงานบังคับคดี LED
try:
    _TH_PROVINCES = sorted(json.loads(_TH_GEO_JSON).keys(), key=len, reverse=True)
except Exception:                                                  # noqa: BLE001
    _TH_PROVINCES = []


def _blank(v) -> bool:
    """ถือว่าว่าง ถ้าเป็น None/''/'-'/'0' (LED ใช้ '-' เป็น placeholder ของค่าว่าง)"""
    return (v or "").strip() in ("", "-", "0")


def province_from_texts(*texts) -> str | None:
    """เดาจังหวัดจากข้อความ (ชื่อศาล/สำนักงานบังคับคดี ฯลฯ) โดยจับชื่อจังหวัดที่ปรากฏ"""
    blob = " ".join(t for t in texts if t)
    if not blob:
        return None
    if ("กรุงเทพ" in blob) or ("กทม" in blob):
        return "กรุงเทพมหานคร"
    for p in _TH_PROVINCES:                                        # เรียงยาว→สั้น กันจับซ้อน
        if p and p in blob:
            return p
    return None


def led_extra(ref: str) -> dict:
    """ดึง สำนักงานบังคับคดี + ศาล ของทรัพย์ LED (office_name เป็นคอลัมน์, court_name อยู่ raw_fields)"""
    if DEMO_MODE:
        return {}
    try:
        from core.db import connect
        with connect() as conn:
            row = conn.execute(
                """select office_name, raw_fields->>'court_name' as court_name,
                          raw_fields->>'auction_venue' as auction_venue,
                          raw_fields->'_open_post'->>'province_id' as led_pid,
                          raw_fields->'_open_post'->>'province_name' as led_pname,
                          raw_fields->'_open_post'->>'search_bid_date' as led_bdate,
                          raw_fields->'_open_post'->>'saletypename' as saletype,
                          raw_fields->'_open_post'->>'occupant' as occupant,
                          raw_fields->'_open_post'->>'sale_location1' as sale_location,
                          raw_fields->'_open_post'->>'sale_time1' as sale_time,
                          raw_fields->'_open_post' as op
                     from listing_snapshots
                    where source_code='led_auction' and external_ref=%s
                    order by observed_at desc limit 1""", (ref,)).fetchone()
        if not row:
            return {}
        d = dict(row)
        op = d.pop("op", None) or {}
        # ตารางนัดประมูลทุกนัด (จาก biddate1..8 ที่เก็บไว้)
        import datetime as _dt
        today = _dt.date.today()
        sched = []
        for i in range(1, 9):
            bd = op.get(f"biddate{i}")
            if bd and bd.isdigit() and len(bd) == 8:
                try:
                    dd = _dt.date(int(bd[:4]) - 543, int(bd[4:6]), int(bd[6:8]))
                    sched.append({"round": i, "label": _thai_date(dd), "past": dd < today})
                except ValueError:
                    pass
        d["schedule"] = sched
        return d
    except Exception as exc:                                        # noqa: BLE001
        log.warning("led_extra ล้มเหลว: %s", str(exc)[:120])
        return {}

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
      <a href="/upcoming" class="navlink whitespace-nowrap">กำลังประมูล</a>
      <a href="/auction-results" class="navlink whitespace-nowrap">จบประมูล</a>
      <a href="/articles" class="navlink whitespace-nowrap">บทความ</a>
      <a href="/market" class="navlink whitespace-nowrap">ตลาดขาย-เช่า</a>
      {% if any_login %}
        {% if user_logged_in %}
        <a href="/sell" class="navlink whitespace-nowrap" style="color:var(--survey)">+ ลงประกาศ</a>
        <a href="/favorites" class="navlink whitespace-nowrap">❤️ โปรด</a>
        <a href="/my-listings" class="navlink whitespace-nowrap">ประกาศของฉัน</a>
        <a href="/auth/logout" class="navlink whitespace-nowrap text-slate-400">ออกจากระบบ</a>
        {% else %}
        <a href="/login" class="navlink whitespace-nowrap" style="color:var(--survey)">เข้าสู่ระบบ</a>
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
      <a href="/admin/market{{ tk }}" class="navlink whitespace-nowrap">อนุมัติประกาศ</a>
      <a href="/admin/settings{{ tk }}" class="navlink whitespace-nowrap">ตั้งค่า</a>
      <a href="/admin/sources{{ tk }}" class="navlink whitespace-nowrap">แหล่งข้อมูล</a>
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
    if(r.status===401){ location.href='/login?next='+encodeURIComponent(location.pathname); return null; }
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
      {% if src %}<input type="hidden" name="src" value="{{ src }}">{% endif %}
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
     style="{% if not institution and not src %}background:var(--survey);border-color:var(--survey);color:#fff{% else %}background:#fff;border-color:var(--rule);color:var(--pencil){% endif %}">ทุกแหล่ง</a>
  {% for i in institutions %}
  <a href="/?institution={{ i }}{% if admin_token %}&token={{ admin_token }}{% endif %}"
     class="chip px-3 py-1.5 text-[13px] font-medium border transition hover:border-slate-400"
     style="{% if i==institution %}background:var(--survey);border-color:var(--survey);color:#fff{% else %}background:#fff;border-color:var(--rule);color:var(--pencil){% endif %}">{{ i }}</a>
  {% endfor %}
  <span class="mx-1 text-slate-300">|</span>
  <a href="/?src=member{% if admin_token %}&token={{ admin_token }}{% endif %}"
     class="chip px-3 py-1.5 text-[13px] font-medium border transition hover:border-sky-400"
     style="{% if src=='member' %}background:#1C86C9;border-color:#1C86C9;color:#fff{% else %}background:#EEF6FF;border-color:#BEE0F7;color:#1C86C9{% endif %}">🏠 เจ้าของลงเอง</a>
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
  {% if src %}<input type="hidden" name="src" value="{{ src }}">{% endif %}
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
{% if r.is_member %}
<a href="/m/{{ r.id }}" class="sheet overflow-hidden transition block">
  <div class="relative imgwrap" style="background:#EEF1F3">
    {% if r.image %}<img src="{{ r.image }}" alt="{{ r.title }}" loading="lazy" class="w-full h-48 object-cover">{% else %}<div class="h-48"></div>{% endif %}
    <span class="absolute top-2.5 left-2.5 text-[11px] px-2 py-1 rounded text-white font-medium" style="background:{{ '#1C86C9' if r.listing_kind=='rent' else 'var(--seal)' }}">{{ 'ให้เช่า' if r.listing_kind=='rent' else 'ขาย' }}</span>
    <span class="absolute top-2.5 right-2.5 text-[11px] px-2 py-1 rounded bg-white/95 font-medium" style="color:#1C86C9">เจ้าของลงเอง</span>
  </div>
  <div class="p-3">
    <div class="text-xl font-semibold">{{ "{:,.0f}".format(r.price or 0) }} <span class="text-sm font-normal text-slate-500">บาท{{ '/เดือน' if r.listing_kind=='rent' else '' }}</span></div>
    <div class="mt-1.5 font-medium text-sm line-clamp-2">{{ r.title }}</div>
    <div class="mt-1 text-xs text-slate-600 flex flex-wrap gap-x-3 gap-y-0.5">
      {% if r.usable_area_sqm %}<span>{{ r.usable_area_sqm }} ตร.ม.</span>{% endif %}
      {% if r.land_area_sqwa %}<span>{{ r.land_area_sqwa }} ตร.ว.</span>{% endif %}
      {% if r.bedrooms %}<span>{{ r.bedrooms }} นอน</span>{% endif %}
      {% if r.bathrooms %}<span>{{ r.bathrooms }} น้ำ</span>{% endif %}
    </div>
    <div class="mt-1.5 text-xs text-slate-500 line-clamp-1">{{ r.type_label }}{% if r.district %} · {{ r.district }}{% endif %}{% if r.province %} {{ r.province }}{% endif %}{% if r.listing_kind=='rent' and r.pets_allowed=='yes' %} · 🐾 เลี้ยงสัตว์ได้{% endif %}</div>
  </div>
</a>
{% else %}
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
{% endif %}
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

  <!-- แชร์ -->
  <div class="sheet p-3 flex items-center gap-2 flex-wrap text-sm">
    <span class="font-medium text-slate-600">แชร์:</span>
    <a href="#" onclick="return shareLine()" class="px-3 py-1.5 rounded-lg text-white text-xs font-medium" style="background:#06C755">LINE</a>
    <a href="#" onclick="return shareFb()" class="px-3 py-1.5 rounded-lg text-white text-xs font-medium" style="background:#1877F2">Facebook</a>
    <button onclick="copyLink(this)" class="px-3 py-1.5 rounded-lg border text-xs font-medium text-slate-600 hover:bg-slate-50">คัดลอกลิงก์</button>
    {% if any_login %}
    <button type="button" onclick="toggleFav(this)" data-sc="{{ r.source_code }}" data-ref="{{ r.external_ref }}"
      aria-pressed="{{ 'true' if is_fav else 'false' }}"
      class="ml-auto px-3 py-1.5 rounded-lg border text-xs font-medium hover:bg-slate-50"
      style="border-color:var(--seal);color:var(--seal)">{{ '❤️ บันทึกแล้ว' if is_fav else '🤍 บันทึกทรัพย์นี้' }}</button>
    {% endif %}
  </div>

  <div class="sheet p-4">
    <h2 class="font-semibold mb-3">รายละเอียดทรัพย์</h2>
    <dl class="grid grid-cols-2 sm:grid-cols-3 gap-y-3 gap-x-4 text-sm">
      {% for k, v in specs %}
      <div><dt class="text-slate-500 text-xs">{{ k }}</dt><dd class="font-medium">{{ v }}</dd></div>
      {% endfor %}
    </dl>
    {% if r.led_pid and r.led_bdate %}
    <form method="post" action="https://asset.led.go.th/newbidreg/asset_search_day.asp" target="_blank" rel="noopener" class="mt-4">
      <input type="hidden" name="province_id" value="{{ r.led_pid }}">
      <input type="hidden" name="province_name" value="{{ r.led_pname }}">
      <input type="hidden" name="search_bid_date" value="{{ r.led_bdate }}">
      <button class="inline-flex items-center gap-1.5 text-sm border rounded-lg px-3 py-2 hover:bg-slate-50 font-medium">
        ↗ ดูประกาศที่กรมบังคับคดี
        <span class="text-xs font-normal text-slate-400">สำนักงาน{{ r.led_pname }} · วันขาย {{ r.led_bdate }}</span>
      </button>
    </form>
    <p class="text-[11px] text-slate-400 mt-1">เปิดหน้ารายการของสำนักงานในวันขายที่บันทึกไว้ — ถ้าเลยวันขายแล้ว ค้นนัดถัดไปในเว็บกรมฯ</p>
    {% endif %}
  </div>

  {% if r.lat and r.lng %}
  <script>
  (function(){
  function nbInit(){
    var el=document.getElementById('nearby'); if(!el) return;
    var lat=el.getAttribute('data-lat'), lng=el.getAttribute('data-lng');
    var body=document.getElementById('nb-body');
    var CAT=[['rail','🚊'],['transport','🚌'],['edu','🎓'],['health','🏥'],['life','🛒']];
    var DATA=null;
    function fd(m){ return m<1000 ? m+' ม.' : (m/1000).toFixed(1)+' กม.'; }
    function render(){
      if(!DATA) return;
      var rings=[[500,'500 ม.'],[1000,'1 กม.'],[3000,'3 กม.']];
      var h='';
      rings.forEach(function(rg,idx){
        var R=rg[0], prevR=idx===0?0:rings[idx-1][0];
        var cats=CAT.map(function(c){
          var d=DATA[c[0]]||{c500:0,c1000:0,c3000:0};
          var cnt = R===500?d.c500 : R===1000?d.c1000 : d.c3000;
          return '<span class="nb-cat'+(cnt?'':' zero')+'">'+c[1]+'<b>'+cnt+'</b></span>';
        }).join('');
        var top=[];
        CAT.forEach(function(c){
          var arr=((DATA[c[0]]||{}).near||[]).filter(function(x){ return x.dist>prevR && x.dist<=R; });
          if(arr.length){ arr.sort(function(a,b){ return a.dist-b.dist; });
            top.push({ic:c[1], nm:arr[0].name||'', d:arr[0].dist}); }
        });
        top.sort(function(a,b){ return a.d-b.d; });
        top=top.map(function(t){ return '<span class="nb-ic">'+t.ic+'</span> <span class="nb-nm">'+t.nm+'</span> <span class="nb-dm">'+fd(t.d)+'</span>'; });
        h+='<div class="nb-ring" style="animation-delay:'+(idx*0.14).toFixed(2)+'s">'
          +'<div class="nb-rlabel">'+rg[1]+'</div>'
          +'<div class="nb-cats">'+cats+'</div>'
          +(top.length?'<div class="nb-names">'+top.join(' · ')+'</div>':'')
          +'</div>';
      });
      body.innerHTML=h;
    }
    fetch('/api/nearby?lat='+encodeURIComponent(lat)+'&lng='+encodeURIComponent(lng))
      .then(function(r){return r.json();}).then(function(res){
        if(res&&res.ok){ DATA=res.data; render(); }
        else { body.innerHTML='<span class="text-amber-600 text-sm">'+((res&&res.message)||'ดึงข้อมูลรอบทรัพย์ไม่สำเร็จ')+'</span>'; }
      }).catch(function(){ body.innerHTML='<span class="text-amber-600 text-sm">ดึงข้อมูลรอบทรัพย์ไม่สำเร็จ ลองรีเฟรช</span>'; });
  }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded', nbInit); else nbInit();
  })();
  </script>
  {% endif %}

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

  {% if r.led_schedule %}
  <div class="sheet p-4">
    <h2 class="font-semibold text-sm mb-2">📅 นัดประมูล <span class="text-xs font-normal text-slate-400">(ราคานัดหลังมักลดลงถ้ายังไม่มีผู้สู้ราคา)</span></h2>
    <div class="flex flex-wrap gap-2">
      {% for s in r.led_schedule %}
      <span class="text-xs px-2.5 py-1 rounded-lg border {% if s.past %}bg-slate-100 text-slate-400{% else %}text-slate-700 bg-white{% endif %}">
        นัด {{ s.round }} · {{ s.label }}{% if s.past %} · ผ่านแล้ว{% endif %}</span>
      {% endfor %}
    </div>
    {% if r.led_sale_location and r.led_sale_location != '-' %}<div class="text-xs text-slate-500 mt-3">📍 สถานที่ขาย: {{ r.led_sale_location }}</div>{% endif %}
  </div>
  {% endif %}

  {% if auc_result %}
  <div class="sheet p-4" style="border-left:4px solid {{ '#10b981' if auc_result.is_sold else '#94a3b8' }}">
    <div class="text-xs text-slate-500">🔨 ผลการประมูล · นัดขาย {{ auc_result.date_label }}</div>
    {% if auc_result.is_sold %}
    <div class="text-xl font-bold mt-0.5" style="color:var(--survey-deep)">✓ ขายได้ {{ "{:,.0f}".format(auc_result.sold_price or 0) }} บาท</div>
    {% if auc_result.suspect %}
    <div class="text-[11px] text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-2 py-1.5 mt-1 leading-snug">
      ⚠️ ราคาต่ำผิดปกติเทียบประเมิน {{ "{:,.0f}".format(auc_result.appraised_price or 0) }} — มักเป็นการขายเฉพาะส่วน หรือโจทก์/เจ้าหนี้ซื้อได้แล้วหักกับหนี้ (ไม่ใช่ราคาตลาด) ควรตรวจสอบกับกรมบังคับคดี
    </div>
    {% elif auc_result.pct is not none %}<div class="text-sm {{ 'text-emerald-600' if auc_result.pct>=0 else 'text-red-500' }}">{{ '+' if auc_result.pct>=0 else '' }}{{ auc_result.pct }}% จากราคาประเมิน {{ "{:,.0f}".format(auc_result.appraised_price or 0) }}</div>{% endif %}
    {% else %}
    <div class="text-lg font-semibold text-slate-600 mt-0.5">{{ auc_result.result or 'ไม่มีผู้สู้ราคา' }}</div>
    <div class="text-xs text-slate-400">ราคาประเมิน {{ "{:,.0f}".format(auc_result.appraised_price or 0) }} บาท</div>
    {% endif %}
    {% if auc_result.case_no %}
    <div class="text-xs text-slate-500 mt-2 pt-2 border-t" style="border-color:var(--rule)">
      คดีหมายเลขแดง <b>{{ auc_result.case_no }}</b>{% if auc_result.court %} · ศาล{{ auc_result.court }}{% endif %}{% if auc_result.deed and auc_result.deed != '-' %} · โฉนด {{ auc_result.deed }}{% endif %}
      {% if auc_result.plaintiff %}<div class="text-slate-400">โจทก์: {{ auc_result.plaintiff }}</div>{% endif %}
      <div class="text-[11px] text-slate-400 mt-1">ใช้เลขคดีแดงนี้ค้นสำนวน/ผลที่ <a href="https://asset.led.go.th/report/reports.asp" target="_blank" rel="noopener" class="brandlink">กรมบังคับคดี</a> ได้</div>
    </div>
    {% endif %}
    <a href="/auction-results" class="text-xs brandlink inline-block mt-1.5">ดูสรุปผลจบประมูลทั้งหมด →</a>
  </div>
  {% endif %}

  {% if r.lat and r.lng %}
  <div class="sheet p-4" id="nearby" data-lat="{{ r.lat }}" data-lng="{{ r.lng }}">
    <style>
      #nearby .nb-ring{border-top:1px solid var(--rule);padding-top:11px;margin-top:11px;opacity:0;transform:translateY(7px);animation:nbin .45s ease forwards}
      #nearby .nb-ring:first-of-type{border-top:0;padding-top:0;margin-top:10px}
      #nearby .nb-rlabel{display:inline-block;font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:11px;letter-spacing:.03em;color:#fff;background:var(--ink);border-radius:999px;padding:2px 11px}
      #nearby .nb-cats{display:flex;flex-wrap:wrap;gap:10px;margin-top:8px;font-size:14px}
      #nearby .nb-cat{display:inline-flex;align-items:center;gap:4px;color:#334155}
      #nearby .nb-cat b{font-variant-numeric:tabular-nums;font-weight:600}
      #nearby .nb-cat.zero{opacity:.5}
      #nearby .nb-nm{color:#1f2937;font-weight:500}
      #nearby .nb-dm{color:#6b7280;font-variant-numeric:tabular-nums}
      #nearby .nb-ic{margin-right:2px}
      #nearby .nb-names span{white-space:nowrap}
      #nearby .nb-names{margin-top:6px;font-size:11.5px;color:#64748b;line-height:1.65}
      @keyframes nbin{to{opacity:1;transform:none}}
    </style>
    <h2 class="font-semibold text-sm">📍 รอบทรัพย์นี้ <span class="text-xs font-normal text-slate-400">มีอะไรใกล้ๆ</span></h2>
    <div id="nb-body" class="mt-2 text-sm text-slate-500">
      <span class="inline-flex items-center gap-2"><span class="animate-pulse">⏳</span> กำลังวิเคราะห์รอบทรัพย์…</span>
    </div>
    <p class="text-[11px] text-slate-400 mt-2">สถานที่จาก OpenStreetMap</p>
  </div>
  {% endif %}

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

// ── ประกาศตลาด (ผู้ลงเอง) — ขาย/เช่า เป็นชั้นแยก ติ๊กเปิดได้จาก layer control ──
function marketLayer(kind, color, label){
  fetch('/api/market.geojson'+(kind?('?kind='+kind):'')).then(r=>r.json()).then(gj=>{
    if(!gj.features||!gj.features.length) return;
    const lyr=L.layerGroup();
    gj.features.forEach(f=>{
      const c=f.geometry.coordinates, p=f.properties;
      const mk=L.circleMarker([c[1],c[0]],{radius:8,color:'#fff',weight:2,fillColor:color,fillOpacity:0.95});
      mk.bindPopup(`<div style="min-width:200px;max-width:240px">
        ${p.image?`<img src="${p.image}" style="width:100%;height:110px;object-fit:cover;border-radius:6px">`:''}
        <div style="font-size:11px;color:#64748b;margin-top:5px">${label} · ${p.type_label||''}</div>
        <div style="font-weight:600;line-height:1.3;margin-top:2px">${p.title}</div>
        <div style="font-size:17px;font-weight:600;margin-top:3px">${baht(p.price)}
          <span style="font-size:11px;color:#64748b">บาท${kind==='rent'?'/เดือน':''}</span></div>
        <a href="/m/${p.id}" style="display:block;text-align:center;margin-top:8px;padding:6px;background:#0f172a;color:#fff;border-radius:6px;font-size:12px;text-decoration:none">ดูประกาศ</a>
      </div>`);
      lyr.addLayer(mk);
    });
    layerCtrl.addOverlay(lyr, label+' ('+gj.features.length+')');
  });
}
marketLayer('sale','#E24637','🏷️ ตลาด: ขาย');
marketLayer('rent','#1C86C9','🔑 ตลาด: เช่า');

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

// สร้าง HTML ของ popup เมื่อคลิกเท่านั้น (ไม่สร้างล่วงหน้าทุกหมุด = เร็วขึ้นมาก)
const _gradeColor = {A:'#059669',B:'#65a30d',C:'#f59e0b',D:'#ea580c',E:'#dc2626'};
function popupHTML(p){
  return `<div style="min-width:250px;max-width:280px">
      <img src="${p.image}" loading="lazy" referrerpolicy="no-referrer"
           onerror="this.style.display='none'"
           style="width:100%;height:120px;object-fit:cover;border-radius:6px">
      <div style="display:flex;align-items:center;gap:6px;margin-top:7px">
        ${p.grade?`<span style="background:${_gradeColor[p.grade]||'#94a3b8'};color:#fff;
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
    </div>`;
}

let _reqSeq = 0, _fitted = false;
function loadProps(){
  const my = ++_reqSeq;                       // กันผลเก่าซ้อนผลใหม่ (แก้หมุดเบิล)
  const cEl=document.getElementById('srccount');
  if(cEl) cEl.textContent='กำลังโหลดแผนที่…';
  fetch('/api/properties.geojson?'+selectedQS()).then(r=>r.json()).then(gj=>{
    if(my !== _reqSeq) return;                // มีคำขอใหม่กว่าแล้ว — ทิ้งผลนี้
    cluster.clearLayers();                    // เคลียร์ตอน "ได้ผลจริง" ไม่ใช่ตอนเริ่ม
    const markers=[]; const bounds=[];
    gj.features.forEach(f=>{
      const p=f.properties, c=f.geometry.coordinates;
      const mk=L.circleMarker([c[1],c[0]],{radius:8,color:'#fff',weight:2,fillColor:colorFor(p),fillOpacity:0.95});
      mk.on('click', function(){                // ผูก popup ตอนคลิก (lazy)
        if(!mk._bound){ mk.bindPopup(popupHTML(p),{maxWidth:300}); mk._bound=true; mk.openPopup(); }
      });
      markers.push(mk); bounds.push([c[1],c[0]]);
    });
    cluster.addLayers(markers);               // เพิ่มทีเดียว (เร็วกว่าเพิ่มทีละหมุด)
    if(bounds.length && !_fitted){ map.fitBounds(bounds,{padding:[40,40],maxZoom:14}); _fitted=true; }
    if(cEl){
      const all=[...document.querySelectorAll('.srcflt')];
      const on=all.filter(b=>b.checked).length;
      const flt=(on && on<all.length) ? (' · กรอง '+on+' แหล่ง') : ' · ทุกแหล่ง';
      cEl.textContent='แสดง '+(gj.features.length).toLocaleString('th-TH')+' ทรัพย์'+flt;
    }
  }).catch(()=>{ if(my===_reqSeq && cEl) cEl.textContent='โหลดแผนที่ไม่สำเร็จ ลองใหม่'; });
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
    <th class="p-2">ref</th><th class="p-2">หน่วยงานที่ขาย</th>
    <th class="p-2">จังหวัด</th><th class="p-2">อำเภอ</th>
    <th class="p-2">เลขโฉนด</th><th class="p-2">พิกัดแปลง (lat,lng)</th></tr></thead>
  <tbody>
  {% for r in pending %}
    <tr class="border-b hover:bg-slate-50">
      <td class="p-2 font-mono text-xs align-top">{{ r.external_ref }}
        {% if r.led_pid and r.led_bdate %}
        <form method="post" action="https://asset.led.go.th/newbidreg/asset_search_day.asp" target="_blank" rel="noopener" class="mt-1">
          <input type="hidden" name="province_id" value="{{ r.led_pid }}">
          <input type="hidden" name="province_name" value="{{ r.led_pname }}">
          <input type="hidden" name="search_bid_date" value="{{ r.led_bdate }}">
          <button class="text-blue-600 hover:underline text-[11px]">↗ เปิดที่กรมบังคับคดี</button>
        </form>{% endif %}
      </td>
      <td class="p-2 text-xs text-slate-600 max-w-[220px]">{{ r.office or '-' }}</td>
      <td class="p-2">{{ r.province or '-' }}{% if r.province_guessed %}<span class="text-[10px] text-amber-600 ml-1">จากศาล</span>{% endif %}</td>
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
    <tr><td class="p-6 text-slate-500" colspan="6">ครบแล้ว — ไม่มีทรัพย์ที่รอพิกัดแปลง 🎉</td></tr>
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
"login_user.html": """
{% extends "layout.html" %}{% block body %}
<div class="max-w-md mx-auto mt-6">
  <div class="sheet p-6 sm:p-8 text-center">
    <div class="text-3xl">❤️</div>
    <h1 class="text-xl font-semibold mt-2">เข้าสู่ระบบ แปลงดี</h1>
    <p class="text-sm text-slate-500 mt-1.5 leading-relaxed">
      บันทึกทรัพย์โปรด และรับแจ้งเตือนเมื่อราคาลด/ใกล้วันประมูล</p>
    <div class="mt-6 space-y-3">
      {% if line_login %}
      <a href="/auth/line/login?next={{ next_url|urlencode }}"
         class="flex items-center justify-center gap-2 rounded-lg px-4 py-3 text-white font-medium"
         style="background:#06C755">เข้าสู่ระบบด้วย LINE</a>
      {% endif %}
      {% if google_login %}
      <a href="/auth/google/login?next={{ next_url|urlencode }}"
         class="flex items-center justify-center gap-2 rounded-lg px-4 py-3 font-medium border"
         style="border-color:var(--rule);color:var(--ink)">
        <span style="color:#4285F4;font-weight:700">G</span> เข้าสู่ระบบด้วย Google</a>
      {% endif %}
      {% if not line_login and not google_login %}
      <div class="text-sm text-slate-500">ยังไม่ได้เปิดใช้การเข้าสู่ระบบ</div>
      {% endif %}
    </div>
    <p class="text-[11px] text-slate-400 mt-6 leading-relaxed">
      การเข้าสู่ระบบถือว่ายอมรับ
      <a href="/terms" class="brandlink">เงื่อนไขการใช้งาน</a> และ
      <a href="/privacy" class="brandlink">นโยบายความเป็นส่วนตัว</a></p>
  </div>
</div>
{% endblock %}
""",
"sell.html": """
{% extends "layout.html" %}{% block body %}
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<div class="max-w-2xl mx-auto">
  <h1 class="text-xl font-semibold mb-1">{% if edit_id %}✏️ แก้ไขประกาศ{% else %}ลงประกาศขาย / ให้เช่า <span class="text-sm font-normal text-slate-500">ฟรี</span>{% endif %}</h1>
  <p class="text-sm text-slate-500 mb-4">{% if edit_id %}แก้ไขข้อมูลแล้วกดบันทึกด้านล่าง · กลับไปที่ <a href="/my-listings" class="brandlink">ประกาศของฉัน</a>{% else %}ประกาศจะขึ้นเว็บหลังทีมงานตรวจอนุมัติ · ดูสถานะได้ที่ <a href="/my-listings" class="brandlink">ประกาศของฉัน</a>{% endif %}</p>
  <form method="post" action="/sell" class="space-y-4">
    {% if edit_id %}<input type="hidden" name="edit_id" value="{{ edit_id }}">{% endif %}
    <div class="sheet p-4 space-y-3">
      <div class="flex gap-2">
        {% for k,v in [('sale','ขาย'),('rent','ให้เช่า')] %}
        <label class="flex-1 border rounded-lg px-3 py-2 text-center text-sm cursor-pointer">
          <input type="radio" name="listing_kind" value="{{ k }}" {% if (f.listing_kind or 'sale')==k %}checked{% endif %}> {{ v }}</label>
        {% endfor %}
      </div>
      <label class="block text-sm">ชื่อประกาศ *
        <input name="title" required maxlength="200" placeholder="เช่น บ้านเดี่ยว 2 ชั้น หมู่บ้าน... พร้อมอยู่"
          value="{{ f.title or '' }}" class="mt-1 w-full border rounded-lg px-3 py-2"></label>
      <div class="grid grid-cols-2 gap-3">
        <label class="block text-sm">ประเภท
          <select name="property_type" class="mt-1 w-full border rounded-lg px-2 py-2">
            <option value="">-</option>
            {% for k,v in type_labels.items() %}<option value="{{ k }}" {% if k==f.property_type %}selected{% endif %}>{{ v }}</option>{% endfor %}
          </select></label>
        <label class="block text-sm" id="pricelbl">ราคา (บาท)
          <input name="price" type="number" value="{{ f.price|int if f.price else '' }}" class="mt-1 w-full border rounded-lg px-2 py-2"></label>
      </div>
      <div class="grid grid-cols-3 gap-3">
        <label class="block text-sm">มัดจำ (เช่า)
          <input name="deposit" type="number" value="{{ f.deposit|int if f.deposit else '' }}" class="mt-1 w-full border rounded-lg px-2 py-2"></label>
        <label class="block text-sm">เนื้อที่ (ตร.ว.)
          <input name="land_area_sqwa" type="number" step="0.1" value="{{ f.land_area_sqwa or '' }}" class="mt-1 w-full border rounded-lg px-2 py-2"></label>
        <label class="block text-sm">ใช้สอย (ตร.ม.)
          <input name="usable_area_sqm" type="number" step="0.1" value="{{ f.usable_area_sqm or '' }}" class="mt-1 w-full border rounded-lg px-2 py-2"></label>
      </div>
      <div class="grid grid-cols-3 gap-3">
        <label class="block text-sm">นอน<input name="bedrooms" type="number" value="{{ f.bedrooms or '' }}" class="mt-1 w-full border rounded-lg px-2 py-2"></label>
        <label class="block text-sm">น้ำ<input name="bathrooms" type="number" value="{{ f.bathrooms or '' }}" class="mt-1 w-full border rounded-lg px-2 py-2"></label>
        <label class="block text-sm">จอดรถ<input name="parking" type="number" value="{{ f.parking or '' }}" class="mt-1 w-full border rounded-lg px-2 py-2"></label>
      </div>
      <div id="rentonly" class="hidden">
        <label class="block text-sm">🐾 การเลี้ยงสัตว์ <span class="text-xs text-slate-400">(สำหรับปล่อยเช่า)</span>
          <select name="pets_allowed" class="mt-1 w-full border rounded-lg px-2 py-2">
            <option value="">— ไม่ระบุ —</option>
            <option value="yes" {% if f.pets_allowed=='yes' %}selected{% endif %}>อนุญาตให้เลี้ยงสัตว์ได้</option>
            <option value="no" {% if f.pets_allowed=='no' %}selected{% endif %}>ไม่อนุญาตให้เลี้ยงสัตว์</option>
            <option value="ask" {% if f.pets_allowed=='ask' %}selected{% endif %}>สอบถามเจ้าของก่อน</option>
          </select></label>
      </div>
    </div>
    <div class="sheet p-4 space-y-3">
      <div class="grid grid-cols-3 gap-3">
        <label class="block text-sm">จังหวัด
          <select name="province" id="sd-prov" class="mt-1 w-full border rounded-lg px-2 py-2 bg-white"
            data-init-prov="{{ f.province or '' }}" data-init-dist="{{ f.district or '' }}" data-init-sub="{{ f.subdistrict or '' }}">
            <option value="">— เลือกจังหวัด —</option>
          </select></label>
        <label class="block text-sm">อำเภอ/เขต
          <select name="district" id="sd-dist" class="mt-1 w-full border rounded-lg px-2 py-2 bg-white" disabled>
            <option value="">— เลือกอำเภอ —</option>
          </select></label>
        <label class="block text-sm">ตำบล/แขวง
          <select name="subdistrict" id="sd-sub" class="mt-1 w-full border rounded-lg px-2 py-2 bg-white" disabled>
            <option value="">— เลือกตำบล —</option>
          </select></label>
      </div>
      <label class="block text-sm">ที่อยู่/จุดสังเกต<input name="address_raw" maxlength="300" value="{{ f.address_raw or '' }}" class="mt-1 w-full border rounded-lg px-2 py-2"></label>
      <div>
        <div class="text-sm mb-1">ปักหมุดตำแหน่ง <span class="text-xs text-slate-400">— คลิกบนแผนที่เพื่อวางหมุด (ลากปรับได้)</span></div>
        <div class="flex gap-2 mb-1 items-center">
          <button type="button" onclick="useMyLoc()" class="text-xs border rounded px-2 py-1">📍 ใช้ตำแหน่งปัจจุบัน</button>
          <span id="latlngtext" class="text-xs text-slate-500"></span>
        </div>
        <div id="pickmap" class="rounded-lg border" style="height:280px"></div>
        <input type="hidden" name="lat" id="lat" value="{{ f.lat or '' }}"><input type="hidden" name="lng" id="lng" value="{{ f.lng or '' }}">
      </div>
      <label class="block text-sm">รายละเอียด
        <textarea name="description" rows="4" maxlength="4000" class="mt-1 w-full border rounded-lg px-3 py-2" placeholder="สภาพทรัพย์ จุดเด่น เฟอร์นิเจอร์ เงื่อนไข ฯลฯ">{{ f.description or '' }}</textarea></label>
    </div>
    <div class="sheet p-4 space-y-2">
      <div class="text-sm font-medium">รูปภาพ (สูงสุด 12 รูป)</div>
      {% if storage_enabled %}
      <input type="file" id="imgfile" accept="image/jpeg,image/png,image/webp" multiple class="text-sm">
      <div id="thumbs" class="flex flex-wrap gap-2 mt-1"></div>
      {% else %}
      <div class="text-xs text-amber-700 bg-amber-50 rounded p-2">ยังไม่ได้เปิดระบบอัปโหลดรูป (แอดมินตั้งค่า Supabase Storage) — ลงประกาศแบบไม่มีรูปก่อนได้</div>
      {% endif %}
      <input type="hidden" name="image_urls" id="image_urls" value="{{ f.images|join(',') if f.images else '' }}">
    </div>
    <div class="sheet p-4 space-y-3">
      <div class="text-sm font-medium">ข้อมูลติดต่อ</div>
      <div class="grid grid-cols-3 gap-3">
        <label class="block text-sm">ชื่อผู้ติดต่อ<input name="contact_name" maxlength="100" value="{{ f.contact_name or '' }}" class="mt-1 w-full border rounded-lg px-2 py-2"></label>
        <label class="block text-sm">เบอร์โทร <span class="text-red-500">*</span><input name="contact_phone" required maxlength="40" inputmode="tel" placeholder="08x-xxx-xxxx" value="{{ f.contact_phone or '' }}" class="mt-1 w-full border rounded-lg px-2 py-2"></label>
        <label class="block text-sm">LINE ID<input name="contact_line" maxlength="100" value="{{ f.contact_line or '' }}" class="mt-1 w-full border rounded-lg px-2 py-2"></label>
      </div>
      <p class="text-[11px] text-slate-400">ต้องกรอกเบอร์โทรเพื่อยืนยันตัวตนผู้ลงประกาศ · ประกาศจะแสดงหลังทีมงานตรวจอนุมัติ</p>
    </div>
    <button class="w-full rounded-lg px-4 py-3 text-white font-medium" style="background:var(--survey)">{% if edit_id %}💾 บันทึกการแก้ไข{% else %}ส่งประกาศเพื่อรออนุมัติ{% endif %}</button>
    <p class="text-[11px] text-slate-400 text-center">การลงประกาศถือว่ายอมรับ <a href="/terms" class="brandlink">เงื่อนไข</a> — ห้ามประกาศเท็จ/หลอกลวง</p>
  </form>
</div>
<script>
(function(){
  var box=document.getElementById('thumbs'), hidden=document.getElementById('image_urls');
  if(!hidden) return;
  var urls = hidden.value ? hidden.value.split(',').filter(Boolean) : [];
  function sync(){ hidden.value=urls.join(','); }
  function render(){
    if(!box) return;
    box.innerHTML='';
    urls.forEach(function(u,idx){
      var w=document.createElement('div'); w.style.position='relative';
      var im=document.createElement('img'); im.src=u;
      im.style.cssText='width:84px;height:84px;object-fit:cover;border-radius:8px;display:block';
      var b=document.createElement('button'); b.type='button'; b.textContent='×'; b.title='ลบรูป';
      b.style.cssText='position:absolute;top:-6px;right:-6px;background:#ef4444;color:#fff;border:none;border-radius:999px;width:20px;height:20px;line-height:18px;font-size:13px;cursor:pointer';
      b.addEventListener('click',function(){ urls.splice(idx,1); sync(); render(); });
      w.appendChild(im); w.appendChild(b); box.appendChild(w);
    });
  }
  render();
  var inp=document.getElementById('imgfile');
  if(inp){
    inp.addEventListener('change',function(){
      Array.prototype.forEach.call(inp.files,function(file){
        if(urls.length>=12){alert('ได้สูงสุด 12 รูป');return;}
        if(file.size>6*1024*1024){alert('รูปใหญ่เกิน 6MB: '+file.name);return;}
        var ph=document.createElement('div'); ph.className='text-[11px] text-slate-400'; ph.textContent='กำลังอัปโหลด…'; if(box) box.appendChild(ph);
        fetch('/api/upload-image',{method:'POST',headers:{'Content-Type':file.type},body:file})
        .then(function(r){if(r.status===401){location.href='/login?next=/sell';return null;}return r.json();})
        .then(function(d){ if(box&&ph.parentNode) box.removeChild(ph); if(!d)return;
          if(d.ok){urls.push(d.url); sync(); render();}
          else { alert('อัปโหลดไม่สำเร็จ'+(d.message?': '+d.message:'')); }
        }).catch(function(){ if(box&&ph.parentNode) box.removeChild(ph); alert('อัปโหลดไม่สำเร็จ');});
      });
      inp.value='';
    });
  }
})();
</script>
<script>
(function(){
  var el=document.getElementById('pickmap'); if(!el||typeof L==='undefined') return;
  var map=L.map('pickmap').setView([13.75,100.52],6);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; OpenStreetMap'}).addTo(map);
  var mk=null;
  function save(lat,lng){
    document.getElementById('lat').value=lat.toFixed(6);
    document.getElementById('lng').value=lng.toFixed(6);
    document.getElementById('latlngtext').textContent='พิกัด: '+lat.toFixed(5)+', '+lng.toFixed(5);
  }
  function setPin(lat,lng){
    if(mk){mk.setLatLng([lat,lng]);}
    else{mk=L.marker([lat,lng],{draggable:true}).addTo(map);
      mk.on('dragend',function(){var p=mk.getLatLng();save(p.lat,p.lng);});}
    save(lat,lng);
  }
  map.on('click',function(e){setPin(e.latlng.lat,e.latlng.lng);});
  var ila=parseFloat(document.getElementById('lat').value), iln=parseFloat(document.getElementById('lng').value);
  if(!isNaN(ila)&&!isNaN(iln)){ map.setView([ila,iln],16); setPin(ila,iln); }
  window.useMyLoc=function(){
    if(!navigator.geolocation){alert('เบราว์เซอร์ไม่รองรับระบุตำแหน่ง');return;}
    navigator.geolocation.getCurrentPosition(function(pos){
      map.setView([pos.coords.latitude,pos.coords.longitude],16);
      setPin(pos.coords.latitude,pos.coords.longitude);
    },function(){alert('ขอตำแหน่งไม่สำเร็จ ลองคลิกบนแผนที่แทน');});
  };
  setTimeout(function(){map.invalidateSize();},250);
})();
</script>
<script>
// โชว์ช่อง "เลี้ยงสัตว์" เฉพาะเมื่อเลือกให้เช่า + ปรับ label ราคา
(function(){
  var rentonly=document.getElementById('rentonly'), pl=document.getElementById('pricelbl');
  function sync(){
    var k=document.querySelector('input[name="listing_kind"]:checked');
    var rent = k && k.value==='rent';
    if(rentonly) rentonly.classList.toggle('hidden', !rent);
    if(pl) pl.firstChild.nodeValue = rent ? 'ค่าเช่า/เดือน (บาท)' : 'ราคา (บาท)';
  }
  Array.prototype.forEach.call(document.querySelectorAll('input[name="listing_kind"]'),
    function(r){ r.addEventListener('change', sync); });
  sync();
})();
</script>
<script>
// จังหวัด → อำเภอ → ตำบล : dropdown เชื่อมโยงกัน (โหลดข้อมูลทางการครั้งเดียว)
(function(){
  var prov=document.getElementById('sd-prov'),
      dist=document.getElementById('sd-dist'),
      sub =document.getElementById('sd-sub');
  if(!prov) return;
  var IP=prov.getAttribute('data-init-prov')||'', ID=prov.getAttribute('data-init-dist')||'', IS=prov.getAttribute('data-init-sub')||'';
  function fill(sel, list, ph){
    sel.innerHTML='';
    var o0=document.createElement('option'); o0.value=''; o0.textContent=ph; sel.appendChild(o0);
    list.forEach(function(v){var o=document.createElement('option'); o.value=v; o.textContent=v; sel.appendChild(o);});
  }
  function toText(sel, val){                   // สำรอง: ถ้าโหลดข้อมูลไม่ได้ ให้พิมพ์เอง
    var t=document.createElement('input'); t.type='text'; t.name=sel.getAttribute('name');
    t.maxLength=60; t.className=sel.className; if(val) t.value=val;
    sel.parentNode.replaceChild(t, sel);
  }
  fetch('/api/th-geo.json').then(function(r){return r.json();}).then(function(GEO){
    fill(prov, Object.keys(GEO).sort(), '— เลือกจังหวัด —');
    prov.addEventListener('change', function(){
      var ds = prov.value ? Object.keys(GEO[prov.value]||{}) : [];
      fill(dist, ds, '— เลือกอำเภอ —'); dist.disabled = ds.length===0;
      fill(sub, [], '— เลือกตำบล —'); sub.disabled = true;
    });
    dist.addEventListener('change', function(){
      var ss = (prov.value && dist.value) ? (GEO[prov.value][dist.value]||[]) : [];
      fill(sub, ss, '— เลือกตำบล —'); sub.disabled = ss.length===0;
    });
    // เติมค่าเดิมตอนแก้ไขประกาศ
    if(IP && GEO[IP]){
      prov.value=IP;
      var ds=Object.keys(GEO[IP]); fill(dist, ds, '— เลือกอำเภอ —'); dist.disabled=ds.length===0;
      if(ID && GEO[IP][ID]){
        dist.value=ID;
        var ss=GEO[IP][ID]||[]; fill(sub, ss, '— เลือกตำบล —'); sub.disabled=ss.length===0;
        if(IS && ss.indexOf(IS)>=0) sub.value=IS;
      }
    }
  }).catch(function(){ toText(prov, IP); toText(dist, ID); toText(sub, IS); });
})();
</script>
{% endblock %}
""",
"market.html": """
{% extends "layout.html" %}{% block body %}
<section class="mb-4 rounded-2xl overflow-hidden relative" style="background:linear-gradient(135deg,var(--ink),var(--survey-deep))">
  <div class="relative px-5 py-6 text-white">
    <div class="text-[11px] tracking-widest uppercase font-semibold" style="color:rgba(255,255,255,.7)">ประกาศจากผู้ลงเอง (ไม่ใช่ทรัพย์ NPA)</div>
    <h1 class="display text-2xl font-bold mt-1">ตลาดซื้อ-ขาย-เช่า อสังหาฯ</h1>
    <p class="text-white/80 text-sm mt-1">ประกาศขาย/ให้เช่าจากเจ้าของและนายหน้าโดยตรง · <a href="/sell" class="underline">ลงประกาศฟรี →</a></p>
  </div>
</section>
<form class="sheet p-3 mb-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-6 items-end text-sm">
  <label class="block">ประเภทประกาศ
    <select name="kind" class="mt-1 w-full border rounded-lg px-2 py-1.5 bg-white">
      <option value="">ทั้งหมด</option>
      <option value="sale" {% if kind=='sale' %}selected{% endif %}>ขาย</option>
      <option value="rent" {% if kind=='rent' %}selected{% endif %}>ให้เช่า</option>
    </select></label>
  <label class="block">ประเภททรัพย์
    <select name="ptype" class="mt-1 w-full border rounded-lg px-2 py-1.5 bg-white">
      <option value="">ทั้งหมด</option>
      {% for k,v in type_labels.items() %}<option value="{{ k }}" {% if k==ptype %}selected{% endif %}>{{ v }}</option>{% endfor %}
    </select></label>
  <label class="block">จังหวัด
    <select name="province" id="mk-prov" data-cur="{{ province }}" class="mt-1 w-full border rounded-lg px-2 py-1.5 bg-white">
      <option value="">ทั้งหมด</option>
    </select></label>
  <label class="block">อำเภอ/เขต
    <select name="district" id="mk-dist" data-cur="{{ district }}" class="mt-1 w-full border rounded-lg px-2 py-1.5 bg-white">
      <option value="">ทั้งหมด</option>
    </select></label>
  <label class="block">ราคาต่ำสุด
    <input name="min_price" type="number" value="{{ min_price|int if min_price else '' }}" placeholder="0" class="mt-1 w-full border rounded-lg px-2 py-1.5"></label>
  <label class="block">ราคาสูงสุด
    <input name="max_price" type="number" value="{{ max_price|int if max_price else '' }}" placeholder="ไม่จำกัด" class="mt-1 w-full border rounded-lg px-2 py-1.5"></label>
  <div class="sm:col-span-2 lg:col-span-6 flex gap-2">
    <button class="bg-slate-900 text-white rounded-lg px-5 py-1.5">กรอง</button>
    <a href="/market" class="border rounded-lg px-4 py-1.5">ล้างตัวกรอง</a>
  </div>
</form>
<div class="flex items-center justify-between mb-3">
  <h2 class="font-semibold">พบ <span class="num">{{ "{:,}".format(count) }}</span> ประกาศ</h2>
  <a href="/sell" class="text-sm text-white rounded-lg px-3 py-1.5" style="background:var(--survey)">+ ลงประกาศ</a>
</div>
{% if not rows %}
<div class="sheet p-10 text-center text-slate-500">ยังไม่มีประกาศตรงเงื่อนไข — เป็นคนแรกที่ <a href="/sell" class="brandlink">ลงประกาศ</a> เลยไหม?</div>
{% else %}
<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
{% for r in rows %}
<a href="/m/{{ r.id }}" class="sheet overflow-hidden block">
  <div class="relative imgwrap" style="background:#EEF1F3">
    {% if r.image %}<img src="{{ r.image }}" alt="{{ r.title }}" loading="lazy" class="w-full h-44 object-cover">
    {% else %}<div class="h-44"></div>{% endif %}
    <span class="absolute top-2 left-2 text-[11px] px-2 py-1 rounded text-white font-medium"
      style="background:{{ '#1C86C9' if r.listing_kind=='rent' else 'var(--seal)' }}">{{ 'ให้เช่า' if r.listing_kind=='rent' else 'ขาย' }}</span>
  </div>
  <div class="p-3">
    <div class="text-lg font-semibold">{{ "{:,.0f}".format(r.price or 0) }}
      <span class="text-xs font-normal text-slate-500">บาท{{ '/เดือน' if r.listing_kind=='rent' else '' }}</span></div>
    <div class="mt-0.5 text-sm font-medium line-clamp-1">{{ r.title }}</div>
    <div class="mt-1 text-xs text-slate-500 line-clamp-1">{{ r.type_label }}{% if r.district %} · {{ r.district }}{% endif %}{% if r.province %} {{ r.province }}{% endif %}</div>
  </div>
</a>
{% endfor %}
</div>
{% endif %}
<script>
// ตัวกรอง จังหวัด → อำเภอ (ข้อมูลทางการ) พร้อมคงค่าที่เลือกไว้
(function(){
  var prov=document.getElementById('mk-prov'), dist=document.getElementById('mk-dist');
  if(!prov||!dist) return;
  var curP=prov.getAttribute('data-cur')||'', curD=dist.getAttribute('data-cur')||'';
  function fill(sel,list,cur){ sel.innerHTML='<option value="">ทั้งหมด</option>';
    list.forEach(function(v){var o=document.createElement('option');o.value=v;o.textContent=v;if(v===cur)o.selected=true;sel.appendChild(o);}); }
  fetch('/api/th-geo.json').then(function(r){return r.json();}).then(function(GEO){
    fill(prov, Object.keys(GEO).sort(), curP);
    fill(dist, (curP&&GEO[curP])?Object.keys(GEO[curP]):[], curD);
    prov.addEventListener('change', function(){
      fill(dist, (prov.value&&GEO[prov.value])?Object.keys(GEO[prov.value]):[], ''); });
  }).catch(function(){});
})();
</script>
{% endblock %}
""",
"member_detail.html": """
{% extends "layout.html" %}{% block body %}
{% if d.lat and d.lng %}
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
{% endif %}
<div class="max-w-6xl mx-auto">
  <div class="flex items-center justify-between">
    <a href="/market" class="text-sm brandlink">← กลับตลาดประกาศ</a>
    {% if is_owner %}<a href="/sell?edit={{ d.id }}" class="text-sm rounded-lg px-3 py-1.5 border font-medium text-slate-600 hover:bg-slate-50">✏️ แก้ไขประกาศ</a>{% endif %}
  </div>
  <div class="mt-2 {% if d.lat and d.lng %}grid gap-5 lg:grid-cols-3 lg:items-start{% endif %}">
  <div class="{% if d.lat and d.lng %}lg:col-span-2{% endif %}">
  <div class="sheet overflow-hidden mt-2">
    {% if d.images %}
    <div class="flex gap-1 overflow-x-auto">
      {% for u in d.images %}<img src="{{ u }}" alt="{{ d.title }}" class="h-64 w-auto object-cover flex-shrink-0">{% endfor %}
    </div>
    {% endif %}
    <div class="p-5">
      <span class="text-[11px] px-2 py-1 rounded text-white font-medium"
        style="background:{{ '#1C86C9' if d.listing_kind=='rent' else 'var(--seal)' }}">{{ 'ให้เช่า' if d.listing_kind=='rent' else 'ขาย' }}</span>
      {% if d.status != 'approved' %}<span class="text-[11px] px-2 py-1 rounded bg-amber-100 text-amber-800 ml-1">สถานะ: {{ d.status }}</span>{% endif %}
      <h1 class="text-xl font-semibold mt-2">{{ d.title }}</h1>
      <div class="text-2xl font-bold mt-1" style="color:var(--survey-deep)">{{ "{:,.0f}".format(d.price or 0) }}
        <span class="text-sm font-normal text-slate-500">บาท{{ '/เดือน' if d.listing_kind=='rent' else '' }}</span></div>
      {% if d.deposit %}<div class="text-sm text-slate-500">มัดจำ {{ "{:,.0f}".format(d.deposit) }} บาท</div>{% endif %}
      <div class="mt-1 text-xs text-slate-400">👁 ดู {{ "{:,}".format(views_30d or 0) }} ครั้งใน 30 วัน</div>
      <div class="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-sm text-slate-600">
        <span>{{ d.type_label }}</span>
        {% if d.land_area_sqwa %}<span>{{ d.land_area_sqwa }} ตร.ว.</span>{% endif %}
        {% if d.usable_area_sqm %}<span>{{ d.usable_area_sqm }} ตร.ม.</span>{% endif %}
        {% if d.bedrooms %}<span>{{ d.bedrooms }} นอน</span>{% endif %}
        {% if d.bathrooms %}<span>{{ d.bathrooms }} น้ำ</span>{% endif %}
        {% if d.parking %}<span>จอด {{ d.parking }}</span>{% endif %}
        {% if d.listing_kind=='rent' and d.pets_allowed %}<span class="px-2 rounded" style="background:#EEF6FF;color:#1C86C9">🐾 {{ {'yes':'เลี้ยงสัตว์ได้','no':'ห้ามเลี้ยงสัตว์','ask':'สอบถามเรื่องสัตว์เลี้ยง'}[d.pets_allowed] }}</span>{% endif %}
      </div>
      {% if d.province or d.district %}<div class="mt-2 text-sm text-slate-600">📍 {{ d.subdistrict }} {{ d.district }} {{ d.province }}{% if d.address_raw %} · {{ d.address_raw }}{% endif %}</div>{% endif %}
      {% if d.description %}<div class="mt-4 text-[15px] leading-relaxed text-slate-700 whitespace-pre-line">{{ d.description }}</div>{% endif %}
    </div>
  </div>
  {% if d.renovations %}
  <div class="sheet p-4 mt-4">
    <h2 class="font-semibold text-sm">ภาพจำลองรีโนเวท (AI)
      <span class="text-[11px] font-normal text-amber-600">— ภาพจำลองเพื่อดูไอเดีย ไม่ใช่สภาพจริง</span></h2>
    <div class="mt-3 space-y-4">
      {% for r in d.renovations %}
      <div>
        <div class="ba" style="position:relative;user-select:none;line-height:0;border-radius:8px;overflow:hidden">
          <img class="ba-before" src="{{ r.source_url }}" style="width:100%;display:block">
          <div class="ba-aw" style="position:absolute;top:0;left:0;height:100%;width:50%;overflow:hidden">
            <img class="ba-after" src="{{ r.result_url }}" style="height:100%;display:block;max-width:none">
          </div>
          <div class="ba-line" style="position:absolute;top:0;bottom:0;left:50%;width:2px;background:#fff;box-shadow:0 0 4px rgba(0,0,0,.5)"></div>
          <span style="position:absolute;top:6px;left:6px;background:rgba(0,0,0,.6);color:#fff;font-size:10px;padding:1px 6px;border-radius:4px">ก่อน</span>
          <span style="position:absolute;top:6px;right:6px;background:rgba(226,70,55,.9);color:#fff;font-size:10px;padding:1px 6px;border-radius:4px">หลัง · จำลอง AI</span>
          <input type="range" min="0" max="100" value="50" oninput="baMove(this)"
            style="position:absolute;bottom:8px;left:5%;width:90%">
        </div>
        <div class="text-[11px] text-slate-400 mt-1">เลื่อนแถบเทียบก่อน/หลัง{% if r.style and reno_styles.get(r.style) %} · สไตล์ {{ reno_styles.get(r.style)[0] }}{% endif %}</div>
      </div>
      {% endfor %}
    </div>
  </div>
  {% endif %}
  {% if can_renovate %}
  <div class="sheet p-4 mt-4">
    <h2 class="font-semibold text-sm">🎨 รีโนเวทด้วย AI <span class="text-[11px] font-normal text-slate-400">(ภาพจำลอง — เจ้าของ/แอดมิน)</span></h2>
    {% if reno_remaining is not none %}<div class="text-xs text-slate-500 mt-0.5">สิทธิ์ฟรีเหลือ {{ reno_remaining }}/{{ reno_limit }} รูป</div>{% endif %}
    {% if d.images %}
    <div class="mt-2 flex flex-wrap gap-2 items-end">
      <label class="text-sm">รูป
        <select id="renoImg" class="mt-1 border rounded-lg px-2 py-1.5">
          {% for u in d.images %}<option value="{{ u }}">รูปที่ {{ loop.index }}</option>{% endfor %}
        </select></label>
      <label class="text-sm">สไตล์
        <select id="renoStyle" class="mt-1 border rounded-lg px-2 py-1.5">
          {% for k,v in reno_styles.items() %}<option value="{{ k }}">{{ v[0] }}</option>{% endfor %}
        </select></label>
      <button type="button" id="renoBtn" onclick="doRenovate('{{ d.id }}')" class="rounded-lg px-4 py-2 text-white text-sm" style="background:var(--survey)">สร้างภาพ (~30 วิ)</button>
    </div>
    <div id="renoResult" class="mt-3"></div>
    {% else %}
    <div class="text-xs text-amber-700 mt-2">ยังไม่มีรูปในประกาศ — อัปโหลดรูปก่อนถึงจะรีโนเวทได้</div>
    {% endif %}
  </div>
  {% endif %}

  <div class="sheet p-4 mt-4">
    <h2 class="font-semibold text-sm mb-2">ติดต่อผู้ลงประกาศ</h2>
    {% if user_logged_in %}
    <div class="flex flex-wrap gap-2 text-sm">
      {% if d.contact_phone %}<a href="tel:{{ d.contact_phone }}" class="rounded-lg px-4 py-2 text-white" style="background:var(--survey)">โทร {{ d.contact_phone }}</a>{% endif %}
      {% if d.contact_line %}<span class="rounded-lg px-4 py-2 border">LINE: {{ d.contact_line }}</span>{% endif %}
      {% if d.contact_name %}<span class="px-2 py-2 text-slate-500">({{ d.contact_name }})</span>{% endif %}
      {% if not d.contact_phone and not d.contact_line %}<span class="text-slate-400 text-sm py-2">ผู้ลงไม่ได้ให้ข้อมูลติดต่อ</span>{% endif %}
    </div>
    {% else %}
    <div class="rounded-lg border border-dashed p-4 text-center">
      <div class="text-sm text-slate-500 mb-2">🔒 เข้าสู่ระบบเพื่อดูเบอร์โทร / LINE ของผู้ลงประกาศ</div>
      <a href="/login?next=/m/{{ d.id }}" class="inline-block rounded-lg px-5 py-2 text-white text-sm font-medium" style="background:var(--survey)">เข้าสู่ระบบเพื่อดูข้อมูลติดต่อ</a>
    </div>
    {% endif %}
    <p class="text-[11px] text-amber-700 bg-amber-50 rounded p-2 mt-3">⚠️ ประกาศจากผู้ใช้ทั่วไป — แปลงดีไม่ได้ตรวจสอบกรรมสิทธิ์ ควรตรวจเอกสาร/ดูของจริงก่อนโอนเงินทุกครั้ง</p>
  </div>
  </div>{# /left col #}

  {% if d.lat and d.lng %}
  <aside class="space-y-4 mt-4 lg:mt-0">
  <div class="sheet overflow-hidden"><div id="mmap" style="height:260px"></div></div>
  <script>
  (function(){var el=document.getElementById('mmap');if(!el||typeof L==='undefined')return;
    var lat={{ d.lat }}, lng={{ d.lng }};
    var m=L.map('mmap').setView([lat,lng],15);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; OpenStreetMap'}).addTo(m);
    L.marker([lat,lng]).addTo(m);
    setTimeout(function(){m.invalidateSize();},250);
  })();
  </script>

  <div class="sheet p-4 mt-4" id="nearby" data-lat="{{ d.lat }}" data-lng="{{ d.lng }}">
    <style>
      #nearby .nb-ring{border-top:1px solid var(--rule);padding-top:11px;margin-top:11px;opacity:0;transform:translateY(7px);animation:nbin .45s ease forwards}
      #nearby .nb-ring:first-of-type{border-top:0;padding-top:0;margin-top:10px}
      #nearby .nb-rlabel{display:inline-block;font-weight:600;font-size:11px;letter-spacing:.03em;color:#fff;background:var(--ink);border-radius:999px;padding:2px 11px}
      #nearby .nb-cats{display:flex;flex-wrap:wrap;gap:12px;margin-top:8px;font-size:14px}
      #nearby .nb-cat{display:inline-flex;align-items:center;gap:4px;color:#334155}
      #nearby .nb-cat b{font-variant-numeric:tabular-nums;font-weight:600}
      #nearby .nb-cat.zero{opacity:.5}
      #nearby .nb-nm{color:#1f2937;font-weight:500}
      #nearby .nb-dm{color:#6b7280;font-variant-numeric:tabular-nums}
      #nearby .nb-ic{margin-right:2px}
      #nearby .nb-names span{white-space:nowrap}
      #nearby .nb-names{margin-top:6px;font-size:12px;color:#64748b;line-height:1.65}
      @keyframes nbin{to{opacity:1;transform:none}}
    </style>
    <h2 class="font-semibold text-sm">📍 รอบทรัพย์นี้ <span class="text-xs font-normal text-slate-400">มีอะไรใกล้ๆ</span></h2>
    <div id="nb-body" class="mt-2 text-sm text-slate-500">
      <span class="inline-flex items-center gap-2"><span class="animate-pulse">⏳</span> กำลังวิเคราะห์รอบทรัพย์…</span>
    </div>
    <p class="text-[11px] text-slate-400 mt-2">สถานที่จาก OpenStreetMap</p>
  </div>
  <script>
  (function(){
  function nbInit(){
    var el=document.getElementById('nearby'); if(!el) return;
    var lat=el.getAttribute('data-lat'), lng=el.getAttribute('data-lng');
    var body=document.getElementById('nb-body');
    var CAT=[['rail','🚊'],['transport','🚌'],['edu','🎓'],['health','🏥'],['life','🛒']];
    var DATA=null;
    function fd(m){ return m<1000 ? m+' ม.' : (m/1000).toFixed(1)+' กม.'; }
    function render(){
      if(!DATA) return;
      var rings=[[500,'500 ม.'],[1000,'1 กม.'],[3000,'3 กม.']];
      var h='';
      rings.forEach(function(rg,idx){
        var R=rg[0], prevR=idx===0?0:rings[idx-1][0];
        var cats=CAT.map(function(c){
          var d=DATA[c[0]]||{c500:0,c1000:0,c3000:0};
          var cnt = R===500?d.c500 : R===1000?d.c1000 : d.c3000;
          return '<span class="nb-cat'+(cnt?'':' zero')+'">'+c[1]+'<b>'+cnt+'</b></span>';
        }).join('');
        var top=[];
        CAT.forEach(function(c){
          var arr=((DATA[c[0]]||{}).near||[]).filter(function(x){ return x.dist>prevR && x.dist<=R; });
          if(arr.length){ arr.sort(function(a,b){ return a.dist-b.dist; });
            top.push({ic:c[1], nm:arr[0].name||'', d:arr[0].dist}); }
        });
        top.sort(function(a,b){ return a.d-b.d; });
        top=top.map(function(t){ return '<span class="nb-ic">'+t.ic+'</span> <span class="nb-nm">'+t.nm+'</span> <span class="nb-dm">'+fd(t.d)+'</span>'; });
        h+='<div class="nb-ring" style="animation-delay:'+(idx*0.14).toFixed(2)+'s">'
          +'<div class="nb-rlabel">'+rg[1]+'</div>'
          +'<div class="nb-cats">'+cats+'</div>'
          +(top.length?'<div class="nb-names">'+top.join(' · ')+'</div>':'')
          +'</div>';
      });
      body.innerHTML=h;
    }
    fetch('/api/nearby?lat='+encodeURIComponent(lat)+'&lng='+encodeURIComponent(lng))
      .then(function(r){return r.json();}).then(function(res){
        if(res&&res.ok){ DATA=res.data; render(); }
        else { body.innerHTML='<span class="text-amber-600 text-sm">'+((res&&res.message)||'ดึงข้อมูลรอบทรัพย์ไม่สำเร็จ')+'</span>'; }
      }).catch(function(){ body.innerHTML='<span class="text-amber-600 text-sm">ดึงข้อมูลรอบทรัพย์ไม่สำเร็จ ลองรีเฟรช</span>'; });
  }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded', nbInit); else nbInit();
  })();
  </script>
  </aside>
  {% endif %}
  </div>{# /grid #}
</div>
<script>
function baMove(r){var el=r.closest('.ba');el.querySelector('.ba-aw').style.width=r.value+'%';el.querySelector('.ba-line').style.left=r.value+'%';}
(function(){document.querySelectorAll('.ba').forEach(function(el){
  var af=el.querySelector('.ba-after');
  function fit(){af.style.width=el.clientWidth+'px';}
  if(af.complete)fit();else af.addEventListener('load',fit);
  window.addEventListener('resize',fit);
});})();
window.doRenovate=function(lid){
  var btn=document.getElementById('renoBtn');
  var sel=document.getElementById('renoImg'); if(!sel||!sel.value){alert('ยังไม่มีรูป');return;}
  var img=sel.value, style=document.getElementById('renoStyle').value, old=btn.textContent;
  btn.disabled=true; btn.textContent='กำลังสร้าง…';
  var box=document.getElementById('renoResult');
  box.innerHTML='<div class="text-xs text-slate-400">AI กำลังสร้างภาพ อาจใช้เวลา ~10-40 วินาที…</div>';
  fetch('/api/renovate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({listing_id:lid,image_url:img,style:style})})
  .then(function(r){return r.json();}).then(function(d){
    btn.disabled=false; btn.textContent=old;
    if(d.ok){box.innerHTML='<div class="text-sm text-emerald-700 mb-2">✓ สร้างเสร็จ ('+d.style+')'+(d.remaining!=null?(' · สิทธิ์เหลือ '+d.remaining):'')+'</div><img src="'+d.result+'" style="width:100%;border-radius:8px"><div class="text-[11px] text-slate-400 mt-1">ภาพจำลอง AI — รีเฟรชหน้าเพื่อดูแบบ before/after</div>';}
    else{box.innerHTML='<div class="text-sm text-red-600">'+(d.message||'ไม่สำเร็จ')+'</div>'; if(d.need_login&&d.login_url)location.href=d.login_url;}
  }).catch(function(){btn.disabled=false;btn.textContent=old;box.innerHTML='<div class="text-sm text-red-600">เกิดข้อผิดพลาด ลองใหม่</div>';});
};
</script>
{% endblock %}
""",
"admin_sources.html": """
{% extends "layout.html" %}{% block body %}
<div class="flex items-center justify-between mb-3">
  <h1 class="text-xl font-semibold">แหล่งข้อมูล (Sources)</h1>
  <a href="/admin?token={{ token }}" class="text-sm brandlink">← แอดมิน</a>
</div>
{% if msg %}<div class="sheet p-3 mb-3 text-sm bg-emerald-50 text-emerald-800">{{ msg }}</div>{% endif %}
<div class="sheet overflow-x-auto">
<table class="w-full text-sm">
  <thead><tr class="text-xs text-slate-400 text-left border-b" style="border-color:var(--rule)">
    <th class="p-3 font-normal">แหล่ง</th><th class="font-normal">ทรัพย์</th>
    <th class="font-normal">สถานะสิทธิ์</th><th class="font-normal">สถานะ</th>
    <th class="font-normal text-right p-3">เปิด/ปิด</th></tr></thead>
  <tbody>
  {% for r in rows %}
  <tr class="border-b hover:bg-slate-50" style="border-color:var(--rule)">
    <td class="p-3"><div class="font-medium">{{ r.code }}</div>
      <div class="text-xs text-slate-500 line-clamp-1">{{ r.name }}</div></td>
    <td class="tabular-nums text-slate-500">{{ "{:,}".format(r.n or 0) }}</td>
    <td>
      {% if r.institution_code %}
      <form method="post" action="/admin/sources/legal" style="display:inline">
        <input type="hidden" name="token" value="{{ token }}">
        <input type="hidden" name="code" value="{{ r.code }}">
        <select name="status" onchange="this.form.submit()"
          class="text-[11px] border rounded px-1.5 py-1 bg-white
          {% if r.legal_status in ('permitted','checked') %}text-emerald-700 border-emerald-300
          {% elif r.legal_status in ('restricted','prohibited') %}text-red-700 border-red-300
          {% else %}text-amber-700 border-amber-300{% endif %}">
          {% for s in legal_statuses %}<option value="{{ s }}" {% if r.legal_status==s %}selected{% endif %}>{{ s }}</option>{% endfor %}
        </select>
      </form>
      {% else %}<span class="text-slate-300 text-xs">- (ไม่มีสถาบัน)</span>{% endif %}
    </td>
    <td>{% if r.is_active %}<span class="text-[11px] px-2 py-0.5 rounded bg-emerald-100 text-emerald-800">🟢 เปิด</span>
      {% else %}<span class="text-[11px] px-2 py-0.5 rounded bg-slate-200 text-slate-600">⚪ ปิด</span>{% endif %}</td>
    <td class="text-right p-3">
      <form method="post" action="/admin/sources/toggle" style="display:inline">
        <input type="hidden" name="token" value="{{ token }}">
        <input type="hidden" name="code" value="{{ r.code }}">
        <input type="hidden" name="to" value="{{ 'off' if r.is_active else 'on' }}">
        {% if r.is_active %}
        <button class="text-xs rounded-lg px-3 py-1.5 border text-slate-600 hover:bg-slate-50">ปิด</button>
        {% else %}
        <button {% if r.legal_status in ('restricted','unknown','prohibited') %}onclick="return confirm('แหล่งนี้สถานะสิทธิ์ = {{ r.legal_status }} — การเปิดถือว่าคุณตรวจ ToS/ยอมรับความเสี่ยงลิขสิทธิ์เองแล้ว ยืนยันเปิด?')"{% endif %}
          class="text-xs rounded-lg px-3 py-1.5 text-white" style="background:var(--survey)">เปิด</button>
        {% endif %}
      </form>
    </td>
  </tr>
  {% endfor %}
  </tbody></table>
</div>
<p class="text-[11px] text-slate-400 mt-3">⚠️ เปิดแหล่งที่สถานะ restricted/unknown = ยอมรับความเสี่ยง ToS/ลิขสิทธิ์เอง (เช่น ออมสิน footer ห้ามทำซ้ำ) · แนะนำขออนุญาต/เป็นพันธมิตรก่อนใช้เชิงพาณิชย์ · การเปิดจะตั้งสถานะสิทธิ์เป็น checked ให้ผ่าน guard</p>
{% endblock %}
""",
"auction_stats.html": """
{% extends "layout.html" %}{% block body %}
{% macro statrows(items, keyname, prefix) %}
<div class="overflow-x-auto"><table class="w-full text-sm">
  <thead><tr class="text-xs text-slate-400 text-left">
    <th class="font-normal py-1">รายการ</th><th class="font-normal">ทรัพย์</th>
    <th class="font-normal w-2/5">% ขายได้</th><th class="font-normal text-right">ส่วนลด (กลาง)</th></tr></thead>
  <tbody>
  {% for r in items %}
  <tr class="border-t hover:bg-slate-50" style="border-color:var(--rule)">
    <td class="py-2 font-medium">{{ prefix }}{{ r[keyname] }}</td>
    <td class="text-slate-500 tabular-nums">{{ r.n }}</td>
    <td class="py-2"><div class="flex items-center gap-2">
      <div class="flex-1 bg-slate-100 rounded-full h-2 overflow-hidden max-w-[150px]">
        <div class="h-full rounded-full" style="width:{{ r.pct }}%;background:var(--survey)"></div></div>
      <span class="text-xs tabular-nums text-slate-500">{{ r.pct }}%</span></div></td>
    <td class="text-right tabular-nums font-medium
      {% if r.med is none %}text-slate-300{% elif r.med<0 %}text-emerald-600{% elif r.med>0 %}text-rose-600{% else %}text-slate-500{% endif %}">
      {% if r.med is not none %}{{ '+' if r.med>0 else '' }}{{ r.med }}%{% else %}—{% endif %}</td>
  </tr>
  {% endfor %}
  </tbody></table></div>
{% endmacro %}

<div class="flex items-center justify-between flex-wrap gap-2 mb-1">
  <h1 class="text-xl font-semibold">📊 แดชบอร์ดกลยุทธ์ประมูล</h1>
  <a href="/auction-results" class="text-sm brandlink">← กลับหน้าจบประมูล</a>
</div>
<p class="text-sm text-slate-500 mb-3">วิเคราะห์ผลการขายทอดตลาดกรมบังคับคดี เพื่อวางกลยุทธ์ — รอนัดไหน ประเภท/จังหวัดไหน ได้ส่วนลดดีและมีโอกาสขายออก</p>
{% if astats.outliers and astats.outliers > 0 %}
<div class="text-[11px] text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 mb-3 leading-snug">
  ℹ️ กันรายการราคาต่ำผิดปกติ (ขายได้ &lt; 20% ของประเมิน — มักเป็นเลขคงที่เช่น 50,000 จากการขายเฉพาะส่วน/โจทก์ซื้อได้แล้วหักหนี้) ออกจากสถิติส่วนลดแล้ว {{ astats.outliers }} รายการ เพื่อไม่ให้ค่ากลางเพี้ยน
</div>
{% endif %}

<div class="flex items-center gap-2 mb-4 flex-wrap">
  <span class="text-xs text-slate-400">ช่วงวันขาย:</span>
  {% for d,lbl in ranges %}
  <a href="/auction-stats?days={{ d }}" class="px-3 py-1.5 rounded-lg border text-sm {% if days==d %}text-white{% else %}text-slate-600 hover:bg-slate-50{% endif %}"
     style="{% if days==d %}background:var(--survey);border-color:var(--survey){% endif %}">{{ lbl }}</a>
  {% endfor %}
</div>

{% if astats.n == 0 %}
<div class="sheet p-10 text-center text-slate-500">ยังไม่มีข้อมูลผลจบประมูลในช่วงนี้ — รอ scraper ดึงผลจากกรมบังคับคดี (ย้อนหลัง 6 เดือน)</div>
{% else %}

<div class="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-4">
  <div class="sheet p-4"><div class="text-xs text-slate-400">จบประมูล</div>
    <div class="text-2xl font-bold" style="color:var(--ink)">{{ "{:,}".format(astats.n) }}</div><div class="text-xs text-slate-400">รายการ</div></div>
  <div class="sheet p-4"><div class="text-xs text-slate-400">ขายออก</div>
    <div class="text-2xl font-bold text-emerald-600">{{ astats.pct }}%</div><div class="text-xs text-slate-400">{{ "{:,}".format(astats.sold) }} รายการ</div></div>
  <div class="sheet p-4"><div class="text-xs text-slate-400">ส่วนลดกลาง (เมื่อขายได้)</div>
    <div class="text-2xl font-bold {% if astats.med is not none and astats.med<0 %}text-emerald-600{% elif astats.med is not none and astats.med>0 %}text-rose-600{% else %}text-slate-600{% endif %}">{% if astats.med is not none %}{{ '+' if astats.med>0 else '' }}{{ astats.med }}%{% else %}—{% endif %}</div><div class="text-xs text-slate-400">เทียบราคาประเมิน</div></div>
  <div class="sheet p-4"><div class="text-xs text-slate-400">มูลค่าขายรวม</div>
    <div class="text-2xl font-bold" style="color:var(--survey-deep)">{{ "{:,.0f}".format(astats.sum_sold) }}</div><div class="text-xs text-slate-400">บาท</div></div>
</div>

<div class="sheet p-4 mb-4">
  <div class="flex items-center justify-between gap-2 flex-wrap mb-3">
    <h2 class="font-semibold">🎛️ กราฟปรับมุมมองเอง</h2>
    <div class="flex items-center gap-3 flex-wrap text-sm">
      <label class="flex items-center gap-1.5">
        <span class="text-xs text-slate-400">แกน</span>
        <select id="dimSel" class="border rounded-lg px-2 py-1.5 bg-white">
          <option value="rounds">ตามนัดประมูล</option>
          <option value="types">ตามประเภททรัพย์</option>
          <option value="provs">ตามจังหวัด (Top 10)</option>
          <option value="months">ตามเดือน</option>
        </select>
      </label>
      <label class="flex items-center gap-1.5">
        <span class="text-xs text-slate-400">ค่า</span>
        <select id="metSel" class="border rounded-lg px-2 py-1.5 bg-white">
          <option value="pct">% ขายได้</option>
          <option value="med">ส่วนลดกลาง (เทียบประเมิน)</option>
          <option value="n">จำนวนทรัพย์</option>
          <option value="sold">จำนวนที่ขายได้</option>
        </select>
      </label>
      <label class="flex items-center gap-1.5 text-slate-500">
        <input type="checkbox" id="sortChk" class="rounded"> <span class="text-xs">เรียงมาก→น้อย</span>
      </label>
    </div>
  </div>
  <div id="mainChart" style="width:100%;height:380px"></div>
  <p id="chartHint" class="text-[11px] text-slate-400 mt-1"></p>
</div>

<div class="grid gap-4 lg:grid-cols-2">
  <div class="sheet p-4">
    <h2 class="font-semibold mb-1">⏱️ ตามนัดประมูล</h2>
    <p class="text-xs text-slate-400 mb-2">ยิ่งนัดหลัง ราคายิ่งลด (ส่วนลดติดลบมากขึ้น) แต่ของดีมักถูกซื้อในนัดต้น</p>
    {{ statrows(astats.rounds, 'round', 'นัด ') }}
  </div>
  <div class="sheet p-4">
    <h2 class="font-semibold mb-1">🏠 ตามประเภททรัพย์</h2>
    <p class="text-xs text-slate-400 mb-2">ประเภทไหนขายออกง่าย / ได้ส่วนลดลึก</p>
    {{ statrows(astats.types, 'type', '') }}
  </div>
  <div class="sheet p-4">
    <h2 class="font-semibold mb-1">📍 จังหวัดที่มีประมูลมากสุด (Top 10)</h2>
    <p class="text-xs text-slate-400 mb-2">โฟกัสพื้นที่ที่มีของเยอะ + โอกาสขายออก</p>
    {{ statrows(astats.provs, 'prov', '') }}
  </div>
  <div class="sheet p-4">
    <h2 class="font-semibold mb-1">💸 การกระจายส่วนลด (เฉพาะที่ขายได้)</h2>
    <p class="text-xs text-slate-400 mb-3">ราคาจบเทียบราคาประเมิน — <span class="text-emerald-600">เขียว=ถูกกว่า</span> · <span class="text-rose-600">แดง=แพงกว่า</span></p>
    <div class="space-y-2">
    {% for b in astats.dist %}
      <div class="flex items-center gap-2 text-sm">
        <div class="w-40 text-xs text-slate-500 flex-shrink-0">{{ b.label }}</div>
        <div class="flex-1 bg-slate-100 rounded-full h-3 overflow-hidden">
          <div class="h-full rounded-full" style="width:{{ (100*b.n/astats.dmax)|round(0,'floor')|int if astats.dmax else 0 }}%;background:{{ '#10b981' if b.neg else '#e11d48' }}"></div></div>
        <div class="w-8 text-right text-xs tabular-nums text-slate-500">{{ b.n }}</div>
      </div>
    {% endfor %}
    </div>
  </div>
</div>

{% if astats.months|length > 1 %}
<div class="sheet p-4 mt-4">
  <h2 class="font-semibold mb-1">📈 แนวโน้มรายเดือน</h2>
  <p class="text-xs text-slate-400 mb-3">จำนวนจบประมูล (แท่ง) + %ขายออก (ตัวเลข) ต่อเดือน</p>
  <div class="flex items-end gap-3 overflow-x-auto pb-1" style="min-height:120px">
    {% set mmax = astats.months|map(attribute='n')|max %}
    {% for m in astats.months %}
    <div class="flex flex-col items-center gap-1 flex-shrink-0" style="width:52px">
      <div class="text-[11px] text-slate-500 tabular-nums">{{ m.pct }}%</div>
      <div class="w-7 rounded-t" style="height:{{ (80*m.n/mmax)|round(0,'floor')|int if mmax else 0 }}px;min-height:3px;background:var(--survey)"></div>
      <div class="text-[10px] text-slate-400">{{ m.ym }}</div>
      <div class="text-[10px] text-slate-400 tabular-nums">{{ m.n }}</div>
    </div>
    {% endfor %}
  </div>
</div>
{% endif %}

{% endif %}

<p class="text-[11px] text-slate-400 mt-6 text-center">ข้อมูลจากรายงานผลการขายทอดตลาด กรมบังคับคดี · จับคู่กับทรัพย์ที่ระบบเก็บก่อนประมูล · ตัวเลขจะแม่นขึ้นเมื่อดึงผลครบทุกหน่วยงาน · ควรตรวจสอบกับกรมฯ ก่อนตัดสินใจ</p>

{% if astats.n > 0 %}
<script src="/static/echarts.min.js"></script>
<script>
(function(){
  var A = {{ astats_json|safe }};
  var el = document.getElementById('mainChart');
  if(!el || !window.echarts){ return; }
  var chart = echarts.init(el);
  var DIMS = {
    rounds: {arr:A.rounds||[], key:'round', pre:'นัด ', axis:'นัดประมูล'},
    types:  {arr:A.types||[],  key:'type',  pre:'',      axis:'ประเภททรัพย์'},
    provs:  {arr:A.provs||[],   key:'prov',  pre:'',      axis:'จังหวัด'},
    months: {arr:A.months||[],  key:'ym',    pre:'',      axis:'เดือน'}
  };
  var METS = {
    pct:  {name:'% ขายได้', suffix:'%', diverge:false, hint:'สัดส่วนทรัพย์ที่ขายออกได้ (ยิ่งสูงยิ่งมีสภาพคล่อง)'},
    med:  {name:'ส่วนลดกลาง', suffix:'%', diverge:true,  hint:'ราคาจบเทียบราคาประเมิน (ค่ากลาง) — เขียว=ถูกกว่าประเมิน, แดง=แพงกว่า'},
    n:    {name:'จำนวนทรัพย์', suffix:'', diverge:false, hint:'จำนวนทรัพย์ที่จบประมูลในกลุ่มนี้'},
    sold: {name:'ขายได้ (รายการ)', suffix:'', diverge:false, hint:'จำนวนทรัพย์ที่ขายออกได้จริง'}
  };
  var dimSel = document.getElementById('dimSel');
  var metSel = document.getElementById('metSel');
  var sortChk = document.getElementById('sortChk');
  var hint = document.getElementById('chartHint');

  function render(){
    var D = DIMS[dimSel.value] || DIMS.rounds;
    var M = METS[metSel.value] || METS.pct;
    var rows = (D.arr||[]).slice();
    rows = rows.filter(function(r){ return metSel.value!=='med' || r.med!==null && r.med!==undefined; });
    if(sortChk.checked){
      rows.sort(function(a,b){ return (b[metSel.value]||0) - (a[metSel.value]||0); });
    }
    var cats = rows.map(function(r){ return D.pre + (r[D.key]!==undefined ? r[D.key] : ''); });
    var vals = rows.map(function(r){
      var v = r[metSel.value];
      return (v===null||v===undefined) ? null : v;
    });
    var data = vals.map(function(v){
      var color = '#1C86C9';
      if(M.diverge){ color = (v<0)?'#10b981':((v>0)?'#e11d48':'#94a3b8'); }
      return {value:v, itemStyle:{color:color, borderRadius:[4,4,0,0]}};
    });
    hint.textContent = M.hint;
    chart.setOption({
      grid:{left:8,right:16,top:24,bottom:24,containLabel:true},
      tooltip:{trigger:'axis', axisPointer:{type:'shadow'},
        formatter:function(p){
          var it=p[0]; if(!it) return '';
          var v=it.value; var txt=(v===null||v===undefined)?'—':((M.diverge&&v>0?'+':'')+v+M.suffix);
          return it.axisValue+'<br/><b>'+M.name+': '+txt+'</b>';
        }},
      xAxis:{type:'category', data:cats, name:D.axis, nameLocation:'end', nameGap:8,
        nameTextStyle:{color:'#94a3b8',fontSize:11},
        axisLabel:{color:'#64748b', interval:0, rotate: cats.length>7?30:0, fontSize:11},
        axisLine:{lineStyle:{color:'#e2e8f0'}}, axisTick:{show:false}},
      yAxis:{type:'value',
        axisLabel:{color:'#94a3b8', formatter:function(v){ return v+M.suffix; }},
        splitLine:{lineStyle:{color:'#f1f5f9'}}},
      series:[{type:'bar', data:data, barMaxWidth:46,
        label:{show:true, position:'top', color:'#475569', fontSize:10,
          formatter:function(o){ var v=o.value; return (v===null||v===undefined)?'':((M.diverge&&v>0?'+':'')+v+M.suffix); }}
      }]
    }, true);
  }
  dimSel.addEventListener('change', render);
  metSel.addEventListener('change', render);
  sortChk.addEventListener('change', render);
  window.addEventListener('resize', function(){ chart.resize(); });
  render();
})();
</script>
{% endif %}
{% endblock %}
""",
"auction_upcoming.html": """
{% extends "layout.html" %}{% block body %}
<section class="mb-4 rounded-2xl overflow-hidden relative" style="background:linear-gradient(135deg,#047857,var(--survey-deep))">
  <div class="relative px-5 py-6 text-white">
    <div class="text-[11px] tracking-widest uppercase font-semibold" style="color:rgba(255,255,255,.7)">ปฏิทินขายทอดตลาด · กรมบังคับคดี</div>
    <h1 class="display text-2xl font-bold mt-1">กำลังจะประมูล — ทรัพย์ที่ยังไม่ถึงวันนัด</h1>
    <p class="text-white/80 text-sm mt-1">ทรัพย์ LED ที่มีนัดขายทอดตลาดข้างหน้า จัดตามวันนัดถัดไป (ใกล้สุดก่อน) พร้อมราคาเริ่มต้นและนับถอยหลัง</p>
    <div class="mt-4 text-sm"><span class="text-white/60">กำลังจะประมูล</span> <b class="text-lg">{{ "{:,}".format(count) }}</b> รายการ</div>
  </div>
</section>

<div class="sheet p-3 mb-3 rounded-xl text-xs text-slate-500 flex items-start gap-2">
  <span>💡</span><span>ราคา = ราคาเริ่มต้นประมูล · "นัด X/รวม" = นัดถัดไปจากทั้งหมดตามประกาศ · ยิ่งนัดหลังมักได้ส่วนลดลึกขึ้น (ดู <a href="/auction-stats" class="brandlink">กลยุทธ์ประมูล</a>) · ควรตรวจสอบวันนัดกับกรมบังคับคดีอีกครั้งก่อนไป</span>
</div>

<form class="sheet p-3 mb-4 flex flex-wrap items-end gap-2 text-sm">
  <label>นัดครั้งที่
    <select name="round" onchange="this.form.submit()" class="mt-1 border rounded-lg px-2 py-1.5 bg-white">
      <option value="0">ทุกนัด</option>
      {% for r in round_opts %}<option value="{{ r.round }}" {% if r.round==sel_round %}selected{% endif %}>นัด {{ r.round }} ({{ r.n }})</option>{% endfor %}
    </select></label>
  <label>ประเภท
    <select name="ptype" onchange="this.form.submit()" class="mt-1 border rounded-lg px-2 py-1.5 bg-white">
      <option value="">ทุกประเภท</option>
      {% for t in types %}<option value="{{ t.code }}" {% if t.code==ptype %}selected{% endif %}>{{ t.label }} ({{ t.n }})</option>{% endfor %}
    </select></label>
  <label class="ml-auto">วันนัด
    <select name="date" onchange="this.form.submit()" class="mt-1 border rounded-lg px-2 py-1.5 bg-white max-w-[190px]">
      <option value="">ทุกวัน ({{ date_opts|length }} วัน)</option>
      {% for d in date_opts %}<option value="{{ d.iso }}" {% if d.iso==date %}selected{% endif %}>{{ d.label }} ({{ d.n }})</option>{% endfor %}
    </select></label>
  <label>จังหวัด
    <select name="province" onchange="this.form.submit()" class="mt-1 border rounded-lg px-2 py-1.5 bg-white">
      <option value="">ทุกจังหวัด</option>
      {% for p in provinces %}<option value="{{ p }}" {% if p==province %}selected{% endif %}>{{ p }}</option>{% endfor %}
    </select></label>
</form>

{% if not groups %}
<div class="sheet p-10 text-center text-slate-500">
  ยังไม่มีทรัพย์ที่กำลังจะประมูลในเงื่อนไขนี้ — ลองล้างตัวกรอง หรือรอ scraper ดึงประกาศนัดใหม่จากกรมบังคับคดี
</div>
{% else %}
{% for grp in groups %}
<div class="flex items-center gap-2 mt-5 mb-2">
  <h2 class="font-semibold text-slate-700">วันนัด {{ grp.label }}</h2>
  <span class="text-[11px] px-2 py-0.5 rounded-full {% if grp.days_left<=3 %}bg-rose-100 text-rose-700{% elif grp.days_left<=7 %}bg-amber-100 text-amber-800{% else %}bg-slate-100 text-slate-500{% endif %}">
    {% if grp.days_left==0 %}วันนี้{% elif grp.days_left==1 %}พรุ่งนี้{% else %}อีก {{ grp.days_left }} วัน{% endif %}</span>
  <span class="text-xs text-slate-400">{{ grp.rows|length }} รายการ</span>
  <span class="flex-1 border-t" style="border-color:var(--rule)"></span>
</div>
<div class="grid gap-2.5 sm:grid-cols-2 lg:grid-cols-3">
  {% for r in grp.rows %}
  <a href="/p/led_auction/{{ r.ref }}" class="sheet p-3.5 block hover:shadow-md transition">
    <div class="flex items-start justify-between gap-2">
      <div class="min-w-0">
        <div class="font-medium text-sm line-clamp-1">{{ type_labels.get(r.property_type, 'ทรัพย์') }}
          {% if r.subdistrict or r.district %}<span class="text-slate-500 font-normal">· {{ r.district or r.subdistrict }}</span>{% endif %}</div>
        <div class="text-xs text-slate-400 mt-0.5">
          <span class="text-slate-500 font-medium">นัด {{ r.next_round }}{% if r.total_rounds %}/{{ r.total_rounds }}{% endif %}</span>
          {% if r.province %} · {{ r.province }}{% endif %}</div>
        {% if r.title %}<div class="text-[11px] text-slate-400 line-clamp-1 mt-0.5">{{ r.title }}</div>{% endif %}
      </div>
      {% if r.grade %}<span class="seal g-{{ r.grade }}">{{ r.grade }}</span>{% endif %}
    </div>
    <div class="mt-2.5 flex items-end justify-between gap-2">
      <div>
        <div class="text-[11px] text-slate-400">ราคาเริ่มต้น</div>
        <div class="text-lg font-bold leading-tight" style="color:var(--survey-deep)">{{ "{:,.0f}".format(r.opening_price or 0) }}</div>
        {% if r.appraised_price %}<div class="text-[11px] text-slate-400">ประเมิน {{ "{:,.0f}".format(r.appraised_price) }}</div>{% endif %}
      </div>
      <div class="text-right">
        <span class="text-[11px] px-2 py-0.5 rounded-full font-medium {% if r.days_left<=3 %}bg-rose-100 text-rose-700{% elif r.days_left<=7 %}bg-amber-100 text-amber-800{% else %}bg-emerald-50 text-emerald-700{% endif %}">
          {% if r.days_left==0 %}วันนี้{% elif r.days_left==1 %}พรุ่งนี้{% else %}อีก {{ r.days_left }} วัน{% endif %}</span>
      </div>
    </div>
  </a>
  {% endfor %}
</div>
{% endfor %}

{% if pages > 1 %}
<nav class="mt-6 flex items-center justify-center gap-1 flex-wrap">
  {% set qs %}{% if province %}&province={{ province }}{% endif %}{% if ptype %}&ptype={{ ptype }}{% endif %}{% if date %}&date={{ date }}{% endif %}{% if sel_round %}&round={{ sel_round }}{% endif %}{% endset %}
  {% if page > 1 %}<a href="/upcoming?page={{ page-1 }}{{ qs }}" class="px-3 py-1.5 rounded-lg border text-sm text-slate-600 hover:bg-slate-50">← ก่อนหน้า</a>{% endif %}
  <span class="px-3 py-1.5 text-sm text-slate-500">หน้า {{ page }}/{{ pages }}</span>
  {% if page < pages %}<a href="/upcoming?page={{ page+1 }}{{ qs }}" class="px-3 py-1.5 rounded-lg border text-sm text-slate-600 hover:bg-slate-50">ถัดไป →</a>{% endif %}
</nav>
{% endif %}
{% endif %}

<p class="text-[11px] text-slate-400 mt-6 text-center">วันนัดจากประกาศขายทอดตลาด กรมบังคับคดี · เลขนัดอิงตามประกาศ (รีเซ็ตเมื่อประกาศใหม่) · ควรตรวจสอบกับกรมฯ ก่อนตัดสินใจ</p>
{% endblock %}
""",
"auction_results.html": """
{% extends "layout.html" %}{% block body %}
<section class="mb-4 rounded-2xl overflow-hidden relative" style="background:linear-gradient(135deg,var(--ink),var(--survey-deep))">
  <div class="relative px-5 py-6 text-white">
    <div class="text-[11px] tracking-widest uppercase font-semibold" style="color:rgba(255,255,255,.7)">รายงานผลการขายทอดตลาด · กรมบังคับคดี</div>
    <h1 class="display text-2xl font-bold mt-1">จบประมูลแล้ว — ขายที่ราคาเท่าไหร่</h1>
    <p class="text-white/80 text-sm mt-1">ทรัพย์ LED ที่ครบกำหนดประมูล พร้อมผลจริง (ขายได้/ไม่มีผู้สู้ราคา) จัดตามวันขาย</p>
    <div class="mt-4 flex flex-wrap gap-x-6 gap-y-2 text-sm">
      <div><span class="text-white/60">จบประมูล</span> <b class="text-lg">{{ "{:,}".format(stats.n or 0) }}</b> รายการ</div>
      <div><span class="text-white/60">ขายได้</span> <b class="text-lg text-emerald-300">{{ "{:,}".format(stats.sold or 0) }}</b></div>
      <div><span class="text-white/60">มูลค่าขายรวม</span> <b class="text-lg">{{ "{:,.0f}".format(stats.sum_sold or 0) }}</b> บาท</div>
    </div>
  </div>
</section>

{% if astats.rounds %}
<div class="sheet p-4 mb-4">
  <div class="flex items-center justify-between gap-2 mb-1">
    <h2 class="font-semibold">📊 กลยุทธ์: รอประมูลนัดไหนคุ้ม</h2>
    <a href="/auction-stats" class="text-xs brandlink whitespace-nowrap">ดูแดชบอร์ดเต็ม (กรองช่วงวัน) →</a>
  </div>
  <p class="text-xs text-slate-500 mb-3">จากผลจริง {{ "{:,}".format(astats.n) }} รายการที่จับคู่ได้ (ขายออก {{ astats.pct }}%) — ยิ่งนัดหลัง ราคายิ่งลด แต่ของดีมักถูกซื้อไปในนัดต้น ๆ</p>
  <div class="overflow-x-auto">
  <table class="w-full text-sm">
    <thead><tr class="text-xs text-slate-400 text-left">
      <th class="py-1 font-normal">นัด</th><th class="font-normal">ทรัพย์</th>
      <th class="font-normal w-2/5">% ขายได้</th><th class="font-normal text-right">ราคาจบเทียบประเมิน</th>
    </tr></thead>
    <tbody>
    {% for r in astats.rounds %}
    <tr class="border-t" style="border-color:var(--rule)">
      <td class="py-2 font-medium whitespace-nowrap">นัด {{ r.round }}</td>
      <td class="text-slate-500 tabular-nums">{{ r.n }}</td>
      <td class="py-2">
        <div class="flex items-center gap-2">
          <div class="flex-1 bg-slate-100 rounded-full h-2 overflow-hidden max-w-[160px]">
            <div class="h-full rounded-full" style="width:{{ r.pct }}%;background:var(--survey)"></div>
          </div>
          <span class="text-xs tabular-nums text-slate-500">{{ r.pct }}%</span>
        </div>
      </td>
      <td class="text-right tabular-nums font-medium {% if r.med is not none and r.med < 0 %}text-emerald-600{% elif r.med is not none %}text-slate-500{% else %}text-slate-300{% endif %}">
        {% if r.med is not none %}{{ '+' if r.med>=0 else '' }}{{ r.med }}%{% else %}—{% endif %}
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
  <p class="text-[11px] text-slate-400 mt-2">% ขายได้ = สัดส่วนที่ขายออกในนัดนั้น · ราคาจบเทียบประเมิน = ค่ากลาง (ติดลบ = ถูกกว่าประเมิน) · ตัวเลขจะแม่นขึ้นเมื่อดึงผลครบทุกหน่วยงาน</p>
</div>
{% endif %}

<form class="sheet p-3 mb-4 flex flex-wrap items-end gap-2 text-sm">
  <div class="flex gap-1">
    {% for v,lbl in [('','ทั้งหมด'),('sold','ขายได้'),('nobid','ไม่มีผู้สู้ราคา')] %}
    <a href="/auction-results?result={{ v }}{% if province %}&province={{ province }}{% endif %}{% if date %}&date={{ date }}{% endif %}{% if sel_round %}&round={{ sel_round }}{% endif %}"
       class="px-3 py-1.5 rounded-lg border {% if result==v %}text-white{% else %}text-slate-600 hover:bg-slate-50{% endif %}"
       style="{% if result==v %}background:var(--survey);border-color:var(--survey){% endif %}">{{ lbl }}</a>
    {% endfor %}
  </div>
  {% if result %}<input type="hidden" name="result" value="{{ result }}">{% endif %}
  <label class="ml-auto">นัดครั้งที่
    <select name="round" onchange="this.form.submit()" class="mt-1 border rounded-lg px-2 py-1.5 bg-white">
      <option value="0">ทุกนัด</option>
      {% for r in round_opts %}<option value="{{ r.round }}" {% if r.round==sel_round %}selected{% endif %}>นัด {{ r.round }} ({{ r.n }})</option>{% endfor %}
    </select></label>
  <label>วันขาย
    <select name="date" onchange="this.form.submit()" class="mt-1 border rounded-lg px-2 py-1.5 bg-white max-w-[190px]">
      <option value="">ทุกวัน ({{ date_opts|length }} วัน)</option>
      {% for d in date_opts %}<option value="{{ d.iso }}" {% if d.iso==date %}selected{% endif %}>{{ d.label }} ({{ d.n }})</option>{% endfor %}
    </select></label>
  <label>จังหวัด
    <select name="province" onchange="this.form.submit()" class="mt-1 border rounded-lg px-2 py-1.5 bg-white">
      <option value="">ทุกจังหวัด</option>
      {% for p in provinces %}<option value="{{ p }}" {% if p==province %}selected{% endif %}>{{ p }}</option>{% endfor %}
    </select></label>
</form>

{% if not groups %}
<div class="sheet p-10 text-center text-slate-500">
  ยังไม่มีผลจบประมูลในเงื่อนไขนี้ — ระบบดึงผลจากกรมบังคับคดี (ย้อนหลัง 6 เดือน) หลังทรัพย์ครบกำหนดขาย
</div>
{% else %}
{% for grp in groups %}
<div class="flex items-center gap-2 mt-5 mb-2">
  <h2 class="font-semibold text-slate-700">วันขาย {{ grp.label }}</h2>
  <span class="text-xs text-slate-400">{{ grp.rows|length }} รายการ</span>
  <span class="flex-1 border-t" style="border-color:var(--rule)"></span>
</div>
<div class="grid gap-2.5 sm:grid-cols-2 lg:grid-cols-3">
  {% for r in grp.rows %}
  {% if r.ref %}
  <a href="/p/led_auction/{{ r.ref }}" class="sheet p-3.5 block hover:shadow-md transition">
  {% else %}
  <div class="sheet p-3.5 block" style="opacity:.92">
  {% endif %}
    <div class="flex items-start justify-between gap-2">
      <div class="min-w-0">
        <div class="font-medium text-sm line-clamp-1">{{ type_labels.get(r.property_type, r.property_type_th or 'ทรัพย์') }}
          {% if r.subdistrict or r.district %}<span class="text-slate-500 font-normal">· {{ r.district or r.subdistrict }}</span>{% endif %}
          {% if not r.ref %}<span class="text-[10px] text-slate-400 font-normal">(ยังไม่มีในระบบ)</span>{% endif %}</div>
        <div class="text-xs text-slate-400 mt-0.5">{% if r.round %}<span class="text-slate-500 font-medium">นัด {{ r.round }}</span> · {% endif %}{{ r.province or '' }}{% if r.court %} · ศาล{{ r.court }}{% endif %}</div>
        {% if r.case_no %}<div class="text-[11px] text-slate-400 mt-0.5">คดีแดง {{ r.case_no }}{% if r.deed and r.deed != '-' %} · โฉนด {{ r.deed }}{% endif %}</div>{% endif %}
        {% if r.plaintiff %}<div class="text-[11px] text-slate-400 line-clamp-1">โจทก์ {{ r.plaintiff }}</div>{% endif %}
      </div>
      {% if r.grade %}<span class="seal g-{{ r.grade }}">{{ r.grade }}</span>{% endif %}
    </div>
    <div class="mt-2.5 flex items-end justify-between gap-2">
      <div>
        <div class="text-[11px] text-slate-400">ราคาประเมิน</div>
        <div class="text-sm text-slate-600">{{ "{:,.0f}".format(r.appraised_price or 0) }}</div>
      </div>
      {% if r.is_sold %}
      <div class="text-right">
        <span class="text-[11px] px-2 py-0.5 rounded-full font-medium bg-emerald-100 text-emerald-800">✓ ขายได้</span>
        <div class="text-lg font-bold leading-tight mt-0.5" style="color:var(--survey-deep)">{{ "{:,.0f}".format(r.sold_price or 0) }}</div>
        {% if r.suspect %}<div class="text-[11px] text-amber-600" title="ราคาต่ำผิดปกติ — มักเป็นขายเฉพาะส่วน/โจทก์ซื้อได้แล้วหักหนี้ ไม่ใช่ราคาตลาด">⚠️ ราคาต่ำผิดปกติ</div>
        {% elif r.pct is not none %}<div class="text-[11px] {{ 'text-emerald-600' if r.pct>=0 else 'text-red-500' }}">{{ '+' if r.pct>=0 else '' }}{{ r.pct }}% จากประเมิน</div>{% endif %}
      </div>
      {% else %}
      <div class="text-right">
        <span class="text-[11px] px-2 py-0.5 rounded-full font-medium
          {% if 'ถอน' in r.result %}bg-slate-200 text-slate-600
          {% elif 'นัดที่เหลือ' in r.result %}bg-amber-100 text-amber-800
          {% else %}bg-slate-100 text-slate-500{% endif %}">{{ r.result or 'ไม่มีผู้สู้ราคา' }}</span>
      </div>
      {% endif %}
    </div>
  {% if r.ref %}</a>{% else %}</div>{% endif %}
  {% endfor %}
</div>
{% endfor %}

{% if pages > 1 %}
<div class="flex justify-center gap-1 mt-6 text-sm">
  {% for p in range(1, pages+1) %}
  {% if p==page %}<span class="px-3 py-1.5 rounded-lg text-white" style="background:var(--survey)">{{ p }}</span>
  {% else %}<a href="/auction-results?page={{ p }}{% if result %}&result={{ result }}{% endif %}{% if province %}&province={{ province }}{% endif %}{% if date %}&date={{ date }}{% endif %}" class="px-3 py-1.5 rounded-lg border hover:bg-slate-50">{{ p }}</a>{% endif %}
  {% endfor %}
</div>
{% endif %}
{% endif %}

<p class="text-[11px] text-slate-400 mt-6 text-center">ข้อมูลผลการขายจาก รายงานผลการขายทอดตลาด กรมบังคับคดี · จับคู่กับทรัพย์ที่ระบบเก็บก่อนประมูลด้วยราคาประเมิน · ควรตรวจสอบกับกรมฯ อีกครั้งก่อนตัดสินใจ</p>
{% endblock %}
""",
"my_listings.html": """
{% extends "layout.html" %}{% block body %}
<div class="flex items-center justify-between mb-4">
  <h1 class="text-xl font-semibold">ประกาศของฉัน</h1>
  <a href="/sell" class="text-sm text-white rounded-lg px-3 py-1.5" style="background:var(--survey)">+ ลงประกาศ</a>
</div>
{% if just_posted %}<div class="sheet p-3 mb-3 text-sm bg-emerald-50 text-emerald-800">✓ ส่งประกาศแล้ว รอทีมงานตรวจอนุมัติ</div>{% endif %}
{% if just_updated %}<div class="sheet p-3 mb-3 text-sm bg-emerald-50 text-emerald-800">✓ บันทึกการแก้ไขแล้ว</div>{% endif %}
{% if not rows %}
<div class="sheet p-10 text-center text-slate-500">ยังไม่มีประกาศ — <a href="/sell" class="brandlink">ลงประกาศแรก</a> ได้เลย</div>
{% else %}
<div class="space-y-3">
{% for r in rows %}
<div class="sheet p-3">
  <div class="flex gap-3 items-center">
    <a href="/m/{{ r.id }}" class="w-20 h-20 rounded-lg bg-slate-100 flex-shrink-0 overflow-hidden block">
      {% if r.image %}<img src="{{ r.image }}" class="w-full h-full object-cover">{% endif %}</a>
    <a href="/m/{{ r.id }}" class="min-w-0 flex-1 block">
      <div class="font-medium line-clamp-1">{{ r.title }}</div>
      <div class="text-sm text-slate-500">{{ "{:,.0f}".format(r.price or 0) }} บาท{{ '/เดือน' if r.listing_kind=='rent' else '' }} · {{ 'ให้เช่า' if r.listing_kind=='rent' else 'ขาย' }}</div>
      <div class="text-xs text-slate-400 mt-0.5">👁 {{ "{:,}".format(r.views_30d or 0) }} วิว / 30 วัน</div>
    </a>
    <div class="flex flex-col items-end gap-1 flex-shrink-0">
      <span class="text-[11px] px-2 py-1 rounded font-medium
        {% if r.status=='approved' and r.expired %}bg-slate-200 text-slate-600{% elif r.status=='approved' %}bg-emerald-100 text-emerald-800{% elif r.status=='rejected' %}bg-red-100 text-red-700{% else %}bg-amber-100 text-amber-800{% endif %}">
        {% if r.status=='approved' and r.expired %}หมดอายุ{% elif r.status=='approved' %}เผยแพร่แล้ว{% elif r.status=='rejected' %}ไม่ผ่าน{% else %}รออนุมัติ{% endif %}</span>
      {% if r.status=='approved' and not r.expired and r.days_left is not none %}<span class="text-[11px] text-slate-400">เหลือ {{ r.days_left }} วัน</span>{% endif %}
    </div>
  </div>
  <div class="mt-2 flex items-center gap-2 flex-wrap">
    <a href="/sell?edit={{ r.id }}" class="text-xs rounded-lg px-3 py-1.5 border font-medium text-slate-600 hover:bg-slate-50">✏️ แก้ไข</a>
    {% if r.status=='approved' %}
    <button type="button" onclick="bumpListing('{{ r.id }}', this)" {% if not r.can_bump %}disabled{% endif %}
      class="text-xs rounded-lg px-3 py-1.5 text-white" style="background:var(--survey);{% if not r.can_bump %}opacity:.4;pointer-events:none{% endif %}">
      {% if r.expired %}🔄 ต่ออายุประกาศ{% else %}⬆️ ดันประกาศขึ้นบนสุด{% endif %}</button>
    {% if not r.can_bump %}<span class="text-[11px] text-slate-400">ดันได้อีกครั้งในวันพรุ่งนี้</span>
    {% elif r.expired %}<span class="text-[11px] text-red-500">กดเพื่อกลับมาแสดงบนเว็บอีกครั้ง</span>{% endif %}
    {% elif r.status=='rejected' %}<span class="text-[11px] text-slate-400">แก้ไขแล้วจะส่งไปรออนุมัติใหม่</span>{% endif %}
  </div>
  {% if r.status=='rejected' and r.reject_reason %}<div class="text-xs text-red-600 mt-1">เหตุผล: {{ r.reject_reason }}</div>{% endif %}
</div>
{% endfor %}
</div>
{% endif %}
<script>
function bumpListing(id, btn){
  btn.disabled=true; var old=btn.textContent; btn.textContent='กำลังดัน…';
  fetch('/api/bump/'+id,{method:'POST'})
    .then(function(r){ if(r.status===401){location.href='/login?next=/my-listings';return null;} return r.json(); })
    .then(function(d){ if(!d)return;
      if(d.ok){ location.reload(); }
      else { btn.textContent=old; btn.disabled=false; alert(d.message||'ดันประกาศไม่สำเร็จ'); } })
    .catch(function(){ btn.textContent=old; btn.disabled=false; alert('ดันประกาศไม่สำเร็จ'); });
}
</script>
{% endblock %}
""",
"admin_market.html": """
{% extends "layout.html" %}{% block body %}
<div class="flex items-center justify-between mb-3">
  <h1 class="text-xl font-semibold">อนุมัติประกาศสมาชิก</h1>
  <div class="flex gap-1 text-sm">
    {% set tk = '&token=' ~ admin_token if admin_token else '' %}
    {% for s,lbl in [('pending','รออนุมัติ'),('approved','ผ่าน'),('rejected','ไม่ผ่าน')] %}
    <a href="/admin/market?status={{ s }}{{ tk }}" class="px-3 py-1 rounded {{ 'bg-slate-900 text-white' if s==status else 'border' }}">{{ lbl }}</a>
    {% endfor %}
  </div>
</div>
{% if not rows %}<div class="sheet p-10 text-center text-slate-500">ไม่มีประกาศในสถานะนี้</div>{% else %}
<div class="space-y-3">
{% for r in rows %}
<div class="sheet p-3">
  <div class="flex gap-3">
    <a href="/m/{{ r.id }}{% if admin_token %}?token={{ admin_token }}{% endif %}" class="w-24 h-24 rounded-lg bg-slate-100 overflow-hidden flex-shrink-0 block">
      {% if r.image %}<img src="{{ r.image }}" class="w-full h-full object-cover">{% endif %}</a>
    <div class="min-w-0 flex-1">
      <div class="font-medium">{{ r.title }}</div>
      <div class="text-sm text-slate-500">{{ 'ให้เช่า' if r.listing_kind=='rent' else 'ขาย' }} · {{ "{:,.0f}".format(r.price or 0) }} บาท · {{ r.type_label }} · {{ r.district }} {{ r.province }}</div>
      {% if r.description %}<div class="text-xs text-slate-500 mt-1 line-clamp-2">{{ r.description }}</div>{% endif %}
      <div class="text-[11px] text-slate-400 mt-1">โดย {{ r.posted_by }} · ติดต่อ {{ r.contact_phone or '-' }} {{ r.contact_line or '' }}</div>
    </div>
  </div>
  <form method="post" action="/admin/market/{{ r.id }}{% if admin_token %}?token={{ admin_token }}{% endif %}" class="mt-2 flex gap-2 items-center">
    {% if r.status != 'approved' %}<button name="action" value="approve" class="rounded-lg px-3 py-1.5 text-sm text-white" style="background:var(--survey)">อนุมัติ</button>{% endif %}
    {% if r.status != 'rejected' %}
    <input name="reject_reason" placeholder="เหตุผล (ถ้าไม่ผ่าน)" class="border rounded-lg px-2 py-1.5 text-sm flex-1">
    <button name="action" value="reject" class="rounded-lg px-3 py-1.5 text-sm border text-red-600">ไม่ผ่าน</button>{% endif %}
  </form>
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
        "line_login": LINE_LOGIN_ENABLED, "google_login": GOOGLE_LOGIN_ENABLED,
        "any_login": LINE_LOGIN_ENABLED or GOOGLE_LOGIN_ENABLED,
        "user_logged_in": False, "fav_pairs": set()}


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
          sort: str = Query(""), near_transit: str = Query(""), token: str = Query(""),
          src: str = Query("")):
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
    src = (src or "").strip()
    if src == "member":
        # โหมด "เจ้าของลงเอง" — โชว์เฉพาะประกาศสมาชิก (แบ่งหน้า) ตามตัวกรองเดียวกัน
        rows, total = member_page(province=province, district=district, ptype=ptype,
                                  min_price=min_price_v, max_price=max_price_v,
                                  page=page, page_size=PAGE_SIZE)
    else:
        rows, total = fetch_rows(
            province=province, district=district, ptype=ptype, max_price=max_price_v,
            min_price=min_price_v, hide_critical=hide_critical, institution=institution,
            min_grade=min_grade, show_special=show_special, is_admin=is_admin,
            page=page, page_size=PAGE_SIZE, order=order, near_transit=near_transit_v)
        # ปนประกาศเจ้าของเข้าฟีดหลัก (หน้า 1, มุมมองทั่วไป ไม่เจาะแหล่ง NPA/ตัวกรองเฉพาะ)
        if page == 1 and not any([institution, near_transit_v, min_grade,
                                  hide_critical, show_special]):
            mcards = member_feed_cards(province=province, district=district, ptype=ptype,
                                       min_price=min_price_v, max_price=max_price_v, limit=6)
            if mcards:
                rows = _interleave_member(mcards, rows)
    pages = max(1, -(-total // PAGE_SIZE))
    opts = filter_options(province)

    # "ทรัพย์แนะนำ" โชว์เฉพาะหน้าแรกที่ไม่ได้กรองอะไร (หน้าโฮมจริง ๆ)
    featured = []
    if page == 1 and not src and not any([province, district, ptype, max_price_v, min_price_v,
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
    if page == 1 and not src and not any([province, district, ptype, max_price_v, min_price_v,
                              institution, min_grade, hide_critical, show_special,
                              near_transit_v]):
        try:
            promoted = promoted_list(is_admin, n=6)
        except Exception as exc:                               # noqa: BLE001
            log.warning("ทรัพย์โปรโมทโหลดไม่สำเร็จ: %s", str(exc)[:100])
            promoted = []

    qs_parts = {"province": province, "district": district, "ptype": ptype, "src": src or None,
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
        canonical=canonical, jsonld=home_jsonld, src=src,
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
    prov_inferred = False
    if source_code == "led_auction":
        ex = led_extra(ref)                       # สำนักงาน + ศาล (จาก DB โดยตรง)
        if ex.get("office_name") and not r.get("office_name"):
            r["office_name"] = ex["office_name"]
        r["court_name"] = ex.get("court_name")
        r["auction_venue"] = ex.get("auction_venue")
        r["led_pid"] = ex.get("led_pid")
        r["led_pname"] = ex.get("led_pname")
        r["led_bdate"] = ex.get("led_bdate")
        r["led_saletype"] = ex.get("saletype")
        r["led_occupant"] = ex.get("occupant")
        r["led_sale_location"] = ex.get("sale_location")
        r["led_schedule"] = ex.get("schedule") or []
        if _blank(r.get("district")):
            r["district"] = None
        if _blank(r.get("subdistrict")):
            r["subdistrict"] = None
        if _blank(r.get("province")):             # เดาจังหวัดจากหน่วยงานที่ขาย
            guessed = province_from_texts(r.get("office_name"), ex.get("court_name"),
                                          ex.get("auction_venue"), r.get("title"),
                                          r.get("address_raw"))
            if guessed:
                r["province"] = guessed
                prov_inferred = True
    # ผลจบประมูล (ถ้าทรัพย์นี้ครบกำหนดขายแล้ว และดึงผลจาก LED มาแล้ว)
    auc_result = None
    if source_code == "led_auction" and not DEMO_MODE:
        try:
            from core.db import connect
            with connect() as conn:
                _ar = conn.execute(
                    "select sale_date, result, is_sold, sold_price, appraised_price, "
                    "case_no, court, deed, plaintiff "
                    "from led_auction_results where matched_ref = %s "
                    "order by sale_date desc limit 1", (ref,)).fetchone()
            if _ar:
                auc_result = dict(_ar)
                auc_result["date_label"] = _thai_date(auc_result.get("sale_date"))
                if auc_result.get("is_sold") and auc_result.get("appraised_price") \
                        and auc_result.get("sold_price"):
                    _ratio = float(auc_result["sold_price"]) / float(auc_result["appraised_price"])
                    if _ratio < _AUC_OUTLIER_RATIO:
                        auc_result["suspect"] = True                # ราคาต่ำผิดปกติ — ไม่โชว์ %
                    else:
                        auc_result["pct"] = round((_ratio - 1) * 100)
        except Exception as exc:                                    # noqa: BLE001
            log.warning("ดึงผลประมูลไม่สำเร็จ: %s", str(exc)[:100])
    _op = r.get("opening_price")
    _usm = r.get("usable_area_sqm")
    _pps_m = round(_op / _usm) if _op and _usm else None          # ราคา/ตร.ม. (คอนโด/ห้องชุด)
    _stn, _stn_m = r.get("nearest_station_name"), r.get("nearest_station_m")
    _stn_txt = None
    if _stn and _stn_m is not None:
        _sd = f"{int(_stn_m):,} ม." if _stn_m < 1000 else f"{_stn_m / 1000:.1f} กม."
        _stn_txt = f"{_stn} · ห่าง {_sd}"
    specs = [(k, v) for k, v in [
        ("ประเภท", r["type_label"]),
        ("เนื้อที่ดิน", f"{r['land_area_sqwa']} ตร.ว." if r.get("land_area_sqwa") else None),
        ("พื้นที่ใช้สอย", f"{r['usable_area_sqm']} ตร.ม." if r.get("usable_area_sqm") else None),
        ("ห้องนอน", f"{r['bedrooms']} ห้อง" if r.get("bedrooms") else None),
        ("ห้องน้ำ", f"{r['bathrooms']} ห้อง" if r.get("bathrooms") else None),
        ("ที่จอดรถ", f"{r['parking']} คัน" if r.get("parking") else None),
        ("ราคา/ตร.ม.", f"{_pps_m:,} บาท" if _pps_m else None),
        ("ราคา/ตร.ว.", f"{r['price_per_sqwa']:,.0f} บาท" if r.get("price_per_sqwa") else None),
        ("ราคาตั้งขาย", f"{r['list_price']:,.0f} บาท" if r.get("list_price") else None),
        ("ราคาพิเศษ", f"{r['special_price']:,.0f} บาท" if r.get("special_price") else None),
        ("สถานีใกล้สุด", _stn_txt),
        ("นัดขายครั้งที่", r.get("auction_round")),
        ("วันขายทอดตลาด", r.get("auction_date")),
        ("หน่วยงานที่ขาย", r.get("office_name")),
        ("สถานที่ขายทอดตลาด", r.get("auction_venue")),
        ("การจำนอง", "ติดไปกับทรัพย์" if r.get("mortgage_carried") else "ไม่ติดไป"),
        ("ภาระผูกพัน", r.get("led_saletype") if not _blank(r.get("led_saletype")) else None),
        ("ผู้อยู่อาศัย/ผู้ครอบครอง", r.get("occupancy_note") or (r.get("led_occupant") if not _blank(r.get("led_occupant")) else None)),
        ("สภาพทรัพย์", "ปรับปรุง/รีโนเวทแล้ว" if r.get("renovated") else None),
        ("จังหวัด", (r.get("province") + " (จากศาล)") if prov_inferred and r.get("province") else r.get("province")),
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
        title=r["title"], r=r, specs=specs, auc_result=auc_result,
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


_MAP_CACHE: dict = {}          # geojson แผนที่ (เหมือนกันทุกคน) — cache กันสร้างซ้ำ
_MAP_TTL = 600                 # 10 นาที

# ── "รอบทรัพย์นี้" — วิเคราะห์ POI รัศมี 500ม./1/3 กม. จาก OpenStreetMap (Overpass) ──
# เดิมใช้ 5 กม. ในย่านหนาแน่น (กทม.) query หนักจน mirror timeout → "ไม่สำเร็จ"
# ลดเหลือ 3 กม. (พื้นที่ ~36%) เร็วขึ้นและเสถียรกว่ามาก
_OSM_ENDPOINTS = [
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",   # เสถียรสุดจากที่ทดสอบ
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
_POI_CACHE_DAYS = 45           # cache ผลต่อพิกัดกี่วัน
_NEARBY_RADIUS = 3000          # เมตร (รัศมีสูงสุดที่ยิง Overpass)


def _haversine_m(lat1, lng1, lat2, lng2):
    import math
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _poi_category(tags: dict) -> str | None:
    """แม็พ OSM tags -> หมวดของเรา (transport/edu/health/life)"""
    a = tags.get("amenity", "")
    s = tags.get("shop", "")
    rw = tags.get("railway", "")
    pt = tags.get("public_transport", "")
    le = tags.get("leisure", "")
    if rw in ("station", "halt") or tags.get("station") in ("subway", "light_rail", "monorail"):
        return "rail"
    if a == "bus_station" or tags.get("aeroway") == "aerodrome" or pt == "station":
        return "transport"
    if a in ("school", "university", "college", "kindergarten"):
        return "edu"
    if a in ("hospital", "clinic", "doctors") or tags.get("healthcare"):
        return "health"
    if s in ("mall", "supermarket", "convenience", "department_store") \
            or a in ("marketplace", "bank", "fuel") or le == "park":
        return "life"
    return None


def _poi_subtype(tags: dict) -> str:
    a = tags.get("amenity", ""); s = tags.get("shop", ""); le = tags.get("leisure", "")
    rw = tags.get("railway", ""); pt = tags.get("public_transport", "")
    m = {"school": "โรงเรียน", "university": "มหาวิทยาลัย", "college": "วิทยาลัย",
         "kindergarten": "อนุบาล", "hospital": "โรงพยาบาล", "clinic": "คลินิก",
         "doctors": "คลินิก", "marketplace": "ตลาด", "bank": "ธนาคาร", "fuel": "ปั๊มน้ำมัน",
         "bus_station": "สถานีขนส่ง"}
    ms = {"mall": "ห้างสรรพสินค้า", "department_store": "ห้างสรรพสินค้า",
          "supermarket": "ซูเปอร์มาร์เก็ต", "convenience": "ร้านสะดวกซื้อ"}
    if rw in ("station", "halt") or tags.get("station") in ("subway", "light_rail", "monorail"):
        return "สถานีรถไฟฟ้า/รถไฟ"
    if tags.get("aeroway") == "aerodrome":
        return "สนามบิน"
    if pt == "station":
        return "จุดขนส่งสาธารณะ"
    if le == "park":
        return "สวนสาธารณะ"
    return m.get(a) or ms.get(s) or "สถานที่"


def _overpass_query(lat: float, lng: float) -> str:
    r = _NEARBY_RADIUS
    ar = f"(around:{r},{lat},{lng})"
    parts = [
        f'node["railway"~"^(station|halt)$"]{ar};',
        f'node["public_transport"="station"]{ar};',
        f'node["amenity"="bus_station"]{ar};',
        f'node["aeroway"="aerodrome"]{ar};way["aeroway"="aerodrome"]{ar};',
        f'node["amenity"~"^(school|university|college|kindergarten)$"]{ar};',
        f'way["amenity"~"^(school|university|college)$"]{ar};',
        f'node["amenity"~"^(hospital|clinic|doctors)$"]{ar};',
        f'way["amenity"="hospital"]{ar};',
        f'node["shop"~"^(mall|supermarket|convenience|department_store)$"]{ar};',
        f'way["shop"~"^(mall|department_store|supermarket)$"]{ar};',
        f'node["amenity"~"^(marketplace|bank|fuel)$"]{ar};',
        f'node["leisure"="park"]{ar};way["leisure"="park"]{ar};',
    ]
    return "[out:json][timeout:20];(" + "".join(parts) + ");out center tags 500;"


def _build_nearby(lat: float, lng: float, elements: list) -> dict:
    """จัดหมวด + คิดระยะ + นับ 1/3/5 กม. + เก็บที่ใกล้สุดต่อหมวด"""
    cats = {k: [] for k in ("rail", "transport", "edu", "health", "life")}
    seen = set()
    for el in elements:
        tags = el.get("tags") or {}
        cat = _poi_category(tags)
        if not cat:
            continue
        if el.get("type") == "node":
            elat, elng = el.get("lat"), el.get("lon")
        else:
            c = el.get("center") or {}
            elat, elng = c.get("lat"), c.get("lon")
        if elat is None or elng is None:
            continue
        name = (tags.get("name:th") or tags.get("name") or tags.get("name:en") or "").strip()
        key = (name, round(elat, 4), round(elng, 4))
        if key in seen:
            continue
        seen.add(key)
        d = _haversine_m(lat, lng, elat, elng)
        if d > _NEARBY_RADIUS:
            continue
        cats[cat].append({"name": name or _poi_subtype(tags), "dist": int(round(d))})
    out = {}
    for cat, items in cats.items():
        items.sort(key=lambda x: x["dist"])
        out[cat] = {
            "c500": sum(1 for i in items if i["dist"] <= 500),
            "c1000": sum(1 for i in items if i["dist"] <= 1000),
            "c3000": len(items),         # ทั้งหมด (<= _NEARBY_RADIUS = 3000)
            "near": items[:40],          # ส่งเยอะขึ้นเพื่อให้แบ่งช่วงระยะได้
        }
    return out


def fetch_nearby(lat: float, lng: float) -> dict:
    """คืนผลวิเคราะห์รอบทรัพย์ (จาก cache หรือยิง Overpass) — {ok, data} """
    import json as _j
    import time
    import urllib.error
    import urllib.parse
    import urllib.request
    ck = f"v4:{round(lat, 3)},{round(lng, 3)}"      # v4 = แยกหมวดรถไฟฟ้า (rail) ออกจากคมนาคม
    if not DEMO_MODE:
        try:
            from core.db import connect
            with connect() as conn:
                row = conn.execute(
                    "select data from poi_nearby_cache where coord_key=%s "
                    "and fetched_at > now() - interval '%s days'",
                    (ck, _POI_CACHE_DAYS)).fetchone()
            if row:
                return {"ok": True, "cached": True, "data": row["data"]}
        except Exception as exc:                                    # noqa: BLE001
            log.warning("อ่าน poi cache ล้มเหลว (รัน migration 042?): %s", str(exc)[:120])
    ql = _overpass_query(lat, lng)
    body = urllib.parse.urlencode({"data": ql}).encode()
    elements = None
    for ep in _OSM_ENDPOINTS:
        try:
            req = urllib.request.Request(ep, data=body, method="POST",
                                         headers={"User-Agent": "plaengdee.com neighborhood/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                elements = _j.loads(r.read().decode("utf-8")).get("elements", [])
            break
        except Exception as exc:                                    # noqa: BLE001
            log.warning("Overpass %s ล้มเหลว: %s", ep, str(exc)[:120])
            continue
    if elements is None:
        return {"ok": False, "message": "ดึงข้อมูลรอบทรัพย์ไม่สำเร็จ ลองใหม่อีกครั้ง"}
    data = _build_nearby(lat, lng, elements)
    if not DEMO_MODE:
        try:
            from core.db import connect
            with connect() as conn:
                conn.execute(
                    "insert into poi_nearby_cache (coord_key, data) values (%s, %s) "
                    "on conflict (coord_key) do update set data=excluded.data, fetched_at=now()",
                    (ck, _j.dumps(data)))
                conn.commit()
        except Exception as exc:                                    # noqa: BLE001
            log.warning("เขียน poi cache ล้มเหลว: %s", str(exc)[:120])
    return {"ok": True, "cached": False, "data": data}


@app.get("/api/nearby")
def api_nearby(lat: str = Query(""), lng: str = Query("")):
    """วิเคราะห์รอบทรัพย์ (POI 1/3/5 กม.) — โหลด async จากหน้าทรัพย์"""
    from fastapi.responses import JSONResponse
    la, lo = _num(lat), _num(lng)
    if la is None or lo is None or not (5.0 < la < 21.0 and 96.0 < lo < 106.0):
        return JSONResponse({"ok": False, "message": "พิกัดไม่ถูกต้อง"}, status_code=400)
    res = fetch_nearby(la, lo)
    return JSONResponse(res, status_code=200 if res.get("ok") else 502,
                        headers={"Cache-Control": "public, max-age=86400"})


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
    import time as _t
    from fastapi.responses import Response
    is_admin = admin_ok(request, token)
    _ck = (tuple(sorted(institution or ())), province or "", district or "", ptype or "",
           max_price or "", hide_critical, min_grade or "", show_special, is_admin)
    _hdr = {"Cache-Control": "public, max-age=600"}
    _now = _t.time()
    _hit = _MAP_CACHE.get(_ck)
    if _hit and _now - _hit[0] < _MAP_TTL:
        return Response(_hit[1], media_type="application/json", headers=_hdr)
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
    import decimal

    def _je(o):                                 # DB คืนค่าเป็น Decimal — แปลงให้ json ได้
        if isinstance(o, decimal.Decimal):
            return float(o)
        return str(o)
    body = json.dumps({"type": "FeatureCollection", "features": feats},
                      ensure_ascii=False, separators=(",", ":"),
                      default=_je).encode("utf-8")
    _MAP_CACHE[_ck] = (_now, body)
    if len(_MAP_CACHE) > 40:                    # กันหน่วยความจำบวม — ทิ้งอันเก่าสุด
        _MAP_CACHE.pop(min(_MAP_CACHE, key=lambda k: _MAP_CACHE[k][0]), None)
    return Response(body, media_type="application/json", headers=_hdr)


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
        "Disallow: /login\n"
        "Disallow: /favorites\n"
        "Disallow: /sell\n"
        "Disallow: /my-listings\n"
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

    # Marketplace — หน้าตลาด + ประกาศสมาชิกที่อนุมัติแล้ว
    urls.append((f"{base_u}/market", None, "daily", "0.6"))
    if not DEMO_MODE:
        try:
            from core.db import connect
            with connect() as conn:
                for r in conn.execute(
                        "select id, updated_at::date as lastmod from member_listings "
                        f"where status='approved' and {MEMBER_ACTIVE_SQL} "
                        "order by last_bumped_at desc limit 5000").fetchall():
                    urls.append((f"{base_u}/m/{r['id']}", r["lastmod"], "weekly", "0.6"))
        except Exception as exc:                                   # noqa: BLE001
            log.warning("sitemap market ล้มเหลว: %s", str(exc)[:100])

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
    line_uid = prof.get("userId")
    if not line_uid:
        raise HTTPException(502, "ไม่ได้ข้อมูลผู้ใช้จาก LINE")
    return _finish_login("line:" + line_uid, prof.get("displayName"),
                         prof.get("pictureUrl"), next_url)


# ---------------------------------------------------------------------
# Google (Gmail) Login — OAuth 2.0 (openid + email + profile)
# ---------------------------------------------------------------------
@app.get("/auth/google/login")
def google_login(request: Request, next: str = Query("/favorites")):
    import secrets
    import urllib.parse
    from fastapi.responses import RedirectResponse
    if not GOOGLE_LOGIN_ENABLED:
        raise HTTPException(503, "ยังไม่ได้เปิดใช้ Google Login")
    if not next.startswith("/"):
        next = "/favorites"
    state = secrets.token_urlsafe(16)
    redirect_uri = (BASE_URL or str(request.base_url).rstrip("/")) + "/auth/google/callback"
    params = {"response_type": "code", "client_id": GOOGLE_CLIENT_ID,
              "redirect_uri": redirect_uri, "state": state,
              "scope": "openid email profile", "access_type": "online",
              "prompt": "select_account"}
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    resp = RedirectResponse(url, status_code=303)
    payload = f"{state}|{next}"
    resp.set_cookie("npa_oauth", f"{payload}.{_sign(payload)}",
                    max_age=600, httponly=True, samesite="lax")
    return resp


@app.get("/auth/google/callback")
def google_callback(request: Request, code: str = Query(""), state: str = Query("")):
    import hmac
    import json as _json
    import urllib.parse
    import urllib.request
    if not GOOGLE_LOGIN_ENABLED:
        raise HTTPException(503, "ยังไม่ได้เปิดใช้ Google Login")
    raw = request.cookies.get("npa_oauth", "")
    if "." not in raw:
        raise HTTPException(400, "เซสชันหมดอายุ ลองเข้าสู่ระบบใหม่")
    payload, sig = raw.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(payload)):
        raise HTTPException(400, "state ไม่ถูกต้อง")
    saved_state, _, next_url = payload.partition("|")
    if not code or not state or state != saved_state:
        raise HTTPException(400, "ยืนยันตัวตนไม่สำเร็จ ลองใหม่อีกครั้ง")
    redirect_uri = (BASE_URL or str(request.base_url).rstrip("/")) + "/auth/google/callback"
    try:
        data = urllib.parse.urlencode({
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri, "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET}).encode()
        treq = urllib.request.Request(
            "https://oauth2.googleapis.com/token", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(treq, timeout=10) as r:
            tok = _json.loads(r.read())
        access_token = tok.get("access_token")
        if not access_token:
            raise ValueError("no access_token")
        ureq = urllib.request.Request(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": "Bearer " + access_token})
        with urllib.request.urlopen(ureq, timeout=10) as r:
            info = _json.loads(r.read())
    except Exception as exc:                                        # noqa: BLE001
        log.warning("Google OAuth ล้มเหลว: %s", str(exc)[:150])
        raise HTTPException(502, "เชื่อมต่อ Google ไม่สำเร็จ ลองใหม่อีกครั้ง")
    sub = info.get("sub")
    if not sub:
        raise HTTPException(502, "ไม่ได้ข้อมูลผู้ใช้จาก Google")
    name = info.get("name") or info.get("email")
    return _finish_login("google:" + sub, name, info.get("picture"), next_url)


@app.get("/login", response_class=HTMLResponse)
def user_login_page(request: Request, next: str = Query("/favorites")):
    from fastapi.responses import RedirectResponse
    if current_user(request):
        return RedirectResponse(next if next.startswith("/") else "/", status_code=303)
    nxt = next if next.startswith("/") else "/favorites"
    return env.get_template("login_user.html").render(
        title="เข้าสู่ระบบ แปลงดี", next_url=nxt, canonical=_abs_url(request, "/login"),
        og_desc="เข้าสู่ระบบเพื่อบันทึกทรัพย์โปรด และรับแจ้งเตือน", **ubase(request))


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
                             "login_url": "/login"}, status_code=401)
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
        return RedirectResponse("/login?next=/favorites", status_code=303)
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
        **ubase(request, fav_pairs=fav_pairs))


# ---------------------------------------------------------------------
# Marketplace — ผู้ใช้ลงประกาศขาย/เช่าเอง (แยกจาก NPA: อยู่ที่ /market, /m/{id})
# ---------------------------------------------------------------------
_MAX_UPLOAD = 6 * 1024 * 1024


def _int(s):
    try:
        return int(float(s)) if s not in (None, "") else None
    except (TypeError, ValueError):
        return None


@app.post("/api/upload-image")
async def api_upload_image(request: Request):
    """อัปโหลดรูป 1 ไฟล์ (raw body — ไม่พึ่ง multipart) → คืน public URL"""
    from fastapi.responses import JSONResponse
    if not current_user(request):
        return JSONResponse({"ok": False, "need_login": True, "login_url": "/login"},
                            status_code=401)
    if not STORAGE_ENABLED:
        return JSONResponse({"ok": False,
                             "message": "ยังไม่ได้ตั้งค่าที่เก็บรูป (Supabase Storage)"},
                            status_code=503)
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype not in _IMG_EXT:
        return JSONResponse({"ok": False, "message": "รองรับเฉพาะ JPG / PNG / WebP"},
                            status_code=422)
    data = await request.body()
    if not data or len(data) > _MAX_UPLOAD:
        return JSONResponse({"ok": False, "message": "ไฟล์ว่างหรือใหญ่เกิน 6MB"},
                            status_code=413)
    pub = upload_image_to_storage(data, ctype)
    if not pub:
        return JSONResponse({"ok": False, "message": "อัปโหลดไม่สำเร็จ ลองใหม่อีกครั้ง"},
                            status_code=502)
    return JSONResponse({"ok": True, "url": pub})


@app.get("/api/_diag/storage")
async def api_diag_storage(request: Request):
    """วินิจฉัย Supabase Storage (admin เท่านั้น) — ลองอัปโหลดรูปเทสต์ 1x1 แล้วรายงานผล"""
    from fastapi.responses import JSONResponse
    if not admin_ok(request, request.query_params.get("token", "")):
        return JSONResponse({"ok": False, "message": "admin only"}, status_code=403)
    import base64
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQAY3Y2wAAAAAElFTkSuQmCC")
    pub = upload_image_to_storage(png, "image/png")
    return JSONResponse({
        "ok": bool(pub),
        "storage_enabled": STORAGE_ENABLED,
        "supabase_url_set": bool(SUPABASE_URL),
        "service_key_set": bool(SUPABASE_SERVICE_KEY),
        "bucket": SUPABASE_BUCKET,
        "test_url": pub,
        "last_error": _LAST_STORAGE_ERR,
    })


@app.get("/api/th-geo.json")
async def api_th_geo():
    """ข้อมูลจังหวัด/อำเภอ/ตำบล สำหรับ dropdown เชื่อมโยงกันในหน้า /sell"""
    from fastapi.responses import Response
    return Response(_TH_GEO_JSON, media_type="application/json; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=604800, immutable"})


@app.post("/api/renovate")
async def api_renovate(request: Request):
    """สร้างภาพจำลองรีโนเวทด้วย AI (Gemini) — เจ้าของประกาศ/แอดมิน · ฟรี 5 รูป/คน"""
    from fastapi.responses import JSONResponse
    uid = current_user(request)
    is_adm = admin_ok(request)
    if not uid and not is_adm:
        return JSONResponse({"ok": False, "need_login": True, "login_url": "/login"},
                            status_code=401)
    if not RENOVATE_ENABLED:
        return JSONResponse({"ok": False,
                             "message": "ยังไม่ได้เปิด AI Renovate (ต้องตั้ง GEMINI_API_KEY + Storage)"},
                            status_code=503)
    try:
        data = await request.json()
    except Exception:                                              # noqa: BLE001
        data = dict(await read_form(request))
    lid = (data.get("listing_id") or "").strip()
    src = (data.get("image_url") or "").strip()
    style = data.get("style") if data.get("style") in RENO_STYLES else "clean"
    if not (lid and src):
        raise HTTPException(422, "ต้องมี listing_id + image_url")
    d = _load_member(lid, allow_for=(uid or None))
    if not d and is_adm:
        d = _load_member(lid, allow_for="*")
    if not d:
        raise HTTPException(404, "ไม่พบประกาศนี้")
    if not is_adm and d.get("posted_by") != uid:
        raise HTTPException(403, "ทำได้เฉพาะเจ้าของประกาศ")
    if src not in (d.get("images") or []):
        raise HTTPException(422, "รูปนี้ไม่อยู่ในประกาศ")
    # quota ฟรี (แอดมินไม่จำกัด)
    if not is_adm:
        used = _reno_used(uid)
        if used >= RENO_FREE_LIMIT:
            return JSONResponse({"ok": False, "quota": True,
                                 "message": f"ใช้สิทธิ์ฟรี {RENO_FREE_LIMIT} รูปหมดแล้ว "
                                            f"— เร็ว ๆ นี้จะมีแพ็กเกจเติมเพิ่ม"},
                                status_code=402)
    img, mime = _fetch_image_bytes(src)
    if not img:
        return JSONResponse({"ok": False, "message": "โหลดรูปต้นทางไม่สำเร็จ"},
                            status_code=502)
    out = gemini_renovate(img, mime, RENO_STYLES[style][1])
    if not out:
        return JSONResponse({"ok": False, "message": "AI สร้างภาพไม่สำเร็จ ลองใหม่อีกครั้ง"},
                            status_code=502)
    out_bytes, out_mime = out
    pub = upload_image_to_storage(out_bytes, out_mime)
    if not pub:
        return JSONResponse({"ok": False, "message": "อัปโหลดผลลัพธ์ไม่สำเร็จ"},
                            status_code=502)
    from core.db import connect
    with connect() as conn:
        conn.execute("insert into member_renovations "
                     "(listing_id, source_url, result_url, style, created_by) "
                     "values (%s, %s, %s, %s, %s)",
                     (lid, src, pub, style, uid or "admin"))
        conn.commit()
    remaining = None if is_adm else max(0, RENO_FREE_LIMIT - _reno_used(uid))
    return JSONResponse({"ok": True, "source": src, "result": pub,
                         "style": RENO_STYLES[style][0], "remaining": remaining})


def _load_member(lid, allow_for=None):
    """คืน dict ประกาศ+รูป; allow_for=uid เจ้าของ(เห็น pending ตัวเอง) หรือ '*'(admin)"""
    if DEMO_MODE:
        return None
    try:
        from core.db import connect
        with connect() as conn:
            row = conn.execute("select * from member_listings where id=%s", (lid,)).fetchone()
            if not row:
                return None
            d = dict(row)
            imgs = conn.execute(
                "select url from member_listing_images where listing_id=%s "
                "order by sort_order, created_at", (lid,)).fetchall()
    except Exception as exc:                                        # noqa: BLE001
        log.warning("โหลดประกาศไม่สำเร็จ: %s", str(exc)[:120])
        return None
    if d["status"] != "approved" and allow_for != "*" and d["posted_by"] != allow_for:
        return None
    d["images"] = [i["url"] for i in imgs]
    d["type_label"] = TYPE_LABELS.get(d.get("property_type"), "อื่น ๆ")
    # ภาพจำลองรีโนเวท AI (ถ้ามี)
    d["renovations"] = []
    try:
        from core.db import connect
        with connect() as conn:
            for rr in conn.execute(
                    "select source_url, result_url, style from member_renovations "
                    "where listing_id=%s order by created_at desc", (lid,)).fetchall():
                d["renovations"].append(dict(rr))
    except Exception:                                              # noqa: BLE001
        pass
    return d


def _member_cards(mrows, conn):
    """แนบรูปหลัก + type_label ให้แถวประกาศ (ใช้ query รูปครั้งเดียว)"""
    ids = [r["id"] for r in mrows]
    imgmap: dict = {}
    if ids:
        for ir in conn.execute(
                "select distinct on (listing_id) listing_id, url "
                "from member_listing_images where listing_id = any(%s) "
                "order by listing_id, sort_order", (ids,)).fetchall():
            imgmap[ir["listing_id"]] = ir["url"]
    out = []
    for r in mrows:
        d = dict(r)
        d["image"] = imgmap.get(d["id"])
        d["type_label"] = TYPE_LABELS.get(d.get("property_type"), "อื่น ๆ")
        out.append(d)
    return out


def member_feed_cards(province=None, district=None, ptype=None,
                      min_price=None, max_price=None, kind=None, limit=6):
    """ประกาศสมาชิก (approved + ยังไม่หมดอายุ) ที่ตรงตัวกรอง — ไปโชว์รวมในหน้าแรก/ค้นหา"""
    if DEMO_MODE:
        return []
    try:
        from core.db import connect
        conds = ["status='approved'", MEMBER_ACTIVE_SQL]
        params: list = []
        if kind in ("sale", "rent"):
            conds.append("listing_kind=%s"); params.append(kind)
        if province:
            conds.append("province=%s"); params.append(province)
        if district:
            conds.append("district=%s"); params.append(district)
        if ptype in TYPE_LABELS:
            conds.append("property_type=%s"); params.append(ptype)
        if min_price is not None:
            conds.append("price >= %s"); params.append(min_price)
        if max_price is not None:
            conds.append("price <= %s"); params.append(max_price)
        where = "where " + " and ".join(conds)
        with connect() as conn:
            mrows = conn.execute(
                f"select * from member_listings {where} "
                f"order by last_bumped_at desc limit {int(limit)}", tuple(params)).fetchall()
            return _member_cards(mrows, conn)
    except Exception as exc:                                        # noqa: BLE001
        log.warning("member_feed_cards ล้มเหลว (รัน migration 038/041?): %s", str(exc)[:120])
        return []


def member_page(province=None, district=None, ptype=None, min_price=None,
                max_price=None, page=1, page_size=24):
    """ประกาศสมาชิก (approved+active) แบบแบ่งหน้า — ใช้ตอนกรอง 'เจ้าของลงเอง' ในหน้าแรก"""
    if DEMO_MODE:
        return [], 0
    try:
        from core.db import connect
        conds = ["status='approved'", MEMBER_ACTIVE_SQL]
        params: list = []
        if province:
            conds.append("province=%s"); params.append(province)
        if district:
            conds.append("district=%s"); params.append(district)
        if ptype in TYPE_LABELS:
            conds.append("property_type=%s"); params.append(ptype)
        if min_price is not None:
            conds.append("price >= %s"); params.append(min_price)
        if max_price is not None:
            conds.append("price <= %s"); params.append(max_price)
        where = "where " + " and ".join(conds)
        off = (max(1, page) - 1) * page_size
        with connect() as conn:
            total = conn.execute(f"select count(*) as n from member_listings {where}",
                                 tuple(params)).fetchone()["n"]
            mrows = conn.execute(
                f"select * from member_listings {where} "
                f"order by last_bumped_at desc limit {int(page_size)} offset {int(off)}",
                tuple(params)).fetchall()
            cards = _member_cards(mrows, conn)
        for c in cards:
            c["is_member"] = True
        return cards, total
    except Exception as exc:                                        # noqa: BLE001
        log.warning("member_page ล้มเหลว (รัน migration 038/041?): %s", str(exc)[:120])
        return [], 0


def _interleave_member(members, npa):
    """สอดการ์ดประกาศเจ้าของแทรกในฟีด NPA (ทุก ๆ 3 ใบ) เพื่อให้ปนกันดูเป็นธรรมชาติ"""
    for m in members:
        m["is_member"] = True
    out: list = []
    mi = 0
    for i, r in enumerate(npa):
        out.append(r)
        if mi < len(members) and (i + 1) % 3 == 0:
            out.append(members[mi]); mi += 1
    out.extend(members[mi:])
    return out


def _annotate_bump(rows):
    """เติมสถานะ หมดอายุ/เหลือกี่วัน/ดันได้ไหม ให้ประกาศสมาชิก (หน้า my-listings)"""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    for r in rows:
        lb = r.get("last_bumped_at")
        if lb is None:
            r["expired"] = False; r["days_left"] = None; r["can_bump"] = True
            continue
        if lb.tzinfo is None:
            lb = lb.replace(tzinfo=timezone.utc)
        age_h = (now - lb).total_seconds() / 3600.0
        age_d = age_h / 24.0
        r["expired"] = age_d > MEMBER_ACTIVE_DAYS
        r["days_left"] = max(0, int(round(MEMBER_ACTIVE_DAYS - age_d)))
        r["can_bump"] = age_h >= MEMBER_BUMP_COOLDOWN_H


@app.get("/sell", response_class=HTMLResponse)
def sell_form(request: Request, edit: str = Query("")):
    from fastapi.responses import RedirectResponse
    uid = current_user(request)
    if not uid:
        return RedirectResponse("/login?next=/sell", status_code=303)
    f: dict = {}
    edit_id = None
    if edit and not DEMO_MODE:
        try:
            from core.db import connect
            with connect() as conn:
                row = conn.execute("select * from member_listings where id = %s",
                                   (edit,)).fetchone()
                if row and (str(row["posted_by"]) == str(uid) or admin_ok(request)):
                    f = dict(row)
                    imgs = conn.execute(
                        "select url from member_listing_images where listing_id = %s "
                        "order by sort_order", (edit,)).fetchall()
                    f["images"] = [i["url"] for i in imgs]
                    edit_id = str(row["id"])
        except Exception as exc:                                     # noqa: BLE001
            log.warning("โหลดประกาศเพื่อแก้ไขไม่สำเร็จ: %s", str(exc)[:120])
    return env.get_template("sell.html").render(
        title=("แก้ไขประกาศ" if edit_id else "ลงประกาศขาย/ให้เช่า ฟรี"),
        storage_enabled=STORAGE_ENABLED, f=f, edit_id=edit_id,
        canonical=_abs_url(request, "/sell"),
        og_desc="ลงประกาศขายหรือให้เช่าอสังหาฯ ฟรีบนแปลงดี", **ubase(request))


@app.post("/sell")
async def sell_submit(request: Request):
    from fastapi.responses import RedirectResponse
    uid = current_user(request)
    if not uid:
        return RedirectResponse("/login?next=/sell", status_code=303)
    if DEMO_MODE:
        raise HTTPException(400, "โหมดตัวอย่าง: ลงประกาศไม่ได้")
    f = await read_form(request)
    title = (f.get("title") or "").strip()[:200]
    if not title:
        raise HTTPException(422, "ต้องกรอกชื่อประกาศ")
    phone = (f.get("contact_phone") or "").strip()[:40]
    if sum(ch.isdigit() for ch in phone) < 9:
        raise HTTPException(422, "ต้องกรอกเบอร์โทรที่ติดต่อได้ (อย่างน้อย 9 หลัก)")
    kind = f.get("listing_kind") if f.get("listing_kind") in ("sale", "rent") else "sale"
    ptype = f.get("property_type") if f.get("property_type") in TYPE_LABELS else None
    pets = f.get("pets_allowed") if f.get("pets_allowed") in ("yes", "no", "ask") else None
    images = [u.strip() for u in (f.get("image_urls") or "").split(",") if u.strip()][:12]
    fields = {
        "posted_by": uid, "listing_kind": kind, "property_type": ptype, "title": title,
        "description": (f.get("description") or "").strip()[:4000] or None,
        "province": (f.get("province") or "").strip()[:60] or None,
        "district": (f.get("district") or "").strip()[:60] or None,
        "subdistrict": (f.get("subdistrict") or "").strip()[:60] or None,
        "address_raw": (f.get("address_raw") or "").strip()[:300] or None,
        "lat": _num(f.get("lat") or ""), "lng": _num(f.get("lng") or ""),
        "price": _num(f.get("price") or ""), "deposit": _num(f.get("deposit") or ""),
        "land_area_sqwa": _num(f.get("land_area_sqwa") or ""),
        "usable_area_sqm": _num(f.get("usable_area_sqm") or ""),
        "bedrooms": _int(f.get("bedrooms")), "bathrooms": _int(f.get("bathrooms")),
        "parking": _int(f.get("parking")),
        "pets_allowed": pets if kind == "rent" else None,
        "contact_name": (f.get("contact_name") or "").strip()[:100] or None,
        "contact_phone": phone or None,
        "contact_line": (f.get("contact_line") or "").strip()[:100] or None,
    }
    edit_id = (f.get("edit_id") or "").strip() or None
    from core.db import connect
    with connect() as conn:
        if edit_id:
            own = conn.execute(
                "select posted_by, status from member_listings where id = %s",
                (edit_id,)).fetchone()
            if not own or (str(own["posted_by"]) != str(uid) and not admin_ok(request)):
                raise HTTPException(403, "แก้ไขได้เฉพาะประกาศของตัวเอง")
            upd = {k: v for k, v in fields.items() if k != "posted_by"}
            # ถ้าเคยถูกปฏิเสธ พอแก้แล้วส่งกลับไปรออนุมัติใหม่อัตโนมัติ
            if own["status"] == "rejected":
                upd["status"] = "pending"
                upd["reject_reason"] = None
            set_sql = ", ".join(f"{k} = %s" for k in upd)
            conn.execute(f"update member_listings set {set_sql} where id = %s",
                         tuple(upd.values()) + (edit_id,))
            conn.execute("delete from member_listing_images where listing_id = %s",
                         (edit_id,))
            for i, u in enumerate(images):
                conn.execute("insert into member_listing_images (listing_id, url, sort_order) "
                             "values (%s, %s, %s)", (edit_id, u, i))
            conn.commit()
            return RedirectResponse("/my-listings?updated=1", status_code=303)
        cols = ", ".join(fields.keys())
        ph = ", ".join(["%s"] * len(fields))
        lid = conn.execute(
            f"insert into member_listings ({cols}) values ({ph}) returning id",
            tuple(fields.values())).fetchone()["id"]
        for i, u in enumerate(images):
            conn.execute("insert into member_listing_images (listing_id, url, sort_order) "
                         "values (%s, %s, %s)", (lid, u, i))
        conn.commit()
    return RedirectResponse("/my-listings?posted=1", status_code=303)


@app.get("/market", response_class=HTMLResponse)
def market(request: Request, kind: str = Query(""), province: str = Query(""),
           district: str = Query(""), ptype: str = Query(""),
           min_price: str = Query(""), max_price: str = Query(""),
           page: int = Query(1, ge=1)):
    rows: list = []
    total = 0
    provinces: list = []
    min_price_v = _num(min_price)
    max_price_v = _num(max_price)
    if not DEMO_MODE:
        try:
            from core.db import connect
            conds = ["status = 'approved'", MEMBER_ACTIVE_SQL]
            params: list = []
            if kind in ("sale", "rent"):
                conds.append("listing_kind = %s"); params.append(kind)
            if province:
                conds.append("province = %s"); params.append(province)
            if district:
                conds.append("district = %s"); params.append(district)
            if ptype in TYPE_LABELS:
                conds.append("property_type = %s"); params.append(ptype)
            if min_price_v is not None:
                conds.append("price >= %s"); params.append(min_price_v)
            if max_price_v is not None:
                conds.append("price <= %s"); params.append(max_price_v)
            where = "where " + " and ".join(conds)
            ps = 24
            off = (page - 1) * ps
            with connect() as conn:
                total = conn.execute(
                    f"select count(*) as n from member_listings {where}",
                    tuple(params)).fetchone()["n"]
                mrows = conn.execute(
                    f"select * from member_listings {where} "
                    f"order by last_bumped_at desc limit {ps} offset {off}",
                    tuple(params)).fetchall()
                rows = _member_cards(mrows, conn)
                provinces = [r["province"] for r in conn.execute(
                    "select distinct province from member_listings "
                    "where status='approved' and province is not null "
                    "order by province").fetchall()]
        except Exception as exc:                                    # noqa: BLE001
            log.warning("market โหลดไม่สำเร็จ (รัน migration 038/041?): %s", str(exc)[:120])
    pages = max(1, -(-total // 24))
    return env.get_template("market.html").render(
        title="ตลาดซื้อ-ขาย-เช่า อสังหาฯ (ประกาศจากผู้ลงเอง)", rows=rows, count=total,
        page=page, pages=pages, kind=kind, province=province, district=district,
        ptype=ptype, min_price=min_price_v, max_price=max_price_v,
        provinces=provinces, canonical=_abs_url(request, "/market"),
        og_desc="ประกาศขาย/ให้เช่าบ้าน ที่ดิน คอนโด จากผู้ลงประกาศโดยตรงบนแปลงดี",
        **ubase(request))


_THAI_MONTHS = ["", "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
                "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"]


def _thai_date(d) -> str:
    if not d:
        return "-"
    return f"{d.day} {_THAI_MONTHS[d.month]} {d.year + 543}"


_AUC_STATS_CACHE: dict = {}
_AUC_STATS_TTL = 1800
# ราคาขายได้ที่ต่ำกว่าประเมินขนาดนี้ = ผิดปกติ (มักเป็นเลขคงที่ 50,000 บนทรัพย์หลายล้าน)
# ตีความว่าเป็นค่าตามขั้นตอน เช่น โจทก์/เจ้าหนี้เป็นผู้ซื้อได้แล้วหักกับหนี้ (หักส่วนได้ใช้แทน)
# ไม่ใช่ราคาตลาดจริง — กันออกจากสถิติส่วนลด และติดป้ายเตือนบนการ์ด
_AUC_OUTLIER_RATIO = 0.20
AUC_RANGES = [(30, "30 วัน"), (90, "90 วัน"), (180, "6 เดือน"), (0, "ทั้งหมด")]


def _med(xs):
    xs = sorted(xs)
    return round(xs[len(xs) // 2]) if xs else None


def _auction_stats(days: int = 0) -> dict:
    """สถิติผลประมูลเชิงกลยุทธ์ กรองช่วงวันได้ (days=0=ทั้งหมด) — cache 30 นาที/ช่วง

    คืน: kpi + by_round + by_type + by_province(top10) + discount distribution + trend รายเดือน
    """
    import time as _t
    import datetime as _dt
    ck = f"d{days}"
    c = _AUC_STATS_CACHE.get(ck)
    if c and _t.time() - c["t"] < _AUC_STATS_TTL:
        return c["data"]

    def _bud(s):
        try:
            return _dt.date(int(s[:4]) - 543, int(s[4:6]), int(s[6:8]))
        except (ValueError, TypeError):
            return None

    def _add(b, sold, disc):
        b["n"] += 1
        if sold:
            b["sold"] += 1
            if disc is not None:
                b["disc"].append(disc)

    def _pack(d, key, label=None, sort=None, top=None):
        items = [{key: (label(k) if label else k), "_k": k, "n": b["n"], "sold": b["sold"],
                  "pct": round(100 * b["sold"] / b["n"]) if b["n"] else 0, "med": _med(b["disc"])}
                 for k, b in d.items()]
        items.sort(key=sort or (lambda x: x["_k"]))
        return items[:top] if top else items

    stats = {"rounds": [], "types": [], "provs": [], "months": [], "dist": [],
             "n": 0, "sold": 0, "pct": 0, "sum_sold": 0, "med": None, "outliers": 0,
             "maxn": 1, "maxn_t": 1, "maxn_p": 1, "dmax": 1, "days": days}
    try:
        from collections import defaultdict
        from core.db import connect
        cond = "ar.matched_ref is not null and ar.appraised_price > 0"
        params: list = []
        if days:
            cond += " and ar.sale_date >= %s"
            params.append(_dt.date.today() - _dt.timedelta(days=days))
        with connect() as conn:
            rows = conn.execute(f"""
                select ar.sale_date, ar.is_sold, ar.sold_price, ar.appraised_price,
                       coalesce(ls.property_type, ls.raw_fields->>'property_type') ptype,
                       coalesce(ls.province, ls.raw_fields->>'province') prov,
                       ls.raw_fields->'_open_post' op
                  from led_auction_results ar
                  join listing_snapshots ls on ls.source_code='led_auction'
                       and ls.external_ref = ar.matched_ref
                 where {cond}
            """, tuple(params)).fetchall()

        R = defaultdict(lambda: {"n": 0, "sold": 0, "disc": []})
        T = defaultdict(lambda: {"n": 0, "sold": 0, "disc": []})
        P = defaultdict(lambda: {"n": 0, "sold": 0, "disc": []})
        M = defaultdict(lambda: {"n": 0, "sold": 0, "disc": []})
        all_disc: list = []
        tn = ts = 0
        n_outlier = 0
        sum_sold = 0.0
        for r in rows:
            op = r["op"] or {}
            rnd = None
            for i in range(1, 9):
                bd = op.get(f"biddate{i}")
                if bd and _bud(bd) == r["sale_date"]:
                    rnd = i
                    break
            disc = None
            outlier = False
            if r["is_sold"] and r["sold_price"] and r["appraised_price"]:
                _ratio = float(r["sold_price"]) / float(r["appraised_price"])
                if _ratio < _AUC_OUTLIER_RATIO:
                    outlier = True                                  # ต่ำผิดปกติ — กันออกจากสถิติส่วนลด
                else:
                    disc = (_ratio - 1) * 100
            tn += 1
            if r["is_sold"]:
                ts += 1
                if outlier:
                    n_outlier += 1
                else:
                    sum_sold += float(r["sold_price"] or 0)
                if disc is not None:
                    all_disc.append(disc)
            if rnd:
                _add(R[rnd], r["is_sold"], disc)
            _add(T[r["ptype"] or "other"], r["is_sold"], disc)
            _add(P[r["prov"] or "ไม่ระบุ"], r["is_sold"], disc)
            _add(M[r["sale_date"].strftime("%Y-%m")], r["is_sold"], disc)

        rounds = _pack(R, "round", sort=lambda x: x["_k"])
        types = _pack(T, "type", label=lambda k: TYPE_LABELS.get(k, k), sort=lambda x: -x["n"])
        provs = _pack(P, "prov", sort=lambda x: -x["n"], top=10)
        months = _pack(M, "ym", sort=lambda x: x["_k"])
        buckets = [(-1e9, -30, "ต่ำกว่าประเมิน >30%", True), (-30, -10, "ต่ำกว่า 10-30%", True),
                   (-10, 0, "ต่ำกว่า 0-10%", True), (0, 10, "สูงกว่าประเมิน 0-10%", False),
                   (10, 1e9, "สูงกว่าประเมิน >10%", False)]
        dist = [{"label": lbl, "n": sum(1 for d in all_disc if lo <= d < hi), "neg": neg}
                for (lo, hi, lbl, neg) in buckets]
        stats = {
            "rounds": rounds, "types": types, "provs": provs, "months": months, "dist": dist,
            "n": tn, "sold": ts, "pct": round(100 * ts / tn) if tn else 0,
            "sum_sold": round(sum_sold), "med": _med(all_disc), "outliers": n_outlier,
            "maxn": max((x["n"] for x in rounds), default=1) or 1,
            "maxn_t": max((x["n"] for x in types), default=1) or 1,
            "maxn_p": max((x["n"] for x in provs), default=1) or 1,
            "dmax": max((x["n"] for x in dist), default=1) or 1,
            "days": days,
        }
    except Exception as exc:                                        # noqa: BLE001
        log.warning("auction stats ล้มเหลว: %s", str(exc)[:150])
    _AUC_STATS_CACHE[ck] = {"t": _t.time(), "data": stats}
    return stats


@app.get("/auction-results", response_class=HTMLResponse)
def auction_results(request: Request, result: str = Query(""),
                    province: str = Query(""), date: str = Query(""),
                    sel_round: int = Query(0, alias="round"), page: int = Query(1, ge=1)):
    """หน้าสรุป "จบประมูล" — ผลการขายทอดตลาด LED (ราคาจบ/ไม่มีผู้สู้ราคา) จัดตามวันขาย"""
    if date and not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        date = ""
    if sel_round not in range(1, 9):
        sel_round = 0
    groups: list = []
    total = 0
    provinces: list = []
    date_opts: list = []
    round_opts: list = []
    stats = {"n": 0, "sold": 0, "sum_sold": 0}
    if not DEMO_MODE:
        try:
            from core.db import connect
            # นัดครั้งที่ = biddateN (พ.ศ. YYYYMMDD) ของทรัพย์ที่จับคู่ ตรงกับวันขาย
            _bexp = "to_char(l.sale_date + interval '543 years','YYYYMMDD')"
            rnd_expr = ("(case "
                        + " ".join(f"when {_bexp}=ls.raw_fields->'_open_post'->>'biddate{i}' then {i}"
                                   for i in range(1, 9))
                        + " end)")
            # ดึงผลทุกแถวตามวันจริง (ไม่ dedupe) รวมรายการที่จับคู่ทรัพย์ไม่ได้ (unmatched) ด้วย
            base = "from led_auction_results l"
            join = ("left join v_listings_with_grade g "
                    "on g.source_code='led_auction' and g.external_ref = l.matched_ref")
            # snapshot ล่าสุดต่อทรัพย์ (สำหรับคำนวณนัด) — กัน fan-out
            jr = ("left join (select distinct on (external_ref) external_ref, raw_fields "
                  "from listing_snapshots where source_code='led_auction' "
                  "order by external_ref, observed_at desc) ls on ls.external_ref = l.matched_ref")
            conds = ["1=1"]
            params: list = []
            if result == "sold":
                conds.append("l.is_sold")
            elif result == "nobid":
                conds.append("not l.is_sold")
            if province:
                conds.append("g.province = %s"); params.append(province)
            if date:
                conds.append("l.sale_date = %s::date"); params.append(date)
            if sel_round:
                conds.append(f"{rnd_expr} = %s"); params.append(sel_round)
            where = " and ".join(conds)
            ps = 60
            off = (page - 1) * ps
            with connect() as conn:
                total = conn.execute(
                    f"select count(*) n {base} {join} {jr} where {where}",
                    tuple(params)).fetchone()["n"]
                rows = [dict(r) for r in conn.execute(f"""
                    select l.matched_ref as ref, l.sale_date, l.result, l.is_sold, l.sold_price,
                           l.appraised_price, l.property_type_th, l.court, l.case_no,
                           l.plaintiff, l.deed, {rnd_expr} as round,
                           g.province, g.district, g.subdistrict, g.property_type, g.grade
                      {base} {join} {jr}
                     where {where}
                     order by l.sale_date desc, l.is_sold desc, l.sold_price desc nulls last
                     limit {ps} offset {off}""", tuple(params)).fetchall()]
                st = conn.execute(
                    "select count(*) n, count(*) filter (where is_sold) sold, "
                    "coalesce(sum(sold_price) filter (where is_sold),0) sum_sold "
                    "from led_auction_results").fetchone()
                stats = dict(st)
                provinces = [r["province"] for r in conn.execute(
                    f"select distinct g.province {base} {join} "
                    f"where g.province is not null order by 1").fetchall()]
                date_opts = [{"iso": r["sale_date"].isoformat(),
                              "label": _thai_date(r["sale_date"]), "n": r["n"]}
                             for r in conn.execute(
                    "select sale_date, count(*) n from led_auction_results "
                    "group by sale_date order by sale_date desc").fetchall()]
                round_opts = [{"round": r["rnd"], "n": r["n"]} for r in conn.execute(
                    f"select {rnd_expr} rnd, count(*) n {base} {jr} "
                    f"group by 1 having {rnd_expr} is not null order by 1").fetchall()]
            # เตรียมข้อมูลแสดง: %ต่างจากประเมิน + จัดกลุ่มตามวันขาย
            for r in rows:
                ap, sp = r.get("appraised_price"), r.get("sold_price")
                r["pct"] = None
                r["suspect"] = False
                if r["is_sold"] and ap and sp:
                    _ratio = float(sp) / float(ap)
                    if _ratio < _AUC_OUTLIER_RATIO:
                        r["suspect"] = True             # ต่ำผิดปกติ — ไม่โชว์ %
                    else:
                        r["pct"] = round((_ratio - 1) * 100)
            from itertools import groupby
            for d, grp in groupby(rows, key=lambda x: x["sale_date"]):
                groups.append({"label": _thai_date(d), "rows": list(grp)})
        except Exception as exc:                                    # noqa: BLE001
            log.warning("auction-results ล้มเหลว (รัน migration 043 + led_results.py?): %s",
                        str(exc)[:150])
    pages = max(1, -(-total // 60))
    return env.get_template("auction_results.html").render(
        title="ผลจบประมูล ทรัพย์ขายทอดตลาด กรมบังคับคดี", groups=groups, count=total,
        page=page, pages=pages, result=result, province=province, provinces=provinces,
        date=date, date_opts=date_opts, sel_round=sel_round, round_opts=round_opts,
        stats=stats, astats=_auction_stats(), canonical=_abs_url(request, "/auction-results"),
        og_desc="ผลการขายทอดตลาดกรมบังคับคดี — ทรัพย์ไหนขายจบที่ราคาเท่าไหร่ หรือไม่มีผู้สู้ราคา ดูตามวันขาย",
        **ubase(request))


@app.get("/upcoming", response_class=HTMLResponse)
def upcoming_auctions(request: Request, province: str = Query(""), date: str = Query(""),
                      ptype: str = Query(""), sel_round: int = Query(0, alias="round"),
                      page: int = Query(1, ge=1)):
    """หน้า "กำลังจะประมูล" — ทรัพย์ LED ที่นัดถัดไปยังไม่ถึง (จาก biddate) จัดตามวันนัดถัดไป (ใกล้สุดก่อน)"""
    import datetime as _dt
    if date and not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        date = ""
    if sel_round not in range(1, 9):
        sel_round = 0
    today = _dt.date.today()
    groups: list = []
    total = 0
    provinces: list = []
    date_opts: list = []
    round_opts: list = []
    types: list = []
    if not DEMO_MODE:
        try:
            from core.db import connect

            def _bud(s):
                try:
                    return _dt.date(int(s[:4]) - 543, int(s[4:6]), int(s[6:8]))
                except (ValueError, TypeError, IndexError):
                    return None

            with connect() as conn:
                raw = conn.execute("""
                    select g.external_ref ref, g.province, g.district, g.subdistrict,
                           g.property_type, g.grade, g.opening_price, g.appraised_price,
                           g.list_price, g.special_price, g.title, g.image_url,
                           g.land_area_sqwa, g.usable_area_sqm,
                           ls.raw_fields->'_open_post' op
                      from v_listings_with_grade g
                      join (select distinct on (external_ref) external_ref, raw_fields
                              from listing_snapshots where source_code='led_auction'
                             order by external_ref, observed_at desc) ls
                        on ls.external_ref = g.external_ref
                     where g.source_code = 'led_auction'
                """).fetchall()
            items: list = []
            for r in raw:
                op = r["op"] or {}
                futs = []
                for i in range(1, 9):
                    v = op.get(f"biddate{i}")
                    dd = _bud(v) if v else None
                    if dd and dd >= today:
                        futs.append((dd, i))
                if not futs:
                    continue
                nd, ni = min(futs)
                it = dict(r); it.pop("op", None)
                it["next_date"] = nd
                it["next_round"] = ni
                it["days_left"] = (nd - today).days
                it["total_rounds"] = sum(1 for i in range(1, 9) if op.get(f"biddate{i}"))
                items.append(it)
            from collections import Counter
            dcnt = Counter(it["next_date"] for it in items)
            date_opts = [{"iso": d.isoformat(), "label": _thai_date(d), "n": n}
                         for d, n in sorted(dcnt.items())]
            rcnt = Counter(it["next_round"] for it in items)
            round_opts = [{"round": rr, "n": rcnt[rr]} for rr in sorted(rcnt)]
            provinces = sorted({it["province"] for it in items if it["province"]})
            tcnt = Counter(it["property_type"] for it in items if it["property_type"])
            types = [{"code": c, "label": TYPE_LABELS.get(c, c), "n": tcnt[c]}
                     for c in sorted(tcnt, key=lambda x: -tcnt[x])]

            def _keep(it):
                if province and it["province"] != province:
                    return False
                if ptype and it["property_type"] != ptype:
                    return False
                if sel_round and it["next_round"] != sel_round:
                    return False
                if date and it["next_date"].isoformat() != date:
                    return False
                return True

            items = [it for it in items if _keep(it)]
            items.sort(key=lambda x: (x["next_date"], -(float(x["opening_price"] or 0))))
            total = len(items)
            ps = 60
            items = items[(page - 1) * ps: page * ps]
            from itertools import groupby
            for d, grp in groupby(items, key=lambda x: x["next_date"]):
                groups.append({"label": _thai_date(d), "days_left": (d - today).days,
                               "rows": list(grp)})
        except Exception as exc:                                    # noqa: BLE001
            log.warning("upcoming ล้มเหลว: %s", str(exc)[:150])
    pages = max(1, -(-total // 60))
    return env.get_template("auction_upcoming.html").render(
        title="กำลังจะประมูล — ทรัพย์ขายทอดตลาด กรมบังคับคดี", groups=groups, count=total,
        page=page, pages=pages, province=province, provinces=provinces,
        date=date, date_opts=date_opts, sel_round=sel_round, round_opts=round_opts,
        ptype=ptype, types=types, type_labels=TYPE_LABELS,
        canonical=_abs_url(request, "/upcoming"),
        og_desc="ทรัพย์ขายทอดตลาดกรมบังคับคดีที่กำลังจะถึงวันประมูล — ราคาเริ่มต้น วันนัด และนับถอยหลัง",
        **ubase(request))


@app.get("/auction-stats", response_class=HTMLResponse)
def auction_stats_page(request: Request, days: int = Query(0)):
    """แดชบอร์ดกลยุทธ์ประมูล — สถิติผลจบประมูลตามนัด/ประเภท/จังหวัด + กระจายส่วนลด (กรองช่วงวัน)"""
    if days not in (0, 30, 90, 180):
        days = 0
    astats = _auction_stats(days) if not DEMO_MODE else {
        "rounds": [], "types": [], "provs": [], "months": [], "dist": [],
        "n": 0, "sold": 0, "pct": 0, "sum_sold": 0, "med": None, "outliers": 0,
        "maxn": 1, "maxn_t": 1, "maxn_p": 1, "dmax": 1, "days": days}
    return env.get_template("auction_stats.html").render(
        title="แดชบอร์ดกลยุทธ์ประมูล — ทรัพย์ขายทอดตลาด", astats=astats, days=days,
        astats_json=json.dumps(astats, ensure_ascii=False),
        ranges=AUC_RANGES, canonical=_abs_url(request, "/auction-stats"),
        og_desc="แดชบอร์ดวิเคราะห์ผลการขายทอดตลาด — รอบนัดไหนคุ้ม ประเภท/จังหวัดไหนส่วนลดดี",
        **ubase(request))


@app.get("/api/market.geojson")
def market_geojson(request: Request, kind: str = Query("")):
    """ประกาศสมาชิกที่อนุมัติ + มีพิกัด → GeoJSON สำหรับหน้าแผนที่ (กรองขาย/เช่า)"""
    from fastapi.responses import JSONResponse
    feats: list = []
    if not DEMO_MODE:
        try:
            from core.db import connect
            conds = ["status='approved'", MEMBER_ACTIVE_SQL,
                     "lat is not null", "lng is not null"]
            params: list = []
            if kind in ("sale", "rent"):
                conds.append("listing_kind=%s"); params.append(kind)
            where = "where " + " and ".join(conds)
            with connect() as conn:
                rows = conn.execute(
                    f"select id, title, price, listing_kind, property_type, lat, lng "
                    f"from member_listings {where} limit 3000", tuple(params)).fetchall()
                ids = [r["id"] for r in rows]
                imgmap: dict = {}
                if ids:
                    for ir in conn.execute(
                            "select distinct on (listing_id) listing_id, url "
                            "from member_listing_images where listing_id = any(%s) "
                            "order by listing_id, sort_order", (ids,)).fetchall():
                        imgmap[ir["listing_id"]] = ir["url"]
            for r in rows:
                feats.append({
                    "type": "Feature",
                    "geometry": {"type": "Point",
                                 "coordinates": [float(r["lng"]), float(r["lat"])]},
                    "properties": {"id": str(r["id"]), "title": r["title"],
                                   "price": float(r["price"]) if r["price"] else None,
                                   "kind": r["listing_kind"],
                                   "type_label": TYPE_LABELS.get(r["property_type"], ""),
                                   "image": imgmap.get(r["id"])}})
        except Exception as exc:                                    # noqa: BLE001
            log.warning("market.geojson ล้มเหลว: %s", str(exc)[:120])
    return JSONResponse({"type": "FeatureCollection", "features": feats})


@app.get("/m/{lid}", response_class=HTMLResponse)
def member_detail(request: Request, lid: str):
    uid = current_user(request)
    d = _load_member(lid, allow_for=(uid or None))
    if not d and admin_ok(request):
        d = _load_member(lid, allow_for="*")
    if not d:
        raise HTTPException(404, "ไม่พบประกาศนี้")
    # เก็บ log ผู้กดดู (ไม่นับเจ้าของดูเอง) + นับยอดวิว 30 วัน
    views_30d = 0
    if not DEMO_MODE and d.get("status") == "approved":
        try:
            from core.db import connect
            with connect() as conn:
                if uid != d.get("posted_by"):
                    conn.execute("insert into member_listing_views "
                                 "(listing_id, viewer_uid) values (%s, %s)", (lid, uid))
                    conn.commit()
                views_30d = conn.execute(
                    "select count(*) as n from member_listing_views where listing_id=%s "
                    "and viewed_at > now() - interval '30 days'", (lid,)).fetchone()["n"]
        except Exception as exc:                                    # noqa: BLE001
            log.warning("บันทึก/นับวิวประกาศไม่สำเร็จ (รัน migration 039?): %s", str(exc)[:100])
    is_adm = admin_ok(request)
    can_renovate = RENOVATE_ENABLED and (is_adm or (uid and uid == d.get("posted_by")))
    reno_remaining = None if is_adm else max(0, RENO_FREE_LIMIT - _reno_used(uid)) if uid else RENO_FREE_LIMIT
    return env.get_template("member_detail.html").render(
        title=d["title"], d=d, views_30d=views_30d, og_title=d["title"],
        og_desc=(d.get("description") or d["title"])[:150],
        og_image=_abs_url(request, d["images"][0]) if d.get("images") else None,
        og_type="product", og_url=_abs_url(request, f"/m/{lid}"),
        can_renovate=can_renovate, reno_remaining=reno_remaining,
        is_owner=bool(is_adm or (uid and uid == d.get("posted_by"))),
        reno_styles=RENO_STYLES, reno_limit=RENO_FREE_LIMIT,
        canonical=_abs_url(request, f"/m/{lid}"), **ubase(request))


@app.get("/my-listings", response_class=HTMLResponse)
def my_listings(request: Request, posted: str = Query(""), updated: str = Query("")):
    from fastapi.responses import RedirectResponse
    uid = current_user(request)
    if not uid:
        return RedirectResponse("/login?next=/my-listings", status_code=303)
    rows: list = []
    if not DEMO_MODE:
        try:
            from core.db import connect
            with connect() as conn:
                mrows = conn.execute(
                    "select * from member_listings where posted_by=%s "
                    "order by created_at desc limit 100", (uid,)).fetchall()
                rows = _member_cards(mrows, conn)
                ids = [r["id"] for r in rows]
                if ids:
                    vmap = {vr["listing_id"]: vr["n"] for vr in conn.execute(
                        "select listing_id, count(*) as n from member_listing_views "
                        "where listing_id = any(%s) and viewed_at > now() - interval '30 days' "
                        "group by listing_id", (ids,)).fetchall()}
                    for r in rows:
                        r["views_30d"] = vmap.get(r["id"], 0)
                _annotate_bump(rows)
        except Exception as exc:                                    # noqa: BLE001
            log.warning("my-listings ล้มเหลว: %s", str(exc)[:120])
    return env.get_template("my_listings.html").render(
        title="ประกาศของฉัน", rows=rows, just_posted=bool(posted), just_updated=bool(updated),
        canonical=_abs_url(request, "/my-listings"),
        og_desc="ประกาศขาย/เช่าของฉันบนแปลงดี", **ubase(request))


@app.post("/api/bump/{lid}")
async def api_bump(request: Request, lid: str):
    """ดันประกาศ/ต่ออายุ — เจ้าของเท่านั้น · เว้นระยะ MEMBER_BUMP_COOLDOWN_H ชม."""
    from fastapi.responses import JSONResponse
    uid = current_user(request)
    if not uid:
        return JSONResponse({"ok": False, "need_login": True}, status_code=401)
    if DEMO_MODE:
        return JSONResponse({"ok": False, "message": "โหมดตัวอย่าง"}, status_code=400)
    try:
        from datetime import datetime, timezone
        from core.db import connect
        with connect() as conn:
            row = conn.execute(
                "select posted_by, status, last_bumped_at from member_listings where id=%s",
                (lid,)).fetchone()
            if not row or row["posted_by"] != uid:
                return JSONResponse({"ok": False, "message": "ไม่พบประกาศ หรือไม่ใช่ของคุณ"},
                                    status_code=404)
            if row["status"] != "approved":
                return JSONResponse({"ok": False, "message": "ดันได้เฉพาะประกาศที่อนุมัติแล้ว"},
                                    status_code=400)
            lb = row["last_bumped_at"]
            if lb is not None:
                if lb.tzinfo is None:
                    lb = lb.replace(tzinfo=timezone.utc)
                age_h = (datetime.now(timezone.utc) - lb).total_seconds() / 3600.0
                if age_h < MEMBER_BUMP_COOLDOWN_H:
                    return JSONResponse({"ok": False, "message": "เพิ่งดันไปแล้ว ลองใหม่วันพรุ่งนี้"},
                                        status_code=429)
            conn.execute("update member_listings set last_bumped_at=now(), updated_at=now() "
                         "where id=%s", (lid,))
            conn.commit()
        return JSONResponse({"ok": True})
    except Exception as exc:                                        # noqa: BLE001
        log.warning("bump ล้มเหลว: %s", str(exc)[:120])
        return JSONResponse({"ok": False, "message": "ดันประกาศไม่สำเร็จ"}, status_code=500)


@app.get("/admin/market", response_class=HTMLResponse)
def admin_market(request: Request, token: str = Query(""), status: str = Query("pending")):
    g = guard(request, token)
    if g:
        return g
    status = status if status in ("pending", "approved", "rejected") else "pending"
    rows: list = []
    if not DEMO_MODE:
        try:
            from core.db import connect
            with connect() as conn:
                mrows = conn.execute(
                    "select * from member_listings where status=%s "
                    "order by created_at desc limit 100", (status,)).fetchall()
                rows = _member_cards(mrows, conn)
        except Exception as exc:                                    # noqa: BLE001
            log.warning("admin_market ล้มเหลว (รัน migration 038?): %s", str(exc)[:120])
    return env.get_template("admin_market.html").render(
        title="อนุมัติประกาศสมาชิก", rows=rows, status=status,
        **base(is_admin=True, admin_token=token))


@app.post("/admin/market/{lid}")
async def admin_market_action(request: Request, lid: str, token: str = Query("")):
    from fastapi.responses import RedirectResponse
    if not admin_ok(request, token):
        raise HTTPException(403, "ต้องเป็นแอดมิน")
    f = await read_form(request)
    action = f.get("action")
    new = {"approve": "approved", "reject": "rejected",
           "pending": "pending"}.get(action)
    if not new:
        raise HTTPException(422, "action ไม่ถูกต้อง")
    from core.db import connect
    with connect() as conn:
        conn.execute("update member_listings set status=%s, reject_reason=%s, "
                     "updated_at=now() where id=%s",
                     (new, (f.get("reject_reason") or None) if new == "rejected" else None,
                      lid))
        conn.commit()
    tk = f"?token={token}" if token else ""
    return RedirectResponse(f"/admin/market{tk}", status_code=303)


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


@app.get("/admin/sources", response_class=HTMLResponse)
def admin_sources(request: Request, token: str = Query(""), msg: str = Query("")):
    """จัดการแหล่งข้อมูล — เปิด/ปิด (is_active) + สถานะสิทธิ์ (ToS)"""
    _require_admin(request, token)
    rows: list = []
    if not DEMO_MODE:
        from core.db import connect
        with connect() as conn:
            rows = [dict(r) for r in conn.execute("""
                select s.code, s.name, s.is_active, s.institution_code, s.notes,
                       i.legal_status,
                       (select count(*) from listing_snapshots ls
                         where ls.source_code = s.code) as n
                  from sources s
                  left join institutions i on i.code = s.institution_code
                 order by s.is_active desc, n desc, s.code""").fetchall()]
    return env.get_template("admin_sources.html").render(
        title="จัดการแหล่งข้อมูล", rows=rows, token=token, msg=msg,
        legal_statuses=LEGAL_STATUSES, **base())


@app.post("/admin/sources/toggle")
async def admin_sources_toggle(request: Request):
    """เปิด/ปิด source — ตอนเปิด ถ้าสถานะสิทธิ์บล็อกอยู่ (unknown/restricted) จะเคลียร์เป็น checked
       (ถือว่าแอดมินยอมรับความเสี่ยง ToS/ลิขสิทธิ์เอง) · prohibited เปิดผ่านปุ่มนี้ไม่ได้"""
    from fastapi.responses import RedirectResponse
    form = await read_form(request)
    _require_admin(request, form.get("token", ""))
    if DEMO_MODE:
        raise HTTPException(400, "โหมดตัวอย่างแก้ไม่ได้")
    code = (form.get("code") or "").strip()
    turn_on = form.get("to") == "on"
    tk = form.get("token", "")
    msg = ""
    from core.db import connect
    with connect() as conn:
        src = conn.execute(
            "select s.institution_code, i.legal_status from sources s "
            "left join institutions i on i.code=s.institution_code where s.code=%s",
            (code,)).fetchone()
        if not src:
            return RedirectResponse(f"/admin/sources?token={tk}&msg=ไม่พบแหล่ง", status_code=303)
        if turn_on:
            if src["legal_status"] == "prohibited":
                return RedirectResponse(
                    f"/admin/sources?token={tk}&msg=แหล่ง {code} สถานะ prohibited เปิดผ่านปุ่มไม่ได้ ต้องเคลียร์สิทธิ์ก่อน",
                    status_code=303)
            # เคลียร์ guard ถ้าสถานะยังบล็อก (แอดมินยอมรับความเสี่ยงเอง)
            if src["legal_status"] in ("unknown", "restricted") and src["institution_code"]:
                conn.execute("update institutions set legal_status='checked' where code=%s",
                             (src["institution_code"],))
            conn.execute("update sources set is_active=true where code=%s", (code,))
            msg = f"เปิด {code} แล้ว (ตั้งสถานะสิทธิ์เป็น checked — ถือว่ายอมรับความเสี่ยง ToS)"
        else:
            conn.execute("update sources set is_active=false where code=%s", (code,))
            msg = f"ปิด {code} แล้ว"
        conn.commit()
    return RedirectResponse(f"/admin/sources?token={tk}&msg={msg}", status_code=303)


LEGAL_STATUSES = ["permitted", "checked", "restricted", "unknown", "prohibited"]


@app.post("/admin/sources/legal")
async def admin_sources_legal(request: Request):
    """ตั้งสถานะสิทธิ์ (legal_status) ของสถาบันที่ผูกกับแหล่ง — มีผลกับทุก source ของสถาบันนั้น"""
    from fastapi.responses import RedirectResponse
    form = await read_form(request)
    _require_admin(request, form.get("token", ""))
    if DEMO_MODE:
        raise HTTPException(400, "โหมดตัวอย่างแก้ไม่ได้")
    code = (form.get("code") or "").strip()
    status = (form.get("status") or "").strip()
    tk = form.get("token", "")
    if status not in LEGAL_STATUSES:
        return RedirectResponse(f"/admin/sources?token={tk}&msg=สถานะไม่ถูกต้อง", status_code=303)
    from core.db import connect
    with connect() as conn:
        src = conn.execute("select institution_code from sources where code=%s", (code,)).fetchone()
        if src and src["institution_code"]:
            conn.execute("update institutions set legal_status=%s where code=%s",
                         (status, src["institution_code"]))
            conn.commit()
            msg = f"ตั้งสถานะสิทธิ์ {code} = {status}"
        else:
            msg = f"{code} ไม่มีสถาบันผูก — แก้สถานะไม่ได้"
    return RedirectResponse(f"/admin/sources?token={tk}&msg={msg}", status_code=303)


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
                      office_name,
                      raw_fields->>'court_name' as court_name,
                      raw_fields->>'deed_no' as deed_no,
                      raw_fields->'_open_post'->>'province_id' as led_pid,
                      raw_fields->'_open_post'->>'province_name' as led_pname,
                      raw_fields->'_open_post'->>'search_bid_date' as led_bdate
                 from (select distinct on (source_code, external_ref)
                              external_ref, province, district, office_name,
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
    for p in pending:                             # เติมจังหวัดจาก "หน่วยงานที่ขาย" ถ้าว่าง
        p["office"] = p.get("office_name")        # หน่วยงานที่ขาย (ไม่ใช่ศาล)
        if _blank(p.get("district")):
            p["district"] = None                  # โชว์ '-' แทน placeholder
        if _blank(p.get("province")):
            g = province_from_texts(p.get("office_name"), p.get("court_name"))
            p["province"] = g or None
            p["province_guessed"] = bool(g)
    return pending, done


@app.get("/admin/parcels", response_class=HTMLResponse)
def admin_parcels(request: Request, token: str = Query(""), saved: bool = Query(False),
                  err: str = Query("")):
    _require_admin(request, token)
    pending, done = _parcel_pending()
    return env.get_template("admin_parcels.html").render(
        title="พิกัดแปลง LED", pending=pending, done=done,
        token=token, saved=saved, err=err, **base())


@app.get("/admin/led/fill-province")
def admin_led_fill_province(request: Request, token: str = Query("")):
    """เติมคอลัมน์ 'จังหวัด' ให้ทรัพย์ LED ที่ยังว่าง โดยเดาจากชื่อศาล/สำนักงานบังคับคดี
    (แก้ทั้งเว็บ: แผนที่/ค้นหา/โซน) — กดครั้งเดียว ทำเฉพาะที่ว่างอยู่"""
    from fastapi.responses import PlainTextResponse
    _require_admin(request, token)
    if DEMO_MODE:
        return PlainTextResponse("โหมดตัวอย่าง")
    scanned = filled = 0
    try:
        from core.db import connect
        with connect() as conn:
            rows = conn.execute(
                """select external_ref, office_name,
                          raw_fields->>'court_name' as court_name
                     from (select distinct on (source_code, external_ref)
                                  external_ref, province, office_name, raw_fields, observed_at
                             from listing_snapshots where source_code='led_auction'
                            order by source_code, external_ref, observed_at desc) s
                    where coalesce(s.province,'') in ('', '-', '0')
                    limit 12000""").fetchall()
            for r in rows:
                scanned += 1
                g = province_from_texts(r["office_name"], r["court_name"])
                if g:
                    conn.execute(
                        "update listing_snapshots set province=%s "
                        "where source_code='led_auction' and external_ref=%s "
                        "and coalesce(province,'') in ('', '-', '0')", (g, r["external_ref"]))
                    filled += 1
            conn.commit()
        _MAP_CACHE.clear()                          # ล้าง cache แผนที่ให้เห็นผลทันที
    except Exception as exc:                        # noqa: BLE001
        return PlainTextResponse(f"ผิดพลาด: {str(exc)[:200]}", status_code=500)
    return PlainTextResponse(
        f"สแกน {scanned} รายการที่จังหวัดว่าง · เติมจังหวัดจากหน่วยงานที่ขายได้ {filled} รายการ\n"
        f"(เหลือ {scanned - filled} ที่เดาไม่ได้)")


@app.get("/admin/led/raw")
def admin_led_raw(request: Request, token: str = Query(""), ref: str = Query("")):
    """ดูข้อมูลดิบของทรัพย์ LED 1 รายการ (admin) — ใช้ตรวจว่าฟิลด์ 'หน่วยงานที่ขาย' อยู่ตรงไหน"""
    from fastapi.responses import JSONResponse
    _require_admin(request, token)
    if DEMO_MODE or not ref:
        return JSONResponse({"note": "ใส่ ?ref=led:xxxxx"}, status_code=400)
    try:
        from core.db import connect
        with connect() as conn:
            row = conn.execute(
                """select province, district, subdistrict, office_name, raw_fields
                     from listing_snapshots
                    where source_code='led_auction' and external_ref=%s
                    order by observed_at desc limit 1""", (ref,)).fetchone()
        if not row:
            return JSONResponse({"note": "ไม่พบ ref นี้"}, status_code=404)
        d = dict(row)
        rf = d.get("raw_fields") or {}
        if isinstance(rf, str):
            rf = json.loads(rf)
        # ดึงเฉพาะฟิลด์ที่เกี่ยวกับ location/หน่วยงาน เพื่ออ่านง่าย
        keys = ["province_name", "city", "deedcity", "ampur", "deedampur",
                "tumbol", "deedtumbol", "court_name", "office_name", "law_court_name",
                "auction_venue", "sale_location1"]
        return JSONResponse({
            "columns": {"province": d["province"], "district": d["district"],
                        "subdistrict": d["subdistrict"], "office_name": d["office_name"]},
            "raw_location_fields": {k: rf.get(k) for k in keys if k in rf},
            "all_raw_keys": sorted(rf.keys()),
        })
    except Exception as exc:                                        # noqa: BLE001
        return JSONResponse({"error": str(exc)[:200]}, status_code=500)


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
