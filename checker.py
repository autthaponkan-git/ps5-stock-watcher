"""PS5 stock watcher: checks product pages and emails via Gmail when an item
is in stock at or below its list price. Standard library only."""

import json
import os
import re
import smtplib
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).parent
PRODUCTS_FILE = ROOT / "products.json"
STATE_FILE = ROOT / "state.json"
BKK = timezone(timedelta(hours=7))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
IN_STOCK = {"instock", "preorder", "limitedavailability", "onlineonly", "presale"}
FAIL_ALERT_AFTER = 12  # consecutive failed checks before a "checker broken" email


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "th,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def iter_offers(node):
    """Yield every schema.org Offer-like dict inside a JSON-LD structure."""
    if isinstance(node, list):
        for n in node:
            yield from iter_offers(n)
    elif isinstance(node, dict):
        if "availability" in node or node.get("@type") in ("Offer", "AggregateOffer"):
            yield node
        for v in node.values():
            if isinstance(v, (dict, list)):
                yield from iter_offers(v)


def parse(html):
    """Return (available: bool, price: float|None) from JSON-LD on the page."""
    available, price, found = False, None, False
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        for offer in iter_offers(data):
            avail = str(offer.get("availability", "")).rsplit("/", 1)[-1].lower()
            if not avail:
                continue
            found = True
            p = offer.get("price") or offer.get("lowPrice")
            try:
                p = float(str(p).replace(",", "")) if p is not None else None
            except ValueError:
                p = None
            if avail in IN_STOCK:
                available = True
                if p is not None:
                    price = p if price is None else min(price, p)
            elif price is None and p is not None:
                price = p
    if not found:
        raise ValueError("no stock info (JSON-LD availability) found on page")
    # JIB-specific double check: its buy button says "out of stock" when sold out.
    if available and re.search(r'class="[^"]*\bout_stock\b', html):
        available = False
    return available, price


def send_email(subject, body):
    user = os.environ["GMAIL_USER"]
    pw = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    to = os.environ.get("MAIL_TO") or user
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
    print(f"email sent: {subject}")


def send_line(text):
    """Push a message to yourself via a LINE Official Account (Messaging API)."""
    token = os.environ["LINE_CHANNEL_ACCESS_TOKEN"].strip()
    to = os.environ["LINE_USER_ID"].strip()
    payload = json.dumps({"to": to, "messages": [{"type": "text", "text": text[:5000]}]}).encode()
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push", data=payload, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()
    print("LINE sent")


def notify(subject, body):
    """Send via every configured channel; one failing does not block the other."""
    sent, errors = 0, []
    channels = []
    if os.environ.get("GMAIL_USER") and os.environ.get("GMAIL_APP_PASSWORD"):
        channels.append(("Gmail", lambda: send_email(subject, body)))
    if os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") and os.environ.get("LINE_USER_ID"):
        channels.append(("LINE", lambda: send_line(f"{subject}\n\n{body}")))
    for name, fn in channels:
        try:
            fn()
            sent += 1
        except Exception as e:
            errors.append(f"{name}: {e}")
            print(f"[NOTIFY FAIL] {name}: {e}")
    if not sent:
        raise RuntimeError("no notification sent: " + ("; ".join(errors) or "no channel configured"))
    if errors:  # make the workflow run show red so a broken channel gets noticed
        raise RuntimeError("some channels failed: " + "; ".join(errors))


def try_notify(subject, body):
    try:
        notify(subject, body)
    except Exception as e:
        print(f"[NOTIFY FAIL] {e}")


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def main():
    if "--test-email" in sys.argv or "--test" in sys.argv:
        notify("✅ ทดสอบระบบแจ้งเตือน PS5", "ถ้าได้รับข้อความนี้ แปลว่าระบบส่งแจ้งเตือนช่องทางนี้ทำงานปกติครับ")
        return

    dry = "--dry-run" in sys.argv
    products = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))
    state = load_state()
    now = datetime.now(BKK)
    hits = []

    for i, p in enumerate(products):
        if i:
            time.sleep(2)  # be polite to the store
        url = p["url"]
        st = state.setdefault(url, {"available": False, "fails": 0})
        try:
            available, price = parse(fetch(url))
        except Exception as e:  # network error, blocked, page changed...
            st["fails"] = st.get("fails", 0) + 1
            print(f"[FAIL x{st['fails']}] {p['name']}: {e}")
            if st["fails"] == FAIL_ALERT_AFTER and not dry:
                try_notify(f"⚠️ เช็คสต็อกไม่ได้: {p['name']}",
                           f"ระบบเช็คหน้านี้ไม่สำเร็จติดกัน {FAIL_ALERT_AFTER} ครั้งแล้ว\n"
                           f"สาเหตุล่าสุด: {e}\n\n{url}\n\nหน้าเว็บอาจเปลี่ยน หรือสินค้าถูกลบไปแล้ว")
            continue

        st["fails"] = 0
        ok = available and (price is None or price <= p["max_price"])
        price_txt = f"{price:,.0f} บาท" if price is not None else "ไม่ทราบราคา"
        print(f"[{'IN STOCK' if ok else 'no'}] {p['name']}: available={available} price={price_txt}")
        if ok and not st.get("available"):
            hits.append((p, price_txt))
        st["available"] = ok

    if hits and not dry:
        names = ", ".join(p["name"] for p, _ in hits)
        body = "มีของเข้าแล้ว! รีบกดลิงก์ด้านล่างเพื่อสั่งซื้อ\n\n"
        for p, price_txt in hits:
            body += f"🎮 {p['name']}\n   ราคา: {price_txt}\n   👉 {p['url']}\n\n"
        body += f"ตรวจพบเมื่อ {now:%d/%m/%Y %H:%M} น."
        # save state first so a notify error can't cause repeat alerts every run
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        notify(f"🔥 PS5 มีของแล้ว: {names}", body)

    # Heartbeat so the repo has activity (GitHub pauses idle scheduled workflows).
    state["_heartbeat"] = now.strftime("%Y-%m-%d")
    if not dry:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
