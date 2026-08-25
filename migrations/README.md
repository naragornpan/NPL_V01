# ลำดับการรัน migration

## รันครั้งแรก

รันทีละไฟล์ใน Supabase SQL Editor **ตามลำดับนี้เท่านั้น**

| ลำดับ | ไฟล์ | ต้องมีก่อน |
|---|---|---|
| 1 | `../schema.sql` | — |
| 2 | `002_infra_and_price.sql` | schema |
| 3 | `003_appreciation.sql` | 002 |
| 4 | `004_model_lifecycle.sql` | 003 |
| 5 | `005_price_sources.sql` | 003 |
| 6 | `006_images.sql` | schema |
| 7 | `007_market_comps.sql` | **005** |
| 8 | `008_contractors.sql` | schema |
| 9 | `009_leads_consent.sql` | schema |
| 10 | `010_analytics.sql` | schema |
| 11 | `011_institutions_grading.sql` | schema |

## ถ้าเจอ error

**Supabase รันทั้งไฟล์เป็นทรานแซกชันเดียว** ถ้าบรรทัดท้าย ๆ พัง
ตารางที่สร้างไว้ตอนต้นจะหายไปด้วยทั้งหมด ไม่ใช่ค้างครึ่ง ๆ

จึงเป็นไปได้ที่รัน 005 แล้วเห็น error แต่คิดว่าไม่เป็นไรและข้ามไป
พอถึง 007 ก็หา `price_observations` ไม่เจอ เพราะมันไม่เคยถูกสร้างจริง

**วิธีตรวจ** รัน `000_check_status.sql` จะบอกว่าไฟล์ไหนรันสำเร็จแล้วบ้าง
รันกี่ครั้งก็ได้ ไม่แก้อะไร

## ปัญหาที่เจอบ่อยที่สุด: PostGIS

Supabase ติดตั้ง PostGIS ไว้ที่ schema `extensions` ไม่ใช่ `public`
ถ้าไม่เพิ่มเข้า `search_path` จะหา type `geometry` ไม่เจอ แล้ว 002 rollback ทั้งไฟล์

ไฟล์ 002 จัดการให้แล้ว แต่ถ้ายังพัง ให้รันบรรทัดนี้แยกก่อน

```sql
create extension if not exists postgis with schema extensions;
```

แล้วเช็คด้วยส่วนท้ายของ `000_check_status.sql`

## ถ้าเคยรัน schema เวอร์ชันเก่าไปแล้ว

`create table if not exists` **ไม่เพิ่มคอลัมน์ให้ตารางที่มีอยู่แล้ว** มันข้ามไปเฉย ๆ
ถ้าสร้าง `listing_snapshots` ไว้ก่อนที่จะมีคอลัมน์ `title` / `bedrooms` / `list_price`
(ซึ่งเพิ่มตอนทำ adapter BAM) พอรัน 011 จะพังเพราะ view หาคอลัมน์ไม่เจอ

`schema.sql` มีบล็อก `alter table ... add column if not exists` แก้ให้แล้ว
**รัน `schema.sql` ซ้ำอีกครั้งก่อน** แล้วค่อยรัน 011

ทดสอบแล้วกับฐานที่สร้างด้วย schema เวอร์ชันเก่าจริง — อัปเกรดครบ 8 คอลัมน์
และ migration ทั้งชุดผ่านหมด

## รันซ้ำได้ไหม

ได้ทุกไฟล์ ทุกคำสั่งใช้ `if not exists` / `create or replace` / `on conflict do nothing`
ถ้าไม่แน่ใจว่าไฟล์ไหนสำเร็จ ให้รันซ้ำตั้งแต่ต้นได้เลย ไม่เสียข้อมูล

## ทดสอบแล้ว

ชุด migration นี้รันผ่านครบทุกไฟล์บน PostgreSQL 16 + PostGIS ที่จำลอง
สภาพแวดล้อมแบบ Supabase (postgis อยู่ที่ schema `extensions`) และทดสอบแล้วว่า

- function ทั้ง 4 ตัวเรียกใช้ได้จริง ไม่ใช่แค่สร้างผ่าน
- view ทั้ง 18 ตัว query ได้
- trigger ทำงานถูกต้องทั้ง 4 ตัว: กันเปิด source ที่ยังไม่เคลียร์สิทธิ์ ·
  กันแก้ค่าพยากรณ์เดิม · กันส่งข้อมูลให้สถาบันโดยไม่มี consent ·
  กันใช้ราคาประกาศขายคำนวณ uplift

## ยังไม่ต้องรีบ

ตอนนี้ scraper ยังเปิดใช้ไม่ได้ (BAM รอตรวจ ToS · LED รออนุญาต)
ต่อให้ตั้ง database เสร็จก็ยังไม่มีข้อมูลไหลเข้า

เว็บรันได้โดยไม่ต้องมี database เลย — `python src\web.py`
