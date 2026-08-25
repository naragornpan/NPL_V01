"""Test all 3 Supabase endpoints: transaction pooler, session pooler, direct."""
import socket, psycopg

PW = "nanak0rn1233"
REF = "imzlimiovftrvrphasgu"
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
