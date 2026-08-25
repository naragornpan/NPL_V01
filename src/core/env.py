"""โหลดไฟล์ .env ให้อัตโนมัติ

ต้อง import โมดูลนี้เป็นอันแรกสุดในทุก entry point (web.py, run.py, run_all.py,
model_ops.py, tools/*) ก่อนที่โมดูลอื่นจะอ่าน os.environ

เดินหาไฟล์ .env ขึ้นไปตามลำดับโฟลเดอร์แม่ จึงรันจากที่ไหนก็ได้
ไม่จำเป็นต้อง cd เข้า ingest ก่อน
"""
from __future__ import annotations

import os
import pathlib


def load_env(verbose: bool = False) -> pathlib.Path | None:
    """หาและโหลด .env — คืน path ที่เจอ หรือ None

    ไม่ทับค่าที่ตั้งไว้แล้วใน environment (ตัวแปรจริงชนะไฟล์เสมอ)
    เพื่อให้ GitHub Actions ที่ส่งค่ามาทาง secrets ทำงานได้ตามปกติ
    """
    here = pathlib.Path(__file__).resolve()
    for folder in [pathlib.Path.cwd(), *here.parents]:
        candidate = folder / ".env"
        if candidate.is_file():
            _parse_into_environ(candidate)
            if verbose:
                print(f"โหลด .env จาก {candidate}")
            return candidate
    return None


def _parse_into_environ(path: pathlib.Path) -> None:
    """อ่านเอง ไม่พึ่ง library เพื่อลดสิ่งที่ต้องติดตั้ง

    รองรับ: บรรทัดว่าง, คอมเมนต์ #, เครื่องหมายคำพูดครอบค่า,
    คำว่า export นำหน้า, และ BOM ที่ Notepad ของ Windows ชอบใส่มา
    """
    raw = path.read_bytes()
    # Notepad บน Windows บันทึกได้หลาย encoding ต้องรองรับให้ครบ
    # ไม่งั้นผู้ใช้จะงงว่าทำไมไฟล์ดูถูกแต่โปรแกรมไม่เห็น
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16", errors="replace")     # "Unicode" ใน Notepad
    else:
        for enc in ("utf-8-sig", "cp874", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", errors="replace")

    for line in text.splitlines():
        # ตัด BOM ที่อาจติดมาต้นบรรทัดแรก และอักขระ zero-width
        line = line.lstrip("\ufeff\u200b").strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def describe() -> str:
    """ข้อความสรุปสถานะ ใช้แสดงตอนสตาร์ท"""
    url = os.environ.get("DATABASE_URL")
    if not url:
        return "ไม่พบ DATABASE_URL"
    # ซ่อนรหัสผ่านก่อนพิมพ์ออกจอเสมอ
    safe = url
    if "@" in url and "://" in url:
        head, _, tail = url.partition("://")
        creds, _, host = tail.partition("@")
        user = creds.split(":")[0]
        safe = f"{head}://{user}:****@{host}"
    return f"DATABASE_URL = {safe}"


# โหลดทันทีที่ import
ENV_PATH = load_env()
