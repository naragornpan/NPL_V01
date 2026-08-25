"""ตั้งค่าระบบที่ admin ปรับได้จากหน้าเว็บ

เก็บในฐานข้อมูล ไม่ใช่ไฟล์ config เพราะต้องแก้ได้ตอนรันโดยไม่ต้อง deploy ใหม่
มี cache สั้น ๆ เพื่อไม่ให้ query ทุกครั้งที่โหลดหน้า
"""
from __future__ import annotations

import time

_CACHE: dict[str, str] = {}
_CACHE_AT = 0.0
CACHE_SECONDS = 20

DEFAULTS = {
    "show_source_link": "false",
    "show_institution_name": "true",
    "show_market_code": "false",
    "contact_line_url": "",
}


def load(conn, force: bool = False) -> dict[str, str]:
    global _CACHE, _CACHE_AT
    if not force and _CACHE and time.time() - _CACHE_AT < CACHE_SECONDS:
        return _CACHE
    rows = conn.execute("select key, value from app_settings").fetchall()
    _CACHE = {**DEFAULTS, **{r["key"]: r["value"] for r in rows}}
    _CACHE_AT = time.time()
    return _CACHE


def save(conn, key: str, value: str, by: str = "admin") -> None:
    conn.execute(
        """insert into app_settings (key, value, label, updated_by, updated_at)
           values (%s, %s, %s, %s, now())
           on conflict (key) do update set
             value = excluded.value, updated_by = excluded.updated_by,
             updated_at = now()""",
        (key, value, key, by))
    conn.commit()
    invalidate()


def invalidate() -> None:
    global _CACHE_AT
    _CACHE_AT = 0.0


def as_bool(settings: dict, key: str) -> bool:
    return str(settings.get(key, DEFAULTS.get(key, "false"))).lower() == "true"


def link_allowed(settings: dict, institution_override: bool | None) -> bool:
    """สถาบันทับค่ากลางได้

    บางสัญญานายหน้ากำหนดว่าต้องลิงก์กลับไปหน้าเจ้าของทรัพย์
    จึงต้องบังคับเปิดเฉพาะเจ้านั้นได้ แม้ค่ากลางจะปิดอยู่
    """
    if institution_override is not None:
        return institution_override
    return as_bool(settings, "show_source_link")
