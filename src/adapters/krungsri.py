"""Adapter: กรุงศรี (Krungsri Property) — ทรัพย์ NPA / บ้านมือสองธนาคารกรุงศรีอยุธยา

โครงหน้าเว็บ (ยืนยันจาก DOM จริง 2026-08-29)

  หน้า list:  /search-result?page={n}
      - server-render 10 การ์ด/หน้า, ~167 หน้า (รวม ~1,666 รายการ)
      - ?page= เลื่อนหน้าจริง (page1/page2 คนละชุด ไม่ซ้ำ) — ไม่ใช่ client paginate
      - แต่ละการ์ด = <div class="result-card ..." onclick="_open('BX1453')">
        ฟิลด์ผูกกับ id ท้าย = {hash}_card_{CODE}_{field}:
          name        = "ทาวน์เฮาส์, กรุงเทพมหานคร" (ประเภท, จังหวัด)
          location    = "คลองสามวา, กรุงเทพมหานคร" (อำเภอ/เขต, จังหวัด)
          originalPrice / promoPrice = "7,062,000 บาท" / "3,700,000 บาท"
          bedRoom / bathRoom = จำนวนห้อง
          coverImage  = <img src="/images/...">
      - เนื้อที่อยู่ใน .result-card-propertysize = "0 ไร่ 0 งาน 20.1 ตร.ว."

  หน้า detail: /detail?code={CODE}
      - พิกัดจริงฝังใน JS: window.open("...google.com/maps?q=${lat},${lng}")
        (ตัวเลขจริงถูก render ลงไปแล้ว) → เติมด้วย enrich.py krungsri-coords (parcel)
      - ไม่มี PII ลูกหนี้

external_ref = krungsri:{CODE}   (CODE = รหัสในปุ่ม _open('...') เช่น BX1453)

หมายเหตุ ToS/PDPA: krungsriproperty.com เป็นของธนาคารกรุงศรีฯ ควรอ่าน ToS/PDPA
และพิจารณาขอพันธมิตร/นายหน้า ก่อนเปิดใช้เชิงพาณิชย์ (is_active=false ไว้ก่อน)
"""
from __future__ import annotations

import logging
import re
import time
from typing import Iterable, Iterator

import requests
from bs4 import BeautifulSoup

from core.base_adapter import BaseAdapter
from core.http import Response

log = logging.getLogger(__name__)

BASE = "https://www.krungsriproperty.com"

# ประเภท (คำไทยขึ้นต้นในชื่อการ์ด) -> รหัสภายในโปรเจกต์ — เรียงยาวก่อนสั้น
TYPE_MAP = [
    ("ทาวน์เฮาส์", "townhouse"), ("ทาวน์เฮ้าส์", "townhouse"), ("ทาวน์โฮม", "townhouse"),
    ("บ้านเดี่ยว", "house"), ("บ้านแฝด", "house"), ("บ้านพัก", "house"),
    ("อาคารพาณิชย์", "commercial"), ("ตึกแถว", "commercial"), ("สำนักงาน", "commercial"),
    ("โรงงาน", "industrial"), ("โกดัง", "industrial"), ("คลังสินค้า", "industrial"),
    ("คอนโด", "condo"), ("ห้องชุด", "condo"), ("อาคารชุด", "condo"), ("แฟลต", "condo"),
    ("ที่ดิน", "land"),
]

_OPEN_RE = re.compile(r"_open\(['\"]([^'\"]+)['\"]\)")
_PRICE_RE = re.compile(r"([\d,]+)")


def _num(s: str | None) -> float | None:
    if not s:
        return None
    m = _PRICE_RE.search(s)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
        return v if v > 0 else None
    except ValueError:
        return None


def _num_before(text: str, label: str) -> float | None:
    m = re.search(r"([\d,]+\.?\d*)\s*" + re.escape(label), text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


class KrungsriAdapter(BaseAdapter):
    source_code = "krungsri"
    parser_version = "0.1.0"

    SEARCH_PATH = "/search-result"

    def discover(self) -> Iterator[dict]:
        """ไล่ทีละหน้า ?page= (10 การ์ด/หน้า) — ทรัพย์ทั้งประเทศในลิสต์เดียว"""
        max_pages = self.config.get("max_pages", 30)
        for page in range(1, max_pages + 1):
            yield {
                "url": BASE + self.SEARCH_PATH,
                "method": "GET",
                "params": {"page": str(page)},
                "meta": {"page": page},
            }

    def parse(self, resp: Response, task: dict) -> Iterable[dict]:
        soup = BeautifulSoup(resp.text, "html.parser")
        seen: set[str] = set()
        for card in soup.select("a.result-card"):
            code = self._code(card)
            if not code:
                continue
            ref = f"krungsri:{code}"
            if ref in seen:
                continue
            seen.add(ref)

            title = self._field(card, code, "name")              # ชื่อโครงการ (บางที = ประเภท+จังหวัด)
            type_th = self._field(card, code, "type") or ""      # ประเภท (ฟิลด์แยก เชื่อถือได้กว่าชื่อ)
            loc = self._field(card, code, "location") or ""      # "อำเภอ/เขต, จังหวัด"
            prov = dist = None
            if loc:
                bits = [b.strip() for b in loc.split(",") if b.strip()]
                if bits:
                    dist = bits[0]
                    if len(bits) > 1:
                        prov = bits[-1]
            if not prov and title and "," in title:              # fallback จังหวัดจากชื่อ
                prov = title.split(",")[-1].strip()

            original = _num(self._field(card, code, "originalPrice"))
            promo = _num(self._field(card, code, "promoPrice"))
            price = promo or original

            size_txt = ""
            sz = card.select_one(".result-card-propertysize")
            if sz:
                size_txt = sz.get_text(" ", strip=True)
            rai = _num_before(size_txt, "ไร่")
            ngan = _num_before(size_txt, "งาน")
            wa = _num_before(size_txt, "ตร.ว.")
            land_sqwa = (rai or 0) * 400 + (ngan or 0) * 100 + (wa or 0)
            sqm = _num_before(size_txt, "ตร.ม.")

            yield {
                "external_ref": ref,
                "listing_id": code,
                "detail_url": f"{BASE}/detail?code={code}",
                "title": (title or None),
                "property_type": self._map_type(type_th),
                "province": prov,
                "district": dist,
                "address_raw": " ".join(x for x in [dist, prov] if x) or None,
                "land_area_sqwa": round(land_sqwa, 2) if land_sqwa else None,
                "usable_area_sqm": sqm,
                "bedrooms": self._int(self._field(card, code, "bedRoom")),
                "bathrooms": self._int(self._field(card, code, "bathRoom")),
                "opening_price": price,
                "list_price": original,
                "special_price": promo if (promo and original and promo < original) else None,
                "image_url": self._image(card, code),
                "krungsri_code": code,
                "_source_url": resp.url,
            }

    # ------------------------------------------------------------------
    @staticmethod
    def _code(card) -> str | None:
        m = _OPEN_RE.search(card.get("onclick", "") or "")
        if m:
            return m.group(1)
        # fallback: จาก id ที่มี _card_{CODE}_
        el = card.find(id=re.compile(r"_card_([^_]+)_"))
        if el:
            mm = re.search(r"_card_([^_]+)_", el.get("id", ""))
            if mm:
                return mm.group(1)
        return None

    @staticmethod
    def _field(card, code: str, suffix: str) -> str | None:
        el = card.find(id=re.compile(r"_card_" + re.escape(code) + r"_" + suffix + r"$"))
        if not el:
            return None
        return el.get_text(" ", strip=True) or None

    @staticmethod
    def _int(s: str | None) -> int | None:
        if not s:
            return None
        m = re.search(r"\d+", s)
        return int(m.group()) if m else None

    @staticmethod
    def _map_type(text: str) -> str:
        for label, code in TYPE_MAP:
            if label in (text or ""):
                return code
        return "other"

    def _image(self, card, code: str) -> str | None:
        img = card.find(id=re.compile(r"_card_" + re.escape(code) + r"_coverImage$"))
        if not img:
            img = card.select_one("img.result-card-image")
        if not img:
            return None
        src = img.get("src") or img.get("data-src")
        if not src:
            return None
        if src.startswith("//"):
            return "https:" + src
        if src.startswith("/"):
            return BASE + src
        return src if src.startswith("http") else None


# =====================================================================
# ดึงพิกัดจริงจากหน้า detail — ใช้โดย enrich.py krungsri-coords
#   /detail?code={CODE} ฝังพิกัดใน JS: google.com/maps?q=${lat},${lng}
#   (ตัวเลขจริงถูก render ลงไปแล้ว) → พิกัดระดับ 'parcel'
# =====================================================================
KRUNGSRI_LATLNG = re.compile(r"maps\?q=\$?\{?(-?\d{1,2}\.\d{3,})\}?,\s*\$?\{?(\d{2,3}\.\d{3,})\}?")
TH_BOUNDS = (5.5, 20.5, 97.0, 106.0)

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "th,en;q=0.8",
}


def fetch_krungsri_detail(url: str, session: requests.Session | None = None) -> dict:
    """คืน {'lat','lng'} จากหน้า detail กรุงศรี (ว่าง = ดึง/หาไม่ได้) — ไม่ throw"""
    s = session or requests
    try:
        resp = s.get(url, headers=_HEADERS, timeout=25)
        resp.encoding = "utf-8"
        html = resp.text
    except Exception as exc:                                   # noqa: BLE001
        log.warning("ดึงหน้า กรุงศรี ไม่สำเร็จ %s — %s", url, exc)
        return {}
    out: dict = {}
    m = KRUNGSRI_LATLNG.search(html)
    if m:
        try:
            lat, lng = float(m.group(1)), float(m.group(2))
            if (TH_BOUNDS[0] <= lat <= TH_BOUNDS[1]
                    and TH_BOUNDS[2] <= lng <= TH_BOUNDS[3]):
                out["lat"], out["lng"] = lat, lng
        except ValueError:
            pass
    time.sleep(0.4)
    return out
