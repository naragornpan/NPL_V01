"""Adapter: กรุงไทย — ทรัพย์มือสอง (NPA)

╔══════════════════════════════════════════════════════════════════════╗
║  สถานะ: ทำได้บางส่วน                                                  ║
║                                                                      ║
║  เว็บเป็น Angular ที่กรองด้วย JavaScript ฝั่งผู้ใช้                     ║
║  query param ไม่ทำงาน หน้าเดียวให้มาราว 16 รายการคงที่                ║
║  การดึงทั้งพอร์ตต้องเรียก API ภายในของเขา ซึ่งเราเลือกไม่ทำ             ║
║                                                                      ║
║  adapter นี้จึงเก็บได้เฉพาะที่เขาเรนเดอร์เป็น HTML สาธารณะ             ║
║  ใช้เป็นตัวอย่างและพร้อมขยายทันทีถ้าได้สิทธิ์เข้าถึงข้อมูลอย่างเป็นทางการ ║
║                                                                      ║
║  ทางที่ควรทำจริงคือขอไฟล์รายการทรัพย์ในฐานะนายหน้าขึ้นทะเบียน          ║
║  ดู docs/SOURCES.md                                                  ║
╚══════════════════════════════════════════════════════════════════════╝

ข้อดีของแหล่งนี้: การ์ดมี **ตำบล** มาให้เลย ต่างจาก BAM ที่ต้องเข้าหน้ารายละเอียด
"""
from __future__ import annotations

import logging
import re
from typing import Iterable, Iterator

from bs4 import BeautifulSoup

from core.base_adapter import BaseAdapter
from core.http import Response

log = logging.getLogger(__name__)

# "ตำบลหอมศีล อำเภอบางปะกง จังหวัดฉะเชิงเทรา"
LOCATION_RE = re.compile(
    r"ตำบล\s*([ก-๙\w]+)\s*อำเภอ\s*([ก-๙\w]+)\s*จังหวัด\s*([ก-๙\w]+)")
# กทม. ใช้ แขวง/เขต แทน
LOCATION_BKK_RE = re.compile(
    r"แขวง\s*([ก-๙\w]+)\s*เขต\s*([ก-๙\w]+)\s*(?:จังหวัด)?\s*(กรุงเทพมหานคร|กทม\.?)")
CODE_RE = re.compile(r"รหัสทรัพย์[:\s]*([A-Z0-9]{6,20})")
PRICE_RE = re.compile(r"([\d,]{6,15})\s*บาท")
AREA_RE = re.compile(r"([\d.,]+)\s*ไร่\s*-?\s*([\d.,]+)?\s*งาน\s*-?\s*([\d.,]+)?\s*ตารางวา")

TYPE_LABELS = {
    "ที่ดินเปล่า": "land",
    "ที่ดินพร้อมสิ่งปลูกสร้าง": "house",
    "บ้านเดี่ยว": "house",
    "บ้านแฝด": "house",
    "ทาวน์เฮ้าส์": "townhouse",
    "ทาวน์เฮาส์": "townhouse",
    "อาคารพาณิชย์": "commercial",
    "ห้องชุด": "condo",
    "คอนโดมิเนียม": "condo",
    "โรงงาน": "industrial",
    "โกดัง": "industrial",
    "อพาร์ทเม้นท์": "commercial",
    "ที่ดิน": "land",
}


def _f(text: str | None) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d.]", "", text)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


class KtbAdapter(BaseAdapter):
    source_code = "ktb"
    parser_version = "0.1.0"

    BASE = "https://npa.krungthai.com"
    SEARCH_PATH = "/search-result"

    # หน้าที่เรนเดอร์ทรัพย์เป็น HTML สาธารณะ (ทดสอบแล้ว 2026-08-23)
    #   /              -> 70 รายการ  ← มากที่สุด
    #   /search-result -> 16 รายการ  ซ้อนกับหน้าแรกบางส่วน
    # รวมสองหน้าได้ 80 รายการไม่ซ้ำ
    #
    # ทดสอบแล้วว่าหน้าเดิมให้ชุดเดิมทุกครั้ง ไม่หมุนเปลี่ยน
    # การรันซ้ำจึงไม่ได้ของเพิ่ม ต่างจาก BAM ที่มีหน้า 2, 3, 4 ให้ไล่
    PAGES = ["/", "/search-result"]

    def discover(self) -> Iterator[dict]:
        """ไล่ทุกหน้าที่เรนเดอร์ทรัพย์เป็น HTML

        เว็บกรองด้วย JavaScript ฝั่งผู้ใช้ ส่ง query param ไปก็ได้ผลเดิม
        (ทดสอบแล้วกับ province / province_id / keyword / page)
        จึงดึงทุกหน้าแล้วกรองจังหวัดฝั่งเราแทน
        """
        for path in self.PAGES:
            yield {
                "url": self.BASE + path,
                "method": "GET",
                "meta": {"province": None, "page": path},
            }

    def parse(self, resp: Response, task: dict) -> Iterable[dict]:
        soup = BeautifulSoup(resp.text, "html.parser")
        text_blocks = self._cards(soup)
        wanted = set(self.config.get("provinces") or [])

        for block, img in text_blocks:
            code = CODE_RE.search(block)
            if not code:
                continue

            sub, dist, prov = self._location(block)
            # เก็บทุกจังหวัดโดยค่าเริ่มต้น
            #
            # ต่างจาก BAM ที่ยิงแยกจังหวัด การกรองจึงประหยัด request จริง
            # แต่กรุงไทยส่งทั้ง 32 จังหวัดมาในหน้าเดียวอยู่แล้ว
            # กรองทิ้งจึงไม่ประหยัดอะไร มีแต่เสียข้อมูลที่ได้มาฟรี
            #
            # ตั้ง strict_province=True ถ้าอยากกรองจริง ๆ
            if wanted and self.config.get("strict_province") and prov not in wanted:
                continue

            prices = [_f(p) for p in PRICE_RE.findall(block)]
            price = max([p for p in prices if p], default=None)

            yield {
                "external_ref": code.group(1),
                "title": self._title(block),
                "property_type": self._ptype(block),
                "province": prov,
                "district": dist,
                "subdistrict": sub,
                "land_area_sqwa": self._area(block),
                "opening_price": price,
                "image_url": img,
                "_card_text": block[:500],
                "_source_url": resp.url,
            }

    # ------------------------------------------------------------------
    @staticmethod
    def _cards(soup: BeautifulSoup) -> list[tuple[str, str | None]]:
        """หา element ที่เป็นการ์ดทรัพย์ โดยอ้างจากคำว่า "รหัสทรัพย์"

        อ้างอิงข้อความแทน class เพราะ Angular สร้าง class แบบสุ่มทุก build
        """
        out: list[tuple[str, str | None]] = []
        for node in soup.find_all(string=CODE_RE):
            el = node.parent
            # ไต่ขึ้นจนเจอ element ที่มีทั้งข้อความครบและรูปของทรัพย์
            # (ข้อความอยู่ลึกกว่ารูปหนึ่งระดับ ถ้าหยุดเร็วไปจะได้ข้อความแต่ไม่ได้รูป)
            for _ in range(8):
                if el is None or el.parent is None:
                    break
                text = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
                has_img = any("/image/product/" in (i.get("src") or "")
                              for i in el.find_all("img"))
                if "บาท" in text and len(text) > 60 and has_img:
                    break
                el = el.parent
            if el is None:
                continue
            text = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
            if not (60 < len(text) < 900):
                continue

            # การ์ดมี slider หลายรูปของทรัพย์ตัวเดียว เอารูปแรกที่เป็นรูปทรัพย์จริง
            # ข้าม thumb เพราะเป็นภาพย่อสำหรับปุ่มเลื่อน
            src = None
            for im in el.find_all("img"):
                u = im.get("src") or im.get("data-src") or ""
                if "/image/product/" in u and "/thumb/" not in u:
                    src = u
                    break
            if not src:
                for im in el.find_all("img"):
                    u = im.get("src") or im.get("data-src") or ""
                    if "/image/product/" in u:
                        src = u
                        break
            if src and src.startswith("/"):
                src = KtbAdapter.BASE + src
            if not any(text[:80] == t[:80] for t, _ in out):
                out.append((text, src))
        return out

    @staticmethod
    def _location(text: str) -> tuple[str | None, str | None, str | None]:
        m = LOCATION_RE.search(text)
        if m:
            return m.group(1), m.group(2), m.group(3)
        m = LOCATION_BKK_RE.search(text)
        if m:
            return m.group(1), m.group(2), "กรุงเทพมหานคร"
        return None, None, None

    @staticmethod
    def _ptype(text: str) -> str:
        for label in sorted(TYPE_LABELS, key=len, reverse=True):
            if label in text:
                return TYPE_LABELS[label]
        return "other"

    @staticmethod
    def _title(text: str) -> str:
        head = CODE_RE.split(text)[0]
        head = re.sub(r"^[\d\s]+", "", head).strip()
        return head[:200] or "ทรัพย์กรุงไทย"

    @staticmethod
    def _area(text: str) -> float | None:
        """รวมเนื้อที่เป็นตารางวา — 1 ไร่ = 400 ตร.ว., 1 งาน = 100 ตร.ว."""
        m = AREA_RE.search(text)
        if not m:
            return None
        rai = _f(m.group(1)) or 0
        ngan = _f(m.group(2)) or 0
        wa = _f(m.group(3)) or 0
        total = rai * 400 + ngan * 100 + wa
        return round(total, 2) if total else None
