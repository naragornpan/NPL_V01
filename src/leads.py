"""
leads.py — โมดูลขายลีดบริการเรื่องบ้าน (lead fee) สำหรับแปลงดี

วิธีต่อเข้า web.py (3 บรรทัด ท้ายไฟล์ ก่อน uvicorn.run):

    import leads
    leads.install_templates(TEMPLATES)          # เติม template เข้า DictLoader เดิม
    leads.install(app, conn=conn, render=render, require_admin=_require_admin,
                  secret_key=SECRET_KEY)

สัญญากับ web.py (ถ้าชื่อไม่ตรง ส่ง lambda มาแทนได้):
  conn()          -> context manager คืน psycopg connection
  render(name, **ctx) -> HTMLResponse  (ตัวเดิมที่ใช้กับ DictLoader)
  require_admin(request, token) -> None ถ้าผ่าน / RedirectResponse ถ้าไม่ผ่าน

กติกาการเงิน (ตั้งใจให้ไม่ต้องมี CS / ไม่ต้องตามหนี้):
  - ร้าน "เติมเครดิตล่วงหน้า" เท่านั้น ไม่มีเครดิตติดลบ ไม่มีการวางบิล
  - หักเครดิตตอน "ส่งลีด" ไม่ใช่ตอนปิดงาน — เราไม่รู้และไม่ต้องรู้ว่าปิดงานได้ไหม
  - เบอร์ลูกค้าเห็นได้เฉพาะร้านที่ถูกหักเครดิตแล้ว
  - ร้านขอคืนเครดิตได้ถ้าติดต่อลูกค้าไม่ได้ (admin กดอนุมัติ)
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

DEPS: dict = {}

LINE_PUSH_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
BASE_URL = os.getenv("BASE_URL", "https://www.plaengdee.com").rstrip("/")
LEAD_RATE_PER_HOUR = int(os.getenv("LEAD_RATE_PER_HOUR", "3"))
# เครดิตแถมตอนแอดมินกด active ครั้งแรก (ให้ร้านได้ลองก่อนควักเงิน)
WELCOME_CREDIT = float(os.getenv("PARTNER_WELCOME_CREDIT", "200"))

_cat_cache: dict = {"at": 0.0, "rows": []}
_prov_cache: dict = {"at": 0.0, "rows": []}


# ── helpers ──────────────────────────────────────────────────────────────────
def _rows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _f(v, default=0.0):
    """psycopg คืน numeric เป็น Decimal — ต้อง float() ก่อนคำนวณผสม float"""
    return default if v is None else float(v)


def _sig(provider_id: str) -> str:
    key = (DEPS.get("secret_key") or "dev").encode()
    return hmac.new(key, str(provider_id).encode(), hashlib.sha256).hexdigest()[:16]


def _check_sig(provider_id: str, k: str) -> bool:
    return bool(k) and hmac.compare_digest(_sig(provider_id), k)


def _ip_hash(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() or (request.client.host if request.client else "")
    return hashlib.sha256((ip + "plaengdee").encode()).hexdigest()[:32]


def _norm_phone(s: str) -> str:
    digits = "".join(ch for ch in (s or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 9 else ""


def _mask_phone(p: str) -> str:
    return f"{p[:3]}-xxx-{p[-2:]}" if p and len(p) >= 9 else "xxx-xxx-xxxx"


async def _form_multi(request: Request) -> dict:
    """อ่านฟอร์ม url-encoded เองด้วย stdlib (ไม่พึ่ง python-multipart) — คืนค่าเป็น list ทุกคีย์"""
    raw = (await request.body()).decode("utf-8", "ignore")
    return urllib.parse.parse_qs(raw, keep_blank_values=True)


async def _form(request: Request) -> dict:
    """เวอร์ชันค่าเดียวต่อคีย์ (ใช้กับฟอร์มทั่วไปที่ไม่มี checkbox หลายค่า)"""
    return {k: v[0] for k, v in (await _form_multi(request)).items()}


def categories(active_only: bool = True) -> list[dict]:
    if time.time() - _cat_cache["at"] < 300 and _cat_cache["rows"]:
        rows = _cat_cache["rows"]
    else:
        with DEPS["conn"]() as c, c.cursor() as cur:
            cur.execute(
                "select code, name, emoji, lead_price, max_fanout, is_active "
                "from service_categories order by sort, code"
            )
            rows = _rows(cur)
        _cat_cache.update(at=time.time(), rows=rows)
    return [r for r in rows if r["is_active"]] if active_only else list(rows)


def provinces() -> list[str]:
    """รายชื่อจังหวัดสำหรับฟอร์มสมัคร — เอาจาก core.geocode ถ้ามี ไม่งั้นถามฐานข้อมูล"""
    if time.time() - _prov_cache["at"] < 3600 and _prov_cache["rows"]:
        return _prov_cache["rows"]
    rows: list[str] = []
    try:
        from core.geocode import PROVINCE_CENTROIDS  # type: ignore
        rows = sorted(PROVINCE_CENTROIDS.keys())
    except Exception:
        try:
            with DEPS["conn"]() as c, c.cursor() as cur:
                cur.execute(
                    "select distinct province from listing_snapshots "
                    "where province is not null and province <> '' order by province")
                rows = [r[0] for r in cur.fetchall()]
        except Exception as e:
            print(f"[leads] province list failed: {e}")
    _prov_cache.update(at=time.time(), rows=rows)
    return rows


# ── core: จับคู่ + หักเครดิต ─────────────────────────────────────────────────
def _match_providers(cur, cat: str, province: str, district: str, price: float, fanout: int):
    cur.execute(
        """
        select p.id, p.name, p.contact_phone, p.line_user_id, p.credit_balance
        from service_providers p
        where p.status = 'active'
          and %s = any(p.categories)
          and (cardinality(p.provinces) = 0 or %s = any(p.provinces))
          and (cardinality(p.districts) = 0 or %s = any(p.districts))
          and p.credit_balance >= %s
          and (
            select count(*) from lead_deliveries d
            where d.provider_id = p.id
              and d.status = 'charged'
              and d.created_at >= date_trunc('day', now())
          ) < p.daily_lead_cap
        order by p.rating desc nulls last, random()
        limit %s
        """,
        (cat, province or "", district or "", price, fanout),
    )
    return _rows(cur)


def _charge(cur, provider_id, lead_id: int, price: float) -> bool:
    """หักเครดิตแบบ atomic — ถ้าเครดิตไม่พอ (แข่งกันหลาย request) จะไม่หักและคืน False"""
    cur.execute(
        "update service_providers set credit_balance = credit_balance - %s "
        "where id = %s and credit_balance >= %s returning credit_balance",
        (price, provider_id, price),
    )
    row = cur.fetchone()
    if not row:
        return False
    balance_after = _f(row[0])
    cur.execute(
        "insert into lead_deliveries (lead_id, provider_id, price) values (%s, %s, %s) "
        "on conflict (lead_id, provider_id) do nothing returning id",
        (lead_id, provider_id, price),
    )
    got = cur.fetchone()
    if not got:  # ส่งซ้ำ — คืนเครดิตกลับ
        cur.execute(
            "update service_providers set credit_balance = credit_balance + %s where id = %s",
            (price, provider_id),
        )
        return False
    cur.execute(
        "insert into provider_credit_ledger (provider_id, delta, reason, ref_id, balance_after) "
        "values (%s, %s, 'lead_charge', %s, %s)",
        (provider_id, -price, got[0], balance_after),
    )
    return True


def _notify(provider: dict, lead: dict, cat_name: str):
    """แจ้งร้านว่ามีลีดใหม่ — เงียบถ้าไม่ได้ตั้ง LINE token"""
    if not (LINE_PUSH_TOKEN and provider.get("line_user_id")):
        return
    link = f"{BASE_URL}/partner/{provider['id']}?k={_sig(provider['id'])}"
    text = (
        f"🔔 ลีดใหม่ — {cat_name}\n"
        f"ทำเล: {lead.get('province') or '-'} {lead.get('district') or ''}\n"
        f"ลูกค้า: {lead.get('customer_name')}\n"
        f"ดูเบอร์/รายละเอียด: {link}"
    )
    body = ('{"to":"%s","messages":[{"type":"text","text":%s}]}'
            % (provider["line_user_id"], _json_str(text))).encode()
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push",
        data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {LINE_PUSH_TOKEN}"},
    )
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:  # ไม่ให้ลีดพังเพราะ LINE ล่ม
        print(f"[leads] line push failed: {e}")


def _json_str(s: str) -> str:
    import json
    return json.dumps(s, ensure_ascii=False)


def _lead_coords(cur, source_code, external_ref):
    """พิกัดของลีด = พิกัดทรัพย์ที่กดมา (ถ้ามา จากหน้าทรัพย์) — ลูกค้าไม่ต้องกรอกเอง

    ลีดที่มาจากหน้า zone/หน้าแรกจะไม่มี source/ref → คืน (None, None)
    ใช้สำหรับปักหมุด/เรียงตามรัศมีในอนาคต ไม่กระทบการจับคู่ปัจจุบัน (ยังใช้จังหวัด/อำเภอ)
    """
    if not (source_code and external_ref):
        return (None, None)
    try:
        cur.execute(
            "select lat, lng from listing_snapshots "
            "where source_code = %s and external_ref = %s and lat is not null "
            "order by observed_at desc limit 1",
            (source_code, external_ref))
        row = cur.fetchone()
        if row and row[0] is not None:
            return (float(row[0]), float(row[1]) if row[1] is not None else None)
    except Exception as e:                                    # noqa: BLE001
        print(f"[leads] lead coords lookup failed: {e}")
    return (None, None)


def _province_coords(province: str):
    """ฐานที่ตั้งร้าน (คร่าว ๆ) = ศูนย์กลางจังหวัด — static ไม่ยิง network"""
    if not province:
        return (None, None)
    try:
        from core.geocode import PROVINCE_CENTROIDS  # type: ignore
        c = PROVINCE_CENTROIDS.get(province.strip())
        if c:
            return (float(c[0]), float(c[1]))
    except Exception:                                         # noqa: BLE001
        pass
    return (None, None)


def create_lead(data: dict) -> dict:
    """สร้างลีด + จับคู่ + หักเครดิต — คืน {lead_id, delivered, price}"""
    cat = (data.get("category") or "").strip()
    phone = _norm_phone(data.get("phone"))
    name = (data.get("name") or "").strip()[:80]
    if not (cat and phone and name):
        return {"error": "กรอกชื่อ เบอร์ และเลือกบริการให้ครบ"}
    if not data.get("consent"):
        return {"error": "ต้องยินยอมให้ส่งข้อมูลติดต่อให้ผู้ให้บริการก่อน"}

    cats = {c["code"]: c for c in categories()}
    if cat not in cats:
        return {"error": "ไม่พบบริการที่เลือก"}
    price = _f(cats[cat]["lead_price"])
    fanout = int(cats[cat]["max_fanout"] or 3)

    province = (data.get("province") or "").strip()
    district = (data.get("district") or "").strip()

    with DEPS["conn"]() as c, c.cursor() as cur:
        # rate limit ต่อ IP
        cur.execute(
            "select count(*) from service_leads where ip_hash = %s and created_at > now() - interval '1 hour'",
            (data.get("ip_hash"),),
        )
        if (cur.fetchone() or [0])[0] >= LEAD_RATE_PER_HOUR:
            return {"error": "ส่งคำขอถี่เกินไป ลองใหม่ในอีก 1 ชั่วโมง"}

        # พิกัดลีดจากทรัพย์ที่กดมา (ถ้ามี) — เก็บไว้ปักหมุด/เรียงรัศมีภายหลัง
        lead_lat, lead_lng = _lead_coords(cur, data.get("source_code"), data.get("external_ref"))
        cur.execute(
            """insert into service_leads
               (category_code, province, district, source_code, external_ref,
                customer_name, customer_phone, customer_line, detail, consent, ip_hash,
                lat, lng)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,true,%s,%s,%s) returning id""",
            (cat, province, district, data.get("source_code"), data.get("external_ref"),
             name, phone, (data.get("line") or "")[:60], (data.get("detail") or "")[:600],
             data.get("ip_hash"), lead_lat, lead_lng),
        )
        lead_id = cur.fetchone()[0]

        providers = _match_providers(cur, cat, province, district, price, fanout)
        lead_ctx = {"province": province, "district": district, "customer_name": name}
        sent = []
        for p in providers:
            if _charge(cur, p["id"], lead_id, price):
                sent.append(p)

        cur.execute(
            "update service_leads set status = %s where id = %s",
            ("delivered" if sent else "no_provider", lead_id),
        )
        c.commit()

    for p in sent:
        _notify(p, lead_ctx, cats[cat]["name"])
    return {"lead_id": lead_id, "delivered": len(sent), "price": price}


def refund_delivery(delivery_id: int, note: str = "") -> bool:
    with DEPS["conn"]() as c, c.cursor() as cur:
        cur.execute(
            "select provider_id, price, status from lead_deliveries where id = %s for update",
            (delivery_id,),
        )
        row = cur.fetchone()
        if not row or row[2] == 'refunded':
            return False
        provider_id, price = row[0], _f(row[1])
        cur.execute(
            "update service_providers set credit_balance = credit_balance + %s "
            "where id = %s returning credit_balance",
            (price, provider_id),
        )
        balance_after = _f((cur.fetchone() or [0])[0])
        cur.execute(
            "update lead_deliveries set status = 'refunded' where id = %s", (delivery_id,)
        )
        cur.execute(
            "insert into provider_credit_ledger (provider_id, delta, reason, ref_id, balance_after, note) "
            "values (%s,%s,'refund',%s,%s,%s)",
            (provider_id, price, delivery_id, balance_after, note[:200]),
        )
        c.commit()
    return True


def topup(provider_id: str, amount: float, note: str = "") -> float:
    with DEPS["conn"]() as c, c.cursor() as cur:
        cur.execute(
            "update service_providers set credit_balance = credit_balance + %s "
            "where id = %s returning credit_balance",
            (amount, provider_id),
        )
        row = cur.fetchone()
        if not row:
            return 0.0
        balance_after = _f(row[0])
        cur.execute(
            "insert into provider_credit_ledger (provider_id, delta, reason, balance_after, note) "
            "values (%s,%s,'topup',%s,%s)",
            (provider_id, amount, balance_after, note[:200]),
        )
        c.commit()
    return balance_after


# ── templates (เติมเข้า DictLoader เดิมของ web.py) ───────────────────────────
SERVICE_BLOCK = """
<section class="sheet p-4 mt-6" id="svcbox">
  <h2 class="text-lg font-semibold">บริการเรื่องบ้านใน{{ svc_zone or 'ทำเลนี้' }}</h2>
  <p class="text-sm text-[var(--pencil)] mt-1">
    กรอกครั้งเดียว ผู้ให้บริการในพื้นที่จะติดต่อกลับหาคุณ ไม่มีค่าใช้จ่ายฝั่งคุณ
  </p>
  <form method="post" action="/api/service-lead" class="mt-3 grid gap-2 sm:grid-cols-2">
    <input type="hidden" name="province" value="{{ svc_province or '' }}">
    <input type="hidden" name="district" value="{{ svc_district or '' }}">
    <input type="hidden" name="source_code" value="{{ svc_source or '' }}">
    <input type="hidden" name="external_ref" value="{{ svc_ref or '' }}">
    <input type="text" name="website" class="hidden" tabindex="-1" autocomplete="off">
    <select name="category" required class="border rounded-lg px-3 py-2 sm:col-span-2">
      <option value="">เลือกบริการที่ต้องการ</option>
      {% for c in svc_categories %}<option value="{{ c.code }}">{{ c.emoji }} {{ c.name }}</option>{% endfor %}
    </select>
    <input name="name" required maxlength="80" placeholder="ชื่อผู้ติดต่อ"
           class="border rounded-lg px-3 py-2">
    <input name="phone" required inputmode="tel" placeholder="เบอร์โทร"
           class="border rounded-lg px-3 py-2">
    <textarea name="detail" rows="2" maxlength="600" placeholder="รายละเอียดสั้น ๆ เช่น คอนโด 35 ตร.ม. อยากได้เสาร์เช้า"
              class="border rounded-lg px-3 py-2 sm:col-span-2"></textarea>
    <label class="text-xs text-[var(--pencil)] sm:col-span-2 flex gap-2 items-start">
      <input type="checkbox" name="consent" value="1" required class="mt-0.5">
      <span>ยินยอมให้ส่งชื่อและเบอร์ให้ผู้ให้บริการในพื้นที่ (สูงสุด 3 ราย) เพื่อติดต่อกลับ</span>
    </label>
    <button class="rounded-lg px-4 py-2 text-white font-medium sm:col-span-2"
            style="background:var(--survey)">ให้ผู้ให้บริการติดต่อกลับ</button>
  </form>
  <p class="text-xs text-[var(--pencil)] mt-2">
    แปลงดีเป็นตัวกลางส่งต่อผู้ติดต่อเท่านั้น ไม่ใช่คู่สัญญา และไม่รับผิดชอบต่อคุณภาพงานหรือราคาที่ตกลงกันเอง
  </p>
</section>
"""

PARTNER_HTML = """
{% extends "layout.html" %}{% block body %}
<div class="sheet p-4">
  <h1 class="text-xl font-semibold">{{ p.name }}</h1>
  <div class="mt-2 flex flex-wrap gap-4 items-baseline">
    <div><span class="text-3xl font-bold" style="color:var(--survey)">{{ '%.0f'|format(p.credit_balance) }}</span>
         <span class="text-sm text-[var(--pencil)]">เครดิตคงเหลือ</span></div>
    <div class="text-sm text-[var(--pencil)]">รับลีดวันนี้ {{ used_today }}/{{ p.daily_lead_cap }}</div>
  </div>
  {% if p.credit_balance < 100 %}
  <p class="mt-2 text-sm px-3 py-2 rounded-lg" style="background:#FDECEA;color:var(--seal)">
    เครดิตใกล้หมด — เมื่อเครดิตไม่พอ ระบบจะข้ามร้านคุณและส่งลีดให้รายอื่นแทน
  </p>{% endif %}
</div>

<div class="sheet p-4 mt-4">
  <h2 class="font-semibold">ลีดที่ได้รับ</h2>
  <div class="overflow-x-auto mt-2">
  <table class="w-full text-sm min-w-[640px]">
    <thead class="text-left text-[var(--pencil)]"><tr>
      <th class="py-1">วันที่</th><th>บริการ</th><th>ทำเล</th><th>ลูกค้า</th><th>เบอร์</th><th>สถานะ</th><th></th>
    </tr></thead>
    <tbody>
    {% for d in deliveries %}
      <tr class="border-t">
        <td class="py-2 whitespace-nowrap">{{ d.created_at.strftime('%d/%m %H:%M') }}</td>
        <td>{{ d.cat_name }}</td>
        <td>{{ d.province }} {{ d.district }}</td>
        <td>{{ d.customer_name }}</td>
        <td>
          {% if d.status == 'charged' %}<a class="font-medium" href="tel:{{ d.customer_phone }}">{{ d.customer_phone }}</a>
          {% else %}<span class="text-[var(--pencil)]">คืนเครดิตแล้ว</span>{% endif %}
        </td>
        <td>{% if d.status == 'charged' %}−{{ '%.0f'|format(d.price) }} เครดิต{% else %}คืนแล้ว{% endif %}</td>
        <td class="text-right">
          {% if d.status == 'charged' and not d.refund_reason %}
          <form method="post" action="/partner/{{ p.id }}/refund/{{ d.id }}?k={{ k }}">
            <button class="text-xs underline text-[var(--pencil)]">ติดต่อไม่ได้ ขอคืนเครดิต</button>
          </form>
          {% elif d.refund_reason %}<span class="text-xs text-[var(--pencil)]">รอตรวจสอบ</span>{% endif %}
        </td>
      </tr>
    {% else %}
      <tr><td colspan="7" class="py-6 text-center text-[var(--pencil)]">ยังไม่มีลีด — ลีดใหม่จะขึ้นที่นี่และแจ้งทาง LINE</td></tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
  {% if d_note %}<p class="text-sm mt-2" style="color:var(--survey)">{{ d_note }}</p>{% endif %}
</div>
{% endblock %}
"""

ADMIN_LEADS_HTML = """
{% extends "layout.html" %}{% block body %}
<h1 class="text-xl font-semibold">ลีดบริการ</h1>
<div class="sheet p-4 mt-3 grid grid-cols-2 sm:grid-cols-4 gap-3 text-center">
  <div><div class="text-2xl font-bold">{{ stat.leads_30 }}</div><div class="text-xs text-[var(--pencil)]">ลีด 30 วัน</div></div>
  <div><div class="text-2xl font-bold">{{ stat.delivered_30 }}</div><div class="text-xs text-[var(--pencil)]">ส่งสำเร็จ</div></div>
  <div><div class="text-2xl font-bold">{{ stat.nomatch_30 }}</div><div class="text-xs text-[var(--pencil)]">ไม่มีร้านรับ</div></div>
  <div><div class="text-2xl font-bold" style="color:var(--survey)">{{ '%.0f'|format(stat.revenue_30) }}</div><div class="text-xs text-[var(--pencil)]">รายได้ (บาท)</div></div>
</div>

<div class="sheet p-4 mt-4 overflow-x-auto">
  <table class="w-full text-sm min-w-[760px]">
    <thead class="text-left text-[var(--pencil)]"><tr>
      <th class="py-1">วันที่</th><th>บริการ</th><th>ทำเล</th><th>ลูกค้า</th><th>เบอร์</th><th>ส่งให้</th><th>รายได้</th>
    </tr></thead>
    <tbody>
    {% for l in leads %}
      <tr class="border-t">
        <td class="py-2 whitespace-nowrap">{{ l.created_at.strftime('%d/%m %H:%M') }}</td>
        <td>{{ l.cat_name or l.category_code }}</td>
        <td>{{ l.province }} {{ l.district }}</td>
        <td>{{ l.customer_name }}</td>
        <td>{{ l.customer_phone }}</td>
        <td>{{ l.n_sent }}</td>
        <td>{{ '%.0f'|format(l.revenue) }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
</div>

{% if refunds %}
<div class="sheet p-4 mt-4">
  <h2 class="font-semibold">คำขอคืนเครดิต</h2>
  {% for r in refunds %}
  <form method="post" action="/admin/leads/refund" class="flex flex-wrap gap-2 items-center border-t py-2 text-sm">
    <input type="hidden" name="delivery_id" value="{{ r.id }}">
    <span class="grow">{{ r.provider_name }} · ลีด #{{ r.lead_id }} · {{ r.customer_phone }} · เหตุผล: {{ r.refund_reason }}</span>
    <button class="rounded px-3 py-1 text-white" style="background:var(--survey)">คืนเครดิต {{ '%.0f'|format(r.price) }}</button>
  </form>
  {% endfor %}
</div>
{% endif %}
{% endblock %}
"""

ADMIN_PROVIDERS_HTML = """
{% extends "layout.html" %}{% block body %}
<div class="flex flex-wrap gap-2 items-baseline">
  <h1 class="text-xl font-semibold">ผู้ให้บริการ</h1>
  <a href="/partner/apply" class="text-sm underline text-[var(--pencil)]">ดูหน้าสมัคร (ลิงก์ที่ส่งให้ร้าน)</a>
</div>
{% set pending = providers|selectattr('status','equalto','pending')|list %}
{% if pending %}
<p class="mt-2 text-sm px-3 py-2 rounded-lg" style="background:#FFF6E5">
  มีใบสมัครรอตรวจ {{ pending|length }} ราย — เปลี่ยนสถานะเป็น active เพื่อเปิดรับลีด
  (ได้เครดิตทดลองอัตโนมัติครั้งแรก)
</p>
{% endif %}

<div class="sheet p-4 mt-3">
  <h2 class="font-semibold">เพิ่มร้านเอง</h2>
  <form method="post" action="/admin/providers" class="grid sm:grid-cols-2 gap-2 mt-2 text-sm">
    <input name="name" required placeholder="ชื่อร้าน/ผู้ให้บริการ" class="border rounded-lg px-3 py-2">
    <input name="contact_phone" required placeholder="เบอร์ติดต่อ" class="border rounded-lg px-3 py-2">
    <input name="categories" required placeholder="หมวด คั่นด้วย , เช่น cleaning,aircon" class="border rounded-lg px-3 py-2">
    <input name="provinces" placeholder="จังหวัด คั่นด้วย , (ว่าง = ทุกจังหวัด)" class="border rounded-lg px-3 py-2">
    <input name="districts" placeholder="อำเภอ/เขต คั่นด้วย , (ว่าง = ทั้งจังหวัด)" class="border rounded-lg px-3 py-2">
    <input name="line_user_id" placeholder="LINE userId (ถ้ามี ใช้แจ้งลีด)" class="border rounded-lg px-3 py-2">
    <button class="rounded-lg px-4 py-2 text-white sm:col-span-2" style="background:var(--survey)">เพิ่มร้าน</button>
  </form>
</div>

<div class="sheet p-4 mt-4 overflow-x-auto">
  <table class="w-full text-sm min-w-[820px]">
    <thead class="text-left text-[var(--pencil)]"><tr>
      <th class="py-1">ร้าน</th><th>หมวด</th><th>พื้นที่</th><th>เครดิต</th><th>ลีดรวม</th><th>สถานะ</th><th>ลิงก์ร้าน</th><th>เติมเครดิต</th>
    </tr></thead>
    <tbody>
    {% for p in providers %}
      <tr class="border-t align-top">
        <td class="py-2">{{ p.name }}<div class="text-xs text-[var(--pencil)]">{{ p.contact_phone }}</div></td>
        <td class="text-xs">{{ p.categories|join(', ') }}</td>
        <td class="text-xs">{{ p.provinces|join(', ') or 'ทุกจังหวัด' }}</td>
        <td class="font-medium">{{ '%.0f'|format(p.credit_balance) }}</td>
        <td>{{ p.n_leads }}</td>
        <td>
          <form method="post" action="/admin/providers/status">
            <input type="hidden" name="provider_id" value="{{ p.id }}">
            <select name="status" onchange="this.form.submit()" class="border rounded px-1 py-0.5 text-xs">
              {% for s in ['pending','active','paused','banned'] %}
              <option value="{{ s }}" {% if p.status==s %}selected{% endif %}>{{ s }}</option>{% endfor %}
            </select>
          </form>
        </td>
        <td class="text-xs"><a class="underline" href="/partner/{{ p.id }}?k={{ p.k }}">เปิดหน้าร้าน</a></td>
        <td>
          <form method="post" action="/admin/providers/topup" class="flex gap-1">
            <input type="hidden" name="provider_id" value="{{ p.id }}">
            <input name="amount" inputmode="numeric" placeholder="500" class="border rounded px-2 py-1 w-20 text-xs">
            <button class="rounded px-2 py-1 text-white text-xs" style="background:var(--survey)">เติม</button>
          </form>
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
</div>
{% endblock %}
"""

THANKS_HTML = """
{% extends "layout.html" %}{% block body %}
<div class="sheet p-6 text-center max-w-lg mx-auto">
  <div class="text-4xl">{{ ok and '✅' or '⚠️' }}</div>
  <h1 class="text-xl font-semibold mt-2">{{ title }}</h1>
  <p class="mt-2 text-[var(--pencil)]">{{ msg }}</p>
  <a href="{{ back or '/' }}" class="inline-block mt-4 rounded-lg px-4 py-2 text-white" style="background:var(--survey)">กลับไปดูทรัพย์</a>
</div>
{% endblock %}
"""


APPLY_HTML = """
{% extends "layout.html" %}{% block body %}
<div class="max-w-2xl mx-auto">
  <div class="sheet p-5">
    <h1 class="text-2xl font-semibold">รับงานจากแปลงดี</h1>
    <p class="mt-2 text-[var(--pencil)]">
      คนที่กำลังดูบ้าน คอนโด และที่ดินบนแปลงดี กดขอผู้ให้บริการในทำเลของเขาทุกวัน
      สมัครไว้ เราส่งชื่อกับเบอร์ลูกค้าให้คุณโดยตรง คุณติดต่อและตกลงราคากันเอง
    </p>

    <div class="grid sm:grid-cols-3 gap-3 mt-4 text-sm">
      <div class="border rounded-lg p-3">
        <div class="font-medium">จ่ายต่อลีด</div>
        <div class="text-[var(--pencil)] mt-1">หักเครดิตเฉพาะตอนได้รับเบอร์ลูกค้า ไม่มีค่าสมาชิกรายเดือน ไม่หัก % จากค่างาน</div>
      </div>
      <div class="border rounded-lg p-3">
        <div class="font-medium">แข่งกันไม่เกิน 3 ราย</div>
        <div class="text-[var(--pencil)] mt-1">ลีดหนึ่งส่งให้ผู้ให้บริการมากสุด 3 ราย และจำกัดจำนวนลีดต่อวันตามที่คุณรับไหว</div>
      </div>
      <div class="border rounded-lg p-3">
        <div class="font-medium">ติดต่อไม่ได้ คืนเครดิต</div>
        <div class="text-[var(--pencil)] mt-1">เบอร์ผิดหรือโทรไม่ติด แจ้งภายใน 48 ชั่วโมง เราคืนเครดิตให้</div>
      </div>
    </div>

    {% if welcome %}
    <p class="mt-4 text-sm px-3 py-2 rounded-lg" style="background:#E7F5EF;color:var(--survey)">
      สมัครตอนนี้ได้เครดิตทดลอง {{ '%.0f'|format(welcome) }} บาท ใช้ได้ทันทีที่เราตรวจสอบเสร็จ
    </p>{% endif %}
  </div>

  <div class="sheet p-5 mt-4">
    <h2 class="font-semibold">กรอกข้อมูลผู้ให้บริการ</h2>
    <form method="post" action="/partner/apply" class="grid gap-3 mt-3 text-sm">
      <input type="text" name="website" class="hidden" tabindex="-1" autocomplete="off">

      <label class="grid gap-1">
        <span>ชื่อร้าน หรือ ชื่อผู้ให้บริการ</span>
        <input name="name" required maxlength="80" class="border rounded-lg px-3 py-2">
      </label>

      <label class="grid gap-1">
        <span>เบอร์โทรที่ให้ลูกค้าติดต่อ</span>
        <input name="contact_phone" required inputmode="tel" class="border rounded-lg px-3 py-2">
      </label>

      <fieldset class="grid gap-1">
        <legend>งานที่รับ (เลือกได้หลายอย่าง)</legend>
        <div class="grid sm:grid-cols-2 gap-1 mt-1">
        {% for c in svc_categories %}
          <label class="flex gap-2 items-center border rounded-lg px-3 py-2">
            <input type="checkbox" name="categories" value="{{ c.code }}">
            <span>{{ c.emoji }} {{ c.name }}</span>
            <span class="ml-auto text-xs text-[var(--pencil)]">{{ '%.0f'|format(c.lead_price) }} บาท/ลีด</span>
          </label>
        {% endfor %}
        </div>
      </fieldset>

      <label class="grid gap-1">
        <span>จังหวัดที่รับงาน</span>
        <select name="provinces" required class="border rounded-lg px-3 py-2">
          <option value="">เลือกจังหวัด</option>
          {% for p in svc_provinces %}<option value="{{ p }}">{{ p }}</option>{% endfor %}
        </select>
      </label>

      <label class="grid gap-1">
        <span>อำเภอ/เขตที่สะดวก <span class="text-[var(--pencil)]">(เว้นว่าง = รับทั้งจังหวัด)</span></span>
        <input name="districts" maxlength="200" placeholder="เช่น ห้วยขวาง, ดินแดง, วัฒนา"
               class="border rounded-lg px-3 py-2">
      </label>

      <div class="grid sm:grid-cols-2 gap-3">
        <label class="grid gap-1">
          <span>LINE ID <span class="text-[var(--pencil)]">(ไม่บังคับ)</span></span>
          <input name="line_id" maxlength="60" class="border rounded-lg px-3 py-2">
        </label>
        <label class="grid gap-1">
          <span>รับลีดได้วันละกี่งาน</span>
          <select name="daily_lead_cap" class="border rounded-lg px-3 py-2">
            {% for n in [2,3,5,8,12] %}<option value="{{ n }}" {% if n==5 %}selected{% endif %}>{{ n }}</option>{% endfor %}
          </select>
        </label>
      </div>

      <label class="grid gap-1">
        <span>แนะนำตัวสั้น ๆ <span class="text-[var(--pencil)]">(ประสบการณ์ ทีมงาน ราคาโดยประมาณ)</span></span>
        <textarea name="note" rows="3" maxlength="500" class="border rounded-lg px-3 py-2"></textarea>
      </label>

      <label class="flex gap-2 items-start text-xs text-[var(--pencil)]">
        <input type="checkbox" name="consent" value="1" required class="mt-0.5">
        <span>ยอมรับว่าแปลงดีเป็นผู้ส่งต่อผู้ติดต่อเท่านั้น ไม่ใช่คู่สัญญาและไม่รับผิดชอบต่องานที่ตกลงกับลูกค้า
          และยินยอมให้แสดงชื่อร้านกับพื้นที่ให้บริการบนเว็บ</span>
      </label>

      <button class="rounded-lg px-4 py-2 text-white font-medium" style="background:var(--survey)">
        ส่งใบสมัคร
      </button>
    </form>
  </div>

  <div class="sheet p-5 mt-4 text-sm text-[var(--pencil)]">
    <div class="font-medium" style="color:var(--ink)">หลังส่งใบสมัคร</div>
    <p class="mt-1">เราตรวจข้อมูลด้วยคนก่อนเปิดรับลีด โดยทั่วไปภายใน 1–2 วันทำการ
      เมื่อเปิดแล้วคุณจะได้ลิงก์หน้าร้านสำหรับดูลีดและยอดเครดิต เก็บลิงก์นั้นไว้ให้ดี ใครมีลิงก์ก็เปิดดูได้</p>
  </div>
</div>
{% endblock %}
"""

APPLY_DONE_HTML = """
{% extends "layout.html" %}{% block body %}
<div class="sheet p-6 max-w-xl mx-auto">
  <div class="text-4xl">📩</div>
  <h1 class="text-xl font-semibold mt-2">รับใบสมัครแล้ว</h1>
  <p class="mt-2 text-[var(--pencil)]">
    เราจะตรวจข้อมูลแล้วเปิดรับลีดให้ภายใน 1–2 วันทำการ
    เมื่อเปิดแล้วจะติดต่อกลับที่เบอร์ {{ phone_masked }} พร้อมลิงก์หน้าร้านของคุณ
  </p>
  <div class="mt-4 border rounded-lg p-3 text-sm">
    <div class="font-medium">ลิงก์หน้าร้านของคุณ</div>
    <p class="text-[var(--pencil)] mt-1">บันทึกลิงก์นี้ไว้เลย ใช้ดูลีดที่ได้รับและยอดเครดิตคงเหลือ</p>
    <input readonly value="{{ partner_url }}" onclick="this.select()"
           class="w-full mt-2 border rounded px-2 py-1 text-xs">
  </div>
  <a href="/" class="inline-block mt-4 rounded-lg px-4 py-2 text-white" style="background:var(--survey)">กลับหน้าแรก</a>
</div>
{% endblock %}
"""


def install_templates(templates: dict):
    templates["partner_apply.html"] = APPLY_HTML
    templates["partner_apply_done.html"] = APPLY_DONE_HTML
    templates["service_block.html"] = SERVICE_BLOCK
    templates["partner.html"] = PARTNER_HTML
    templates["admin_leads.html"] = ADMIN_LEADS_HTML
    templates["admin_providers.html"] = ADMIN_PROVIDERS_HTML
    templates["lead_thanks.html"] = THANKS_HTML


# ── routes ───────────────────────────────────────────────────────────────────
def install(app, conn, render, require_admin, secret_key: str):
    DEPS.update(conn=conn, render=render, require_admin=require_admin, secret_key=secret_key)

    @app.post("/api/service-lead")
    async def api_service_lead(request: Request):
        data = await _form(request)
        if data.get("website"):  # honeypot
            return RedirectResponse("/", status_code=303)
        data["consent"] = bool(data.get("consent"))
        data["ip_hash"] = _ip_hash(request)
        res = create_lead(data)
        back = "/"
        if data.get("source_code") and data.get("external_ref"):
            back = f"/p/{data['source_code']}/{data['external_ref']}"
        if res.get("error"):
            return render("lead_thanks.html", ok=False, title="ส่งคำขอไม่สำเร็จ",
                          msg=res["error"], back=back)
        if not res["delivered"]:
            return render("lead_thanks.html", ok=True, title="รับคำขอแล้ว",
                          msg="ตอนนี้ยังไม่มีผู้ให้บริการที่รับงานในทำเลนี้ เราเก็บคำขอไว้และจะติดต่อกลับเมื่อมีรายที่ตรง",
                          back=back)
        return render("lead_thanks.html", ok=True, title="ส่งให้ผู้ให้บริการแล้ว",
                      msg=f"ส่งคำขอให้ผู้ให้บริการ {res['delivered']} ราย จะติดต่อกลับตามเบอร์ที่ให้ไว้",
                      back=back)

    @app.get("/partner/apply", response_class=HTMLResponse)
    def partner_apply_form(request: Request):
        return render("partner_apply.html", svc_categories=categories(),
                      svc_provinces=provinces(), welcome=WELCOME_CREDIT,
                      title="สมัครเป็นผู้ให้บริการ — แปลงดี")

    @app.post("/partner/apply")
    async def partner_apply_submit(request: Request):
        multi = await _form_multi(request)
        d = {k: v[0] for k, v in multi.items()}
        if d.get("website"):  # honeypot
            return RedirectResponse("/", status_code=303)

        valid = {c["code"] for c in categories()}
        cats = [c.strip() for c in multi.get("categories", []) if c.strip() in valid]

        name = (d.get("name") or "").strip()[:80]
        phone = _norm_phone(d.get("contact_phone"))
        province = (d.get("provinces") or "").strip()
        if not (name and phone and province and cats and d.get("consent")):
            return render("lead_thanks.html", ok=False, title="สมัครไม่สำเร็จ",
                          msg="กรอกชื่อ เบอร์ จังหวัด เลือกงานที่รับ และติ๊กยอมรับเงื่อนไขให้ครบ",
                          back="/partner/apply")

        ip = _ip_hash(request)
        with conn() as c, c.cursor() as cur:
            cur.execute(
                "select count(*) from service_providers where created_at > now() - interval '1 day' "
                "and note like %s", (f"%[ip:{ip}]%",))
            if (cur.fetchone() or [0])[0] >= 2:
                return render("lead_thanks.html", ok=False, title="สมัครถี่เกินไป",
                              msg="สมัครได้วันละไม่เกิน 2 ครั้งต่อเครื่อง ลองใหม่พรุ่งนี้",
                              back="/")
            cur.execute("select id from service_providers where contact_phone = %s", (phone,))
            if cur.fetchone():
                return render("lead_thanks.html", ok=False, title="เบอร์นี้สมัครไว้แล้ว",
                              msg="เบอร์นี้มีในระบบแล้ว ถ้าหาลิงก์หน้าร้านไม่เจอ ทักมาที่ไลน์แปลงดีเพื่อขอลิงก์ใหม่",
                              back="/")

            try:
                cap = max(1, min(20, int(d.get("daily_lead_cap") or 5)))
            except ValueError:
                cap = 5
            districts = [x.strip() for x in (d.get("districts") or "").split(",") if x.strip()]
            note = f"{(d.get('note') or '')[:400]} [ip:{ip}]"
            plat, plng = _province_coords(province)   # ฐานที่ตั้งร้าน = ศูนย์กลางจังหวัด
            cur.execute(
                """insert into service_providers
                   (name, contact_phone, line_id, categories, provinces, districts,
                    daily_lead_cap, status, note, lat, lng)
                   values (%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s) returning id""",
                (name, phone, (d.get("line_id") or "").strip() or None, cats,
                 [province], districts, cap, note, plat, plng))
            pid = str(cur.fetchone()[0])
            c.commit()

        return render("partner_apply_done.html",
                      partner_url=f"{BASE_URL}/partner/{pid}?k={_sig(pid)}",
                      phone_masked=_mask_phone(phone),
                      title="รับใบสมัครแล้ว")

    @app.get("/partner/{pid}", response_class=HTMLResponse)
    def partner(request: Request, pid: str, k: str = "", note: str = ""):
        if not _check_sig(pid, k):
            return JSONResponse({"error": "invalid link"}, status_code=403)
        with conn() as c, c.cursor() as cur:
            cur.execute(
                "select id, name, credit_balance, daily_lead_cap, status from service_providers where id = %s",
                (pid,))
            rows = _rows(cur)
            if not rows:
                return JSONResponse({"error": "not found"}, status_code=404)
            p = rows[0]
            cur.execute(
                """select d.id, d.price, d.status, d.refund_reason, d.created_at,
                          l.province, l.district, l.customer_name, l.customer_phone,
                          coalesce(sc.name, l.category_code) as cat_name
                   from lead_deliveries d
                   join service_leads l on l.id = d.lead_id
                   left join service_categories sc on sc.code = l.category_code
                   where d.provider_id = %s
                   order by d.created_at desc limit 100""", (pid,))
            deliveries = _rows(cur)
            cur.execute(
                "select count(*) from lead_deliveries where provider_id = %s and status='charged' "
                "and created_at >= date_trunc('day', now())", (pid,))
            used_today = (cur.fetchone() or [0])[0]
        return render("partner.html", p=p, deliveries=deliveries, used_today=used_today,
                      k=k, d_note=note, title=f"หน้าร้าน — {p['name']}")

    @app.post("/partner/{pid}/refund/{did}")
    def partner_refund(pid: str, did: int, k: str = ""):
        if not _check_sig(pid, k):
            return JSONResponse({"error": "invalid link"}, status_code=403)
        with conn() as c, c.cursor() as cur:
            cur.execute(
                "update lead_deliveries set refund_reason = 'ติดต่อลูกค้าไม่ได้' "
                "where id = %s and provider_id = %s and status = 'charged' "
                "and created_at > now() - interval '48 hours' and refund_reason is null",
                (did, pid))
            ok = cur.rowcount > 0
            c.commit()
        msg = "ส่งคำขอคืนเครดิตแล้ว รอตรวจสอบ" if ok else "ขอคืนได้ภายใน 48 ชม. และขอได้ครั้งเดียวต่อลีด"
        return RedirectResponse(f"/partner/{pid}?k={k}&note={urllib.parse.quote(msg)}", status_code=303)

    @app.get("/admin/leads", response_class=HTMLResponse)
    def admin_leads(request: Request, token: str = ""):
        blocked = require_admin(request, token)
        if blocked:
            return blocked
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """select l.id, l.created_at, l.category_code, l.province, l.district,
                          l.customer_name, l.customer_phone, sc.name as cat_name,
                          count(d.id) filter (where d.status='charged') as n_sent,
                          coalesce(sum(d.price) filter (where d.status='charged'),0) as revenue
                   from service_leads l
                   left join lead_deliveries d on d.lead_id = l.id
                   left join service_categories sc on sc.code = l.category_code
                   group by l.id, sc.name order by l.created_at desc limit 200""")
            leads = _rows(cur)
            cur.execute(
                """select count(*) as leads_30,
                          count(*) filter (where status='delivered') as delivered_30,
                          count(*) filter (where status='no_provider') as nomatch_30
                   from service_leads where created_at > now() - interval '30 days'""")
            stat = _rows(cur)[0]
            cur.execute(
                "select coalesce(sum(price),0) from lead_deliveries "
                "where status='charged' and created_at > now() - interval '30 days'")
            stat["revenue_30"] = _f((cur.fetchone() or [0])[0])
            cur.execute(
                """select d.id, d.lead_id, d.price, d.refund_reason, p.name as provider_name,
                          l.customer_phone
                   from lead_deliveries d
                   join service_providers p on p.id = d.provider_id
                   join service_leads l on l.id = d.lead_id
                   where d.status='charged' and d.refund_reason is not null
                   order by d.created_at desc limit 50""")
            refunds = _rows(cur)
        return render("admin_leads.html", leads=leads, stat=stat, refunds=refunds,
                      title="ลีดบริการ")

    @app.post("/admin/leads/refund")
    async def admin_leads_refund(request: Request, token: str = ""):
        blocked = require_admin(request, token)
        if blocked:
            return blocked
        data = await _form(request)
        refund_delivery(int(data.get("delivery_id") or 0), "admin approved")
        return RedirectResponse("/admin/leads", status_code=303)

    @app.get("/admin/providers", response_class=HTMLResponse)
    def admin_providers(request: Request, token: str = ""):
        blocked = require_admin(request, token)
        if blocked:
            return blocked
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """select p.*, (select count(*) from lead_deliveries d
                                where d.provider_id = p.id and d.status='charged') as n_leads
                   from service_providers p order by p.created_at desc""")
            providers = _rows(cur)
        for p in providers:
            p["k"] = _sig(p["id"])
        return render("admin_providers.html", providers=providers, title="ผู้ให้บริการ")

    @app.post("/admin/providers")
    async def admin_provider_add(request: Request, token: str = ""):
        blocked = require_admin(request, token)
        if blocked:
            return blocked
        d = await _form(request)
        split = lambda s: [x.strip() for x in (s or "").split(",") if x.strip()]
        provs = split(d.get("provinces"))
        plat, plng = _province_coords(provs[0] if provs else "")
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """insert into service_providers
                   (name, contact_phone, categories, provinces, districts, line_user_id,
                    status, lat, lng)
                   values (%s,%s,%s,%s,%s,%s,'pending',%s,%s)""",
                (d.get("name"), _norm_phone(d.get("contact_phone")), split(d.get("categories")),
                 provs, split(d.get("districts")),
                 (d.get("line_user_id") or "").strip() or None, plat, plng))
            c.commit()
        return RedirectResponse("/admin/providers", status_code=303)

    @app.post("/admin/providers/topup")
    async def admin_provider_topup(request: Request, token: str = ""):
        blocked = require_admin(request, token)
        if blocked:
            return blocked
        d = await _form(request)
        try:
            amount = float(d.get("amount") or 0)
        except ValueError:
            amount = 0.0
        if amount:
            topup(d.get("provider_id"), amount, "เติมโดยแอดมิน")
        return RedirectResponse("/admin/providers", status_code=303)

    @app.post("/admin/providers/status")
    async def admin_provider_status(request: Request, token: str = ""):
        blocked = require_admin(request, token)
        if blocked:
            return blocked
        d = await _form(request)
        pid, status = d.get("provider_id"), d.get("status")
        if status in ("pending", "active", "paused", "banned") and pid:
            first_time = False
            with conn() as c, c.cursor() as cur:
                cur.execute("select verified_at from service_providers where id = %s", (pid,))
                row = cur.fetchone()
                first_time = bool(row) and row[0] is None and status == "active"
                cur.execute(
                    "update service_providers set status = %s, "
                    "verified_at = case when %s = 'active' then coalesce(verified_at, now()) "
                    "else verified_at end where id = %s",
                    (status, status, pid))
                c.commit()
            if first_time and WELCOME_CREDIT > 0:
                topup(pid, WELCOME_CREDIT, "เครดิตทดลองตอนเปิดร้าน")
        return RedirectResponse("/admin/providers", status_code=303)
