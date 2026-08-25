"""Adapter: กรมบังคับคดี — ประกาศขายทอดตลาด

╔══════════════════════════════════════════════════════════════════════╗
║  หยุดอ่านก่อนเปิดใช้งาน                                              ║
║                                                                      ║
║  เว็บ asset.led.go.th ระบุท้ายหน้าว่า "ข้อมูลต่าง ๆ ในเว็บไซต์นี้     ║
║  ถือเป็นสมบัติของกรมบังคับคดี ห้ามผู้ใดนำไปใช้ ทำซ้ำ ดัดแปลง          ║
║  แก้ไขข้อมูลดังกล่าวโดยมิได้รับอนุญาต"                                ║
║                                                                      ║
║  source นี้จึงถูกตั้ง is_active = false ไว้ใน schema                  ║
║  เปิดใช้ได้เมื่อได้รับหนังสืออนุญาตแล้วเท่านั้น                        ║
║  ดูขั้นตอนขออนุญาตที่ docs/DATA_PERMISSION.md                        ║
╚══════════════════════════════════════════════════════════════════════╝


สถานะ: โครงพร้อม แต่ส่วนที่ทำเครื่องหมาย TODO ต้องเติมหลังรัน
    python tools/probe.py <url> --encoding tis-620
เพราะเป็นเว็บ ASP legacy ที่ชื่อ parameter เดาไม่ได้และเปลี่ยนได้

หลักการเขียน parser ที่นี่:
  1. ดึงทุกฟิลด์ที่เห็น อย่าเลือกเฉพาะที่คิดว่าจะใช้ — ของที่ไม่ map
     จะไปกองใน raw_fields ซึ่งกู้คืนได้ทีหลัง
  2. อย่า raise เมื่อฟิลด์เดียวพัง ให้ปล่อยเป็น None แล้วบันทึก parse_failure
  3. ห้ามเก็บชื่อคู่ความ/เลขคดี — core.pii จะตัดให้ แต่อย่าพยายามดึงมาแต่แรก
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Iterable, Iterator

from bs4 import BeautifulSoup

from core.base_adapter import BaseAdapter
from core.http import Response

log = logging.getLogger(__name__)

THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")

PROPERTY_TYPE_MAP = {
    "ที่ดินว่างเปล่า": "land",
    "ที่ดินพร้อมสิ่งปลูกสร้าง": "house",
    "บ้านเดี่ยว": "house",
    "ทาวน์เฮ้าส์": "townhouse",
    "ทาวน์เฮาส์": "townhouse",
    "ห้องชุด": "condo",
    "อาคารพาณิชย์": "commercial",
}


def to_number(text: str | None) -> float | None:
    if not text:
        return None
    cleaned = text.translate(THAI_DIGITS)
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def to_thai_date(d: date) -> str:
    """แปลงเป็นรูปแบบที่เว็บต้องการ: dd/mm/yyyy พ.ศ."""
    return f"{d.day:02d}/{d.month:02d}/{d.year + 543}"


def to_date(text: str | None) -> date | None:
    """แปลงวันที่ไทย เช่น '15 มีนาคม 2569' -> date(2026, 3, 15)"""
    if not text:
        return None
    months = ["มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
              "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"]
    t = text.translate(THAI_DIGITS)
    m = re.search(r"(\d{1,2})\s*([ก-ฮ]+)\s*(\d{4})", t)
    if not m:
        return None
    day, month_th, year_be = m.groups()
    for idx, name in enumerate(months, start=1):
        if month_th.startswith(name[:4]):
            return date(int(year_be) - 543, idx, int(day))
    return None


class LedAuctionAdapter(BaseAdapter):
    source_code = "led_auction"
    parser_version = "0.1.0"

    # ยืนยันจาก probe เมื่อ 2026-08-23
    BASE_URL = "https://asset.led.go.th/newbidreg/"
    DAY_URL = BASE_URL + "asset_day.asp"          # ขั้น 1: สรุปรายสำนักงาน
    LIST_URL = BASE_URL + "asset_search_day.asp"  # ขั้น 2: รายการทรัพย์จริง

    # =================================================================
    # ทำไมใช้ asset_day.asp ไม่ใช่ฟอร์มค้นหาละเอียด
    #
    # ฟอร์มค้นหาละเอียด (default.asp) มีช่อง "รหัสยืนยัน" — oseckey ซ่อนไว้
    # คู่กับ seckey ที่ให้ผู้ใช้กรอก นั่นคือกลไกกันบอท
    #
    # **ห้ามเขียนโค้ดอ่าน oseckey แล้วกรอกกลับ** ต่อให้ทำได้ง่ายก็ตาม
    # ถ้าต้องใช้ผลจากฟอร์มนั้น ให้ค้นด้วยมือแล้วเซฟหน้าไว้
    # แล้วใช้ tools/import_html.py แทน
    #
    # asset_day.asp ไม่มีรหัสยืนยัน เป็นปุ่ม "ทรัพย์ขายวันนี้" ที่เว็บเปิดให้กดตรง ๆ
    # รับพารามิเตอร์เดียวคือวันที่ (พ.ศ. รูปแบบ dd/mm/yyyy)
    # =================================================================

    def discover(self) -> Iterator[dict]:
        """ขั้น 1 — ถามว่าวันไหนมีของที่สำนักงานไหนบ้าง

        คืนเฉพาะ task ของหน้าสรุป ส่วนหน้ารายการทรัพย์จะถูกสร้างต่อ
        ใน follow_up() หลัง parse หน้าสรุปแล้ว

        เหตุผลที่ทำสองขั้น: วันหนึ่งมีทรัพย์ทั่วประเทศเป็นหมื่นรายการ
        แต่เราสนใจแค่ไม่กี่สำนักงาน การถามหน้าสรุปก่อนทำให้ยิงเฉพาะที่ต้องการ
        """
        days_ahead = self.config.get("days_ahead", 45)
        days_back = self.config.get("days_back", 3)
        start = date.today() - timedelta(days=days_back)

        for offset in range(days_back + days_ahead + 1):
            d = start + timedelta(days=offset)
            yield {
                "url": self.DAY_URL,
                "method": "POST",
                "data": {"search_bid_date": to_thai_date(d), "search": "ok"},
                "meta": {"bid_date": d, "stage": "summary"},
            }

    def follow_up(self, resp: Response, task: dict) -> Iterator[dict]:
        """ขั้น 2 — จากหน้าสรุป สร้าง task ของสำนักงานที่สนใจ

        กรองด้วย office_filter จาก config ถ้าไม่ตั้งจะดึงทุกสำนักงาน
        ซึ่งหนักมาก (ทั้งประเทศราว 9,000 รายการต่อวัน) จึงควรตั้งเสมอ
        """
        if task["meta"].get("stage") != "summary":
            return
        soup = BeautifulSoup(resp.text, "html.parser")
        wanted = self.config.get("office_filter")

        for form in soup.find_all("form", action="asset_search_day.asp"):
            data = {i.get("name"): i.get("value")
                    for i in form.find_all("input") if i.get("name")}
            office = data.get("province_name", "")
            if wanted and not any(w in office for w in wanted):
                continue
            yield {
                "url": self.LIST_URL,
                "method": "POST",
                "data": data,
                "meta": {"bid_date": task["meta"]["bid_date"],
                         "stage": "list", "office_name": office,
                         "office_id": data.get("province_id")},
            }

    def parse_summary(self, resp: Response) -> list[dict]:
        """อ่านจำนวนทรัพย์รายสำนักงาน — ใช้ตรวจว่าเราดึงครบไหม"""
        soup = BeautifulSoup(resp.text, "html.parser")
        out = []
        table = soup.find("table", class_="asset-table")
        if not table:
            return out
        for row in table.find_all("tr")[1:]:
            cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
            if len(cells) >= 2 and cells[1].isdigit():
                out.append({"office_name": cells[0], "expected_count": int(cells[1])})
        return out

    # ─────────────────────────────────────────────────────────────────
    # โครงจริงของหน้า asset_search_day.asp (ยืนยันจากไฟล์จริง 2026-08-24)
    #
    # แต่ละทรัพย์ = <form action="asset_open.asp" name="webN"> ที่มี hidden
    # input ครบทุกฟิลด์ ไม่ต้องเดาตำแหน่งคอลัมน์เลย ดึงจาก input โดยตรง
    #
    # PII — ห้ามเก็บเด็ดขาด: person1, person2, owner_suit_name, ownername,
    #   debtname, debtdetail, debtprice, law_suit_no, law_suit_year
    #   (ชื่อคู่ความ/ลูกหนี้/เลขคดี) เราจะ "ไม่แตะ" ฟิลด์เหล่านี้เลย
    #   เก็บเฉพาะฟิลด์ใน SAFE_FIELDS ด้านล่าง
    # ─────────────────────────────────────────────────────────────────

    # ฟิลด์ hidden ที่ปลอดภัยพอจะเก็บลง raw_fields (ไม่มีชื่อบุคคล/เลขคดี)
    SAFE_HIDDEN = {
        "str_bid_num", "rai", "quaterrai", "wa",
        "biddate1", "biddate2", "biddate3", "biddate4",
        "biddate5", "biddate6", "biddate7", "biddate8",
        "AssetTypeID", "assettypedesc", "addrno", "tumbol", "ampur", "city",
        "province_name", "province_id", "auc_asset_gen",
        "ReserveFund", "ReserveFund1",
        "assetprice1", "assetprice2", "assetprice3", "assetprice4", "assetprice5",
        "assetprice6", "assetprice7", "assetprice8", "assetprice9",
        "sale_location1", "sale_location2", "sale_time1", "sale_time2",
        "deedno", "deedtumbol", "deedampur", "deedcity", "landtype", "landdesc",
        "occupant", "saletypename", "eauc", "law_court_name", "law_court_id",
        "issale", "remark",
    }
    # ฟิลด์ที่เป็น PII — กันไว้อีกชั้น ต่อให้เผลอก็ไม่หลุด
    PII_HIDDEN = {
        "person1", "person2", "owner_suit_name", "ownername",
        "debtname", "debtdetail", "debtprice", "law_suit_no", "law_suit_year",
    }

    def parse(self, resp: Response, task: dict) -> Iterable[dict]:
        # หน้าสรุปไม่มีข้อมูลทรัพย์ ข้ามไป (ใช้ follow_up สร้าง task ต่อแทน)
        if task["meta"].get("stage") == "summary":
            return

        soup = BeautifulSoup(resp.text, "html.parser")
        bid_date = task["meta"].get("bid_date")
        office = task["meta"].get("office_name")

        seen: set[str] = set()
        for form in soup.find_all("form", action="asset_open.asp"):
            h = {i.get("name"): (i.get("value") or "").strip()
                 for i in form.find_all("input") if i.get("name")
                 and i.get("name") not in self.PII_HIDDEN}
            if not h:
                continue

            asset_id = h.get("auc_asset_gen") or h.get("str_bid_num")
            if not asset_id:
                continue
            ref = f"led:{asset_id}"
            if ref in seen:
                continue
            seen.add(ref)

            rai = to_number(h.get("rai"))
            ngan = to_number(h.get("quaterrai"))     # quaterrai = งาน
            wa = to_number(h.get("wa"))
            land_sqwa = (rai or 0) * 400 + (ngan or 0) * 100 + (wa or 0)

            adate, rnd = self._auction_date_round(h, bid_date)
            appraised = self._max_price(h)
            rounds = self._auction_rounds(h)      # ทุกนัด: [{round, date}]

            safe = {k: v for k, v in h.items() if k in self.SAFE_HIDDEN}

            yield {
                "external_ref": ref,
                # province_name ที่จริงคือ "ชื่อสำนักงานที่ขาย" (กทม.มีหลาย สนง.
                # เช่น "แพ่งกรุงเทพมหานคร 6") ส่วนจังหวัดจริงอยู่ที่ city
                "office_name": (h.get("province_name") or office or "").strip() or None,
                # ที่ตั้งทรัพย์: บางประเภท (คอนโด) ช่อง tumbol/ampur/city ว่าง
                # ต้อง fallback ไปใช้ข้อมูลจากโฉนด (deedtumbol/deedampur/deedcity)
                "province": (h.get("city") or h.get("deedcity") or "").strip() or None,
                "district": (self._strip_admin(h.get("ampur"))
                             or self._strip_admin(h.get("deedampur"))),
                "subdistrict": (self._strip_admin(h.get("tumbol"))
                                or self._strip_admin(h.get("deedtumbol"))),
                # ที่อยู่จริงของทรัพย์ (ไม่ใช่ sale_location1 ซึ่งเป็นสถานที่จัดประมูล)
                "address_raw": self._property_address(h),
                "property_type": self._map_type(h.get("assettypedesc"),
                                                h.get("landtype")),
                "land_area_sqwa": round(land_sqwa, 2) if land_sqwa else None,
                "appraised_price": appraised,
                # LED ไม่ได้แยก "ราคาเปิดประมูล" ในหน้ารายการ ใช้ราคาประเมินแทน
                # (สมมติฐาน ดู _price_note) พอมีหน้ารายละเอียดค่อยแก้ให้ตรงนัด
                "opening_price": appraised,
                "auction_date": adate or bid_date,
                "auction_round": rnd,
                "occupancy_note": h.get("occupant") or None,
                "deposit_amount": to_number(h.get("ReserveFund")),
                # รูปทรัพย์: สร้าง URL จาก landpicture (Z:\... -> /PPKPicture/...)
                # ยืนยันแล้วว่าตรงกับที่ asset_open.asp สร้าง — ไม่ต้องยิง detail
                # (หน้า detail บังคับต้องส่งชื่อคู่ความ/PII ถึงจะคืนค่า จึงเลี่ยง)
                "image_url": self._image_from_landpic(h.get("landpicture")),
                # เลขโฉนด + จังหวัด/อำเภอของโฉนด — ไว้ค้นพิกัดจริงจาก LandsMaps
                # (กรมที่ดิน) ภายหลัง ถ้าเลือกทำ enrichment ระดับแปลง
                "deed_no": self._clean_deed(h.get("deedno")),
                # รายละเอียดประมูล (จากหน้า list — ไม่ต้องแตะ detail/PII)
                # ศาล/สำนักงาน, สถานที่จัดประมูล, เวลา — เก็บไว้แสดง/อ้างอิง
                "court_name": (h.get("law_court_name") or "").strip() or None,
                "auction_venue": (h.get("sale_location1") or "").strip() or None,
                "auction_time": (h.get("sale_time1") or "").strip() or None,
                "house_no": (h.get("addrno") or "").strip() or None,
                "_price_note": "opening=appraised (LED list page ไม่แยกราคาเปิด)",
                # ทุกนัดประมูล: [{"round": 1, "date": "2026-06-22"}, ...]
                "auction_rounds": rounds,
                "auction_rounds_total": len(rounds) or None,
                "_safe_hidden": safe,
                # payload สำหรับ replay POST ไป asset_open.asp ดึงหน้ารายละเอียด
                # (รูป/เนื้อที่/ราคาประเมิน/สถานะนัด) — เป็น hidden ที่ตัด PII แล้ว
                "_open_post": h,
                "_source_url": resp.url,
            }

    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _strip_admin(text: str | None) -> str | None:
        """ตัดคำนำหน้าเขตปกครองออก เช่น 'ตำบลแม่เล่ย์' -> 'แม่เล่ย์'
        'อำเภอกิ่งอำเภอแม่วงก์' -> 'แม่วงก์'  (ตัดซ้อนได้)
        """
        if not text:
            return None
        t = text.strip()
        for _ in range(3):
            for p in ("ตำบล", "แขวง", "อำเภอ", "กิ่งอำเภอ", "เขต", "จังหวัด"):
                if t.startswith(p):
                    t = t[len(p):].strip()
                    break
            else:
                break
        return t or None

    @staticmethod
    def _map_type(desc: str | None, landtype: str | None) -> str:
        """แม็พประเภททรัพย์จาก assettypedesc (ยาวก่อนสั้น)"""
        text = f"{desc or ''} {landtype or ''}"
        ordered = [
            ("ที่ดินพร้อมสิ่งปลูกสร้าง", "house"),
            ("อาคารพาณิชย์", "commercial"),
            ("ตึกแถว", "commercial"),
            ("ห้องชุด", "condo"),
            ("คอนโด", "condo"),
            ("ทาวน์เฮ้าส์", "townhouse"),
            ("ทาวน์เฮาส์", "townhouse"),
            ("ทาวน์โฮม", "townhouse"),
            ("บ้านแฝด", "house"),
            ("บ้านเดี่ยว", "house"),
            ("บ้าน", "house"),
            ("สิ่งปลูกสร้าง", "house"),
            ("ที่ดินว่างเปล่า", "land"),
            ("ที่ดินเปล่า", "land"),
            ("ที่ดิน", "land"),
        ]
        for label, code in ordered:
            if label in text:
                return code
        return "other"

    def _property_address(self, h: dict) -> str | None:
        """ประกอบที่อยู่ทรัพย์จริงจากชิ้นส่วน (เลขที่ + ตำบล/อำเภอ/จังหวัด)

        ใช้ข้อมูลโฉนดเป็น fallback เมื่อช่องหลักว่าง (พบบ่อยในคอนโด)
        ไม่ใช้ sale_location1 เพราะนั่นคือ "สถานที่จัดประมูล" ไม่ใช่ที่ตั้งทรัพย์
        """
        no = (h.get("addrno") or "").strip()
        sub = self._strip_admin(h.get("tumbol")) or self._strip_admin(h.get("deedtumbol"))
        dist = self._strip_admin(h.get("ampur")) or self._strip_admin(h.get("deedampur"))
        prov = (h.get("city") or h.get("deedcity") or "").strip()
        parts = []
        if no and no not in ("-", "0"):
            parts.append(f"เลขที่ {no}")
        if sub:
            parts.append(f"แขวง/ตำบล {sub}")
        if dist:
            parts.append(f"เขต/อำเภอ {dist}")
        if prov:
            parts.append(prov)
        return " ".join(parts) or None

    @staticmethod
    def _image_from_landpic(z: str | None) -> str | None:
        """แปลง path รูปภายใน (Z:\\ปี\\...\\ไฟล์.jpg) เป็น URL เว็บ

        เซิร์ฟเวอร์ LED map: Z:\\<rest> -> https://asset.led.go.th/PPKPicture/<rest>
        (ยืนยันจากการ POST จริง: landpicture 'Z:\\2568\\12-2568\\23\\2758-60p.jpg'
        ให้รูปเดียวกับ '/PPKPicture/2568/12-2568/23/2758-60p.jpg')
        ทรัพย์ล้มละลายจะมีโฟลเดอร์หมวด (เช่น งานล้ม) อยู่ใน Z:\\ path เองอยู่แล้ว
        """
        from urllib.parse import quote
        if not z:
            return None
        z = z.strip()
        if not re.search(r"\.(?:jpg|jpeg|png)$", z, re.I):
            return None
        z = re.sub(r"^[A-Za-z]:", "", z)          # ตัด 'Z:' นำหน้า
        path = z.replace("\\", "/").lstrip("/")
        if not path:
            return None
        return "https://asset.led.go.th/PPKPicture/" + quote(path, safe="/.-_")

    @staticmethod
    def _clean_deed(s: str | None) -> str | None:
        """เลขโฉนด — '-' หรือว่าง แปลว่าไม่มี (ที่ดิน น.ส.3 ก. ฯลฯ ไม่มีโฉนด)"""
        if not s:
            return None
        s = s.strip()
        return s if s and s not in ("-", "0") else None

    @staticmethod
    def _be_yyyymmdd(s: str | None) -> date | None:
        """แปลง '25690824' (พ.ศ. yyyymmdd) -> date(2026, 8, 24)"""
        if not s or len(s) != 8 or not s.isdigit():
            return None
        try:
            return date(int(s[:4]) - 543, int(s[4:6]), int(s[6:8]))
        except ValueError:
            return None

    def _auction_date_round(self, h: dict, bid_date):
        """หาว่าเป็นการขาย 'นัดที่' เท่าไรของวันที่ค้นหา

        biddate1..8 คือวันขายแต่ละนัด (พ.ศ. yyyymmdd) ถ้าวันไหนตรงกับ
        วันที่ค้นหา นั่นคือนัดของวันนี้ คืน (วันที่, เลขนัด)
        ถ้าไม่ตรง คืนวันขายที่ใกล้ที่สุดในอนาคต + เลขนัดของมัน
        """
        dated = [(n, self._be_yyyymmdd(h.get(f"biddate{n}")))
                 for n in range(1, 9)]
        dated = [(n, d) for n, d in dated if d]
        if bid_date:
            for n, d in dated:
                if d == bid_date:
                    return d, n
        future = [(n, d) for n, d in dated if not bid_date or d >= bid_date]
        if future:
            n, d = min(future, key=lambda x: x[1])
            return d, n
        return (None, None)

    def _auction_rounds(self, h: dict) -> list[dict]:
        """คืนทุกนัดประมูลเป็นโครงสร้างสะอาด เรียงตามนัด

        biddate1..8 = วันขายแต่ละนัด (นัด 1-6 ปกติ) แปลง พ.ศ. -> ISO
        เก็บไว้ครบทุกนัด ไม่ใช่แค่นัดของวันนี้ เผื่อวิเคราะห์/แจ้งเตือนล่วงหน้า
        """
        out = []
        for n in range(1, 9):
            d = self._be_yyyymmdd(h.get(f"biddate{n}"))
            if d:
                out.append({"round": n, "date": d.isoformat()})
        return out

    @staticmethod
    def _max_price(h: dict) -> float | None:
        """ราคาประเมิน = ค่ามากสุดใน assetprice1..9 (ช่องที่ไม่ใช่ 0)"""
        best = None
        for n in range(1, 10):
            v = to_number(h.get(f"assetprice{n}"))
            if v and v > 0 and (best is None or v > best):
                best = v
        return best
