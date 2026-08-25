"""Adapter: เฝ้าประกาศ พ.ร.ฎ.เวนคืน และข่าวโครงสร้างพื้นฐาน

ทำไมต้องมีคิวรีวิว
------------------
LLM สกัดข้อมูลจากประกาศได้ดี แต่ **ห้ามให้ผลจาก LLM เข้าระบบคะแนนโดยตรง**
เพราะหลักการข้อ 1 ของโปรเจกต์คือคะแนนต้องอธิบายที่มาได้ และเพราะการบอกลูกค้า
ผิดว่าทรัพย์ "โดนเวนคืน" หรือ "ไม่โดนเวนคืน" คือความเสียหายที่กู้ไม่ได้

flow: ดึงประกาศ -> LLM สกัด -> infra_candidates (pending) -> คนกด approve -> infra_projects

ขั้นตอนที่คนต้องทำเองเสมอ
  - ยืนยันแนวเขตกับแผนที่ท้าย พ.ร.ฎ.
  - วาด geometry ลงแผนที่ (LLM ให้ได้แค่ชื่อแขวง/ตำบล ไม่ใช่พิกัดแนวเส้น)
ตั้งเป้าไว้ที่รีวิวสัปดาห์ละครั้ง ไม่ใช่ทุกวัน
"""
from __future__ import annotations

import json
import logging
import re
from typing import Iterable, Iterator

from bs4 import BeautifulSoup

from core.base_adapter import BaseAdapter
from core.http import Response

log = logging.getLogger(__name__)

# คำที่บ่งชี้ว่าประกาศเกี่ยวกับที่ดิน/คมนาคม — ใช้กรองหยาบก่อนส่งเข้า LLM
# เพื่อไม่ให้เปลืองโทเคนกับประกาศที่ไม่เกี่ยว
KEYWORDS = (
    "เวนคืน", "กำหนดเขตที่ดิน", "ทางพิเศษ", "ทางหลวง", "รถไฟฟ้า",
    "โครงการขนส่ง", "ผังเมืองรวม", "ทางแยกต่างระดับ",
)

EXTRACTION_PROMPT = """\
คุณคือผู้ช่วยสกัดข้อมูลจากประกาศราชการไทย

จากข้อความประกาศต่อไปนี้ ให้สกัดข้อมูลเป็น JSON เท่านั้น ห้ามมีข้อความอื่น

{{
  "is_relevant": true/false,
  "project_name": "ชื่อโครงการ",
  "project_type": "road|expressway|rail|station|other",
  "agency": "หน่วยงาน",
  "certainty_code": "study|cabinet|decree|construction|operational",
  "effective_date": "YYYY-MM-DD หรือ null",
  "areas": [
    {{"province": "...", "district": "...", "subdistrict": "..."}}
  ],
  "summary": "สรุปด้วยคำของคุณเองไม่เกิน 2 ประโยค ห้ามคัดลอกข้อความต้นฉบับ",
  "uncertainty_notes": "สิ่งที่ไม่แน่ใจหรือข้อมูลที่ขาด"
}}

กฎ
- ห้ามเดา ถ้าไม่พบข้อมูลให้ใส่ null
- ห้ามใส่ชื่อบุคคล เลขที่โฉนด หรือบ้านเลขที่ ลงในผลลัพธ์
- summary ต้องเป็นคำพูดของคุณเอง ไม่ใช่ข้อความจากประกาศ

ข้อความประกาศ:
---
{text}
---
"""


class GazetteDecreeAdapter(BaseAdapter):
    source_code = "gazette_decree"
    parser_version = "0.1.0"

    def discover(self) -> Iterator[dict]:
        """TODO(probe): ยืนยัน URL รายการประกาศล่าสุดจาก tools/probe.py

        แนะนำให้ดึงเฉพาะประกาศใหม่ตั้งแต่ครั้งล่าสุดที่รัน (incremental)
        ไม่ใช่ไล่ย้อนหลังทุกรอบ
        """
        for page in range(1, self.config.get("max_pages", 3) + 1):
            yield {
                "url": f"{self.config.get('base_url')}/search",
                "params": {"keyword": "เวนคืน", "page": page},
                "meta": {"page": page},
            }

    def parse(self, resp: Response, task: dict) -> Iterable[dict]:
        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(" ", strip=True)

        if not any(kw in text for kw in KEYWORDS):
            return

        # TODO(probe): เปลี่ยน selector ตามโครงสร้างจริงของหน้ารายการ
        for item in soup.select("a[href]"):
            title = item.get_text(strip=True)
            if not any(kw in title for kw in KEYWORDS):
                continue
            yield {
                "external_ref": self._ref_from(item["href"], title),
                "headline": title,
                "detail_url": item["href"],
                "_needs_llm": True,
            }

    @staticmethod
    def _ref_from(href: str, title: str) -> str:
        m = re.search(r"(\d{3,})", href)
        return m.group(1) if m else str(abs(hash(title)) % (10 ** 12))


def build_extraction_prompt(announcement_text: str) -> str:
    """สร้าง prompt ส่งเข้า Claude API

    ตัดข้อความให้สั้นก่อน ประกาศบางฉบับยาวมากและส่วนที่มีข้อมูลจริง
    อยู่ช่วงต้นกับช่วงที่ระบุท้องที่
    """
    return EXTRACTION_PROMPT.format(text=announcement_text[:12000])


def parse_extraction(response_text: str) -> dict:
    """แปลงผลจาก LLM เป็น dict — ถ้าพังให้คืน is_relevant=False ไม่ raise"""
    cleaned = re.sub(r"^```(?:json)?|```$", "", response_text.strip(),
                     flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        log.warning("LLM คืนค่าที่ไม่ใช่ JSON — ข้ามรายการนี้")
        return {"is_relevant": False, "uncertainty_notes": "parse ไม่สำเร็จ"}

    # กันไว้อีกชั้น เผื่อ LLM ใส่ PII มาแม้สั่งห้ามแล้ว
    for area in data.get("areas") or []:
        area.pop("owner", None)
        area.pop("house_no", None)
    return data
