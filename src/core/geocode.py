"""Geocoding — หาพิกัดจากชื่ออำเภอ/จังหวัด

ใช้ Nominatim ของ OpenStreetMap เพราะไม่ต้องมี API key ไม่ต้องผูกบัตร
แลกกับข้อจำกัดที่ต้องเคารพอย่างเคร่งครัด

    - ไม่เกิน 1 request ต่อวินาที (บังคับในโค้ดแล้ว)
    - ต้องระบุ User-Agent ที่ติดต่อกลับได้
    - ห้ามยิงซ้ำสิ่งที่เคยถามแล้ว -> cache ลงฐานข้อมูลถาวร

ความแม่นระดับอำเภอ
    พิกัดที่ได้คือจุดกึ่งกลางของอำเภอ ไม่ใช่ตำแหน่งทรัพย์จริง
    พอสำหรับ: จัดกลุ่มโซน แสดงหมุดคร่าว ๆ วิเคราะห์ระดับเขต
    ไม่พอสำหรับ: วัดระยะถึงสถานีรถไฟฟ้า ตรวจแนวเวนคืน

    จึงบันทึก precision ไว้ทุกแถว และ rules_infra จะไม่ใช้พิกัดระดับอำเภอ
    คำนวณระยะถึงสถานี เพราะจะได้คำตอบที่ผิดแบบดูน่าเชื่อถือ
"""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "npa-ingest/0.1 (personal research; contact: REPLACE_WITH_YOUR_EMAIL)"
MIN_INTERVAL = 1.2          # วินาที — เผื่อไว้มากกว่าที่เขากำหนด

_last_call = 0.0


def _throttle() -> None:
    global _last_call
    gap = time.monotonic() - _last_call
    if gap < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - gap)
    _last_call = time.monotonic()


def _query(params: dict) -> list[dict]:
    _throttle()
    try:
        resp = requests.get(
            NOMINATIM,
            params={**params, "format": "json", "limit": 3,
                    "addressdetails": 1, "countrycodes": "th"},
            headers={"User-Agent": USER_AGENT, "Accept-Language": "th"},
            timeout=25)
        if resp.status_code != 200:
            log.warning("Nominatim ตอบ HTTP %s", resp.status_code)
            return []
        return resp.json()
    except Exception as exc:                                  # noqa: BLE001
        log.warning("เรียก Nominatim ไม่สำเร็จ: %s", exc)
        return []


def _pick_admin(results: list[dict]) -> dict | None:
    """เลือกผลที่เป็นขอบเขตการปกครองเท่านั้น

    ถ้าไม่กรอง Nominatim จะคืนโรงไฟฟ้า บริษัท หรือร้านก๋วยเตี๋ยวที่ชื่อคล้ายกัน
    ซึ่งพิกัดจะเพี้ยนไปหลายกิโลเมตรโดยที่ดูเหมือนถูกต้อง
    """
    for r in results:
        if r.get("class") == "boundary" and r.get("type") == "administrative":
            return r
    return None


# พิกัดกลางจังหวัด (โดยประมาณ = ตัวเมือง) — fallback สุดท้ายเมื่อ OSM หาไม่เจอ
# ครอบ 77 จังหวัด ทำให้ทรัพย์ที่รู้จังหวัดมีหมุดเสมอ (อย่างน้อยระดับจังหวัด)
PROVINCE_CENTROIDS: dict[str, tuple[float, float]] = {
    "กรุงเทพมหานคร": (13.7563, 100.5018), "นนทบุรี": (13.8591, 100.5217),
    "ปทุมธานี": (14.0208, 100.5250), "สมุทรปราการ": (13.5991, 100.5998),
    "สมุทรสาคร": (13.5475, 100.2745), "สมุทรสงคราม": (13.4098, 100.0021),
    "นครปฐม": (13.8199, 100.0621), "พระนครศรีอยุธยา": (14.3532, 100.5689),
    "อ่างทอง": (14.5896, 100.4550), "ลพบุรี": (14.7995, 100.6534),
    "สิงห์บุรี": (14.8907, 100.3967), "ชัยนาท": (15.1852, 100.1251),
    "สระบุรี": (14.5289, 100.9108), "สุพรรณบุรี": (14.4745, 100.1177),
    "นครนายก": (14.2069, 101.2130), "ชลบุรี": (13.3611, 100.9847),
    "ระยอง": (12.6833, 101.2372), "จันทบุรี": (12.6113, 102.1035),
    "ตราด": (12.2436, 102.5150), "ฉะเชิงเทรา": (13.6904, 101.0779),
    "ปราจีนบุรี": (14.0509, 101.3700), "สระแก้ว": (13.8240, 102.0645),
    "ราชบุรี": (13.5282, 99.8134), "กาญจนบุรี": (14.0227, 99.5328),
    "เพชรบุรี": (13.1119, 99.9399), "ประจวบคีรีขันธ์": (11.8126, 99.7957),
    "เชียงใหม่": (18.7883, 98.9853), "เชียงราย": (19.9105, 99.8406),
    "ลำพูน": (18.5745, 99.0087), "ลำปาง": (18.2888, 99.4909),
    "แม่ฮ่องสอน": (19.3020, 97.9654), "น่าน": (18.7756, 100.7730),
    "พะเยา": (19.1664, 99.9003), "แพร่": (18.1445, 100.1405),
    "อุตรดิตถ์": (17.6200, 100.0993), "ตาก": (16.8695, 99.1258),
    "สุโขทัย": (17.0056, 99.8265), "พิษณุโลก": (16.8211, 100.2659),
    "พิจิตร": (16.4429, 100.3487), "กำแพงเพชร": (16.4828, 99.5226),
    "เพชรบูรณ์": (16.4189, 101.1591), "นครสวรรค์": (15.7047, 100.1372),
    "อุทัยธานี": (15.3794, 100.0246), "นครราชสีมา": (14.9799, 102.0977),
    "บุรีรัมย์": (14.9930, 103.1029), "สุรินทร์": (14.8818, 103.4936),
    "ศรีสะเกษ": (15.1186, 104.3220), "อุบลราชธานี": (15.2448, 104.8473),
    "ยโสธร": (15.7942, 104.1452), "ชัยภูมิ": (15.8069, 102.0316),
    "อำนาจเจริญ": (15.8657, 104.6257), "หนองบัวลำภู": (17.2041, 102.4404),
    "ขอนแก่น": (16.4419, 102.8360), "อุดรธานี": (17.4139, 102.7872),
    "เลย": (17.4860, 101.7223), "หนองคาย": (17.8783, 102.7420),
    "มหาสารคาม": (16.1851, 103.3029), "ร้อยเอ็ด": (16.0538, 103.6520),
    "กาฬสินธุ์": (16.4315, 103.5059), "สกลนคร": (17.1555, 104.1348),
    "นครพนม": (17.4088, 104.7695), "มุกดาหาร": (16.5453, 104.7235),
    "บึงกาฬ": (18.3609, 103.6465), "นครศรีธรรมราช": (8.4304, 99.9631),
    "กระบี่": (8.0863, 98.9063), "พังงา": (8.4510, 98.5253),
    "ภูเก็ต": (7.8804, 98.3923), "สุราษฎร์ธานี": (9.1382, 99.3215),
    "ระนอง": (9.9528, 98.6085), "ชุมพร": (10.4930, 99.1800),
    "สงขลา": (7.1988, 100.5951), "สตูล": (6.6238, 100.0674),
    "ตรัง": (7.5563, 99.6114), "พัทลุง": (7.6167, 100.0742),
    "ปัตตานี": (6.8692, 101.2550), "ยะลา": (6.5410, 101.2803),
    "นราธิวาส": (6.4318, 101.8259),
}


def _province_result(province: str | None) -> dict | None:
    """หาพิกัดระดับจังหวัด — ลอง Nominatim ก่อน ถ้าไม่ได้ใช้ตารางกลางจังหวัด

    ผ่อนเงื่อนไข: ระดับจังหวัดยอมรับผลแรกที่ไม่ใช่ admin ได้ (หยาบอยู่แล้ว)
    สุดท้ายถ้ายังไม่เจอ ใช้ PROVINCE_CENTROIDS เพื่อรับประกันว่ามีหมุด
    """
    prov = (province or "").strip()
    if not prov:
        return None
    results = _query({"state": prov, "country": "Thailand"})
    r = _pick_admin(results) or (results[0] if results else None)
    if r:
        try:
            return _to_result(r, "province", "province_only")
        except (KeyError, ValueError, TypeError):
            pass
    c = PROVINCE_CENTROIDS.get(prov)
    if not c:
        # ข้อมูลเก่าบางแถว province เป็นชื่อสำนักงาน เช่น
        # "แพ่งกรุงเทพมหานคร 1", "ปทุมธานี สาขาธัญบุรี" — หาชื่อจังหวัดจริง
        # ที่เป็นสตริงย่อย เลือกที่ยาวสุดกันจับผิด
        best = None
        for name in PROVINCE_CENTROIDS:
            if name in prov and (best is None or len(name) > len(best)):
                best = name
        if best:
            c = PROVINCE_CENTROIDS[best]
    if c:
        return {"lat": c[0], "lng": c[1], "precision": "province",
                "display_name": prov, "method": "static_centroid",
                "osm_type": "static/province"}
    return None


def geocode_place(province: str, district: str | None = None,
                  subdistrict: str | None = None) -> dict | None:
    """หาพิกัดจากตำบล > อำเภอ > จังหวัด ตามลำดับความละเอียด

    ระดับตำบลสำคัญมาก เพราะถ้าใช้ระดับอำเภอ ทรัพย์ทุกตัวในอำเภอเดียวกัน
    จะได้พิกัดเดียวกันหมด แล้วแผนที่จะสื่อว่าทรัพย์กระจุกอยู่จุดเดียว
    ซึ่งไม่จริงและทำให้ผู้ใช้เข้าใจทำเลผิด
    """
    if subdistrict and district and province:
        # ทดสอบแล้วสองแบบนี้ให้ผลดีที่สุด
        #   city= ตรงกับระดับตำบล/เทศบาลใน OSM
        #   free text ต้องไม่ใส่คำว่า "ตำบล" นำหน้า ไม่งั้นไปเจอสำนักงานที่ดิน
        for params, method in (
            ({"city": subdistrict, "state": province, "country": "Thailand"},
             "structured_sub"),
            ({"q": f"{subdistrict}, {district}, {province}"}, "freetext_sub"),
        ):
            res = _pick_admin(_query(params))
            if res:
                return _to_result(res, "subdistrict", method)

    return geocode_district(district, province) if district else _province_only(province)


def _province_only(province: str) -> dict | None:
    return _province_result(province)


def geocode_district(district: str, province: str) -> dict | None:
    """คืน {lat, lng, precision, display_name, method} หรือ None

    ลองสามชั้นจากแม่นที่สุดไปหยาบที่สุด
      1. structured query อำเภอ + จังหวัด  -> precision 'district'
      2. free text "อำเภอ, จังหวัด"        -> precision 'district'
      3. จังหวัดอย่างเดียว                  -> precision 'province'
    """
    if province and district:
        res = _pick_admin(_query({"county": district, "state": province,
                                  "country": "Thailand"}))
        if res:
            return _to_result(res, "district", "structured")

        res = _pick_admin(_query({"q": f"{district}, {province}, ประเทศไทย"}))
        if res:
            return _to_result(res, "district", "freetext")

    # ชั้นสุดท้าย: ระดับจังหวัด (Nominatim ผ่อนเงื่อนไข -> ตารางกลางจังหวัด)
    return _province_result(province)


def _to_result(r: dict, precision: str, method: str) -> dict:
    return {
        "lat": float(r["lat"]),
        "lng": float(r["lon"]),
        "precision": precision,
        "display_name": r.get("display_name", "")[:300],
        "method": method,
        "osm_type": f"{r.get('class')}/{r.get('type')}",
    }
