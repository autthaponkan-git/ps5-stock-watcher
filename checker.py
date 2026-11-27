"""PS5 stock watcher: checks Thai store pages and alerts (LINE / Gmail) when an
item is in stock at or below its list price. Standard library only.

  python checker.py            # one stock check (what the schedule runs)
  python checker.py --dry-run  # check and print, no messages, no state saved
  python checker.py --test     # send a test message
  python checker.py --health   # send a health report (uses GitHub API + LINE quota)
"""

import json
import os
import re
import smtplib
import sys
import time
import urllib.error
import urllib.parse
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
FAIL_ALERT_AFTER = 12  # consecutive failed checks (~1 hour) before a "checker broken" alert


# ---------------------------------------------------------------- fetching

def fetch(url, accept="text/html,application/json;q=0.9,*/*;q=0.8"):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "th,en;q=0.8", "Accept": accept})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def to_price(v):
    if v is None:
        return None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(v))
    return float(m.group(0).replace(",", "")) if m else None


# ---------------------------------------------------------------- store parsers
# Each returns a list of offers: dicts {available: bool, price: float|None, url: str}

def iter_offers(node):
    if isinstance(node, list):
        for n in node:
            yield from iter_offers(n)
    elif isinstance(node, dict):
        if "availability" in node:
            yield node
        for v in node.values():
            if isinstance(v, (dict, list)):
                yield from iter_offers(v)


def check_jsonld(p):
    """Generic: schema.org JSON-LD on the product page (JIB, HomePro...).
    `out_markers` = regexes that, if present, mean sold out even when JSON-LD says InStock."""
    html = fetch(p["url"])
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
            pr = to_price(offer.get("price") or offer.get("lowPrice"))
            if avail in IN_STOCK:
                available = True
            if pr is not None and (price is None or pr < price):
                price = pr
    if not found:
        raise ValueError("ไม่พบข้อมูลสต็อก (JSON-LD) ในหน้าเว็บ")
    if any(re.search(m, html) for m in p.get("out_markers", [])):
        available = False
    return [{"available": available, "price": price, "url": p["url"]}]


def check_advice(p):
    """Advice: the 'online price' box shows either a price or 'สินค้าหมด'."""
    html = fetch(p["url"])
    i = html.find("ราคาเฉพาะออนไลน์")
    if i < 0:
        raise ValueError("ไม่พบกล่องราคาออนไลน์ในหน้าเว็บ")
    m = re.search(r"<span[^>]*>(.*?)</span>", html[i:i + 3000], re.S)
    if not m:
        raise ValueError("อ่านราคาออนไลน์ไม่ได้")
    txt = re.sub(r"<[^>]+>|\s+", " ", m.group(1)).strip()
    if "หมด" in txt:
        return [{"available": False, "price": None, "url": p["url"]}]
    price = to_price(txt)
    if price is None:
        raise ValueError(f"ข้อความราคาไม่รู้จัก: {txt[:40]}")
    return [{"available": True, "price": price, "url": p["url"]}]


def check_shopify(p):
    """Shopify stores (Sony Store TH): <product-url>.js gives availability + price (satang)."""
    data = json.loads(fetch(p["url"].rstrip("/") + ".js", accept="application/json"))
    price = data.get("price")
    return [{"available": bool(data.get("available")),
             "price": price / 100 if isinstance(price, (int, float)) else to_price(price),
             "url": p["url"]}]


def check_powerbuy_search(p):
    """Power Buy hides sold-out consoles, so watch a search page for console models
    (matched by `model_regex`) that appear with stock."""
    html = fetch(p["url"])
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("ไม่พบข้อมูลสินค้า (__NEXT_DATA__)")
    items = ((json.loads(m.group(1)).get("props", {}).get("pageProps", {})
              .get("productListData") or {}).get("products"))
    if items is None:
        raise ValueError("ไม่พบรายการสินค้าในผลค้นหา")
    offers = []
    for it in items:
        key = f"{it.get('model', '')} {it.get('slugname', '')} {it.get('name', '')}"
        if not re.search(p["model_regex"], key, re.I):
            continue
        stock = it.get("stockAvail") or 0
        offers.append({
            "available": stock > 0 and it.get("skuStatus", "A") == "A",
            "price": to_price(it.get("price")),
            "url": f"https://www.powerbuy.co.th/th/product/{it.get('slugname')}",
        })
    return offers  # empty list = not listed = not available


def check_shopify_collection(p):
    """Shopify collection (Sony Store TH): <collection>/products.json lists every product
    with per-variant availability, so no need to guess product handles. Matches `model_regex`
    against title/handle/SKU."""
    base = p["url"].split("?")[0].rstrip("/")
    data = json.loads(fetch(base + "/products.json?limit=250", accept="application/json"))
    products = data.get("products")
    if products is None:
        raise ValueError("ไม่พบรายการสินค้าในคอลเลกชัน")
    shop = base.split("/collections/")[0]
    offers = []
    for prod in products:
        for v in prod.get("variants", []):
            key = f"{prod.get('title', '')} {prod.get('handle', '')} {v.get('sku', '')} {v.get('title', '')}"
            if re.search(p["model_regex"], key, re.I) and not re.search(p.get("exclude_regex", r"(?!x)x"), key, re.I):
                offers.append({"available": bool(v.get("available")), "price": to_price(v.get("price")),
                               "url": f"{shop}/products/{prod.get('handle')}"})
    return offers


def check_magento_search(p):
    """Magento GraphQL stores (dotlife.store): search `query`, keep items whose name/SKU match
    `model_regex` and cost at least `min_price` (filters out accessories)."""
    q = ('{products(search:%s,pageSize:100){items{sku name url_key url_suffix stock_status '
         'price_range{minimum_price{final_price{value}}}}}}' % json.dumps(p["query"]))
    data = json.loads(fetch(p["url"] + "?" + urllib.parse.urlencode({"query": q}), accept="application/json"))
    items = ((data.get("data") or {}).get("products") or {}).get("items")
    if items is None:
        raise ValueError(f"GraphQL ไม่ตอบรายการสินค้า: {str(data.get('errors'))[:60]}")
    site = p["url"].rsplit("/graphql", 1)[0]
    offers = []
    for it in items:
        price = to_price(((it.get("price_range") or {}).get("minimum_price") or {}).get("final_price", {}).get("value"))
        key = f"{it.get('name', '')} {it.get('sku', '')}"
        if not re.search(p["model_regex"], key, re.I):
            continue
        if p.get("exclude_regex") and re.search(p["exclude_regex"], key, re.I):
            continue
        if price is not None and price < p.get("min_price", 0):
            continue
        offers.append({"available": it.get("stock_status") == "IN_STOCK", "price": price,
                       "url": f"{site}/{it.get('url_key')}{it.get('url_suffix') or '.html'}"})
    return offers  # empty = console not listed


CHECKERS = {"jsonld": check_jsonld, "magento_search": check_magento_search, "advice": check_advice, "shopify": check_shopify,
            "shopify_collection": check_shopify_collection, "powerbuy_search": check_powerbuy_search}


def active_products():
    products = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))
    return [p for p in products if not p.get("disabled")]


# ---------------------------------------------------------------- notifications

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


def line_api(path, payload=None):
    token = os.environ["LINE_CHANNEL_ACCESS_TOKEN"].strip()
    req = urllib.request.Request(
        "https://api.line.me" + path, method="POST" if payload else "GET",
        data=json.dumps(payload).encode() if payload else None,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
    return json.loads(body) if body.strip() else {}


def send_line(text):
    line_api("/v2/bot/message/push", {"to": os.environ["LINE_USER_ID"].strip(),
                                      "messages": [{"type": "text", "text": text[:5000]}]})
    print("LINE sent")


def notify(subject, body):
    """Send via every configured channel; one failing does not block the other."""
    sent, errors, channels = 0, [], []
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


# ---------------------------------------------------------------- state

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def short_error(e):
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    return f"{type(e).__name__}: {e}"[:80]


def fmt_price(v):
    return f"{v:,.0f} บาท" if v is not None else "ไม่ทราบราคา"


# ---------------------------------------------------------------- stock check

def run_check(dry):
    products = active_products()
    state = load_state()
    items = state.setdefault("items", {})
    now = datetime.now(BKK)
    hits = []

    for i, p in enumerate(products):
        if i:
            time.sleep(2)  # be polite to the stores
        st = items.setdefault(p["name"], {"available": False, "fails": 0})
        try:
            offers = CHECKERS[p.get("type", "jsonld")](p)
        except Exception as e:  # network error, blocked, page changed...
            fails = st.get("fails", 0) + 1
            st["fails"] = min(fails, FAIL_ALERT_AFTER)  # cap so state stops changing
            st["status"] = "error"
            st["error"] = short_error(e)
            print(f"[FAIL x{fails}] {p['name']}: {e}")
            if fails == FAIL_ALERT_AFTER and not dry:
                try_notify(f"⚠️ เช็คสต็อกไม่ได้: {p['name']}",
                           f"ระบบเช็คหน้านี้ไม่สำเร็จติดกัน {FAIL_ALERT_AFTER} ครั้งแล้ว (~1 ชม.)\n"
                           f"สาเหตุล่าสุด: {st['error']}\n\n{p['url']}\n\n"
                           "หน้าเว็บอาจเปลี่ยน ถูกบล็อก หรือสินค้าถูกลบไปแล้ว")
            continue

        good = [o for o in offers if o["available"] and (o["price"] is None or o["price"] <= p["max_price"])]
        prices = [o["price"] for o in offers if o["price"] is not None]
        st.update(fails=0, status="ok", error=None, available=bool(good),
                  price=min(prices) if prices else None)
        print(f"[{'IN STOCK' if good else 'no'}] {p['name']}: "
              f"{len(offers)} offer(s), price={fmt_price(st['price'])}, "
              f"raw={[(o['available'], o['price']) for o in offers]}")
        if good and not st.get("alerted"):
            hits.append((p, good))
        st["alerted"] = bool(good)

    if dry:
        return

    # Auto-test mode: proves scheduled runs fire by themselves (enabled via state.json).
    remaining = state.get("autotest_remaining", 0)
    source = os.environ.get("RUN_SOURCE")
    if remaining and source in ("schedule", "cron"):
        total = state.get("autotest_total", remaining)
        n = total - remaining + 1
        state["autotest_remaining"] = remaining - 1
        save_state(state)
        done = "\n\n✅ ครบทุกรอบแล้ว ปิดโหมดทดสอบอัตโนมัติ" if remaining == 1 else ""
        who = "cron-job.org" if source == "cron" else "ตัวตั้งเวลาของ GitHub"
        try_notify(f"🔁 ทดสอบรันอัตโนมัติ รอบ {n}/{total}",
                   f"ระบบเช็คสต็อกเองเมื่อ {now:%H:%M} น. โดยไม่มีใครกด (สั่งโดย {who}){done}")

    if hits:
        names = ", ".join(p["name"] for p, _ in hits)
        body = "มีของเข้าแล้ว! รีบกดลิงก์ด้านล่างเพื่อสั่งซื้อ\n\n"
        for p, good in hits:
            for o in good:
                body += f"🎮 {p['name']}\n   ราคา: {fmt_price(o['price'])}\n   👉 {o['url']}\n\n"
        body += f"ตรวจพบเมื่อ {now:%d/%m/%Y %H:%M} น."
        save_state(state)  # save first so a notify error can't cause repeat alerts
        notify(f"🔥 PS5 มีของแล้ว: {names}", body)

    # Daily heartbeat commit keeps the repo "active" (GitHub pauses idle schedules after 60 days).
    state["heartbeat"] = now.strftime("%Y-%m-%d")
    save_state(state)


# ---------------------------------------------------------------- health report

def github_runs():
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        raise RuntimeError("no GITHUB_REPOSITORY/GITHUB_TOKEN")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/check.yml/runs?per_page=100",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["workflow_runs"]


def run_health():
    now = datetime.now(BKK)
    lines, problems = [], 0

    try:
        runs = [r for r in github_runs()
                if r["event"] == "schedule" or str(r.get("display_title", "")).startswith("auto")]
        since = now - timedelta(hours=24)
        recent = [r for r in runs if datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")) >= since]
        failed = [r for r in recent if r.get("conclusion") == "failure"]
        if runs:
            last = datetime.fromisoformat(runs[0]["created_at"].replace("Z", "+00:00")).astimezone(BKK)
            ago = int((now - last).total_seconds() // 60)
            ok = ago <= 30
            problems += not ok
            lines.append(f"{'✅' if ok else '❌'} รันอัตโนมัติล่าสุด: {last:%H:%M} น. ({ago} นาทีที่แล้ว)")
        else:
            problems += 1
            lines.append("❌ ยังไม่มีการรันอัตโนมัติเลย")
        lines.append(f"{'✅' if not failed else '⚠️'} 24 ชม.ล่าสุด: รัน {len(recent)} รอบ, ล้มเหลว {len(failed)} รอบ")
        problems += bool(failed)
    except Exception as e:
        problems += 1
        lines.append(f"⚠️ อ่านประวัติการรันไม่ได้: {short_error(e)}")

    lines.append("")
    lines.append("ร้านค้า:")
    items = load_state().get("items", {})
    for p in active_products():
        st = items.get(p["name"])
        if not st:
            lines.append(f"⏳ {p['name']}: ยังไม่ได้เช็ค")
        elif st.get("status") == "error":
            problems += 1
            lines.append(f"❌ {p['name']}: เช็คไม่ได้ ({st.get('error')})")
        else:
            stock = "🔥 มีของ" if st.get("available") else ("หมด" if st.get("price") else "หมด/ยังไม่วางขาย")
            price = f" · {st['price']:,.0f}" if st.get("price") else ""
            lines.append(f"✅ {p['name']}: {stock}{price}")

    if os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"):
        try:
            used = line_api("/v2/bot/message/quota/consumption").get("totalUsage")
            quota = line_api("/v2/bot/message/quota")
            limit = quota.get("value") if quota.get("type") == "limited" else "ไม่จำกัด"
            lines.append("")
            lines.append(f"📨 โควตา LINE เดือนนี้: ใช้ไป {used}/{limit}")
        except Exception as e:
            lines.append(f"\n⚠️ อ่านโควตา LINE ไม่ได้: {short_error(e)}")

    head = "✅ ระบบทำงานปกติ" if not problems else f"⚠️ พบปัญหา {problems} จุด"
    notify(f"🩺 รายงานสถานะ {now:%H:%M} น. — {head}", "\n".join(lines))


def main():
    if "--test" in sys.argv or "--test-email" in sys.argv:
        notify("✅ ทดสอบระบบแจ้งเตือน PS5", "ถ้าได้รับข้อความนี้ แปลว่าระบบส่งแจ้งเตือนช่องทางนี้ทำงานปกติครับ")
    elif "--health" in sys.argv:
        run_health()
    else:
        run_check("--dry-run" in sys.argv)


if __name__ == "__main__":
    main()
