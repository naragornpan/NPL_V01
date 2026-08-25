"""Rule engine — ตัวแปรโครงสร้างพื้นฐานและการเปลี่ยนแปลงราคา

หลักการที่ห้ามลืม
-----------------
ถนนใหม่ไม่ได้ดีเสมอ มันเป็นตัวแปรที่มีสองด้านเสมอ:

  ด้านบวก   ที่ดินตาบอดได้ทางเข้าออก / เข้าเมืองเร็วขึ้น / ราคาประเมินขยับ
  ด้านลบ    ถูกเวนคืนบางส่วนจนแปลงเสียรูป / ติดถนนใหญ่จนเสียงดัง ฝุ่น
            ขายคนอยู่อาศัยยาก / ถูกตัดขาดจากถนนเดิม

ระบบจึงต้องคืนทั้ง flag บวกและลบ ห้ามยุบเหลือคะแนนเดียวแล้วบอกว่า "ดี"

และผลกระทบต้องคูณด้วย certainty weight เสมอ — โครงการที่ยังเป็นแค่ผลการศึกษา
กับโครงการที่ประกาศ พ.ร.ฎ. แล้ว ให้น้ำหนักต่างกันเกือบ 5 เท่า
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Flag:
    code: str
    severity: str          # positive | info | caution | critical
    score: float           # ถ่วงน้ำหนักแล้ว
    evidence: str          # ต้องอธิบายให้ลูกค้าอ่านรู้เรื่อง


# ระยะที่ผลกระทบยังมีนัยสำคัญ (เมตร)
STATION_BANDS = [(500, 1.00), (1000, 0.60), (1500, 0.30), (2500, 0.10)]
ROAD_BANDS = [(300, 1.00), (800, 0.55), (1500, 0.25)]


def _band(distance_m: float | None, bands) -> float:
    if distance_m is None:
        return 0.0
    for limit, factor in bands:
        if distance_m <= limit:
            return factor
    return 0.0


def evaluate_infra(feat: dict) -> list[Flag]:
    """feat = แถวจาก property_infra_features + certainty weight"""
    flags: list[Flag] = []
    weight = feat.get("certainty_weight", 0.15)

    # --- ความเสี่ยงก่อนเสมอ ---------------------------------------
    if feat.get("in_expropriation_corridor"):
        flags.append(Flag(
            code="EXPROPRIATION_RISK",
            severity="critical",
            score=-100.0,
            evidence=(
                f"อยู่ในแนวเขตเวนคืนของโครงการ {feat.get('expropriation_project')} "
                "ต้องตรวจแผนที่ท้าย พ.ร.ฎ. ที่สำนักงานที่ดินก่อนตัดสินใจทุกกรณี"
            ),
        ))

    # --- โอกาสจากระบบราง ------------------------------------------
    station_factor = _band(feat.get("nearest_station_m"), STATION_BANDS)
    if station_factor > 0:
        st_weight = feat.get("station_certainty_weight", weight)
        flags.append(Flag(
            code="TRANSIT_PROXIMITY",
            severity="positive",
            score=40 * station_factor * st_weight,
            evidence=(
                f"ห่างจาก {feat.get('nearest_station_name')} "
                f"ประมาณ {feat.get('nearest_station_m'):,.0f} ม. "
                f"(สถานะโครงการถ่วงน้ำหนัก {st_weight:.0%})"
            ),
        ))

    # --- โอกาสจากถนนใหม่ ------------------------------------------
    road_factor = _band(feat.get("nearest_new_road_m"), ROAD_BANDS)
    if road_factor > 0 and not feat.get("in_expropriation_corridor"):
        flags.append(Flag(
            code="NEW_ROAD_UPSIDE",
            severity="positive",
            score=30 * road_factor * weight,
            evidence=(
                f"ใกล้แนวโครงการ {feat.get('nearest_new_road_name')} "
                f"ประมาณ {feat.get('nearest_new_road_m'):,.0f} ม."
            ),
        ))
        # ที่ดินตาบอดที่จะได้ทางเข้าออก = จังหวะที่ดีที่สุดในเกมนี้
        if feat.get("no_access_road") and feat.get("nearest_new_road_m", 9e9) <= 300:
            flags.append(Flag(
                code="ACCESS_UNLOCK",
                severity="positive",
                score=60 * weight,
                evidence=(
                    "ปัจจุบันเป็นที่ดินไม่มีทางเข้าออก แต่แนวถนนใหม่ผ่านใกล้มาก "
                    "หากโครงการเกิดจริงจะเปลี่ยนสถานะทรัพย์ทั้งหมด "
                    "ต้องยืนยันว่ามีจุดเปิดทางเข้าออกจริงก่อน"
                ),
            ))

    # --- ผลข้างเคียงของถนนใหญ่ต่อผู้ซื้ออยู่อาศัย -------------------
    if road_factor >= 1.0 and feat.get("buyer_intent") == "own_use":
        flags.append(Flag(
            code="ARTERIAL_NUISANCE",
            severity="caution",
            score=-15.0,
            evidence="ติดแนวถนนสายหลัก มีผลเรื่องเสียงและฝุ่น สำหรับผู้ซื้ออยู่อาศัยเอง",
        ))

    return flags


def evaluate_price_change(feat: dict) -> list[Flag]:
    """ตัวแปรราคา 2 ชั้น

    gov_price_change_pct   ราคาประเมินกรมธนารักษ์ ระหว่างรอบบัญชี
                           ทางการ เชื่อถือได้ แต่ช้า (รอบละ 4 ปี) และตามหลังตลาด
    market_price_trend_pct ราคาปิดจริงจากข้อมูล led_result ของเราเอง
                           ไวกว่ามาก และไม่มีใครมีนอกจากเรา

    ถ้าสองตัวขัดกัน ให้เชื่อตัวหลังในระยะสั้น และใช้ตัวแรกยืนยันแนวโน้มยาว
    """
    flags: list[Flag] = []

    gov = feat.get("gov_price_change_pct")
    if gov is not None and gov >= 10:
        flags.append(Flag(
            code="GOV_PRICE_JUMP",
            severity="positive",
            score=min(25.0, gov),
            evidence=f"ราคาประเมินราชการโซนนี้ปรับขึ้น {gov:.1f}% เทียบรอบบัญชีก่อน",
        ))
    elif gov is not None and gov <= 0:
        flags.append(Flag(
            code="GOV_PRICE_FLAT",
            severity="caution",
            score=-10.0,
            evidence=f"ราคาประเมินราชการโซนนี้ไม่ขยับ ({gov:.1f}%) สวนทางค่าเฉลี่ยประเทศ",
        ))

    mkt = feat.get("market_price_trend_pct")
    if mkt is not None:
        if mkt <= -10:
            flags.append(Flag(
                code="MARKET_COOLING",
                severity="caution",
                score=-20.0,
                evidence=(
                    f"ราคาปิดจริงจากการขายทอดตลาดในเขตนี้ลดลง {abs(mkt):.1f}% "
                    "ใน 6 เดือน — ซื้อได้ถูกลง แต่ขายต่อก็ยากขึ้นด้วย"
                ),
            ))
        elif mkt >= 10:
            flags.append(Flag(
                code="MARKET_HEATING",
                severity="positive",
                score=20.0,
                evidence=f"ราคาปิดจริงในเขตนี้เพิ่มขึ้น {mkt:.1f}% ใน 6 เดือน",
            ))

    # สัญญาณที่มีค่าที่สุด: ราคาตลาดขยับแต่ราคาราชการยังไม่ตาม
    if gov is not None and mkt is not None and mkt - gov >= 15:
        flags.append(Flag(
            code="APPRAISAL_LAG",
            severity="positive",
            score=25.0,
            evidence=(
                "ราคาตลาดวิ่งนำราคาประเมินราชการอยู่มาก "
                "โซนนี้มีโอกาสถูกปรับราคาประเมินขึ้นในรอบบัญชีถัดไป"
            ),
        ))

    return flags
