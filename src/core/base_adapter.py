"""Base adapter — ทุก source ต้อง implement สาม method นี้เท่านั้น

การแยก discover / fetch / parse ออกจากกันสำคัญ เพราะเวลาเว็บต้นทาง
เปลี่ยนโครงสร้าง เราแก้แค่ parse() แล้ว re-run กับ raw ที่เก็บไว้ได้เลย
ไม่ต้องไปดึงใหม่ทั้งหมด
"""
from __future__ import annotations

import hashlib
import json
import logging
from abc import ABC, abstractmethod
from typing import Iterable, Iterator

from .http import Fetcher, Response
from .pii import assert_clean, scrub_fields, scrub_text

log = logging.getLogger(__name__)


class BaseAdapter(ABC):
    source_code: str = ""
    parser_version: str = "0.1.0"

    # จำนวนหน้าติดต่อกันที่ไม่เจอของใหม่ ก่อนจะข้ามหน้าที่เหลือของกลุ่มนั้น
    # ตั้ง 2 เผื่อกรณีหน้าหนึ่งบังเอิญไม่มีของใหม่แต่หน้าถัดไปมี
    # ตั้ง 0 = ไล่ครบทุกหน้าเสมอ
    STOP_AFTER_STALE_PAGES: int = 2

    def __init__(self, fetcher: Fetcher, config: dict | None = None):
        self.fetcher = fetcher
        self.config = config or {}

    # ---- ต้อง implement ----------------------------------------------
    @abstractmethod
    def discover(self) -> Iterator[dict]:
        """คืน list ของ 'งานที่ต้องดึง' เช่น {'url': ..., 'method': 'POST', 'data': {...}}"""

    @abstractmethod
    def parse(self, resp: Response, task: dict) -> Iterable[dict]:
        """แปลง response เป็น dict ของฟิลด์ดิบ (ยังไม่ scrub) — yield ทีละรายการ"""

    # ---- ใช้ร่วมกันได้เลย ---------------------------------------------
    def fetch(self, task: dict) -> Response:
        if task.get("method", "GET").upper() == "POST":
            return self.fetcher.post(task["url"], data=task.get("data"))
        return self.fetcher.get(task["url"], params=task.get("params"))

    def to_snapshot(self, fields: dict, raw_document_id) -> dict:
        """map ฟิลด์ดิบ -> คอลัมน์ในตาราง + เก็บทุกอย่างที่เหลือลง raw_fields

        ฟิลด์ที่ยัง map ไม่ได้ ไม่ทิ้ง แต่กองไว้ใน raw_fields (jsonb)
        พอเข้าใจข้อมูลมากขึ้นค่อยเลื่อนขึ้นมาเป็นคอลัมน์จริง
        """
        clean = scrub_fields(fields)
        assert_clean(clean)

        mapped = {k: clean.get(k) for k in SNAPSHOT_COLUMNS if k in clean}
        mapped["source_code"] = self.source_code
        mapped["raw_document_id"] = raw_document_id
        mapped["external_ref"] = clean["external_ref"]
        mapped["parser_version"] = self.parser_version
        mapped["address_raw"] = scrub_text(clean.get("address_raw"))
        mapped["raw_fields"] = clean
        mapped["content_hash"] = self._semantic_hash(mapped)
        return mapped

    @staticmethod
    def _semantic_hash(snap: dict) -> str:
        """hash เฉพาะฟิลด์ที่มีความหมาย เพื่อไม่ให้ timestamp ทำให้ดูเหมือนเปลี่ยน"""
        meaningful = {
            k: v for k, v in snap.items()
            if k not in {"observed_at", "raw_document_id", "content_hash", "id"}
        }
        blob = json.dumps(meaningful, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


SNAPSHOT_COLUMNS = [
    "province", "district", "subdistrict", "address_raw", "lat", "lng",
    "geo_precision",
    "property_type", "title_deed_type", "land_area_sqwa", "usable_area_sqm",
    "building_count", "opening_price", "appraised_price", "auction_round",
    "auction_date", "office_name", "deposit_amount", "mortgage_carried",
    "occupancy_note", "sold", "sold_price", "sold_date",
    "bedrooms", "bathrooms", "parking", "list_price", "special_price",
    "renovated", "title", "detail_url", "image_url",
]
