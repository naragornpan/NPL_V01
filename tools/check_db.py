"""Diagnose DATABASE_URL: where it comes from and whether it connects."""
import os, sys, re, pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

def mask(url):
    return re.sub(r"(://[^:]+:)[^@]+@", r"\1*****@", url) if url else url

pre = os.environ.get("DATABASE_URL")
print("1) DATABASE_URL in Windows environment BEFORE loading .env:")
print("   ", mask(pre) if pre else "(not set - good, .env file will be used)")

from core.env import load_env
loaded = load_env()
print("2) .env file loaded from:", loaded)

eff = os.environ.get("DATABASE_URL")
print("3) Effective DATABASE_URL:", mask(eff))

if pre and loaded and pre != eff:
    print("   NOTE: env var was already set; .env did NOT override it")
if pre:
    print("   >>> A stale Windows env var will OVERRIDE the .env file! <<<")

print("4) Connecting + SELECT 1 ...")
import psycopg
try:
    with psycopg.connect(eff, connect_timeout=15, prepare_threshold=None) as c:
        print("   connect OK ->", c.execute("select current_database(), inet_server_addr()").fetchone())
        print("   app_settings rows:", c.execute("select count(*) from app_settings").fetchone())
    print("RESULT: CONNECTION WORKS")
except Exception as e:
    print("RESULT: FAILED ->", type(e).__name__, str(e)[:300])
