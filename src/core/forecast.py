"""ประเมินมูลค่าอนาคต — คืนช่วงราคา 3 ฉาก พร้อมเหตุผลที่ตรวจสอบได้

สิ่งที่โมดูลนี้ทำ
    เทียบทรัพย์กับเหตุการณ์ในอดีตที่คล้ายกัน แล้วบอกว่า
    "โซนที่เคยผ่านสถานการณ์แบบนี้ ราคาขยับอยู่ในช่วงเท่าไหร่"

สิ่งที่โมดูลนี้ไม่ทำ
    ไม่บอกว่าราคาจะเป็นเท่าไหร่ ไม่รับประกันผลตอบแทน
    ไม่แทนที่การประเมินโดยผู้ประเมินที่มีใบอนุญาต

กติกาการนำเสนอ (ห้ามละเมิด)
    1. แสดงเป็นช่วงเสมอ ห้ามแสดงตัวเลขเดียว
    2. แสดง confidence และจำนวนตัวอย่างที่ใช้ควบคู่เสมอ
    3. แสดงเหตุผลทุกข้อพร้อมหลักฐาน ห้ามมีเหตุผลลอย ๆ
    4. ถ้า confidence = low ต้องเขียนกำกับให้ชัดว่าข้อมูลยังน้อย
"""
from __future__ import annotations

from dataclasses import dataclass, field

MODEL_VERSION = "0.1.0"

# ความน่าจะเป็นที่โครงการจะเดินหน้าถึงขั้นเปิดใช้ นับจากขั้นปัจจุบัน
# ค่าตั้งต้นแบบอนุรักษ์นิยม — ต้องแทนที่ด้วยค่าจริงจาก v_stage_base_rates
# ทันทีที่มีข้อมูลโครงการในระบบมากพอ (อย่างน้อย 20 โครงการต่อประเภท)
DEFAULT_COMPLETION_ODDS = {
    "study": 0.20,
    "cabinet": 0.45,
    "decree": 0.75,
    "construction": 0.92,
    "operational": 1.00,
}

# ระยะเวลาเฉลี่ยจากขั้นปัจจุบันถึงเปิดใช้ (เดือน) — ค่าตั้งต้นเช่นกัน
DEFAULT_MONTHS_TO_OPEN = {
    "study": 120,
    "cabinet": 84,
    "decree": 60,
    "construction": 36,
    "operational": 0,
}


@dataclass
class Reason:
    text: str
    direction: str          # positive | negative | neutral
    evidence: str           # ต้องชี้กลับไปที่ข้อมูลได้เสมอ
    weight: float


@dataclass
class Forecast:
    horizon_months: int
    base_value: float
    bear_value: float
    mid_value: float
    bull_value: float
    expected_uplift_pct: float
    confidence: str
    confidence_reason: str
    reasons: list[Reason] = field(default_factory=list)
    assumptions: dict = field(default_factory=dict)

    def to_row(self, property_id, base_value_source: str) -> dict:
        return {
            "property_id": property_id,
            "horizon_months": self.horizon_months,
            "base_value": self.base_value,
            "base_value_source": base_value_source,
            "bear_value": self.bear_value,
            "mid_value": self.mid_value,
            "bull_value": self.bull_value,
            "expected_uplift_pct": self.expected_uplift_pct,
            "confidence": self.confidence,
            "confidence_reason": self.confidence_reason,
            "reasons": [r.__dict__ for r in self.reasons],
            "assumptions": self.assumptions,
            "model_version": MODEL_VERSION,
        }


def distance_band(meters: float | None) -> str | None:
    if meters is None:
        return None
    if meters <= 500:
        return "0-500"
    if meters <= 1000:
        return "500-1000"
    if meters <= 2000:
        return "1000-2000"
    return None


def assess_confidence(curve_samples: int, certainty_code: str,
                      has_local_comps: bool) -> tuple[str, str]:
    """ความเชื่อมั่นมาจากปริมาณหลักฐาน ไม่ใช่ความแรงของตัวเลข

    ข้อผิดพลาดคลาสสิกคือเห็น uplift 60% แล้วบอกว่ามั่นใจมาก
    ทั้งที่คำนวณจากตัวอย่างแค่ 3 ดีล — ตัวเลขยิ่งสวยยิ่งต้องดู n
    """
    if curve_samples < 10:
        return "low", (
            f"อ้างอิงจากเหตุการณ์เทียบเคียงเพียง {curve_samples} รายการ "
            "ยังน้อยเกินกว่าจะสรุป ควรใช้เป็นกรอบความคิด ไม่ใช่ตัวเลขตัดสินใจ"
        )
    if not has_local_comps:
        return "low", "ไม่มีข้อมูลราคาปิดจริงในเขตนี้พอ ต้องอ้างอิงเขตข้างเคียง"
    if certainty_code in ("study", "cabinet"):
        return "medium", (
            "โครงการยังไม่ถึงขั้นประกาศ พ.ร.ฎ. มีโอกาสเลื่อนหรือยกเลิกได้จริง"
        )
    if curve_samples >= 30:
        return "high", f"อ้างอิงจากเหตุการณ์เทียบเคียง {curve_samples} รายการ"
    return "medium", f"อ้างอิงจากเหตุการณ์เทียบเคียง {curve_samples} รายการ"


def forecast(base_value: float, features: dict, curve: dict | None,
             horizon_months: int = 60) -> Forecast:
    """
    base_value  มูลค่าปัจจุบันที่ประเมินจาก comps หรือราคาประเมินปรับแล้ว
    features    แถวจาก property_infra_features + flags
    curve       แถวจาก v_uplift_curves (p25/p50/p75/n_obs) หรือ None
    """
    reasons: list[Reason] = []
    certainty = features.get("nearest_station_certainty_code", "study")
    odds = DEFAULT_COMPLETION_ODDS.get(certainty, 0.2)

    # ---- ไม่มีหลักฐานเทียบเคียง = ไม่พยากรณ์ --------------------------
    if not curve or curve.get("n_obs", 0) < 3:
        return Forecast(
            horizon_months=horizon_months,
            base_value=base_value,
            bear_value=base_value * 0.90,
            mid_value=base_value,
            bull_value=base_value * 1.15,
            expected_uplift_pct=0.0,
            confidence="low",
            confidence_reason=(
                "ยังไม่มีเหตุการณ์เทียบเคียงในฐานข้อมูลมากพอสำหรับทรัพย์ลักษณะนี้ "
                "ตัวเลขที่แสดงเป็นเพียงกรอบกว้าง ๆ ไม่ได้อ้างอิงหลักฐาน"
            ),
            reasons=[Reason(
                text="ยังไม่มีข้อมูลเทียบเคียงพอ",
                direction="neutral",
                evidence="v_uplift_curves มีตัวอย่างน้อยกว่า 3 เหตุการณ์",
                weight=0,
            )],
            assumptions={"note": "no_comparable_events"},
        )

    # ---- ปรับ uplift ด้วยโอกาสที่โครงการจะเกิดจริง --------------------
    p25, p50, p75 = curve["p25"], curve["p50"], curve["p75"]
    adj = lambda x: x * odds                              # noqa: E731

    reasons.append(Reason(
        text=f"โซนระยะ {curve['distance_band']} ม. จากโครงการประเภทเดียวกัน "
             f"เคยขยับ {p50:.0f}% (ช่วง {p25:.0f}–{p75:.0f}%) หลังหักแนวโน้มทั้งจังหวัดแล้ว",
        direction="positive" if p50 > 0 else "negative",
        evidence=f"difference-in-differences จาก {curve['n_obs']} เหตุการณ์ "
                 f"({curve.get('n_deals', 0)} ดีล) ที่ขั้น {curve['event_type']}",
        weight=1.0,
    ))
    reasons.append(Reason(
        text=f"โครงการอยู่ขั้น '{certainty}' โอกาสเดินหน้าถึงเปิดใช้ประมาณ {odds:.0%}",
        direction="neutral",
        evidence="อัตราอ้างอิงจาก v_stage_base_rates (ค่าตั้งต้นถ้ายังไม่มีข้อมูลพอ)",
        weight=0.8,
    ))

    # ---- ความเสี่ยงหักออกตรง ๆ ไม่กลบด้วยด้านบวก ---------------------
    penalty = 0.0
    if features.get("in_expropriation_corridor"):
        penalty += 0.35
        reasons.append(Reason(
            text="อยู่ในแนวเขตเวนคืน — มูลค่าอนาคตขึ้นกับค่าทดแทน ไม่ใช่ราคาตลาด",
            direction="negative",
            evidence=f"โครงการ {features.get('expropriation_project')} ระดับ พ.ร.ฎ. ขึ้นไป",
            weight=1.0,
        ))
    if features.get("market_price_trend_pct", 0) <= -10:
        penalty += 0.15
        reasons.append(Reason(
            text="ราคาปิดจริงในเขตนี้กำลังลดลง สวนทางกับผลบวกของโครงการ",
            direction="negative",
            evidence=f"ราคาปิดจริงเปลี่ยน {features['market_price_trend_pct']:.1f}% ใน 6 เดือน",
            weight=0.9,
        ))
    if features.get("gov_price_change_pct") and features.get("market_price_trend_pct"):
        gap = features["market_price_trend_pct"] - features["gov_price_change_pct"]
        if gap >= 15:
            reasons.append(Reason(
                text="ราคาตลาดวิ่งนำราคาประเมินราชการ มีโอกาสถูกปรับประเมินขึ้นรอบหน้า",
                direction="positive",
                evidence=f"ส่วนต่าง {gap:.1f} จุด ระหว่างราคาตลาดกับราคาประเมิน",
                weight=0.7,
            ))

    keep = max(0.0, 1.0 - penalty)
    bear = base_value * (1 + adj(p25) / 100 * keep)
    mid = base_value * (1 + adj(p50) / 100 * keep)
    bull = base_value * (1 + adj(p75) / 100 * keep)

    conf, conf_reason = assess_confidence(
        curve_samples=curve.get("n_obs", 0),
        certainty_code=certainty,
        has_local_comps=bool(features.get("has_local_comps")),
    )

    return Forecast(
        horizon_months=horizon_months,
        base_value=base_value,
        bear_value=round(bear),
        mid_value=round(mid),
        bull_value=round(bull),
        expected_uplift_pct=round((mid / base_value - 1) * 100, 1),
        confidence=conf,
        confidence_reason=conf_reason,
        reasons=reasons,
        assumptions={
            "completion_odds": odds,
            "certainty_stage": certainty,
            "risk_penalty": penalty,
            "curve": {k: curve.get(k) for k in
                      ("project_type", "event_type", "distance_band", "n_obs")},
            "horizon_months": horizon_months,
        },
    )


DISCLAIMER = (
    "ตัวเลขนี้เป็นการเทียบเคียงกับเหตุการณ์ในอดีต ไม่ใช่การพยากรณ์ราคา "
    "และไม่ใช่คำแนะนำการลงทุน โครงการภาครัฐเลื่อนหรือยกเลิกได้ "
    "ควรตรวจสอบเอกสารสิทธิ์และแนวเขตกับหน่วยงานที่เกี่ยวข้องก่อนตัดสินใจทุกครั้ง"
)
