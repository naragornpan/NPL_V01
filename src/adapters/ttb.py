"""Adapter: ttb (PAMCO) — ทรัพย์ NPA ธนาคารทหารไทยธนชาต

╔══════════════════════════════════════════════════════════════════════╗
║  ก่อนเปิดใช้งานจริง (is_active = false ไว้ก่อน)                       ║
║  ตรวจแล้ว 2026-08-24                                                  ║
║    เว็บจริง: property.pamco.co.th (PAMCO = ผู้บริหารทรัพย์ให้ ttb)    ║
║    (tmbbank.com/property เป็นแค่หน้า mirror + robots.txt error 500)  ║
║  ยังต้องอ่าน ToS ฉบับเต็ม + สมัครนายหน้า ก่อนเปิดใช้ (เหมือน ktb/sam) ║
╚══════════════════════════════════════════════════════════════════════╝

โครงที่ยืนยันจาก network จริง 2026-08-24

  หน้า list เป็น Next.js server-render + ปุ่ม "โหลดเพิ่ม" ที่เรียก
  JSON API สาธารณะ (host แยก):

      GET https://property-api-prod.automer.io/property-new/display
          ?page={n}&limit={m}
      -> {"total": 1302, "list": [ {..ทรัพย์..}, ... ]}

  ข้อดีมหาศาล: **พิกัดจริงมากับ list เลย** (npaProductLatitude/Longitude)
    -> เก็บ geo_precision='parcel' ตั้งแต่ ingest ไม่ต้องเข้า detail
    (ต้องมี 'geo_precision' ใน SNAPSHOT_COLUMNS ของ base_adapter — เพิ่มแล้ว
     เพื่อกัน enrich geocode มาเขียนทับพิกัดจริงด้วยจุดกึ่งกลางตำบล)

  ฟิลด์ต่อรายการ (ที่ใช้):
    idMarket        รหัสทรัพย์ (เช่น B13220 / P00841) -> external_ref
    idProperty      id ในระบบ (numeric) -> listing_id
    slug            ใช้ทำ URL หน้า detail /assets/ttb/{slug}
    npaProductTitle ชื่อ ขึ้นต้นด้วยประเภท ("บ้านเดี่ยว ...","คอนโด ...")
                    -> ใช้แยกประเภททรัพย์ (type int ในAPI ไม่ใช่หมวดทรัพย์)
    lowprice        ราคาตั้ง · priceSp1 ราคาพิเศษ
    npaProductArea  เนื้อที่ดิน (ตร.ว.) · useableArea พื้นที่ใช้สอย (ตร.ม.)
    provinceNameTh / districtNameTh / subDistrictNameTh
    npaProductLatitude / npaProductLongitude   พิกัดจริง
    thumbnail       รูป -> media.pamco.co.th/{thumbnail}
    landid          เลขโฉนด/เอกสารสิทธิ์ (เก็บใน raw_fields)
    telAO           เบอร์ตัวแทน — **ไม่เก็บ** (ข้อมูลติดต่อบุคคล)

detail (ให้คนดู): https://property.pamco.co.th/assets/ttb/{slug}
external_ref = ttb:{idMarket}
"""
from __future__ import annotations

import json
import logging
import math
import re
from typing import Iterable, Iterator

from core.base_adapter import BaseAdapter
from core.http import Response

log = logging.getLogger(__name__)

# ประเภททรัพย์จากคำขึ้นต้นชื่อ -> รหัสภายในโปรเจกต์ (ยาวก่อนสั้น)
TYPE_LABEL_MAP = [
    ("บ้านเดี่ยว", "house"),
    ("บ้านแฝด", "house"),
    ("ทาวน์เฮ้าส์", "townhouse"),
    ("ทาวน์เฮาส์", "townhouse"),
    ("ทาวน์โฮม", "townhouse"),
    ("อาคารพาณิชย์", "commercial"),
    ("ตึกแถว", "commercial"),
    ("สำนักงาน", "commercial"),
    ("โรงแรม", "commercial"),
    ("โกดัง", "industrial"),
    ("คลังสินค้า", "industrial"),
    ("โรงงาน", "industrial"),
    ("คอนโด", "condo"),
    ("ห้องชุด", "condo"),
    ("แฟลต", "condo"),
    ("อพาร์ทเม้นท์", "commercial"),
    ("ที่ดินเปล่า", "land"),
    ("ที่ดิน", "land"),
]

LAT_MIN, LAT_MAX = 5.5, 20.5
LNG_MIN, LNG_MAX = 97.0, 106.0


class TtbAdapter(BaseAdapter):
    source_code = "ttb"
    parser_version = "0.1.0"

    API = "https://property-api-prod.automer.io/property-new/display"
    DETAIL = "https://property.pamco.co.th/assets/ttb/{slug}"
    MEDIA_BASE = "https://media.pamco.co.th/"

    # ttb เล็ก (~1,300 รายการ) ดึงครบทุกจังหวัดเสมอ ไม่ผูก tier
    # (ต่างจาก BAM/LED ที่ใหญ่จนต้องจำกัดด้วย tier)
    STOP_AFTER_STALE_PAGES = 0        # ไล่ให้ครบทุกหน้าตาม total

    def discover(self) -> Iterator[dict]:
        """ขั้น 1 — ดึงหน้าแรกเพื่อรู้ total แล้วค่อยกระจายหน้าที่เหลือใน follow_up"""
        limit = int(self.config.get("page_limit", 100))
        yield {
            "url": self.API,
            "method": "GET",
            "params": {"page": 1, "limit": limit},
            "meta": {"stage": "head", "page": 1, "limit": limit},
        }

    def follow_up(self, resp: Response, task: dict) -> Iterator[dict]:
        """ขั้น 2 — จาก total ในหน้าแรก สร้าง task หน้า 2..N

        คำนวณจำนวนหน้าจาก 'ขนาดหน้าจริง' (len(list)) ไม่ใช่ค่า limit ที่ขอ
        เผื่อเซิร์ฟเวอร์ cap limit ต่ำกว่าที่ขอ จะได้ไม่ดึงตกหล่น
        """
        if task["meta"].get("stage") != "head":
            return
        try:
            data = json.loads(resp.text)
        except (ValueError, TypeError):
            log.warning("ttb: หน้าแรกไม่ใช่ JSON ที่อ่านได้ — หยุด")
            return
        total = int(data.get("total") or 0)
        got = len(data.get("list") or [])
        limit = task["meta"].get("limit", 100)
        effective = got or limit
        if total <= effective or effective == 0:
            return
        pages = math.ceil(total / effective)
        log.info("ttb: ทั้งหมด %s รายการ · หน้าละ %s · รวม %s หน้า",
                 total, effective, pages)
        for p in range(2, pages + 1):
            yield {
                "url": self.API,
                "method": "GET",
                "params": {"page": p, "limit": limit},
                "meta": {"stage": "page", "page": p},
            }

    def parse(self, resp: Response, task: dict) -> Iterable[dict]:
        try:
            data = json.loads(resp.text)
        except (ValueError, TypeError):
            return
        for it in data.get("list") or []:
            code = (str(it.get("idMarket") or "")).strip()
            if not code:
                continue

            list_price = self._num(it.get("lowprice"))
            special = self._num(it.get("priceSp1"))
            opening = special or list_price

            rec = {
                "external_ref": f"ttb:{code}",
                "listing_id": str(it.get("idProperty") or "") or None,
                "detail_url": self.DETAIL.format(
                    slug=(it.get("slug") or code.lower())),
                "title": (str(it.get("npaProductTitle") or "")).strip() or None,
                "property_type": self._type_from_title(it.get("npaProductTitle")),
                "province": (str(it.get("provinceNameTh") or "")).strip() or None,
                "district": (str(it.get("districtNameTh") or "")).strip() or None,
                "subdistrict": (str(it.get("subDistrictNameTh") or "")).strip() or None,
                "land_area_sqwa": self._num(it.get("npaProductArea")),
                "usable_area_sqm": self._num(it.get("useableArea")),
                "opening_price": opening,
                "list_price": list_price,
                "special_price": special,
                "image_url": self._image(it.get("thumbnail")),
                # illustration = array รูปเพิ่ม (10-13 รูป/ทรัพย์) เก็บไว้ทำแกลเลอรี
                "gallery": self._gallery(it.get("illustration")),
                # เก็บไว้อ้างอิง (ไม่ใช่ PII): เลขเอกสารสิทธิ์ + สถานะ type ของ API
                "landid": (str(it.get("landid") or "")).strip() or None,
                "ttb_type_flag": it.get("type"),
                "_source_url": resp.url,
            }
            rec["address_raw"] = self._address(rec)

            lat = self._coord(it.get("npaProductLatitude"), LAT_MIN, LAT_MAX)
            lng = self._coord(it.get("npaProductLongitude"), LNG_MIN, LNG_MAX)
            if lat is not None and lng is not None:
                # พิกัดจริงจากต้นทาง — ระดับแปลง กัน geocode มาเขียนทับ
                rec["lat"] = lat
                rec["lng"] = lng
                rec["geo_precision"] = "parcel"

            yield rec

    # ------------------------------------------------------------------
    @staticmethod
    def _num(v) -> float | None:
        """แปลงเป็นตัวเลข > 0 (คืน None ถ้าว่าง/ศูนย์/แปลงไม่ได้)"""
        if v is None:
            return None
        s = re.sub(r"[^\d.]", "", str(v))
        if not s:
            return None
        try:
            f = float(s)
        except ValueError:
            return None
        return f if f > 0 else None

    @staticmethod
    def _coord(v, lo: float, hi: float) -> float | None:
        if v in (None, "", "0", "0.0"):
            return None
        try:
            f = float(str(v).strip())
        except ValueError:
            return None
        return f if lo <= f <= hi else None

    @staticmethod
    def _type_from_title(title) -> str:
        t = str(title or "")
        for label, code in TYPE_LABEL_MAP:
            if label in t:
                return code
        return "other"

    @staticmethod
    def _first(v):
        """thumbnail/illustration จาก API เป็น array — เอาตัวแรก"""
        if isinstance(v, (list, tuple)):
            return v[0] if v else None
        return v

    def _media_url(self, path) -> str | None:
        s = (str(path or "")).strip()
        if not s or s in ("[]", "()", "None"):
            return None
        if s.startswith("http"):
            return s
        return self.MEDIA_BASE + s.lstrip("/")

    def _image(self, thumb) -> str | None:
        return self._media_url(self._first(thumb))

    def _gallery(self, illustration) -> list:
        if not isinstance(illustration, (list, tuple)):
            illustration = [illustration] if illustration else []
        out = []
        for p in illustration:
            u = self._media_url(p)
            if u:
                out.append(u)
        return out

    @staticmethod
    def _address(rec: dict) -> str | None:
        parts = []
        if rec.get("subdistrict"):
            parts.append(f"ต./แขวง {rec['subdistrict']}")
        if rec.get("district"):
            parts.append(f"อ./เขต {rec['district']}")
        if rec.get("province"):
            parts.append(rec["province"])
        return " ".join(parts) or None
