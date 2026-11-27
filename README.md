# PS5 Stock Watcher

เช็คสต็อก PS5 Slim Disc / PS5 Pro ทุก 5 นาทีบน GitHub Actions (ทำงานแม้ปิดคอม)
เมื่อมีของเข้า **และราคาไม่เกินราคาป้าย** จะแจ้งเตือนทาง Gmail และ LINE พร้อมลิงก์ไปหน้าสินค้า

- แจ้งเตือนครั้งเดียวต่อการกลับมามีของแต่ละรอบ (ไม่ส่งซ้ำทุก 5 นาที)
- ถ้าเช็คหน้าไหนไม่ได้ติดกัน 12 ครั้ง (~1 ชม.) จะส่งอีเมลเตือนว่าระบบมีปัญหา

## แก้รายการสินค้า
แก้ไฟล์ `products.json` เพิ่ม/ลบสินค้าได้ (`max_price` = ราคาสูงสุดที่ยอมรับ)
รองรับเว็บที่มีข้อมูลสินค้าแบบ schema.org (JSON-LD) เช่น JIB

## ตั้งค่า Secrets (Settings → Secrets and variables → Actions)
| ชื่อ | ค่า |
|---|---|
| `GMAIL_USER` | อีเมล Gmail ที่ใช้ส่ง |
| `GMAIL_APP_PASSWORD` | App Password 16 ตัวจาก https://myaccount.google.com/apppasswords |
| `MAIL_TO` | (ไม่บังคับ) อีเมลผู้รับ ถ้าไม่ใส่จะส่งหาตัวเอง |
| `LINE_CHANNEL_ACCESS_TOKEN` | Channel access token (long-lived) จาก LINE Developers → แท็บ Messaging API |
| `LINE_USER_ID` | Your user ID (ขึ้นต้นด้วย U) จาก LINE Developers → แท็บ Basic settings |

ช่องทางไหนไม่ได้ใส่ secret ระบบจะข้ามช่องทางนั้นไป

## ทดสอบ
Actions → PS5 stock check → Run workflow → ติ๊ก "ส่งข้อความทดสอบ"
