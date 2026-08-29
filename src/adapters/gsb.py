"""Adapter: ออมสิน (GSB NPA) — ทรัพย์ NPA / บ้านมือสองธนาคารออมสิน

โครงหน้าเว็บ (ยืนยันจาก DOM จริง 2026-08-29)

  เว็บ: https://npa-assets.gsb.or.th  (Next.js — ข้อมูลฝังใน __NEXT_DATA__ แบบ SSR)
  list: /asset/npa/all?page={n}
      - SSR ฝัง JSON page นั้นใน <script id="__NEXT_DATA__">
      - props.pageProps.list.data = { count, rows[12] }  (12 รายการ/หน้า ~372 หน้า)
      - ?page= เลื่อนหน้าจริง (page1/page2 คนละชุด)
  รูป: /apipr/npa/image?id={image_id}  (absolute)

  แต่ละ row (ฟิลด์ที่ใช้):
      asset_id            = 15090            -> external_ref = gsb:15090
      asset_group_id_npa  = NMA-LB-A-650012  (รหัสทรัพย์)
      asset_type_desc     = "ที่ดินพร้อมสิ่งปลูกสร้าง"
      xprice / xprice_normal = ราคาโปรฯ / ราคาปกติ   (current_offer_price = ราคาปัจจุบัน)
      province_name / district_name / sub_district_name
      sum_rai / sum_ngan / sum_square_wa  (เนื้อที่)  · square_meter (ห้องชุด)
      image_id            = 1352758        · deed_info = "โฉนดที่ดิน 32880"

  ไม่มีพิกัด lat/lng ในลิสต์ -> ใช้ geocode ระดับตำบล/อำเภอ (enrich.py geocode)

external_ref = gsb:{asset_id}

⚠️ ToS: footer เว็บระบุ "ข้อมูลเป็นสมบัติของธนาคารออมสิน ห้ามนำไปใช้/ทำซ้ำ/ดัดแปลง"
   ก่อนใช้เชิงพาณิชย์ควรประเมินความเสี่ยงลิขสิทธิ์ หรือขออนุญาต/ทำ data partnership
   (is_active=false ไว้ก่อน) · มี Cloudflare แต่ไม่บล็อก anonymous GET
"""
from __future__ import annotations

import json
import logging
import re
from typing import Iterable, Iterator

from core.base_adapter import BaseAdapter
from core.http import Response

log = logging.getLogger(__name__)

BASE = "https://npa-assets.gsb.or.th"
_NEXT_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

# asset_type_desc (ไทย) -> รหัสภายในโปรเจกต์ — เรียงยาว/เฉพาะก่อน
TYPE_MAP = [
    ("ห้องชุด", "condo"), ("อาคารชุด", "condo"), ("คอนโด", "condo"),
    ("ทาวน์เฮ", "townhouse"), ("ทาวน์โฮม", "townhouse"),
    ("อาคารพาณิชย์", "commercial"), ("ตึกแถว", "commercial"), ("สำนักงาน", "commercial"),
    ("โรงงาน", "industrial"), ("โกดัง", "industrial"), ("คลังสินค้า", "industrial"),
    ("บ้านเดี่ยว", "house"), ("บ้านแฝด", "house"),
    ("ที่ดินพร้อมสิ่งปลูกสร้าง", "house"),   # ที่ดิน+สิ่งปลูกสร้าง = ส่วนใหญ่บ้าน
    ("ที่ดินเปล่า", "land"), ("ที่ดิน", "land"),
]


def _f(v) -> float | None:
    """แปลงเป็น float (รับทั้งตัวเลขและสตริง) — คืน None ถ้าไม่ใช่/<=0 สำหรับราคา"""
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


class GsbAdapter(BaseAdapter):
    source_code = "gsb"
    parser_version = "0.1.0"

    LIST_PATH = "/asset/npa/all"

    def discover(self) -> Iterator[dict]:
        """ไล่ทีละหน้า ?page= (12/หน้า) — ลิสต์รวมทั้งประเทศ

        ใส่ meta.office_name คงที่ ('gsb') ให้ตัวหยุดเมื่อไม่เจอของใหม่ (STOP_AFTER_STALE)
        ทำงาน: รันประจำวันจะหยุดเร็วเมื่อชนของเดิม (ของใหม่อยู่หน้าต้น ๆ)
        รอบกวาดเต็มใช้ --full
        """
        max_pages = self.config.get("max_pages", 30)
        for page in range(1, max_pages + 1):
            yield {
                "url": BASE + self.LIST_PATH,
                "method": "GET",
                "params": {"page": str(page)},
                "meta": {"office_name": "gsb", "page": page},
            }

    def parse(self, resp: Response, task: dict) -> Iterable[dict]:
        m = _NEXT_RE.search(resp.text)
        if not m:
            log.warning("ออมสิน: ไม่พบ __NEXT_DATA__ (page %s)", task.get("meta", {}).get("page"))
            return
        try:
            nd = json.loads(m.group(1))
            rows = nd["props"]["pageProps"]["list"]["data"].get("rows", [])
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            log.warning("ออมสิน: parse __NEXT_DATA__ ล้มเหลว — %s", str(exc)[:100])
            return

        for r in rows:
            aid = r.get("asset_id")
            if not aid:
                continue
            type_desc = r.get("asset_type_desc") or ""
            land_sqwa = ((_f(r.get("sum_rai")) or 0) * 400
                         + (_f(r.get("sum_ngan")) or 0) * 100
                         + (_f(r.get("sum_square_wa")) or 0))
            list_price = _f(r.get("xprice_normal")) or _f(r.get("current_offer_price"))
            promo = _f(r.get("xprice"))
            special = promo if (promo and list_price and promo < list_price) else None
            price = special or _f(r.get("current_offer_price")) or _f(r.get("group_sell_price")) or list_price
            img_id = r.get("image_id")
            prov = r.get("province_name")
            dist = r.get("district_name")
            sub = r.get("sub_district_name")

            yield {
                "external_ref": f"gsb:{aid}",
                "listing_id": str(aid),
                "detail_url": f"{BASE}/asset/npa/{aid}",
                "title": " ".join(x for x in [type_desc, dist, prov] if x)[:200] or None,
                "property_type": self._map_type(type_desc, r.get("asset_subtype_desc")),
                "province": prov,
                "district": dist,
                "subdistrict": sub,
                "address_raw": " ".join(x for x in [sub, dist, prov] if x) or None,
                "land_area_sqwa": round(land_sqwa, 2) if land_sqwa else None,
                "usable_area_sqm": _f(r.get("square_meter")),
                "opening_price": price,
                "list_price": list_price,
                "special_price": special,
                "image_url": f"{BASE}/apipr/npa/image?id={img_id}" if img_id else None,
                "gsb_code": r.get("asset_group_id_npa"),
                "gsb_deed": r.get("deed_info"),
                "_source_url": resp.url,
            }

    @staticmethod
    def _map_type(type_desc: str, subtype: str | None) -> str:
        hay = f"{type_desc} {subtype or ''}"
        for label, code in TYPE_MAP:
            if label in hay:
                return code
        return "other"
