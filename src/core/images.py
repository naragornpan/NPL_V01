"""จัดการรูปภาพทรัพย์ + placeholder สำหรับกรณีไม่มีรูป

placeholder สร้างเป็น SVG ในเครื่อง ไม่เรียกบริการภายนอก
ทำให้เว็บใช้งานได้แม้ออฟไลน์ และไม่มีปัญหาลิขสิทธิ์
"""
from __future__ import annotations

import base64
import hashlib

# โทนสีตามประเภททรัพย์ ให้แยกออกจากกันได้ตั้งแต่มองผ่าน ๆ
TYPE_PALETTE = {
    "land": ("#166534", "#4ade80", "ที่ดิน"),
    "house": ("#1e3a8a", "#60a5fa", "บ้านเดี่ยว"),
    "townhouse": ("#7c2d12", "#fb923c", "ทาวน์เฮาส์"),
    "condo": ("#4c1d95", "#a78bfa", "ห้องชุด"),
    "commercial": ("#155e75", "#22d3ee", "อาคารพาณิชย์"),
    "industrial": ("#78350f", "#d97706", "โรงงาน/โกดัง"),
    "movable": ("#3f3f46", "#a1a1aa", "สังหาริมทรัพย์"),
    "common_area": ("#065f46", "#34d399", "พื้นที่ส่วนกลาง"),
    "special": ("#831843", "#f472b6", "ทรัพย์เฉพาะทาง"),
    "other": ("#334155", "#94a3b8", "อื่น ๆ"),
}


def placeholder_svg(property_type: str | None, ref: str = "",
                    width: int = 800, height: int = 560) -> str:
    """สร้าง data URI ของ SVG — ใช้เป็น src ของ <img> ได้เลย"""
    dark, light, label = TYPE_PALETTE.get(property_type or "other", TYPE_PALETTE["other"])
    seed = int(hashlib.md5(ref.encode()).hexdigest()[:6], 16) if ref else 0
    angle = seed % 60

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"
  viewBox="0 0 {width} {height}">
<defs><linearGradient id="g" gradientTransform="rotate({angle})">
  <stop offset="0%" stop-color="{dark}"/><stop offset="100%" stop-color="{light}"/>
</linearGradient></defs>
<rect width="{width}" height="{height}" fill="url(#g)"/>
<g fill="#fff" opacity="0.92" text-anchor="middle"
   font-family="'Noto Sans Thai',-apple-system,sans-serif">
  <text x="{width/2}" y="{height/2 - 6}" font-size="34" font-weight="600">{label}</text>
  <text x="{width/2}" y="{height/2 + 30}" font-size="17" opacity="0.75">ยังไม่มีรูป</text>
</g></svg>"""
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def resolve_images(row: dict, public_mode: bool) -> list[dict]:
    """คืนรายการรูปที่ "แสดงได้" ตามโหมดที่รันอยู่

    public_mode = True  -> แสดงเฉพาะรูปที่มีสิทธิ์เผยแพร่ (own_survey, ได้รับอนุญาต)
    public_mode = False -> แสดงได้ทั้งหมด เพราะเป็นการใช้ภายในเพื่อวิเคราะห์

    ถ้าไม่เหลือรูปเลย คืน placeholder หนึ่งใบ เพื่อให้เลย์เอาต์ไม่พัง
    """
    images = row.get("images") or []
    if public_mode:
        images = [i for i in images if i.get("usage_scope") == "publishable"]

    if not images:
        return [{
            "url": placeholder_svg(row.get("property_type"), row.get("external_ref", "")),
            "caption": None, "attribution": None, "is_placeholder": True,
        }]

    return [{
        "url": i.get("cached_path") or i.get("origin_url"),
        "caption": i.get("caption"),
        "attribution": i.get("attribution"),
        "is_placeholder": False,
    } for i in images]


def hidden_image_count(row: dict, public_mode: bool) -> int:
    """จำนวนรูปที่ถูกซ่อนเพราะสิทธิ์ — แสดงให้ผู้ใช้รู้ว่ามีรูปอยู่แต่ดูไม่ได้"""
    if not public_mode:
        return 0
    images = row.get("images") or []
    return sum(1 for i in images if i.get("usage_scope") != "publishable")
