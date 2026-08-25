"""PDPA — ตัดข้อมูลส่วนบุคคลก่อนลงชั้น snapshot

ประกาศขายทอดตลาดมีชื่อโจทก์ ชื่อจำเลย เลขคดี บ้านเลขที่ ซึ่งเป็นข้อมูล
ส่วนบุคคลของคนที่กำลังเดือดร้อนอยู่ ชั้น snapshot ต้องสะอาดตั้งแต่แรก

ทำไมยังเก็บ raw ไว้:
  เพื่อ re-parse ตอนที่ parser เก่งขึ้น (นี่คือความหมายของ "ละเอียดที่สุด")
  แต่ raw ต้องอยู่ในตารางที่ไม่มี policy อ่านได้ และ auto-purge ใน 30 วัน
  ห้าม expose ผ่าน API เด็ดขาด
"""
from __future__ import annotations

import re

# เลขคดี เช่น "คดีหมายเลขแดงที่ พ.1234/2565"
RE_CASE_NO = re.compile(r"(คดีหมายเลข(?:แดง|ดำ)?ที่\s*)[\w\.\-/๐-๙0-9]+")
# บ้านเลขที่ เช่น "บ้านเลขที่ 123/45"
RE_HOUSE_NO = re.compile(r"((?:บ้าน)?เลขที่\s*)[\d๐-๙]+(?:/[\d๐-๙]+)?")
# โจทก์/จำเลย ตามด้วยชื่อ
RE_PARTY = re.compile(
    r"((?:โจทก์|จำเลย|ผู้ร้อง|ลูกหนี้|ผู้ถือกรรมสิทธิ์)\s*(?:ที่\s*[\d๐-๙]+\s*)?[:：]?\s*)"
    r"(?:นาย|นาง|นางสาว|น\.ส\.|บริษัท|ห้างหุ้นส่วน)[^\s,;\n]*(?:\s+[^\s,;\n]+){0,3}"
)
RE_NATIONAL_ID = re.compile(r"\b[\d๐-๙]{13}\b")
RE_PHONE = re.compile(r"\b0[\d\-\s]{8,12}\b")

MASK = "[ตัดออกตาม PDPA]"

PII_FIELD_NAMES = {
    "plaintiff", "defendant", "debtor", "owner_name", "case_no",
    "โจทก์", "จำเลย", "ลูกหนี้", "เจ้าของ", "เลขคดี", "หมายเลขคดี",
}


def scrub_text(text: str | None) -> str | None:
    """ลบ PII ออกจากข้อความอิสระ โดยคงบริบทที่ไม่ระบุตัวไว้"""
    if not text:
        return text
    out = RE_CASE_NO.sub(rf"\1{MASK}", text)
    out = RE_PARTY.sub(rf"\1{MASK}", out)
    out = RE_HOUSE_NO.sub(rf"\1{MASK}", out)
    out = RE_NATIONAL_ID.sub(MASK, out)
    out = RE_PHONE.sub(MASK, out)
    return out


def scrub_fields(fields: dict) -> dict:
    """ตัดทั้งชื่อฟิลด์ที่รู้ว่าเป็น PII และเนื้อหาใน value"""
    clean: dict = {}
    for key, value in fields.items():
        if key.strip().lower() in PII_FIELD_NAMES:
            continue
        clean[key] = scrub_text(value) if isinstance(value, str) else value
    return clean


def assert_clean(fields: dict) -> None:
    """เรียกก่อน insert — ถ้าหลุดให้ระเบิดตอน dev ดีกว่าไปโผล่ใน production"""
    blob = " ".join(str(v) for v in fields.values() if v is not None)
    # ตัด URL ออกก่อนสแกน — เลขในลิงก์รูป/พาธ (เช่น hash 13 หลักในชื่อไฟล์รูป
    # ของ ttb/PAMCO) ไม่ใช่ PII แต่เผลอไปชน regex เลขบัตรประชาชน 13 หลัก
    # เลขบัตร/เลขคดี "จริง" อยู่ในข้อความ (ที่อยู่/ชื่อ) ซึ่งยังตรวจเจอปกติ
    scan = re.sub(r"https?://\S+", " ", blob)
    for pattern, label in (
        (RE_NATIONAL_ID, "เลขบัตรประชาชน"),
        (RE_CASE_NO, "เลขคดี"),
    ):
        if pattern.search(scan):
            raise ValueError(f"พบ {label} ในข้อมูลที่กำลังจะบันทึก — ตรวจ parser")
