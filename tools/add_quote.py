#!/usr/bin/env python3
"""บันทึกใบเสนอราคาผู้รับเหมา — กรอกมือ ไม่ต้องรอระบบ marketplace

    python tools/add_quote.py

ทำไมต้องเริ่มเก็บตั้งแต่วันแรก
    ตาราง unit_costs คือ moat จริงของโปรเจกต์นี้ ใครก็เรียก AI ได้
    แต่ราคาต่อหน่วยที่สอบเทียบจากงานจริงลอกไม่ได้

    ทุกครั้งที่คุณขอราคาผู้รับเหมาให้ลูกค้า นั่นคือข้อมูลฟรีที่หายไป
    ถ้าไม่บันทึก และย้อนกลับไปเก็บไม่ได้

    ระบบ marketplace เต็มรูปแบบรอถึง M7 ได้ แต่ข้อมูลรอไม่ได้

ใช้เวลาราว 2 นาทีต่อใบ
"""
from __future__ import annotations

import json
import pathlib
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from core import env as _env  # noqa: E402,F401

from core.db import connect  # noqa: E402


def ask(prompt, default=None, required=True):
    suffix = f" [{default}]" if default else ""
    while True:
        v = input(f"{prompt}{suffix}: ").strip() or default
        if v or not required:
            return v
        print("  ต้องกรอกค่านี้")


def ask_num(prompt, required=True):
    while True:
        raw = input(f"{prompt}: ").strip().replace(",", "")
        if not raw and not required:
            return None
        try:
            return float(raw)
        except ValueError:
            print("  กรอกเป็นตัวเลข")


def main() -> int:
    print(__doc__)
    with connect() as conn:
        # --- ผู้รับเหมา ---
        rows = conn.execute(
            "select id, display_name from contractors order by display_name"
        ).fetchall()
        if rows:
            print("\nผู้รับเหมาในระบบ:")
            for i, c in enumerate(rows, 1):
                print(f"  {i}. {c['display_name']}")
        choice = ask("\nเลือกหมายเลข หรือพิมพ์ 'new' เพื่อเพิ่มใหม่", default="new")

        if choice.lower() == "new":
            name = ask("ชื่อผู้รับเหมา/ทีม")
            phone = ask("เบอร์ติดต่อ", required=False)
            province = ask("จังหวัดที่รับงานหลัก")
            district = ask("อำเภอ/เขต", required=False)
            cid = conn.execute(
                """insert into contractors (display_name, phone, status)
                   values (%s, %s, 'active') returning id""",
                (name, phone)).fetchone()["id"]
            conn.execute(
                """insert into contractor_areas (contractor_id, province, district, is_primary)
                   values (%s,%s,%s,true) on conflict do nothing""",
                (cid, province, district))
            conn.execute(
                """insert into contractor_subscriptions (contractor_id, plan_code)
                   values (%s,'free') """, (cid,))
            conn.commit()
            print(f"  เพิ่ม {name} แล้ว")
        else:
            cid = rows[int(choice) - 1]["id"]

        # --- งาน ---
        print("\n--- รายละเอียดงาน ---")
        province = ask("จังหวัดของทรัพย์")
        district = ask("อำเภอ/เขต", required=False)
        ptype = ask("ประเภททรัพย์ (house/townhouse/condo/land/commercial)",
                    default="townhouse")
        specs = ask("งานที่ขอราคา (คั่นด้วย comma เช่น painting,tiling,plumbing)")
        spec_list = [x.strip() for x in specs.split(",") if x.strip()]
        accessible = ask("เข้าไปดูภายในได้ไหม (y/n)", default="y").lower() == "y"
        budget = ask_num("งบที่ประเมินไว้เอง (เว้นว่างได้)", required=False)

        job_id = conn.execute(
            """insert into job_requests
                 (province, district, property_type, specialties,
                  budget_estimate, site_accessible, status)
               values (%s,%s,%s,%s,%s,%s,'quoted') returning id""",
            (province, district, ptype, spec_list, budget, accessible)).fetchone()["id"]

        # --- ใบเสนอราคา ---
        print("\n--- ใบเสนอราคา ---")
        amount = ask_num("ราคาที่เสนอ (บาท)")
        days = ask_num("จำนวนวันที่ใช้", required=False)
        warranty = ask_num("รับประกันกี่เดือน", required=False)
        print("\nรายการย่อย (Enter ว่างเพื่อจบ) — ยิ่งละเอียดยิ่งสอบเทียบได้แม่น")
        breakdown = {}
        while True:
            item = input("  รายการ: ").strip()
            if not item:
                break
            val = input(f"  ราคา {item}: ").strip().replace(",", "")
            try:
                breakdown[item] = float(val)
            except ValueError:
                print("  ข้ามรายการนี้")

        conn.execute(
            """insert into contractor_quotes
                 (job_request_id, contractor_id, amount, breakdown,
                  days_estimate, warranty_months)
               values (%s,%s,%s,%s,%s,%s)""",
            (job_id, cid, amount, json.dumps(breakdown, ensure_ascii=False),
             int(days) if days else None, int(warranty) if warranty else None))
        conn.commit()
        print(f"\nบันทึกแล้ว · งาน {job_id}")

        # --- เทียบกับใบอื่นในงานเดียวกัน ---
        others = conn.execute(
            """select c.display_name, q.amount, q.days_estimate
               from contractor_quotes q
               join contractors c on c.id = q.contractor_id
               where q.job_request_id = %s order by q.amount""",
            (job_id,)).fetchall()
        if len(others) > 1:
            print("\nใบเสนอราคาในงานนี้")
            for o in others:
                print(f"  {o['display_name']:24} {o['amount']:>12,.0f}"
                      f"  {o['days_estimate'] or '-'} วัน")
            spread = others[-1]["amount"] / others[0]["amount"] - 1
            print(f"  ส่วนต่างสูงสุด-ต่ำสุด {spread*100:.0f}%")
            if spread > 0.6:
                print("  ส่วนต่างเกิน 60% มักแปลว่าขอบเขตงานที่แต่ละเจ้าเข้าใจไม่ตรงกัน"
                      " ควรทำ BOQ ให้ชัดก่อนขอราคาใหม่")

        print("\nอย่าลืมกลับมาบันทึก job_outcomes เมื่องานจบ "
              "ราคาจริงคือสิ่งที่ทำให้ unit_costs แม่นขึ้น")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
