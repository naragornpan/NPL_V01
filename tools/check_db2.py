"""Test all 3 Supabase endpoints: transaction pooler, session pooler, direct.

รหัสผ่าน/ref อ่านจาก DATABASE_URL ใน .env (หรือ env SUPABASE_DB_PASSWORD + SUPABASE_REF)
— ไม่ hardcode secret ในไฟล์ (กันหลุดเวลา repo เป็น public / อยู่ใน git history)
"""
import os
import pathlib
import socket
import sys
import urllib.parse

import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
try:
    from core.env import load_env  # โหลด .env ให้ DATABASE_URL พร้อมใช้
    load_env()
except Exception:                                      # noqa: BLE001
    pass

PW = ""
REF = ""
url = os.environ.get("DATABASE_URL", "")
if url:
    p = urllib.parse.urlparse(url)
    PW = urllib.parse.unquote(p.password or "")
    user = p.username or ""          # postgres.<ref> (pooler) หรือ postgres (direct)
    if "." in user:
        REF = user.split(".", 1)[1]
# override ด้วย env ตรง ๆ ได้ (เผื่อ direct user = postgres ไม่มี ref ใน url)
PW = os.environ.get("SUPABASE_DB_PASSWORD", PW)
REF = os.environ.get("SUPABASE_REF", REF)

if not (PW and REF):
    print("ตั้งค่า DATABASE_URL ใน .env ก่อน (หรือ env SUPABASE_DB_PASSWORD + SUPABASE_REF)")
    sys.exit(1)

tests = [
    ("Transaction pooler 6543", "aws-0-ap-northeast-2.pooler.supabase.com", 6543, f"postgres.{REF}"),
    ("Session pooler 5432",     "aws-0-ap-northeast-2.pooler.supabase.com", 5432, f"postgres.{REF}"),
    ("Direct 5432 (IPv6)",      f"db.{REF}.supabase.co",                    5432, "postgres"),
]
for label, host, port, user in tests:
    try:
        infos = socket.getaddrinfo(host, port)
        fams = sorted({("IPv6" if i[0] == socket.AF_INET6 else "IPv4") for i in infos})
        print(f"[{label}] DNS OK ({'/'.join(fams)})")
    except Exception as e:
        print(f"[{label}] DNS FAILED: {e}")
        continue
    try:
        with psycopg.connect(host=host, port=port, user=user, password=PW,
                             dbname="postgres", connect_timeout=15,
                             prepare_threshold=None) as c:
            r = c.execute("select count(*) from app_settings").fetchone()
            print(f"[{label}] CONNECT + QUERY OK, app_settings={r[0]}")
    except Exception as e:
        print(f"[{label}] FAILED: {type(e).__name__}: {str(e)[:200]}")
    print()
