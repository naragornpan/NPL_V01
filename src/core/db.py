"""Database layer — psycopg3 ต่อตรงเข้า Supabase Postgres"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


SPECIAL_IN_PASSWORD = set("%&#?/:@ +")


def dsn() -> str:
    """คืน connection string ที่ปลอดภัยต่อการ parse

    ปัญหาที่เจอจริง: รหัสผ่านที่มี % & # ? / : @ ทำให้ psycopg แปลง URL ไม่ได้
    เช่น "%" ท้ายรหัสจะถูกอ่านเป็นรหัส percent-encoding แล้ว error ว่า
    invalid percent-encoded token

    ฟังก์ชันนี้ encode เฉพาะส่วนรหัสผ่านให้อัตโนมัติ
    แต่ทางที่ดีกว่าคือตั้งรหัสเป็นตัวอักษรกับตัวเลขล้วนตั้งแต่แรก
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "ไม่พบ DATABASE_URL — ดูค่าได้ที่ Supabase > Project Settings > Database "
            "แล้วใส่ในไฟล์ .env ที่โฟลเดอร์ ingest"
        )
    return _encode_password(url)


def _encode_password(url: str) -> str:
    from urllib.parse import quote

    if "://" not in url or "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    creds, sep, host = rest.rpartition("@")   # rpartition กันกรณี @ อยู่ในรหัสผ่าน
    if not sep or ":" not in creds:
        return url

    user, _, password = creds.partition(":")
    if not any(c in SPECIAL_IN_PASSWORD for c in password):
        return url                            # ไม่มีอักขระอันตราย ไม่ต้องแตะ

    # ถ้า encode ไว้แล้ว (มี % ตามด้วยเลขฐานสิบหกสองตัว) อย่า encode ซ้ำ
    import re
    if re.search(r"%[0-9A-Fa-f]{2}", password):
        return url

    safe = quote(password, safe="")
    return f"{scheme}://{user}:{safe}@{host}"


@contextmanager
def connect():
    """เปิด connection ที่ใช้ได้กับ connection pooler ของ Supabase

    prepare_threshold=None สำคัญมาก
        Transaction pooler (pgbouncer โหมด transaction) สลับ connection จริง
        ไปมาระหว่าง query แต่ psycopg3 เตรียม prepared statement ไว้อัตโนมัติ
        พอชื่อซ้ำกันคนละ connection จะได้ error
        "prepared statement _pg3_0 already exists"

        ปิดไปเลยง่ายที่สุด เสียประสิทธิภาพเล็กน้อยแต่ทำงานได้ทุกโหมด
    """
    import time

    # สำคัญ: retry ครอบเฉพาะ "การเชื่อมต่อ" เท่านั้น ห้ามครอบ yield
    #
    # บั๊กที่เคยพลาด: เขียน yield ไว้ในบล็อก try/except OperationalError เดียวกัน
    # พอ error เกิดขึ้น *ระหว่างที่โค้ดข้างนอกใช้ conn อยู่* (ไม่ใช่ตอนเชื่อมต่อ)
    # Python จะ throw() exception นั้นกลับเข้ามาที่จุด yield แต่โค้ดกลับไป
    # catch แล้ววนลอง connect ใหม่ ซึ่งผิดกติกาของ generator-based context
    # manager ทำให้เกิด "generator didn't stop after throw()" แทนที่จะเห็น
    # error ที่แท้จริง
    #
    # ทางแก้: แยกขั้นเชื่อมต่อ (retry ได้) ออกจากขั้นใช้งาน (retry ไม่ได้
    # และไม่ควร retry ด้วย เพราะถ้า query กลางทางพังคือพังจริง ต้องให้เห็น)
    conn = None
    last_exc: Exception | None = None
    for attempt, timeout in enumerate((10, 20, 20), start=1):
        try:
            conn = psycopg.connect(dsn(), row_factory=dict_row,
                                   prepare_threshold=None,
                                   connect_timeout=timeout)
            break
        except psycopg.OperationalError as exc:
            last_exc = exc
            if attempt < 3:
                time.sleep(2)

    if conn is None:
        msg = str(last_exc)
        lower = msg.lower()
        if "nxdomain" in lower or "name or service not known" in lower:
            hint = ("หา host ฐานข้อมูลไม่เจอ (DNS) — เช็คเน็ต, ลอง ipconfig /flushdns, "
                    "หรือดูว่า Supabase project ถูกพักอยู่ไหม (เข้า dashboard กด Resume)")
        elif "timeout" in lower:
            hint = ("เชื่อมต่อได้แต่ timeout ระหว่างทาง (ลอง 3 ครั้งแล้วไม่สำเร็จ) — "
                    "1) เข้า Supabase dashboard เช็คว่า project pause อยู่ไหม "
                    "2) ลองสลับจาก Transaction pooler (พอร์ต 6543) เป็น "
                    "Session pooler (พอร์ต 5432) ในหน้า Connect "
                    "3) ลองเน็ตอื่น (มือถือปล่อยเน็ต) เผื่อ Wi-Fi/VPN บล็อกพอร์ตนี้")
        else:
            hint = f"ต่อฐานข้อมูลไม่ได้: {msg[:200]}"
        raise RuntimeError(hint) from last_exc

    # จุดนี้เชื่อมต่อสำเร็จแล้ว — ปล่อยให้ error ระหว่างใช้งานไหลออกไปตามปกติ
    # ไม่ retry ไม่กลืน exception เพราะถ้า query พังกลางทาง ต้องเห็น error จริง
    try:
        yield conn
    finally:
        conn.close()


class Repo:
    def __init__(self, conn):
        self.conn = conn

    # --- runs ---------------------------------------------------------
    def start_run(self, source_code: str) -> uuid.UUID:
        row = self.conn.execute(
            "insert into ingest_runs (source_code) values (%s) returning id",
            (source_code,),
        ).fetchone()
        self.conn.commit()
        return row["id"]

    def finish_run(self, run_id, status: str, **counts) -> None:
        self.conn.execute(
            """update ingest_runs set finished_at = now(), status = %s,
                   pages_fetched = %s, rows_parsed = %s, rows_new = %s,
                   error_count = %s, error_sample = %s
               where id = %s""",
            (
                status,
                counts.get("pages_fetched", 0),
                counts.get("rows_parsed", 0),
                counts.get("rows_new", 0),
                counts.get("error_count", 0),
                counts.get("error_sample"),
                run_id,
            ),
        )
        self.conn.commit()

    # --- raw ----------------------------------------------------------
    def save_raw(self, source_code, run_id, resp) -> uuid.UUID | None:
        """ข้ามถ้าเคยเห็น content เดิมแล้ว — ประหยัดที่และบอกได้ว่าหน้าไม่เปลี่ยน"""
        existing = self.conn.execute(
            "select id from raw_documents where source_code = %s and content_hash = %s limit 1",
            (source_code, resp.content_hash),
        ).fetchone()
        if existing:
            return existing["id"]
        row = self.conn.execute(
            """insert into raw_documents
                 (source_code, run_id, url, http_status, content_hash, body)
               values (%s, %s, %s, %s, %s, %s) returning id""",
            (source_code, run_id, resp.url, resp.status, resp.content_hash, resp.text),
        ).fetchone()
        self.conn.commit()
        return row["id"]

    def purge_expired_raw(self) -> int:
        cur = self.conn.execute("delete from raw_documents where purge_after < now()")
        self.conn.commit()
        return cur.rowcount

    # --- snapshots ----------------------------------------------------
    def save_snapshot(self, snap: dict) -> bool:
        """คืน True ถ้าเป็น snapshot ใหม่จริง (ไม่ซ้ำ content_hash เดิม)"""
        cols = [k for k in snap if k != "raw_fields"]
        placeholders = ", ".join(["%s"] * (len(cols) + 1))
        sql = f"""
            insert into listing_snapshots ({', '.join(cols)}, raw_fields)
            values ({placeholders})
            on conflict (source_code, external_ref, content_hash) do nothing
            returning id
        """
        # default=str: กัน TypeError เมื่อ raw_fields มี date/Decimal/ฯลฯ
        # (เช่น LED เก็บ auction_date เป็น date object) ให้ซีเรียลไลซ์เป็นสตริง
        values = [snap[c] for c in cols] + [json.dumps(snap.get("raw_fields", {}),
                                                       ensure_ascii=False,
                                                       default=str)]
        row = self.conn.execute(sql, values).fetchone()
        self.conn.commit()
        return row is not None

    def log_parse_failure(self, run_id, raw_id, reason: str) -> None:
        self.conn.execute(
            "insert into parse_failures (run_id, raw_document_id, reason) values (%s,%s,%s)",
            (run_id, raw_id, reason[:2000]),
        )
        self.conn.commit()

    def get_source(self, code: str) -> dict:
        row = self.conn.execute("select * from sources where code = %s", (code,)).fetchone()
        if not row:
            raise RuntimeError(f"ไม่พบ source '{code}' ใน DB — รัน schema.sql หรือยัง")
        return row
