"""Adapter: SAM (บสส.) — ทรัพย์สินรอการขาย NPA

╔══════════════════════════════════════════════════════════════════════╗
║  ก่อนเปิดใช้งานจริง                                                   ║
║  1. อ่านเงื่อนไขการใช้งานเว็บ sam.or.th                               ║
║  2. สมัครเป็นนายหน้ากับ SAM (Call Center 02-686-1888)                 ║
║  ทั้งสองข้ออยู่ใน M0 อยู่แล้ว                                        ║
╚══════════════════════════════════════════════════════════════════════╝

ระบบค้นหาอยู่ที่ /site/npa/ ซึ่งไม่มีลิงก์จากเมนูหลักของ sam.or.th
(เว็บองค์กรกับระบบขายทรัพย์เป็นคนละส่วนกัน)

ผลตรวจ 2026-08-23
    ไม่พบข้อความห้ามนำข้อมูลไปใช้
    query param ทำงานจริงทั้ง s_province / limit / page
    ตั้ง limit สูงได้ ดึงทั้งจังหวัดในคำขอเดียว

โครงสร้างข้อมูลดีที่สุดในบรรดาแหล่งที่สำรวจมา
    มีรหัสทรัพย์ · ประเภท · อำเภอ+จังหวัด · เนื้อที่แยกไร่/งาน/วา · ราคา · รูป
"""
from __future__ import annotations

import logging
import re
from typing import Iterable, Iterator

from core.base_adapter import BaseAdapter
from core.http import Response

log = logging.getLogger(__name__)

# รหัสจังหวัดของ SAM — ยืนยันครบ 77 จังหวัดด้วยการทดสอบจริง 2026-08-23
#
# ไม่ใช่รหัสมาตรฐานกรมการปกครองแบบ BAM
# แต่เรียงตามลำดับตัวอักษรไทยของชื่อจังหวัด 1-77
PROVINCE_CODES = {
    "กระบี่": "1", "กรุงเทพมหานคร": "2", "กาญจนบุรี": "3", "กาฬสินธุ์": "4",
    "กำแพงเพชร": "5", "ขอนแก่น": "6", "จันทบุรี": "7", "ฉะเชิงเทรา": "8",
    "ชลบุรี": "9", "ชัยนาท": "10", "ชัยภูมิ": "11", "ชุมพร": "12",
    "ตรัง": "13", "ตราด": "14", "ตาก": "15", "นครนายก": "16",
    "นครปฐม": "17", "นครพนม": "18", "นครราชสีมา": "19", "นครศรีธรรมราช": "20",
    "นครสวรรค์": "21", "นนทบุรี": "22", "นราธิวาส": "23", "น่าน": "24",
    "บุรีรัมย์": "25", "ปทุมธานี": "26", "ประจวบคีรีขันธ์": "27", "ปราจีนบุรี": "28",
    "ปัตตานี": "29", "พระนครศรีอยุธยา": "30", "พะเยา": "31", "พังงา": "32",
    "พัทลุง": "33", "พิจิตร": "34", "พิษณุโลก": "35", "ภูเก็ต": "36",
    "มหาสารคาม": "37", "มุกดาหาร": "38", "ยะลา": "39", "ยโสธร": "40",
    "ระนอง": "41", "ระยอง": "42", "ราชบุรี": "43", "ร้อยเอ็ด": "44",
    "ลพบุรี": "45", "ลำปาง": "46", "ลำพูน": "47", "ศรีสะเกษ": "48",
    "สกลนคร": "49", "สงขลา": "50", "สตูล": "51", "สมุทรปราการ": "52",
    "สมุทรสงคราม": "53", "สมุทรสาคร": "54", "สระบุรี": "55", "สระแก้ว": "56",
    "สิงห์บุรี": "57", "สุพรรณบุรี": "58", "สุราษฎร์ธานี": "59", "สุรินทร์": "60",
    "สุโขทัย": "61", "หนองคาย": "62", "หนองบัวลำภู": "63", "อำนาจเจริญ": "64",
    "อุดรธานี": "65", "อุตรดิตถ์": "66", "อุทัยธานี": "67", "อุบลราชธานี": "68",
    "อ่างทอง": "69", "เชียงราย": "70", "เชียงใหม่": "71", "เพชรบุรี": "72",
    "เพชรบูรณ์": "73", "เลย": "74", "แพร่": "75", "แม่ฮ่องสอน": "76",
    "บึงกาฬ": "77",
}

TYPE_LABELS = {
    "ที่ดินเปล่า": "land",
    "ห้องชุดพักอาศัย": "condo",
    "อาคารชุดพักอาศัย": "condo",
    "ห้องชุดสำนักงาน": "commercial",
    "ห้องชุดพาณิชยกรรม": "commercial",
    "ทาวน์เฮ้าส์": "townhouse",
    "บ้านเดี่ยว": "house",
    "บ้านแฝด": "house",
    "อาคารพาณิชย์": "commercial",
    "อาคารสำนักงาน": "commercial",
    "โรงงาน/โกดัง": "industrial",
    "โรงแรม/รีสอร์ท": "special",
    "โชว์รูม": "commercial",
    "โฮมออฟฟิศ": "commercial",
    "ปั๊มน้ำมัน": "special",
    "อพาร์ทเม้นท์": "commercial",
    "อพาร์ทเมนท์": "commercial",
    "โครงการที่พักอาศัย/พาณิชยกรรม": "special",
    "อาคารพักอาศัย": "house",
    "มินิแฟคตอรี่": "industrial",
    "ฟาร์มเลี้ยงสัตว์": "special",
    "อสังหาริมทรัพย์อื่นๆ": "other",   # ต้นทางระบุเองว่าอื่น ๆ ปล่อยไว้ตามนั้น
}

# การ์ดหนึ่งใบเริ่มที่ card-content และจบก่อนใบถัดไป
CARD_RE = re.compile(r'<div class="card-content">(.*?)(?=<div class="card-content">|</section>)',
                     re.S)
FIELD_RE = {
    "type": re.compile(r"ประเภททรัพย์สิน\s*:\s*<span>\s*(.*?)\s*</span>", re.S),
    "code": re.compile(r"รหัสทรัพย์สิน\s*:\s*<span>\s*(.*?)\s*</span>", re.S),
    "place": re.compile(r"สถานที่ตั้ง\s*:\s*<span>\s*(.*?)\s*</span>", re.S),
    "area": re.compile(r"พื้นที่/เนื้อที่\s*:\s*<span>\s*(.*?)\s*</span>", re.S),
}
PRICE_RE = re.compile(r"ราคาประกาศขาย\s*:\s*([\d,]+)")
ID_RE = re.compile(r"gotoDetail\((\d+)\)")
IMG_RE = re.compile(r'<img src="(https://npa\.sam\.or\.th/[^"]+)"')
TOTAL_RE = re.compile(r"ผลการค้นหา\s*<span>(\d+)</span>")
# "อ.ไทรน้อย จ.นนทบุรี" หรือ "เขตบางรัก กรุงเทพมหานคร"
PLACE_RE = re.compile(r"(?:อ\.|เขต)\s*([ก-๙\s]+?)\s*(?:จ\.|จังหวัด)?\s*([ก-๙]+)\s*$")
AREA_RE = re.compile(r"(?:([\d.,]+)\s*ไร่)?\s*(?:([\d.,]+)\s*งาน)?\s*(?:([\d.,]+)\s*ตร\.ว\.)?")


def _f(text: str | None) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d.]", "", text)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


class SamAdapter(BaseAdapter):
    source_code = "sam"
    parser_version = "0.1.0"

    BASE = "https://sam.or.th"
    LIST_PATH = "/site/npa/page_list.php"
    PAGE_LIMIT = 200          # ทดสอบแล้วรับได้ถึง 300 ใช้ 200 เพื่อความสุภาพ

    def discover(self) -> Iterator[dict]:
        """ยิงทีละจังหวัด ด้วย limit สูงเพื่อลดจำนวนคำขอ

        ต่างจาก BAM ที่ต้องไล่หน้าละ 24 รายการ SAM ให้ตั้ง limit เองได้
        นนทบุรี 227 รายการจึงจบในคำขอเดียว
        """
        provinces = self.config.get("provinces") or list(PROVINCE_CODES)
        # limit 200 ครอบคลุมเกือบทุกจังหวัดในหน้าเดียว
        # จังหวัดใหญ่สุด (ชลบุรี 380, ปทุมธานี 355) ใช้ 2 หน้า
        # จึงไม่ต้องตั้ง max_pages สูง ต่างจาก BAM ที่หน้าละ 24 รายการ
        max_pages = min(self.config.get("max_pages", 3), 5)

        for name in provinces:
            code = PROVINCE_CODES.get(name)
            if not code:
                log.warning("ยังไม่มีรหัสจังหวัด %s ของ SAM — ข้าม "
                            "(หาได้ด้วย tools/probe_sam.py)", name)
                continue
            for page in range(1, max_pages + 1):
                yield {
                    "url": self.BASE + self.LIST_PATH,
                    "method": "GET",
                    "params": {
                        "s_province": code,
                        "s_status_id": "1",       # 1 = ทรัพย์ที่ยังขายอยู่
                        "limit": str(self.PAGE_LIMIT),
                        "page": str(page),
                    },
                    "meta": {"province": name, "page": page},
                }

    def parse(self, resp: Response, task: dict) -> Iterable[dict]:
        html = resp.text
        hint = task["meta"]["province"]

        total = TOTAL_RE.search(html)
        if total and task["meta"]["page"] == 1:
            log.info("SAM %s มีทั้งหมด %s รายการ", hint, total.group(1))

        for block in CARD_RE.findall(html):
            code = FIELD_RE["code"].search(block)
            if not code:
                continue
            ref = code.group(1).strip()

            place = FIELD_RE["place"].search(block)
            district, province = self._place(place.group(1) if place else "", hint)

            area = FIELD_RE["area"].search(block)
            ptype_raw = FIELD_RE["type"].search(block)
            price = PRICE_RE.search(block)
            listing_id = ID_RE.search(block)
            img = IMG_RE.search(block)

            yield {
                "external_ref": ref,
                "listing_id": listing_id.group(1) if listing_id else None,
                # URL จริงคือ detail.php ไม่ใช่ page_detail.php
                # (ยืนยันจาก gotoDetail() ในหน้ารายการ)
                "detail_url": (f"{self.BASE}/site/npa/detail.php?id={listing_id.group(1)}&keyref="
                               if listing_id else None),
                "title": self._title(ptype_raw, district, province),
                "property_type": self._ptype(ptype_raw.group(1) if ptype_raw else ""),
                "province": province,
                "district": district,
                "land_area_sqwa": self._area(area.group(1) if area else ""),
                "usable_area_sqm": self._sqm(area.group(1) if area else ""),
                "opening_price": _f(price.group(1)) if price else None,
                "image_url": img.group(1) if img else None,
                "_card_text": re.sub(r"<[^>]+>", " ", block)[:500],
                "_source_url": resp.url,
            }

    # ------------------------------------------------------------------
    @staticmethod
    def _place(text: str, hint: str) -> tuple[str | None, str]:
        """แยก 'อ.ไทรน้อย จ.นนทบุรี' เป็นอำเภอกับจังหวัด"""
        text = re.sub(r"\s+", " ", text).strip()
        m = PLACE_RE.search(text)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        # กทม. อาจไม่มีคำว่า จ. นำหน้า
        if "กรุงเทพ" in text:
            d = re.sub(r"(เขต|กรุงเทพมหานคร|จ\.|\s)", "", text)
            return d or None, "กรุงเทพมหานคร"
        return None, hint

    @staticmethod
    def _ptype(label: str) -> str:
        label = re.sub(r"\s+", "", label)
        for key in sorted(TYPE_LABELS, key=len, reverse=True):
            if re.sub(r"\s+", "", key) in label:
                return TYPE_LABELS[key]
        return "other"

    @staticmethod
    def _title(ptype_raw, district: str | None, province: str) -> str:
        label = ptype_raw.group(1).strip() if ptype_raw else "ทรัพย์ SAM"
        where = f"{district} {province}" if district else province
        return f"{label} {where}"[:200]

    @staticmethod
    def _sqm(text: str) -> float | None:
        """ห้องชุดให้พื้นที่เป็น ตร.ม. ไม่ใช่ ตร.ว.

        ต้องแยกฟิลด์ ไม่งั้นจะเอา 88 ตร.ม. ไปนับเป็น 88 ตร.ว.
        แล้วราคาต่อหน่วยเพี้ยนไป 4 เท่า
        """
        m = re.search(r"([\d.,]+)\s*ตร\.ม\.", text)
        return _f(m.group(1)) if m else None

    @staticmethod
    def _area(text: str) -> float | None:
        """รวมเนื้อที่เป็นตารางวา — 1 ไร่ = 400 ตร.ว., 1 งาน = 100 ตร.ว."""
        m = AREA_RE.search(re.sub(r"\s+", " ", text).strip())
        if not m:
            return None
        rai = _f(m.group(1)) or 0
        ngan = _f(m.group(2)) or 0
        wa = _f(m.group(3)) or 0
        total = rai * 400 + ngan * 100 + wa
        return round(total, 2) if total else None
