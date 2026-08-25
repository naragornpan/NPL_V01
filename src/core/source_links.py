"""สร้างลิงก์กลับไปยังแหล่งข้อมูลต้นทาง

หลักการ: ลิงก์ที่พาไปหน้าค้นหาแล้วผู้ใช้กรองเอง **ดีกว่า** ลิงก์ตรงที่ 404

เว็บราชการเปลี่ยน URL บ่อยและบางแห่งใช้ session ทำให้ deep link ตายง่าย
เราจึงคืนค่าพร้อมระดับความมั่นใจ แล้วให้ UI แสดงต่างกัน:

    deep    -> ปุ่ม "ดูประกาศต้นทาง"
    search  -> ปุ่ม "ค้นหาในเว็บต้นทาง" + แสดงเลขอ้างอิงให้ก๊อป
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, urlencode


@dataclass
class SourceLink:
    url: str
    label: str
    confidence: str          # deep | search
    hint: str | None = None  # สิ่งที่ผู้ใช้ต้องกรอกเอง ถ้าเป็น search
    kind: str = "source"     # source | map | reference


# TODO(probe): เติม pattern ของ deep link หลังยืนยันจาก tools/probe.py
# ตราบใดที่ยังเป็น None ระบบจะ fallback ไปหน้าค้นหาอัตโนมัติ
DEEP_LINK_PATTERNS: dict[str, str | None] = {
    "led_auction": None,
    "led_result": None,
    "bam": None,
    "scb": None,
}

# URL ยืนยันจากหน้า e-Service ของ led.go.th เมื่อ 2026-08-23
SEARCH_PAGES = {
    "led_auction": "https://asset.led.go.th/newbidreg/",
    "led_result": "https://asset.led.go.th/report",
    "bam": "https://www.bam.co.th/th/npa/property/search",
    "scb": "https://www.scb.co.th/th/personal-banking/promotions/loans/npa-broker.html",
}

SOURCE_LABELS = {
    "led_auction": "กรมบังคับคดี",
    "led_result": "รายงานผลการขาย",
    "bam": "BAM",
    "scb": "SCB NPA",
}


def source_link(source_code: str, external_ref: str | None) -> SourceLink | None:
    """คืนลิงก์ที่ดีที่สุดเท่าที่ทำได้ ไม่เดา URL"""
    label = SOURCE_LABELS.get(source_code, source_code)
    pattern = DEEP_LINK_PATTERNS.get(source_code)

    if pattern and external_ref:
        return SourceLink(
            url=pattern.format(ref=quote(str(external_ref))),
            label=f"ดูประกาศที่ {label}",
            confidence="deep", kind="source",
        )

    search = SEARCH_PAGES.get(source_code)
    if not search:
        return None
    return SourceLink(
        url=search,
        label=f"ค้นหาใน {label}",
        confidence="search", kind="source",
        hint=f"ใช้เลขอ้างอิง {external_ref}" if external_ref else None,
    )


def map_link(lat: float | None, lng: float | None, label: str = "") -> str | None:
    if lat is None or lng is None:
        return None
    q = urlencode({"api": 1, "query": f"{lat},{lng}"})
    return f"https://www.google.com/maps/search/?{q}"


def street_view_link(lat: float | None, lng: float | None) -> str | None:
    """ดูสภาพภายนอกทรัพย์โดยไม่ต้องขับรถไป — คัดกรองรอบแรกได้เร็วมาก"""
    if lat is None or lng is None:
        return None
    q = urlencode({"api": 1, "map_action": "pano", "viewpoint": f"{lat},{lng}"})
    return f"https://www.google.com/maps/@?{q}"


def landsmaps_link(lat: float | None, lng: float | None) -> str | None:
    """LandsMaps กรมที่ดิน — ตรวจรูปแปลง ทางเข้าออก และราคาประเมิน

    ระบบไม่รับพิกัดใน URL โดยตรง จึงเป็นลิงก์หน้าหลัก
    ให้ผู้ใช้ค้นด้วยเลขโฉนดหรือเลื่อนแผนที่เอง
    """
    if lat is None or lng is None:
        return None
    return "https://landsmaps.dol.go.th/"


def all_links(row: dict) -> list[dict]:
    """รวมลิงก์ทั้งหมดของทรัพย์หนึ่งรายการ สำหรับส่งเข้า template

    ติดป้าย kind ไว้ด้วย เพราะการซ่อนลิงก์ต้นทาง (กัน leakage)
    ต้องไม่ไปซ่อนลิงก์แผนที่ซึ่งไม่เกี่ยวกันเลย
    """
    out: list[dict] = []

    src = source_link(row.get("source_code"), row.get("external_ref"))
    if src:
        out.append({"url": src.url, "label": src.label, "kind": src.kind,
                    "confidence": src.confidence, "hint": src.hint})

    detail = row.get("detail_url")
    if detail:
        out.append({"url": detail, "label": "หน้าประกาศต้นทาง", "kind": "source",
                    "confidence": "deep", "hint": None})

    lat, lng = row.get("lat"), row.get("lng")
    for url, label in (
        (map_link(lat, lng), "เปิดแผนที่"),
        (street_view_link(lat, lng), "Street View"),
        (landsmaps_link(lat, lng), "LandsMaps (ตรวจแปลง)"),
    ):
        if url:
            out.append({"url": url, "label": label, "kind": "map",
                        "confidence": "deep", "hint": None})
    return out
