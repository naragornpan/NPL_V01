"""คำนวณเกรดทรัพย์ — A ถึง E พร้อมเหตุผลทุกข้อ

หลักการที่ห้ามละเมิด
    1. ทุกคะแนนมาจากกฎที่ชี้ที่มาได้ ไม่ใช่โมเดลที่อธิบายไม่ได้
    2. **ข้อมูลไม่พอ = ไม่ให้เกรด ไม่ใช่ให้เกรดต่ำ**
       ทรัพย์ที่ข้อมูลน้อยไม่ได้แปลว่าแย่ แค่เรายังไม่รู้
       การให้ D กับทรัพย์ที่แค่ข้อมูลขาด คือการโกหกลูกค้า
    3. flag ระดับ critical กดเพดานทันที ไม่ให้คะแนนดีด้านอื่นมากลบ
       ทรัพย์ที่อยู่ในแนวเวนคืนไม่มีทางเป็นเกรด A ต่อให้ถูกแค่ไหน
"""
from __future__ import annotations

from dataclasses import dataclass, field

MODEL_VERSION = "0.1.0"

# ฟิลด์ที่จำเป็นต่อการให้เกรด และน้ำหนักความสำคัญ
REQUIRED_FIELDS = {
    "opening_price": 0.30,
    "property_type": 0.15,
    "province": 0.10,
    "district": 0.15,
    "land_area_sqwa": 0.15,
    "lat": 0.15,
}
MIN_COMPLETENESS = 0.70


@dataclass
class GradeResult:
    grade: str | None
    score: float | None
    completeness: float
    reasons: list[dict] = field(default_factory=list)

    def to_row(self, source_code: str, external_ref: str) -> dict:
        return {
            "source_code": source_code, "external_ref": external_ref,
            "grade": self.grade, "score": self.score,
            "completeness": round(self.completeness, 2),
            "reasons": self.reasons, "model_version": MODEL_VERSION,
        }


def _f(value) -> float | None:
    """แปลงเป็น float ให้แน่ใจ

    psycopg คืนคอลัมน์ numeric เป็น decimal.Decimal ซึ่งคูณหารกับ float ไม่ได้
    เป็นบั๊กที่ไม่โผล่ตอนทดสอบด้วย dict ธรรมดา แต่พังทันทีเมื่อต่อฐานจริง
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def completeness_of(row: dict) -> float:
    got = sum(w for f, w in REQUIRED_FIELDS.items() if row.get(f) not in (None, ""))
    return got / sum(REQUIRED_FIELDS.values())


def grade_for_score(score: float) -> str:
    for grade, threshold in (("A", 80), ("B", 65), ("C", 50), ("D", 30)):
        if score >= threshold:
            return grade
    return "E"


def compute(row: dict, flags: list[dict] | None = None,
            zone_stats: dict | None = None) -> GradeResult:
    """
    row        แถวจาก v_listings_with_grade
    flags      ผลจาก rule engine
    zone_stats สถิติโซน เช่น sell_through_pct, median_price_sqwa
    """
    flags = flags or []
    zone_stats = zone_stats or {}
    reasons: list[dict] = []

    comp = completeness_of(row)
    if comp < MIN_COMPLETENESS:
        missing = [f for f in REQUIRED_FIELDS if row.get(f) in (None, "")]
        return GradeResult(
            grade=None, score=None, completeness=comp,
            reasons=[{
                "factor": "ข้อมูลไม่พอให้เกรด", "impact": 0,
                "detail": f"ยังขาด: {', '.join(missing)} — "
                          "ไม่ให้เกรดดีกว่าให้เกรดที่เชื่อไม่ได้",
            }],
        )

    score = 50.0  # เริ่มที่กลาง ๆ

    # --- ส่วนลด = สัญญาณหลักของความคุ้ม ------------------------------
    # ทรัพย์ที่มีราคาประเมิน (BAM/LED) เทียบราคาขายกับราคาประเมิน
    # ทรัพย์ธนาคาร (ghb/sam/ttb) ไม่มีราคาประเมิน แต่มี "ราคาตั้ง vs ราคาพิเศษ"
    # ซึ่งเป็นส่วนลดจริงที่ธนาคารประกาศเอง — ใช้เป็นสัญญาณหลักแทนได้
    price = _f(row.get("opening_price"))
    appraised = _f(row.get("appraised_price"))
    special, listed = _f(row.get("special_price")), _f(row.get("list_price"))

    if price and appraised and appraised > 0:
        disc = (1 - price / appraised) * 100
        pts = max(-15, min(30, disc * 0.7))
        score += pts
        reasons.append({
            "factor": "ส่วนลดจากราคาประเมิน", "impact": round(pts, 1),
            "detail": f"ต่ำกว่าราคาประเมิน {disc:.0f}%",
        })
        # ถ้ามีราคาพิเศษเพิ่มอีก โบนัสเล็กน้อย (ไม่ให้ซ้ำกับก้อนหลัก)
        if special and listed and listed > 0 and special < listed:
            cut = (1 - special / listed) * 100
            b = min(8, cut * 0.4)
            score += b
            reasons.append({
                "factor": "ราคาพิเศษ", "impact": round(b, 1),
                "detail": f"ลดจากราคาตั้งขาย {cut:.0f}%",
            })
    elif special and listed and listed > 0 and special < listed:
        # ไม่มีราคาประเมิน → ใช้ส่วนลดจากราคาตั้งขายเป็นสัญญาณหลัก
        cut = (1 - special / listed) * 100
        pts = min(25, cut * 0.7)
        score += pts
        reasons.append({
            "factor": "ส่วนลดจากราคาตั้งขาย", "impact": round(pts, 1),
            "detail": f"ต่ำกว่าราคาตั้งขาย {cut:.0f}% "
                      f"(ทรัพย์ธนาคารไม่มีราคาประเมินให้เทียบ)",
        })

    # --- ราคาต่อตารางวาเทียบค่ากลางของโซน (ที่ดิน/บ้าน) ---------------
    zone_median = _f(zone_stats.get("median_price_sqwa"))
    area = _f(row.get("land_area_sqwa"))
    if zone_median and area and price:
        per_sqwa = price / area
        ratio = per_sqwa / zone_median
        pts = max(-20, min(25, (1 - ratio) * 60))
        score += pts
        reasons.append({
            "factor": "ราคาต่อ ตร.ว. เทียบโซน", "impact": round(pts, 1),
            "detail": f"{per_sqwa:,.0f} บาท/ตร.ว. เทียบค่ากลางโซน "
                      f"{zone_median:,.0f} ({ratio:.0%})",
        })

    # --- ราคาต่อตารางเมตรเทียบโซน (คอนโด/ห้องชุด ที่ไม่มีเนื้อที่ดิน) --
    # ปิดช่องที่คอนโดเสียเปรียบ: เดิมไม่มีเนื้อที่ดินเลยไม่ได้โบนัสก้อนนี้
    # ทำให้แทบไม่แตะเกรด A ทั้งที่ราคาอาจถูกกว่าตลาดมาก
    zone_median_sqm = _f(zone_stats.get("median_price_sqm"))
    usable = _f(row.get("usable_area_sqm"))
    if not (zone_median and area) and zone_median_sqm and usable and price:
        per_sqm = price / usable
        ratio = per_sqm / zone_median_sqm
        pts = max(-20, min(25, (1 - ratio) * 60))
        score += pts
        reasons.append({
            "factor": "ราคาต่อ ตร.ม. เทียบโซน", "impact": round(pts, 1),
            "detail": f"{per_sqm:,.0f} บาท/ตร.ม. เทียบค่ากลางโซน "
                      f"{zone_median_sqm:,.0f} ({ratio:.0%})",
        })

    # --- สภาพคล่องของโซน ----------------------------------------------
    st = _f(zone_stats.get("sell_through_pct"))
    if st is not None:
        pts = (st - 50) * 0.2
        score += pts
        reasons.append({
            "factor": "สภาพคล่องของโซน", "impact": round(pts, 1),
            "detail": f"ทรัพย์ในโซนนี้ขายจบ {st:.0f}% — "
                      + ("ออกของได้" if st >= 50 else "ขายต่อยาก ต้องเผื่อเวลา"),
        })

    # --- ปรับปรุงแล้ว --------------------------------------------------
    if row.get("renovated"):
        score += 6
        reasons.append({
            "factor": "ปรับปรุงแล้ว", "impact": 6,
            "detail": "ลดงบและเวลารีโนเวท แต่ต้องดูของจริงว่าปรับปรุงระดับไหน",
        })

    # --- flag จาก rule engine ------------------------------------------
    cap = None
    for f in flags:
        sev = f.get("severity")
        if sev == "critical":
            score -= 40
            cap = "E"
        elif sev == "caution":
            score -= 12
            # มีข้อควรระวังแม้ข้อเดียว ก็ไม่ควรได้ "น่าสนใจมาก"
            cap = cap or "B"
        elif sev == "positive":
            score += 8
        reasons.append({
            "factor": f.get("code", "flag"),
            "impact": {"critical": -40, "caution": -12,
                       "positive": 8}.get(sev, 0),
            "detail": f.get("evidence", ""),
        })

    score = max(0.0, min(100.0, score))
    grade = grade_for_score(score)

    # เพดานจาก flag — ห้ามให้คะแนนดีด้านอื่นมากลบความเสี่ยง
    # (เดิมเขียนเงื่อนไขกลับด้าน ทำให้ทรัพย์ที่มีผู้อยู่อาศัยยังได้เกรด A)
    if cap and _rank(grade) > _rank(cap):
        reasons.append({
            "factor": "เพดานจากความเสี่ยง", "impact": 0,
            "detail": f"กดเกรดจาก {grade} เหลือ {cap} เพราะมี flag ระดับ"
                      f"{'ร้ายแรง' if cap == 'E' else 'ควรระวัง'}",
        })
        grade = cap

    return GradeResult(grade=grade, score=round(score, 1),
                       completeness=comp, reasons=reasons)


def _rank(grade: str) -> int:
    return {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1}.get(grade, 0)


GRADE_STYLE = {
    "A": ("bg-emerald-600", "น่าสนใจมาก"),
    "B": ("bg-lime-600", "น่าสนใจ"),
    "C": ("bg-amber-500", "พอได้"),
    "D": ("bg-orange-600", "ต้องระวัง"),
    "E": ("bg-red-600", "ไม่แนะนำ"),
    None: ("bg-slate-400", "ข้อมูลไม่พอ"),
}
