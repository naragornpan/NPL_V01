"""LandsMaps (กรมที่ดิน) — พิกัดแปลงจริงจากเลขโฉนด

ทำไมต้องมี
    Nominatim ให้ได้แค่จุดกึ่งกลางตำบล/จังหวัด (หยาบ) ทรัพย์ที่มี "เลขโฉนด"
    เอาไปค้น LandsMaps ได้พิกัดแปลงจริงระดับเมตร (parcellat/parcellon)

Flow (นิรนาม — ไม่ต้องล็อกอิน/ไม่ต้องมี JWT ของตัวเอง)  ยืนยันจริง 2026-08-24
    1. GET  /apiService/JWT/GetJWTAccessToken           -> result[0].access_token
    2. GET  /apiService/LandsMaps/GetParcelByParcelNo/{pvcode}/{amcode}/{deedno}
            header  Authorization: Bearer <token>
            -> result[0].parcellat / parcellon  (+ landprice, rai/ngan/wa)

    รหัสจังหวัด = เลข 2 หลักมาตรฐานกรมการปกครอง (กทม.=10)
    รหัสอำเภอ  = โหลดจาก /data/amphur.json  คีย์ด้วย (pvcode, ชื่ออำเภอไทย)

ข้อควรระวัง
    - เว็บที่ 3 (กรมที่ดิน) มี ToS ของตัวเอง — หน่วงคำขอให้สุภาพ (>=1.5 วิ)
    - ใช้ได้เฉพาะทรัพย์ที่ "มีเลขโฉนด" (ที่ดิน/บ้าน+ที่ดิน/คอนโดที่มีโฉนดที่ดิน)
      ทรัพย์ไม่มีโฉนด (น.ส.3ก. ฯลฯ) ทำไม่ได้ ต้องอยู่ระดับตำบลต่อไป
"""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

BASE = "https://landsmaps.dol.go.th"
_UA = {"User-Agent": "npa-ingest/0.1 (personal research; land parcel lookup)",
       "Accept": "application/json"}
_MIN_INTERVAL = 1.6                 # วินาที ระหว่างคำขอ — สุภาพกับเว็บราชการ
_TIMEOUT = 25

# รหัสจังหวัด 2 หลัก (กรมการปกครอง) = pvcode ของ LandsMaps
PROVINCE_CODES = {
    "กรุงเทพมหานคร": "10", "สมุทรปราการ": "11", "นนทบุรี": "12", "ปทุมธานี": "13",
    "พระนครศรีอยุธยา": "14", "อ่างทอง": "15", "ลพบุรี": "16", "สิงห์บุรี": "17",
    "ชัยนาท": "18", "สระบุรี": "19",
    "ชลบุรี": "20", "ระยอง": "21", "จันทบุรี": "22", "ตราด": "23",
    "ฉะเชิงเทรา": "24", "ปราจีนบุรี": "25", "นครนายก": "26", "สระแก้ว": "27",
    "นครราชสีมา": "30", "บุรีรัมย์": "31", "สุรินทร์": "32", "ศรีสะเกษ": "33",
    "อุบลราชธานี": "34", "ยโสธร": "35", "ชัยภูมิ": "36", "อำนาจเจริญ": "37",
    "บึงกาฬ": "38", "หนองบัวลำภู": "39",
    "ขอนแก่น": "40", "อุดรธานี": "41", "เลย": "42", "หนองคาย": "43",
    "มหาสารคาม": "44", "ร้อยเอ็ด": "45", "กาฬสินธุ์": "46", "สกลนคร": "47",
    "นครพนม": "48", "มุกดาหาร": "49",
    "เชียงใหม่": "50", "ลำพูน": "51", "ลำปาง": "52", "อุตรดิตถ์": "53",
    "แพร่": "54", "น่าน": "55", "พะเยา": "56", "เชียงราย": "57", "แม่ฮ่องสอน": "58",
    "นครสวรรค์": "60", "อุทัยธานี": "61", "กำแพงเพชร": "62", "ตาก": "63",
    "สุโขทัย": "64", "พิษณุโลก": "65", "พิจิตร": "66", "เพชรบูรณ์": "67",
    "ราชบุรี": "70", "กาญจนบุรี": "71", "สุพรรณบุรี": "72", "นครปฐม": "73",
    "สมุทรสาคร": "74", "สมุทรสงคราม": "75", "เพชรบุรี": "76", "ประจวบคีรีขันธ์": "77",
    "นครศรีธรรมราช": "80", "กระบี่": "81", "พังงา": "82", "ภูเก็ต": "83",
    "สุราษฎร์ธานี": "84", "ระนอง": "85", "ชุมพร": "86",
    "สงขลา": "90", "สตูล": "91", "ตรัง": "92", "พัทลุง": "93",
    "ปัตตานี": "94", "ยะลา": "95", "นราธิวาส": "96",
}

_last_call = 0.0
_token: str | None = None
_amphur_map: dict[tuple[str, str], str] | None = None   # (pvcode, ชื่ออำเภอ) -> amcode


def _throttle() -> None:
    global _last_call
    gap = time.monotonic() - _last_call
    if gap < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - gap)
    _last_call = time.monotonic()


def _num(v) -> float | None:
    try:
        return float(str(v).replace(",", "")) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _load_amphur() -> dict[tuple[str, str], str]:
    """โหลด amphur.json (ครั้งเดียวต่อรัน) -> {(pvcode, ชื่ออำเภอไทย): amcode}"""
    global _amphur_map
    if _amphur_map is not None:
        return _amphur_map
    _throttle()
    data = requests.get(f"{BASE}/data/amphur.json", headers=_UA, timeout=_TIMEOUT).json()
    rows = data.get("result") if isinstance(data, dict) else data
    m: dict[tuple[str, str], str] = {}
    for r in rows or []:
        pv = str(r.get("pvcode") or "").strip()
        am = str(r.get("amcode") or "").strip()
        name = (r.get("amnamethai") or "").strip()
        if pv and am and am != "00" and name:
            m[(pv, name)] = am
    _amphur_map = m
    log.info("โหลด amphur.json สำเร็จ %s อำเภอ", len(m))
    return m


def _get_token(force: bool = False) -> str | None:
    global _token
    if _token and not force:
        return _token
    _throttle()
    try:
        j = requests.get(f"{BASE}/apiService/JWT/GetJWTAccessToken",
                         headers=_UA, timeout=_TIMEOUT).json()
        _token = j["result"][0]["access_token"]
        return _token
    except Exception as exc:                                  # noqa: BLE001
        log.warning("ขอ JWT token ไม่สำเร็จ: %s", exc)
        return None


def _amcode(pvcode: str, district: str) -> str | None:
    """หา amcode จากชื่ออำเภอ (ลองแบบตรง ๆ ก่อน แล้วลองตัดช่องว่าง)"""
    m = _load_amphur()
    d = (district or "").strip()
    return m.get((pvcode, d)) or m.get((pvcode, d.replace(" ", "")))


def parcel_coords(province: str | None, district: str | None,
                  deed_no: str | None) -> dict | None:
    """คืนพิกัดแปลงจริงจากเลขโฉนด หรือ None

    คืน {lat, lng, land_price, rai, ngan, wa} — ค่าที่หาไม่เจอเป็น None
    """
    if not (province and district and deed_no):
        return None
    deed = str(deed_no).strip()
    if not deed or deed in ("-", "0"):
        return None

    pvcode = PROVINCE_CODES.get(province.strip())
    if not pvcode:
        return None
    try:
        amcode = _amcode(pvcode, district)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("โหลด amphur ไม่สำเร็จ: %s", exc)
        return None
    if not amcode:
        return None

    if not _get_token():
        return None

    url = f"{BASE}/apiService/LandsMaps/GetParcelByParcelNo/{pvcode}/{amcode}/{deed}"
    for attempt in range(2):
        _throttle()
        try:
            resp = requests.get(url, headers={**_UA, "Authorization": f"Bearer {_token}"},
                                timeout=_TIMEOUT)
        except Exception as exc:                              # noqa: BLE001
            log.warning("ยิง LandsMaps ไม่สำเร็จ (%s): %s", deed, exc)
            return None
        if resp.status_code in (401, 403) and attempt == 0:
            _get_token(force=True)                            # token หมดอายุ ขอใหม่
            continue
        try:
            j = resp.json()
        except ValueError:
            return None
        res = j.get("result") or []
        if not res:
            return None
        p = res[0]
        lat, lng = _num(p.get("parcellat")), _num(p.get("parcellon"))
        # กันค่าเพี้ยน: ต้องอยู่ในกรอบประเทศไทย
        if lat and lng and 5.5 <= lat <= 20.5 and 97.0 <= lng <= 106.0:
            return {"lat": lat, "lng": lng,
                    "land_price": _num(p.get("landprice")),
                    "rai": _num(p.get("rai")), "ngan": _num(p.get("ngan")),
                    "wa": _num(p.get("wa"))}
        return None
    return None
