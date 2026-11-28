# PS5 Stock Watcher

เช็คสต็อก PS5 Slim Disc / PS5 Pro ทุก 5 นาทีบน GitHub Actions (ทำงานแม้ปิดคอม)
เมื่อมีของเข้า **และราคาไม่เกินราคาป้าย** จะแจ้งเตือนทาง LINE (และ Gmail ถ้าตั้งไว้) พร้อมลิงก์ไปหน้าสินค้า

- แจ้งเตือนครั้งเดียวต่อการกลับมามีของแต่ละรอบ (ไม่ส่งซ้ำทุก 5 นาที)
- ถ้าเช็คร้านไหนไม่ได้ติดกัน 12 ครั้ง (~1 ชม.) จะแจ้งเตือนว่าร้านนั้นมีปัญหา
- รายงานสถานะ (health report) เวลา 07:00, 12:00 และ 18:00 น.

## ร้านที่เฝ้า (`products.json`)
| type | ร้าน | วิธีเช็ค |
|---|---|---|
| `jsonld` | JIB, HomePro | ข้อมูล schema.org ในหน้าสินค้า + `out_markers` |
| `advice` | Advice | กล่อง "ราคาเฉพาะออนไลน์" (ราคา หรือ "สินค้าหมด") |
| `powerbuy_search` | Power Buy | หน้าค้นหา แล้วกรองรุ่นด้วย `model_regex` (Power Buy ซ่อนสินค้าที่หมด) |
| `shopify_collection` | Sony Store TH | `<collection>/products.json` + `model_regex` |

`max_price` = ราคาสูงสุดที่ยอมรับ

## Secrets (Settings → Secrets and variables → Actions)
| ชื่อ | ค่า |
|---|---|
| `LINE_CHANNEL_ACCESS_TOKEN` | Channel access token (long-lived) จาก LINE Developers → แท็บ Messaging API |
| `LINE_USER_ID` | Your user ID (ขึ้นต้นด้วย U) จาก LINE Developers → แท็บ Basic settings |
| `GMAIL_USER` / `GMAIL_APP_PASSWORD` / `MAIL_TO` | (ไม่บังคับ) สำหรับส่งอีเมล |

ช่องทางไหนไม่ได้ใส่ secret ระบบจะข้ามไป

## ทดสอบ
- ส่งข้อความทดสอบ: Actions → PS5 stock check → Run workflow → ติ๊ก "ส่งข้อความทดสอบ"
- รายงานสถานะทันที: Actions → Health report → Run workflow
- ทดสอบว่ารันอัตโนมัติจริง: ตั้ง `"autotest_remaining": N, "autotest_total": N` ใน `state.json`
  ระบบจะส่งข้อความ "🔁 ทดสอบรันอัตโนมัติ" ทุกรอบที่ GitHub รันเอง จนครบ N รอบแล้วหยุดเอง

## โควตา LINE
แพ็กเกจฟรีส่งได้ประมาณ 300 ข้อความ/เดือน รายงานสถานะ 3 ครั้ง/วัน ใช้ ~90 ข้อความ/เดือน
(รายงานแสดงยอดที่ใช้ไปทุกครั้ง)
