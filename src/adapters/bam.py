"""Adapter: BAM — ทรัพย์สินรอการขาย (NPA)

╔══════════════════════════════════════════════════════════════════════╗
║  ก่อนเปิดใช้งานจริง                                                   ║
║  1. อ่าน "เงื่อนไขการให้บริการ" ที่ bam.co.th/th/terms-and-conditions ║
║  2. สมัคร "พันธมิตรของ BAM" ที่ bam.co.th/th/npa/partner              ║
║     ในฐานะนายหน้าขึ้นทะเบียน สิทธิ์ในการใช้ข้อมูลจะชัดเจนขึ้นมาก      ║
║  ทั้งสองข้อทำใน M0 อยู่แล้ว                                          ║
╚══════════════════════════════════════════════════════════════════════╝

ต่างจาก LED อย่างสิ้นเชิง
    GET ธรรมดา · query param ตรงไปตรงมา · ไม่มีรหัสยืนยัน
    หน้าเดียวมีข้อมูลครบทั้งการ์ด ไม่ต้องเข้าหน้ารายละเอียด

วิธี parse ที่เลือกใช้: label-anchored ไม่ใช่ class-based
    ดึงค่าโดยอ้างอิงคำกำกับที่อยู่ติดกัน เช่น "...2**ห้องนอน" -> bedrooms=2
    ทนต่อการเปลี่ยน class name และการสลับตำแหน่ง ซึ่งเว็บสมัยใหม่ทำบ่อย
    ถ้าอ้างอิง class เมื่อไหร่ อีกสามเดือน deploy ใหม่ก็พังทันที
"""
from __future__ import annotations

import logging
import re
from typing import Iterable, Iterator

from bs4 import BeautifulSoup

from core.base_adapter import BaseAdapter
from core.districts import match_district
from core.http import Response

log = logging.getLogger(__name__)

# รหัสจังหวัดของ BAM (ยืนยันจากลิงก์บนหน้าเว็บ 2026-08-23)
# BAM ใช้รหัสจังหวัดมาตรฐานของกรมการปกครอง (ยืนยันแล้วด้วยการทดสอบจริง
# กับ 10/11/12/13/20/21/24/50/73/74/90 — ตรงทุกตัว)
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

TYPE_CODES = {
    "single_house": "house",
    "townhouse": "townhouse",
    "condominium": "condo",
    "commercial_building": "commercial",
    "vacant_land": "land",
}

# เรียงจากยาวไปสั้นสำคัญมาก — ต้องจับ "ที่ดินเปล่า" ก่อน "ที่ดิน"
# ไม่งั้นคำว่า "เปล่า" จะตกค้างไปติดกับชื่อจังหวัด
TYPE_LABEL_MAP = {
    "ที่ดินพร้อมสิ่งปลูกสร้าง": "house",
    "อาคารพาณิชย์": "commercial",
    "คอนโดมิเนียม": "condo",
    "อพาร์ทเม้นท์": "commercial",
    "ทาวน์เฮ้าส์": "townhouse",
    "ที่ดินเปล่า": "land",
    "บ้านเดี่ยว": "house",
    "ทาวน์โฮม": "townhouse",
    "บ้านแฝด": "house",
    "ห้องชุด": "condo",
    "สำนักงาน": "commercial",
    "โรงแรม": "commercial",
    "โรงงาน": "industrial",
    "โกดัง": "industrial",
    "คลังสินค้า": "industrial",
    "ที่ดิน": "land",

    # ประเภทที่เจอจริงในพอร์ต BAM นอกเหนือจากอสังหาทั่วไป
    "สังหาริมทรัพย์": "movable",      # เครื่องจักร อุปกรณ์ ไม่ใช่อสังหา
    "Public Service": "common_area",  # พื้นที่ส่วนกลางของโครงการ
    "พื้นที่ส่วนกลาง": "common_area",
    "สนามกอล์ฟ": "special",
    "รีสอร์ท": "special",
    "โรงเรียน": "special",
}

DETAIL_RE = re.compile(r"/th/npa/property/(\d+)")


def _num_before(text: str, label: str) -> float | None:
    """ดึงตัวเลขที่อยู่ก่อนคำกำกับ เช่น '2ห้องนอน' -> 2.0

    '-' แปลว่าไม่ระบุ ไม่ใช่ศูนย์ จึงคืน None
    """
    m = re.search(r"([\d,]+\.?\d*|-)\s*\*{0,2}\s*" + re.escape(label), text)
    if not m or m.group(1) == "-":
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _num_after(text: str, label: str) -> float | None:
    m = re.search(re.escape(label) + r"\s*~?\s*([\d,]+\.?\d*)", text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


class BamAdapter(BaseAdapter):
    source_code = "bam"
    parser_version = "0.1.0"

    BASE = "https://www.bam.co.th"
    SEARCH_PATH = "/th/npa/property/search"

    def discover(self) -> Iterator[dict]:
        """ไล่ทีละจังหวัด ทีละหน้า

        แยกตามจังหวัดแทนที่จะดึงรวม เพราะทำให้หยุดได้เร็วเมื่อหมดหน้า
        และถ้าจังหวัดหนึ่งพัง จังหวัดอื่นยังได้ข้อมูล
        """
        provinces = self.config.get("provinces") or ["กรุงเทพมหานคร"]
        max_pages = self.config.get("max_pages", 30)

        for name in provinces:
            code = PROVINCE_CODES.get(name)
            if not code:
                log.warning("ยังไม่มีรหัสจังหวัด %s ของ BAM — ข้าม", name)
                continue
            for page in range(1, max_pages + 1):
                yield {
                    "url": self.BASE + self.SEARCH_PATH,
                    "method": "GET",
                    "params": {"provinces": code, "page": str(page)},
                    "meta": {"province": name, "page": page},
                }

    def parse(self, resp: Response, task: dict) -> Iterable[dict]:
        soup = BeautifulSoup(resp.text, "html.parser")
        province_hint = task["meta"]["province"]
        seen: set[str] = set()

        for a in soup.find_all("a", href=DETAIL_RE):
            href = a["href"]
            m = DETAIL_RE.search(href)
            if not m:
                continue
            listing_id = m.group(1)
            text = a.get_text(" ", strip=True)
            if len(text) < 30:          # ลิงก์รูปหรือปุ่ม ไม่ใช่การ์ด
                continue

            market_code = self._market_code(text)
            ref = market_code or listing_id
            if ref in seen:
                continue
            seen.add(ref)

            district, province = self._location(text, province_hint)
            list_price, special_price = self._prices(text)

            yield {
                "external_ref": ref,
                "listing_id": listing_id,
                "detail_url": href if href.startswith("http") else self.BASE + href,
                "title": self._title(text),
                "property_type": self._ptype(text),
                "province": province,
                "district": district,
                "district_raw": self._raw_location(text),
                "bedrooms": _num_before(text, "ห้องนอน"),
                "bathrooms": _num_before(text, "ห้องน้ำ"),
                "parking": _num_before(text, "ที่จอดรถ"),
                "rai": _num_before(text, "ไร่"),
                "ngan": _num_before(text, "งาน"),
                "land_area_sqwa": self._total_sqwa(text),
                "usable_area_sqm": _num_before(text, "ตร.ม."),
                "opening_price": special_price or list_price,
                "list_price": list_price,
                "special_price": special_price,
                "renovated": "ปรับปรุงแล้ว" in text,
                "image_url": self._image(a),
                "_card_text": text[:600],
                "_source_url": resp.url,
            }

    # ------------------------------------------------------------------
    @staticmethod
    def _image(anchor) -> str | None:
        """URL รูปหลักจากการ์ด

        เก็บเฉพาะ URL ไม่ดาวน์โหลดมาเก็บ และไม่ไล่เดา index รูปอื่น
        เพราะการยิงลองทีละเลขคือการรบกวนเซิร์ฟเวอร์เขาโดยไม่จำเป็น
        ถ้าอยากได้แกลเลอรีเต็ม ต้องเข้าหน้ารายละเอียดซึ่งเป็นคนละงาน
        """
        img = anchor.find("img")
        if not img:
            return None
        src = img.get("src") or img.get("data-src")
        if not src:
            return None
        if src.startswith("//"):
            return "https:" + src
        if src.startswith("/"):
            return "https://www.bam.co.th" + src
        return src if src.startswith("http") else None

    @staticmethod
    def _market_code(text: str) -> str | None:
        m = re.search(r"รหัสตลาด\s*([A-Z]{2,}[A-Z0-9]+)", text)
        return m.group(1) if m else None

    @staticmethod
    def _title(text: str) -> str:
        """ชื่อทรัพย์ถูกซ้ำสองรอบในการ์ด (alt ของรูป + หัวข้อ) ตัดให้เหลือรอบเดียว"""
        head = re.split(r"ปรับปรุงแล้ว|รหัสตลาด", text)[0].strip()
        half = len(head) // 2
        if head[:half].strip() and head[:half].strip() == head[half:].strip():
            head = head[:half].strip()
        return head[:200]

    @staticmethod
    def _ptype(text: str) -> str:
        for label, code in TYPE_LABEL_MAP.items():
            if label in text:
                return code
        return "other"

    @staticmethod
    def _location(text: str, hint: str) -> tuple[str | None, str]:
        """หาอำเภอและจังหวัดจากข้อความการ์ด

        โครงสร้างจริงของ BAM
            [ปรับปรุงแล้ว] <ชื่อทรัพย์> <อำเภอ>, <จังหวัด> <ประเภท> <สเปก...>

        กับดักที่เจอจริง: ชื่อโครงการบางอันมีจุลภาคอยู่ข้างใน เช่น
            "โครงการ ... พรีเมี่ยม, เฟส บี ธัญบุรี บางบัวทอง, ปทุมธานี"
        การจับจุลภาคตัวแรกด้วย regex จะได้จังหวัดเป็น "บี ธัญบุรี"

        จึงเลิกเดาจากตำแหน่ง หันมาเทียบกับรายชื่อจังหวัดจริงทั้ง 77 ตัวแทน
        แล้วเลือกตำแหน่งที่อยู่ท้ายสุด เพราะชื่อจังหวัดอาจโผล่ในชื่อโครงการด้วย
        (เช่น "โครงการสินเพชร นนทบุรี บางบัวทอง, นนทบุรี")
        """
        best_pos = -1
        province = None
        for name in PROVINCE_CODES:
            pos = text.rfind("," + name)
            if pos < 0:
                pos = text.rfind(", " + name)
            if pos > best_pos:
                best_pos, province = pos, name

        if province is None:
            return None, hint

        before = text[:best_pos]
        district = match_district(before, province) or match_district(before)
        return district, province

    @staticmethod
    def _raw_location(text: str) -> str | None:
        """เก็บข้อความรอบจุดที่เจอชื่อจังหวัด ไว้ตรวจย้อนหลังเมื่อ match ไม่ได้"""
        best = -1
        for name in PROVINCE_CODES:
            for sep in (", ", ","):
                p = text.rfind(sep + name)
                if p > best:
                    best = p
        return text[max(0, best - 30): best + 25].strip() if best >= 0 else None

    @staticmethod
    def _total_sqwa(text: str) -> float | None:
        """รวมเนื้อที่เป็นตารางวา — 1 ไร่ = 400 ตร.ว., 1 งาน = 100 ตร.ว."""
        rai = _num_before(text, "ไร่") or 0
        ngan = _num_before(text, "งาน") or 0
        wa = _num_before(text, "ตร.ว.") or 0
        total = rai * 400 + ngan * 100 + wa
        return round(total, 2) if total else None

    @staticmethod
    def _prices(text: str) -> tuple[float | None, float | None]:
        return _num_after(text, "ราคาตั้งขาย"), _num_after(text, "ราคาพิเศษ")
