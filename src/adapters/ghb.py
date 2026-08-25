"""Adapter: ธอส. (GHB Home Center) — ทรัพย์ NPA / บ้านมือสองธนาคาร

╔══════════════════════════════════════════════════════════════════════╗
║  ก่อนเปิดใช้งานจริง (is_active = false ไว้ก่อน)                       ║
║  ตรวจแล้ว 2026-08-24                                                  ║
║    robots.txt: "User-agent: * / Disallow:" (ว่าง = อนุญาตทุกอย่าง)   ║
║    ไม่พบข้อความห้ามนำข้อมูลไปใช้บนหน้าที่ตรวจ (การ์ด/รายละเอียด)      ║
║  แต่ยังต้องอ่าน ToS ฉบับเต็ม + สมัครนายหน้า/พันธมิตร ธอส. ก่อน        ║
║  เปิดใช้ (ปลดล็อกด้วย migration แบบ 021 หลังเคลียร์สิทธิ์)            ║
╚══════════════════════════════════════════════════════════════════════╝

โครงหน้าเว็บ (ยืนยันจาก DOM จริง 2026-08-24)

  หน้า list:  /property-grid-for-sale/{ProvinceEng}?pg={n}
      - server-render ~20 การ์ด/หน้า กรองด้วยจังหวัด (English slug ในพาธ)
      - แบ่งหน้าจริงด้วย ?pg=  (ยืนยัน page 1/2/3 คนละชุด — ต่างจาก ?page=
        /?p=/?pageSize= ที่เว็บ "ไม่สน" คืนหน้าแรกเสมอ)
      - แต่ละการ์ด = <a href="/property-{ID}"> + ข้อความรูปแบบ
          "ทรัพย์โปรโมชั่น {ราคา} บาท ... ขาย{ประเภท} ({โครงการ})
           ต.{ตำบล} อ.{อำเภอ} จ.{จังหวัด} {เนื้อที่} รหัสทรัพย์ {code} ..."
      - รูป: /v3/property/api/Media/{mediaId}-380-280 (absolute)

  หน้า detail: /property-{ID}
      - มีพิกัดจริงฝังในลิงก์ <a href="...google.com/maps?q={lat},{lng}">
        -> เติมภายหลังด้วย enrich.py ghb-coords (ระดับ 'parcel')
      - ไม่มี PII ลูกหนี้

external_ref = ghb:{ID}
    ID = เลขใน URL /property-{ID} (คงที่ ผูกกับหน้า detail)
    ต่างจาก "รหัสทรัพย์" ที่โชว์บนการ์ด — เก็บรหัสนั้นไว้ใน raw_fields.ghb_code

ทำไม list มีข้อมูลพอ ไม่ต้องเข้า detail ทุกตัวตอน ingest
    การ์ดมี ราคา/ประเภท/ตำบล/อำเภอ/จังหวัด/เนื้อที่/รูป ครบพอทำ snapshot
    เหลือแค่ "พิกัดจริง" ที่อยู่หน้า detail — แยกไปเป็น enrichment (ghb-coords)
    จะได้ ingest เร็ว และไม่ยิง detail 3 หมื่นหน้าทุกวัน
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

# ชื่อจังหวัดไทย -> slug อังกฤษที่ ธอส ใช้ในพาธ (ดึงจาก <option rel="/{slug}">
# ของ dropdown จังหวัดบนเว็บจริง 2026-08-24 — ครบ 77 จังหวัด)
PROVINCE_SLUGS = {
    "กระบี่": "Krabi", "กรุงเทพมหานคร": "Bangkok", "กาญจนบุรี": "Kanchanaburi",
    "กาฬสินธุ์": "Kalasin", "กำแพงเพชร": "KamphaengPhet", "ขอนแก่น": "KhonKaen",
    "จันทบุรี": "Chanthaburi", "ฉะเชิงเทรา": "Chachoengsao", "ชลบุรี": "ChonBuri",
    "ชัยนาท": "ChaiNat", "ชัยภูมิ": "Chaiyaphum", "ชุมพร": "Chumphon",
    "เชียงราย": "ChiangRai", "เชียงใหม่": "ChiangMai", "ตรัง": "Trang",
    "ตราด": "Trat", "ตาก": "Tak", "นครนายก": "NakhonNayok",
    "นครปฐม": "NakhonPathom", "นครพนม": "NakhonPhanom", "นครราชสีมา": "NakhonRatchasima",
    "นครศรีธรรมราช": "NakhonSiThammarat", "นครสวรรค์": "NakhonSawan", "นนทบุรี": "Nonthaburi",
    "นราธิวาส": "Narathiwat", "น่าน": "Nan", "บึงกาฬ": "BuengKan",
    "บุรีรัมย์": "BuriRam", "ปทุมธานี": "PathumThani", "ประจวบคีรีขันธ์": "PrachuapKhiriKhan",
    "ปราจีนบุรี": "PrachinBuri", "ปัตตานี": "Pattani", "พระนครศรีอยุธยา": "PhraNakhonSiAyutthaya",
    "พะเยา": "Phayao", "พังงา": "Phangnga", "พัทลุง": "Phatthalung",
    "พิจิตร": "Phichit", "พิษณุโลก": "Phitsanulok", "เพชรบุรี": "Phetchaburi",
    "เพชรบูรณ์": "Phetchabun", "แพร่": "Phrae", "ภูเก็ต": "Phuket",
    "มหาสารคาม": "MahaSarakham", "มุกดาหาร": "Mukdahan", "แม่ฮ่องสอน": "MaeHongSon",
    "ยโสธร": "Yasothon", "ยะลา": "Yala", "ร้อยเอ็ด": "RoiEt",
    "ระนอง": "Ranong", "ระยอง": "Rayong", "ราชบุรี": "Ratchaburi",
    "ลพบุรี": "LopBuri", "ลำปาง": "Lampang", "ลำพูน": "Lamphun",
    "เลย": "Loei", "ศรีสะเกษ": "SiSaKet", "สกลนคร": "SakonNakhon",
    "สงขลา": "Songkhla", "สตูล": "Satun", "สมุทรปราการ": "SamutPrakan",
    "สมุทรสงคราม": "SamutSongkhram", "สมุทรสาคร": "SamutSakhon", "สระแก้ว": "SaKaeo",
    "สระบุรี": "Saraburi", "สิงห์บุรี": "SingBuri", "สุโขทัย": "Sukhothai",
    "สุพรรณบุรี": "SuphanBuri", "สุราษฎร์ธานี": "SuratThani", "สุรินทร์": "Surin",
    "หนองคาย": "NongKhai", "หนองบัวลำภู": "NongBuaLamPhu", "อ่างทอง": "AngThong",
    "อำนาจเจริญ": "AmnatCharoen", "อุดรธานี": "UdonThani", "อุตรดิตถ์": "Uttaradit",
    "อุทัยธานี": "UthaiThani", "อุบลราชธานี": "UbonRatchathani",
}

# ประเภททรัพย์บนการ์ด (ขึ้นต้น "ขาย...") -> รหัสภายในโปรเจกต์
# เรียงยาวก่อนสั้น กัน "ที่ดิน" ไปจับก่อน "ที่ดินพร้อม..."
TYPE_LABEL_MAP = [
    ("บ้านเดี่ยว", "house"),
    ("บ้านแฝด", "house"),
    ("ทาวน์เฮ้าส์", "townhouse"),
    ("ทาวน์เฮาส์", "townhouse"),
    ("ทาวน์โฮม", "townhouse"),
    ("อาคารพาณิชย์", "commercial"),
    ("ตึกแถว", "commercial"),
    ("สำนักงาน", "commercial"),
    ("โรงงาน", "industrial"),
    ("โกดัง", "industrial"),
    ("คอนโด", "condo"),
    ("ห้องชุด", "condo"),
    ("แฟลต", "condo"),
    ("ที่ดิน", "land"),
]

LINK_RE = re.compile(r"/property-(\d+)")
CODE_RE = re.compile(r"รหัสทรัพย์\s*(\d+)")
PRICE_RE = re.compile(r"([\d,]+)\s*บาท")
TYPE_RE = re.compile(r"ขาย([ก-๙A-Za-z]+)")
PROJECT_RE = re.compile(r"\(([^()]{2,80})\)")
# ที่ตั้ง: การ์ดต่างจังหวัดใช้ ต./อ./จ. — กทม.อาจใช้ แขวง/เขต
SUBDIST_RE = re.compile(r"(?:ต\.|แขวง)\s*([ก-๙A-Za-z0-9]+)")
DISTRICT_RE = re.compile(r"(?:อ\.|เขต)\s*([ก-๙A-Za-z0-9]+)")
PROV_RE = re.compile(r"จ\.\s*([ก-๙A-Za-z]+)")


def _num_before(text: str, label: str) -> float | None:
    m = re.search(r"([\d,]+\.?\d*)\s*" + re.escape(label), text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


class GhbAdapter(BaseAdapter):
    source_code = "ghb"
    parser_version = "0.1.0"

    BASE = "https://www.ghbhomecenter.com"
    GRID_PATH = "/property-grid-for-sale/{slug}"   # ทุกประเภทในจังหวัดเดียว

    def discover(self) -> Iterator[dict]:
        """ไล่ทีละจังหวัด ทีละหน้า (?pg=)

        แยกตามจังหวัดเหมือน BAM: หยุดเร็วเมื่อหมดหน้า และถ้าจังหวัดหนึ่งพัง
        จังหวัดอื่นยังได้ข้อมูล  ใช้ ?pg= ที่ยืนยันแล้วว่าเลื่อนหน้าได้จริง
        """
        provinces = self.config.get("provinces") or ["กรุงเทพมหานคร"]
        max_pages = self.config.get("max_pages", 30)

        for name in provinces:
            slug = PROVINCE_SLUGS.get(name)
            if not slug:
                log.warning("ยังไม่มี slug จังหวัด %s ของ ธอส — ข้าม", name)
                continue
            path = self.GRID_PATH.format(slug=slug)
            for page in range(1, max_pages + 1):
                yield {
                    "url": self.BASE + path,
                    "method": "GET",
                    "params": {"pg": str(page)},
                    "meta": {"province": name, "page": page},
                }

    def parse(self, resp: Response, task: dict) -> Iterable[dict]:
        soup = BeautifulSoup(resp.text, "html.parser")
        prov_th = task["meta"].get("province")
        seen: set[str] = set()

        for a in soup.find_all("a", href=LINK_RE):
            m = LINK_RE.search(a.get("href", ""))
            if not m:
                continue
            pid = m.group(1)
            ref = f"ghb:{pid}"
            if ref in seen:
                continue

            card = self._card_container(a)
            text = (card.get_text(" ", strip=True) if card
                    else a.get_text(" ", strip=True))
            # การ์ดจริงต้องมีรหัสทรัพย์และราคา ไม่งั้นเป็นลิงก์รูป/ปุ่ม
            if "รหัสทรัพย์" not in text or "บาท" not in text:
                continue
            seen.add(ref)

            code_m = CODE_RE.search(text)
            price = self._price(text)
            sub = self._first(SUBDIST_RE, text)
            dist = self._first(DISTRICT_RE, text)
            # จังหวัด: เชื่อจังหวัดที่เรากำลังไล่ (จากพาธ) เป็นหลัก
            # เพราะ slug กรองจังหวัดแม่นแล้ว — ข้อความการ์ดไว้เป็น fallback
            prov = prov_th or self._first(PROV_RE, text)

            rai = _num_before(text, "ไร่")
            ngan = _num_before(text, "งาน")
            wa = _num_before(text, "ตร.ว.")
            land_sqwa = (rai or 0) * 400 + (ngan or 0) * 100 + (wa or 0)
            sqm = _num_before(text, "ตร.ม.")

            detail_url = a.get("href")
            if detail_url and detail_url.startswith("/"):
                detail_url = self.BASE + detail_url

            yield {
                "external_ref": ref,
                "listing_id": pid,
                "detail_url": detail_url,
                "title": self._title(text),
                "property_type": self._map_type(text),
                "province": prov,
                "district": dist,
                "subdistrict": sub,
                "address_raw": self._address(sub, dist, prov),
                "land_area_sqwa": round(land_sqwa, 2) if land_sqwa else None,
                "usable_area_sqm": sqm,
                "opening_price": price,
                "list_price": price,
                "image_url": self._image(card or a),
                "ghb_code": code_m.group(1) if code_m else None,
                "_card_text": text[:400],
                "_source_url": resp.url,
            }

    # ------------------------------------------------------------------
    @staticmethod
    def _card_container(anchor):
        """ไต่ขึ้นหา element ที่ครอบทั้งการ์ด (มีทั้งราคาและรหัสทรัพย์)

        การ์ดหนึ่งใบมีลิงก์ /property-{ID} หลายจุด (รูป+หัวข้อ) และข้อความ
        กระจายในหลาย element ไต่ขึ้นไปหาตัวที่รวมข้อความครบ
        """
        el = anchor
        for _ in range(6):
            if el is None:
                break
            txt = el.get_text(" ", strip=True) if hasattr(el, "get_text") else ""
            if "รหัสทรัพย์" in txt and "บาท" in txt:
                return el
            el = el.parent
        return anchor.parent

    @staticmethod
    def _first(rx: re.Pattern, text: str) -> str | None:
        m = rx.search(text)
        return m.group(1).strip() if m else None

    @staticmethod
    def _price(text: str) -> float | None:
        m = PRICE_RE.search(text)
        if not m:
            return None
        try:
            v = float(m.group(1).replace(",", ""))
            return v if v > 0 else None
        except ValueError:
            return None

    @staticmethod
    def _map_type(text: str) -> str:
        for label, code in TYPE_LABEL_MAP:
            if label in text:
                return code
        return "other"

    @staticmethod
    def _title(text: str) -> str | None:
        """ชื่อทรัพย์ = ชื่อโครงการในวงเล็บ ถ้าไม่มีใช้ท่อน 'ขาย...' """
        pm = PROJECT_RE.search(text)
        if pm:
            return pm.group(1).strip()[:200]
        tm = re.search(r"(ขาย[ก-๙A-Za-z ]{3,60})", text)
        return tm.group(1).strip()[:200] if tm else None

    @staticmethod
    def _address(sub, dist, prov) -> str | None:
        parts = []
        if sub:
            parts.append(f"ต./แขวง {sub}")
        if dist:
            parts.append(f"อ./เขต {dist}")
        if prov:
            parts.append(prov)
        return " ".join(parts) or None

    def _image(self, node) -> str | None:
        if node is None or not hasattr(node, "find"):
            return None
        img = node.find("img")
        if not img:
            return None
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        if not src:
            return None
        if src.startswith("//"):
            return "https:" + src
        if src.startswith("/"):
            return self.BASE + src
        return src if src.startswith("http") else None


# =====================================================================
# ดึงพิกัดจริงจากหน้า detail — ใช้โดย enrich.py ghb-coords
#
# หน้า /property-{ID} ฝังพิกัดในลิงก์ Google Maps: ...?q={lat},{lng}
# เอามาเป็นพิกัดระดับ 'parcel' (เหมือน BAM/SAM) ดีกว่า geocode ระดับตำบล
# =====================================================================
GHB_LATLNG = re.compile(r"[?&]q=(-?\d{1,2}\.\d{2,}),\s*(\d{2,3}\.\d{2,})")
TH_BOUNDS = (5.5, 20.5, 97.0, 106.0)      # lat_min, lat_max, lng_min, lng_max

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept-Language": "th,en;q=0.8",
}


def fetch_ghb_detail(url: str, session: requests.Session | None = None) -> dict:
    """คืน {'lat','lng'} จากหน้า detail ธอส (ว่าง = ดึง/หาไม่ได้)

    ไม่ throw — ถ้าดึงไม่ได้คืน dict ว่าง ให้ผู้เรียกข้ามไปตัวถัดไป
    """
    s = session or requests
    try:
        resp = s.get(url, headers=_HEADERS, timeout=25)
        resp.encoding = "utf-8"
        html = resp.text
    except Exception as exc:                                   # noqa: BLE001
        log.warning("ดึงหน้า ธอส ไม่สำเร็จ %s — %s", url, exc)
        return {}

    out: dict = {}
    m = GHB_LATLNG.search(html)
    if m:
        lat, lng = float(m.group(1)), float(m.group(2))
        if (TH_BOUNDS[0] <= lat <= TH_BOUNDS[1]
                and TH_BOUNDS[2] <= lng <= TH_BOUNDS[3]):
            out["lat"], out["lng"] = lat, lng
    time.sleep(0.5)          # สุภาพกับเซิร์ฟเวอร์ต้นทาง
    return out
