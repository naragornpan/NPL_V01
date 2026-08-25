"""ดึงข้อมูลเพิ่มจากหน้ารายละเอียด — แกลเลอรีรูป + ที่อยู่เต็ม

ดึงสองอย่างในคำขอเดียว เพราะอยู่หน้าเดียวกัน

ทำไมไม่ดึงล่วงหน้าทุกทรัพย์
    ทรัพย์ 3,400 รายการ = 3,400 request เพิ่ม ใช้เวลาเป็นชั่วโมง
    แต่ทรัพย์ส่วนใหญ่ไม่มีใครเปิดดูเลย

    จึงดึงครั้งแรกที่มีคนกดเข้าหน้ารายละเอียด แล้วเก็บถาวร
    ต้นทุนจึงแปรผันตามความสนใจจริง ไม่ใช่ตามจำนวนทรัพย์

บันทึกผลทุกครั้งแม้ไม่เจอรูป
    ไม่งั้นทรัพย์ที่ไม่มีรูปจะถูกยิงซ้ำทุกครั้งที่มีคนเปิด
"""
from __future__ import annotations

import logging
import re

from core.http import Fetcher

log = logging.getLogger(__name__)

# URL รูปทรัพย์ของ BAM — โฟลเดอร์ /bam/ คือรูปเต็ม ส่วน /bam-strapi/ คือไอคอนเว็บ
BAM_ASSET_IMG = re.compile(
    r"https://bam-bo-fs-prd\.bam\.co\.th/bam/[\w.-]+\.(?:jpg|jpeg|png)",
    re.IGNORECASE)

# พิกัดในหน้ารายละเอียด BAM ฝังมากับลิงก์ Google Maps
#   ...google.com/maps?q=13.79137225,100.34482666
# บางหน้าอาจใช้ ll= center= destination= หรือรูปแบบ @lat,lng
# ครอบทุกแบบไว้ก่อน แล้วค่อยกรองด้วยกรอบพิกัดประเทศไทยอีกชั้น
#
# ดึงพิกัดจริงได้แบบนี้ดีกว่า geocode มาก เพราะ geocode ให้แค่จุดกึ่งกลางตำบล
# ทรัพย์ทุกตัวในตำบลเดียวกันจึงซ้อนหมุดกัน ส่วนพิกัดจริงกระจายถูกตำแหน่ง
BAM_LATLNG = re.compile(
    r"(?:[?&](?:q|ll|center|destination|daddr|sll)=|@|q=|=)"
    r"(-?\d{1,2}\.\d{3,}),\s*(-?\d{2,3}\.\d{3,})")


def bam_coords_from_text(page_source: str) -> tuple[float, float] | None:
    """คืน (lat, lng) แรกที่อยู่ในกรอบประเทศไทย หรือ None

    ตรวจกรอบไทยก่อนคืน กันค่าเพี้ยน (เช่น 0,0 หรือพิกัดต่างประเทศที่ติดมา
    ในสคริปต์ของหน้า) ซึ่งจะทำให้หมุดกระเด็นออกนอกแผนที่
    """
    for m in BAM_LATLNG.finditer(page_source):
        try:
            lat, lng = float(m.group(1)), float(m.group(2))
        except ValueError:
            continue
        if 5.5 <= lat <= 20.5 and 97.0 <= lng <= 106.0:
            return lat, lng
    return None


def fetch_bam_gallery(detail_url: str, fetcher: Fetcher | None = None) -> list[str]:
    """คืน URL รูปเรียงตามลำดับในหน้า

    ดึงจาก source ทั้งหน้าเพราะ BAM ใส่รูปไว้ใน JSON ของ Next.js
    ไม่ได้อยู่ในแท็ก img ทั้งหมด
    """
    f = fetcher or Fetcher(encoding="utf-8", rate_limit_s=1.5)
    try:
        resp = f.get(detail_url)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("ดึงแกลเลอรีไม่สำเร็จ %s — %s", detail_url, exc)
        return []
    if resp.status != 200:
        return []

    seen: list[str] = []
    for url in BAM_ASSET_IMG.findall(resp.text):
        if url not in seen:
            seen.append(url)
    return sorted(seen, key=_index_of)


# ที่อยู่ในหน้ารายละเอียดมีรูปแบบสม่ำเสมอ
#   "ที่ตั้งทรัพย์ เลขที่ 64/81 ... ถนนวัดลาดปลาดุก, บางคูรัด, บางบัวทอง, นนทบุรี"
# สามส่วนท้ายคือ ตำบล, อำเภอ, จังหวัด เสมอ
ADDRESS_RE = re.compile(r"ที่ตั้งทรัพย์\s*(.{10,300}?)\s*(?:รายละเอียดทรัพย์|หมายเหตุ|$)")


def parse_bam_address(page_text: str) -> dict | None:
    """แยกที่อยู่เต็มเป็นส่วน ๆ

    คืน {full, street, subdistrict, district, province} หรือ None

    ทำไมสำคัญ: geocoding ระดับอำเภอทำให้ทรัพย์ทุกตัวในอำเภอเดียวกัน
    ได้พิกัดเดียวกันหมด แผนที่จึงกลายเป็นหมุดซ้อนกันเป็นร้อยจุดเดียว
    ซึ่งไม่ใช่แค่ดูไม่สวย แต่สื่อความหมายผิดว่าทรัพย์กระจุกอยู่ตรงนั้น
    """
    m = ADDRESS_RE.search(page_text)
    if not m:
        return None
    full = re.sub(r"\s+", " ", m.group(1)).strip(" ,")
    parts = [x.strip() for x in full.split(",") if x.strip()]
    if len(parts) < 3:
        return {"full": full, "street": full, "subdistrict": None,
                "district": None, "province": None}
    return {
        "full": full,
        "street": ", ".join(parts[:-3]).strip() or None,
        "subdistrict": parts[-3],
        "district": parts[-2],
        "province": parts[-1],
    }


def fetch_bam_detail(detail_url: str, fetcher: Fetcher | None = None) -> dict:
    """ดึงทั้งรูปและที่อยู่ในคำขอเดียว

    แยกจาก fetch_bam_gallery เพื่อไม่ให้ยิงเว็บสองรอบสำหรับหน้าเดียวกัน
    """
    from bs4 import BeautifulSoup

    f = fetcher or Fetcher(encoding="utf-8", rate_limit_s=1.5)
    try:
        resp = f.get(detail_url)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("ดึงหน้ารายละเอียดไม่สำเร็จ %s — %s", detail_url, exc)
        return {"images": [], "address": None, "lat": None, "lng": None}
    if resp.status != 200:
        return {"images": [], "address": None, "lat": None, "lng": None}

    images: list[str] = []
    for url in BAM_ASSET_IMG.findall(resp.text):
        if url not in images:
            images.append(url)

    text = re.sub(r"\s+", " ",
                  BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True))
    # หาพิกัดจาก HTML ดิบ (resp.text) ไม่ใช่จาก text ที่ตัดแท็กแล้ว
    # เพราะลิงก์แผนที่อยู่ใน href/สคริปต์ ซึ่งหายไปหลัง get_text()
    coords = bam_coords_from_text(resp.text)
    return {"images": sorted(images, key=_index_of),
            "address": parse_bam_address(text),
            "lat": coords[0] if coords else None,
            "lng": coords[1] if coords else None}


def _index_of(url: str) -> int:
    """เรียงตามเลขท้ายไฟล์ เช่น -0.jpg -1.jpg -10.jpg

    ถ้าเรียงแบบสตริงจะได้ -10 มาก่อน -2 ซึ่งผิดลำดับที่เจ้าของตั้งใจ
    """
    m = re.search(r"-(\d+)\.(?:jpg|jpeg|png)$", url, re.IGNORECASE)
    return int(m.group(1)) if m else 9999


# =====================================================================
# SAM — หน้ารายละเอียดให้ "พิกัดจริงระดับแปลง" มาเลย
#
# ดีกว่า geocode ทุกกรณี เพราะ geocode ได้แค่จุดกึ่งกลางตำบล
# ทรัพย์ทุกตัวในตำบลเดียวกันจึงซ้อนกัน ส่วนพิกัดจริงกระจายถูกต้อง
# =====================================================================

SAM_DETAIL = "https://sam.or.th/site/npa/detail.php?id={id}&keyref="
SAM_LATLNG = re.compile(r"show_map\.php\?lat=(-?\d+\.\d+)&lng=(-?\d+\.\d+)")
SAM_ADDRESS = re.compile(r"ที่ตั้ง\s*:\s*(.{5,250}?)\s*(?:เขตพื้นที่|ราคาต่อ|ราคาประกาศ|$)")
SAM_SUB = re.compile(r"ตำบล\s*([ก-๙\w]+)")
SAM_DIST = re.compile(r"อำเภอ\s*([ก-๙\w]+)")
SAM_ROAD = re.compile(r"ถนน\s*([^ก-๙]*[ก-๙\w\s.\-()]+?)\s*(?:ตำบล|แขวง|อำเภอ|เขต|จังหวัด)")


def fetch_sam_detail(listing_id: str, fetcher: Fetcher | None = None) -> dict:
    """ดึงพิกัดจริงและที่อยู่เต็มจากหน้ารายละเอียดของ SAM

    คืน {lat, lng, address_full, street, subdistrict, district}
    ค่าที่หาไม่เจอจะเป็น None
    """
    from bs4 import BeautifulSoup

    f = fetcher or Fetcher(encoding="utf-8", rate_limit_s=1.5)
    out: dict = {}
    try:
        resp = f.get(SAM_DETAIL.format(id=listing_id))
    except Exception as exc:                                  # noqa: BLE001
        log.warning("ดึงรายละเอียด SAM ไม่สำเร็จ %s — %s", listing_id, exc)
        return out
    if resp.status != 200:
        return out

    m = SAM_LATLNG.search(resp.text)
    if m:
        lat, lng = float(m.group(1)), float(m.group(2))
        # ตรวจว่าอยู่ในกรอบประเทศไทย กันค่าเพี้ยน
        if 5.5 <= lat <= 20.5 and 97.0 <= lng <= 106.0:
            out["lat"], out["lng"] = lat, lng

    text = re.sub(r"\s+", " ",
                  BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True))
    addr = SAM_ADDRESS.search(text)
    if addr:
        full = addr.group(1).strip()
        out["address_full"] = full
        sub = SAM_SUB.search(full)
        dist = SAM_DIST.search(full)
        road = SAM_ROAD.search(full)
        out["subdistrict"] = sub.group(1) if sub else None
        out["district"] = dist.group(1) if dist else None
        out["street"] = ("ถนน " + road.group(1).strip()) if road else None
    return out


# =====================================================================
# LED (กรมบังคับคดี) — หน้ารายละเอียด asset_open.asp
#
# หน้ารายการให้ path รูปเป็นของภายในเซิร์ฟเวอร์ (Z:\...) ใช้ไม่ได้
# หน้ารายละเอียดถึงจะมี URL รูปจริง (/PPKPicture/<หมวด>/...) + ข้อมูลเชิงลึก
#   - เนื้อที่ห้องชุด (ตร.ม.) ที่หน้ารายการไม่มี
#   - ราคาประเมินจริงของเจ้าพนักงานบังคับคดี
#   - สถานะรายนัด ("งดขายไม่มีผู้สู้ราคา" = ยกมาหลายนัด ราคาลด = น่าสนใจ)
#   - ปลอด/ติดจำนอง · เงินวางหลักประกัน
#
# PII: หน้านี้โชว์ชื่อโจทก์/จำเลย/เจ้าของ — เราไม่ดึงฟิลด์เหล่านั้นเลย
# =====================================================================

LED_OPEN_URL = "https://asset.led.go.th/newbidreg/asset_open.asp"
LED_IMG_RE = re.compile(r"/PPKPicture/[^\"'\s)>]+?\.(?:jpg|jpeg|png)", re.IGNORECASE)


def _led_images(page_source: str) -> list[str]:
    """คืน URL รูปทั้งหมด (normalize encoding) รูปทรัพย์หลักลงท้าย p.jpg

    m.jpg = แผนที่พอสังเขป, j.jpg = แผนที่โจทก์ — เก็บไว้แต่ไม่ใช่รูปหลัก
    """
    from urllib.parse import unquote, quote
    seen: list[str] = []
    for p in LED_IMG_RE.findall(page_source):
        p = unquote(unquote(p))                 # กัน double-encode
        url = "https://asset.led.go.th" + quote(p, safe="/.-_")
        if url not in seen:
            seen.append(url)
    # เรียงรูปทรัพย์ (p) มาก่อนแผนที่ (m/j)
    return sorted(seen, key=lambda u: 0 if re.search(r"p\.jpg$", u, re.I) else 1)


def _led_num(text: str, pattern: str) -> float | None:
    m = re.search(pattern, text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def fetch_led_detail(post_data: dict, fetcher: Fetcher | None = None) -> dict:
    """ดึงรายละเอียดทรัพย์ LED จาก asset_open.asp (POST ด้วย hidden fields เดิม)

    คืน {image_url, images, usable_area_sqm, appraised_price, deposit_amount,
         mortgage_carried, rounds}  — ค่าที่หาไม่เจอเป็น None
    ไม่เก็บชื่อบุคคล/เลขคดีใด ๆ
    """
    # หน้า LED newbidreg ปัจจุบันเป็น UTF-8 (ไม่ใช่ tis-620 ตามชื่อเก่า)
    f = fetcher or Fetcher(encoding="utf-8", rate_limit_s=4.0)
    out: dict = {"image_url": None, "images": [], "usable_area_sqm": None,
                 "appraised_price": None, "deposit_amount": None,
                 "mortgage_carried": None, "rounds": []}
    try:
        resp = f.post(LED_OPEN_URL, data=post_data)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("ดึงรายละเอียด LED ไม่สำเร็จ — %s", exc)
        return out
    if resp.status != 200:
        return out

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(resp.text, "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    images = _led_images(resp.text)
    out["images"] = images
    out["image_url"] = images[0] if images else None

    out["usable_area_sqm"] = _led_num(
        text, r"เนื้อที่ห้องชุด(?:ประมาณ)?\s*([\d,]+\.?\d*)\s*ตร\.ม\.")
    out["appraised_price"] = _led_num(
        text, r"ราคาประเมินของเจ้าพนักงานบังคับคดี\s*จำนวน\s*([\d,]+\.\d{2})")
    out["deposit_amount"] = _led_num(
        text, r"วางหลักประกันเป็นจำนวน\s*([\d,]+\.\d{2})")
    if "ปลอดการจำนอง" in text:
        out["mortgage_carried"] = False
    elif "ติดจำนอง" in text or "จำนองติด" in text:
        out["mortgage_carried"] = True

    # ตารางนัดที่/วันที่/สถานะ
    for tr in soup.select("table tr"):
        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(tds) >= 3 and tds[0].isdigit():
            out["rounds"].append({"round": int(tds[0]), "date": tds[1],
                                  "status": tds[2] or None})
    return out
