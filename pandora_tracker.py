#!/usr/bin/env python3
"""
Pandora LV price tracker (stdlib only, Python 3.8+).

  python pandora_tracker.py --test      fetch once, show what was parsed, no alerts/state
  python pandora_tracker.py --once      one check, then exit (for cron / Task Scheduler)
  python pandora_tracker.py             loop forever, checking every `interval_minutes`
  python pandora_tracker.py --history   print recorded price history
"""
import argparse, gzip, html, json, os, random, re, smtplib, sys, threading, time, urllib.parse, urllib.request
from datetime import datetime
from email.message import EmailMessage

DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(DIR, "config.json")
STATE = os.path.join(DIR, "state.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "lv-LV,lv;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip",
}


META = "__meta__"  # bookkeeping entry in state.json (Telegram offset, paused flag)


def load_cfg():
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    a, env = cfg.setdefault("alerts", {}), os.environ
    if env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
        t = a.setdefault("telegram", {})
        t.update(enabled=True, bot_token=env["TELEGRAM_BOT_TOKEN"])
        if env.get("TELEGRAM_CHAT_ID"):
            t["chat_id"] = env["TELEGRAM_CHAT_ID"]
    if env.get("NTFY_TOPIC"):
        a.setdefault("ntfy", {}).update(enabled=True, topic=env["NTFY_TOPIC"])
    return cfg


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(f"[{now()}] {msg}", flush=True)


def fetch(url, timeout=25, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode(r.headers.get_content_charset() or "utf-8", "replace")
        except Exception as e:
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"fetch failed: {last}")


def to_float(s):
    """'1.234,50' / '1,234.50' / '89,00' / '89.00' -> float"""
    s = re.sub(r"[^\d.,]", "", str(s))
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        head, tail = s.rsplit(",", 1)
        s = head.replace(",", "") + ("." + tail if len(tail) <= 2 else tail)
    try:
        return float(s)
    except ValueError:
        return None


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def parse_page(page):
    """Return dict(name, price, currency, in_stock, source). Tries the most reliable source first."""
    out = {"name": None, "price": None, "currency": None, "in_stock": None, "source": None}

    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', page, re.I)
    if m:
        out["name"] = html.unescape(m.group(1)).strip()

    # 1) JSON-LD (schema.org Product/Offer)
    for blob in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(blob.strip())
        except Exception:
            continue
        for node in _walk(data):
            offers = node.get("offers")
            if not offers:
                continue
            for off in (offers if isinstance(offers, list) else [offers]):
                if not isinstance(off, dict):
                    continue
                p = to_float(off.get("price") or off.get("lowPrice"))
                if p:
                    out.update(price=p, currency=off.get("priceCurrency") or "EUR", source="json-ld")
                    out["name"] = node.get("name") or out["name"]
                    av = str(off.get("availability", ""))
                    if av:
                        out["in_stock"] = "InStock" in av
                    return out

    # 2) meta tags
    for pat in (r'product:price:amount["\'][^>]+content=["\']([\d.,]+)',
                r'itemprop=["\']price["\'][^>]+content=["\']([\d.,]+)',
                r'content=["\']([\d.,]+)["\'][^>]+itemprop=["\']price["\']',
                r'og:price:amount["\'][^>]+content=["\']([\d.,]+)'):
        m = re.search(pat, page, re.I)
        if m and to_float(m.group(1)):
            out.update(price=to_float(m.group(1)), currency="EUR", source="meta")
            return out

    # 3) data attributes / inline JSON used by Salesforce Commerce Cloud sites
    for pat in (r'data-price=["\']([\d.,]+)', r'"(?:sales|salesPrice|price)"\s*:\s*\{[^}]*?"value"\s*:\s*([\d.]+)',
                r'"price"\s*:\s*"?([\d.]+)"?'):
        m = re.search(pat, page, re.I)
        if m and to_float(m.group(1)):
            out.update(price=to_float(m.group(1)), currency="EUR", source="inline")
            return out

    # 4) visible text like "89,00 €" or "€ 89.00" (last resort)
    m = re.search(r'(\d{1,4}(?:[.,]\d{2}))\s*(?:€|&euro;|EUR)|(?:€|&euro;)\s*(\d{1,4}(?:[.,]\d{2}))', page)
    if m:
        out.update(price=to_float(m.group(1) or m.group(2)), currency="EUR", source="text")
    return out


def send_ntfy(cfg, title, body, url):
    topic = cfg["topic"]
    req = urllib.request.Request(
        f"{cfg.get('server', 'https://ntfy.sh').rstrip('/')}/{topic}",
        data=body.encode("utf-8"),
        headers={"Title": title.encode("utf-8").decode("latin-1", "ignore"), "Priority": "high",
                 "Tags": "tada,moneybag", "Click": url},
    )
    urllib.request.urlopen(req, timeout=20).read()


def send_telegram(cfg, title, body, url):
    data = urllib.parse.urlencode({"chat_id": cfg["chat_id"], "text": f"{title}\n\n{body}\n{url}",
                                   "reply_markup": json.dumps(KEYBOARD)}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage", data, timeout=20).read()


def send_email(cfg, title, body, url):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = title, cfg["from"], cfg["to"]
    msg.set_content(f"{body}\n\n{url}")
    with smtplib.SMTP(cfg["host"], cfg.get("port", 587), timeout=30) as s:
        s.starttls()
        s.login(cfg["user"], cfg["password"])
        s.send_message(msg)


SENDERS = {"ntfy": send_ntfy, "telegram": send_telegram, "email": send_email}


def notify(cfg, title, body, url):
    log(f"ALERT: {title} | {body}")
    for name, sender in SENDERS.items():
        c = cfg.get("alerts", {}).get(name)
        if c and c.get("enabled"):
            try:
                sender(c, title, body, url)
            except Exception as e:
                log(f"  {name} alert failed: {e}")


def load_state():
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, ensure_ascii=False)
    os.replace(tmp, STATE)


def evaluate(item, info, st):
    """Return a list of alert reasons for this check and update state."""
    price = info["price"]
    reasons = []
    prev = st.get("last_price")
    low = st.get("lowest")
    target = item.get("target_price")
    drop_pct = item.get("min_drop_percent", 5)

    if target is not None and price <= target and st.get("last_alerted_price") != price:
        reasons.append(f"at or below your target of {target:.2f} EUR")
    if prev and price < prev and (prev - price) / prev * 100 >= drop_pct:
        reasons.append(f"dropped {(prev - price) / prev * 100:.0f}% (was {prev:.2f} EUR)")
    if low is not None and price < low:
        reasons.append(f"new lowest price since tracking began (was {low:.2f} EUR)")
    if prev and price > prev:
        pass  # price increases are recorded but never alerted
    if info["in_stock"] is True and st.get("in_stock") is False:
        reasons.append("back in stock")

    st.setdefault("history", []).append({"t": now(), "price": price, "in_stock": info["in_stock"]})
    st["history"] = st["history"][-5000:]
    st["last_price"] = price
    st["lowest"] = price if low is None else min(low, price)
    if info["in_stock"] is not None:
        st["in_stock"] = info["in_stock"]
    if reasons:
        st["last_alerted_price"] = price
    return reasons


def _check_all(cfg, test=False):
    state = load_state()
    from catalog import check_categories
    check_categories(cfg, state, fetch, notify, log, now, test)
    if not test:
        save_state(state)
    sanity = cfg.get("max_change_percent_sanity", 60)
    for item in cfg["items"]:
        url = item["url"]
        label = item.get("name") or url
        st = state.setdefault(url, {})
        try:
            info = parse_page(fetch(url))
            if not info["price"]:
                raise RuntimeError("no price found on page (layout changed or bot-blocked?)")
        except Exception as e:
            st["fails"] = st.get("fails", 0) + 1
            st["last_error"], st["last_checked"] = str(e)[:150], now()
            log(f"{label}: ERROR {e}")
            if test:
                continue
            if st["fails"] == 3:
                notify(cfg, "Pandora tracker: can't read price", f"{label}\n{e}", url)
            save_state(state)
            continue

        st["fails"], st["last_error"], st["last_checked"] = 0, None, now()
        st["name"] = info["name"]
        log(f"{label}: {info['price']:.2f} {info['currency']} "
            f"(source={info['source']}, in_stock={info['in_stock']}, name={info['name']!r})")
        if test:
            continue

        # guard against mis-parsed prices (e.g. picked up a different number on the page)
        prev = st.get("last_price")
        if prev and abs(info["price"] - prev) / prev * 100 > sanity and not st.get("confirm"):
            log(f"  suspicious jump {prev} -> {info['price']}; will confirm on next check")
            st["confirm"] = info["price"]
            save_state(state)
            continue
        if st.get("confirm") and st["confirm"] != info["price"]:
            st["confirm"] = None
            save_state(state)
            continue
        st["confirm"] = None

        reasons = evaluate(item, info, st)
        if reasons:
            title = f"Pandora price alert: {info['price']:.2f} EUR"
            notify(cfg, title, f"{item.get('name') or info['name']}\n- " + "\n- ".join(reasons), url)
        save_state(state)


LOCK = threading.Lock()
PAUSED = threading.Event()

KEYBOARD = {"inline_keyboard": [
    [{"text": "🔍 Check price now", "callback_data": "check"}, {"text": "📊 Status", "callback_data": "status"}],
    [{"text": "📈 History", "callback_data": "history"}, {"text": "⏯ Pause / resume", "callback_data": "pause"}],
]}


def check_all(cfg, test=False):
    with LOCK:  # the background loop and the Telegram buttons never run a check at the same time
        _check_all(cfg, test)


def tg(cfg, method, **params):
    c = cfg["alerts"]["telegram"]
    data = urllib.parse.urlencode(
        {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in params.items()}).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{c['bot_token']}/{method}", data, timeout=45) as r:
        return json.loads(r.read())


def status_text(cfg):
    state, lines = load_state(), []
    for i, item in enumerate(cfg["items"], 1):
        s = state.get(item["url"], {})
        name = item.get("name") or s.get("name") or item["url"]
        if s.get("last_price") is None:
            lines.append(f"{i}. {name}\n   no price recorded yet")
            continue
        line = f"{i}. {name}\n   Now: {s['last_price']:.2f} EUR"
        if item.get("target_price") is not None:
            line += f"  (target {item['target_price']:.2f})"
        line += f"\n   Lowest seen: {s['lowest']:.2f} EUR"
        if s.get("in_stock") is not None:
            line += "\n   " + ("In stock" if s["in_stock"] else "OUT OF STOCK")
        line += f"\n   Checked: {s.get('last_checked')}"
        if s.get("last_error"):
            line += f"\n   ⚠ last error: {s['last_error']}"
        lines.append(line)
    for url, catalog in state.get(META, {}).get("catalogs", {}).items():
        products = catalog.get("products", {})
        sales = sum(p.get("sale") is True for p in products.values())
        lines.append(f"Category: {url}\n   {len(products)} variants, {sales} on sale\n   Checked: {catalog.get('checked', 'never')}\n   {catalog.get('error') or ('Complete' if catalog.get('complete') else 'Partial scan')}")
    if PAUSED.is_set():
        lines.append("⏸ Automatic checks are paused (the Check button still works).")
    return "\n\n".join(lines)


def history_text(cfg):
    state, out = load_state(), []
    for i, item in enumerate(cfg["items"], 1):
        changes, last = [], None
        for e in state.get(item["url"], {}).get("history", []):
            if e["price"] != last:
                changes.append(e)
                last = e["price"]
        rows = "\n".join(f"   {e['t'][5:16]}  {e['price']:.2f} EUR" for e in changes[-8:]) or "   no data yet"
        out.append(f"{i}. {item.get('name') or item['url']}\n   Price changes (latest last):\n{rows}")
    return "\n\n".join(out)


def handle_update(cfg, u, fresh=False):
    c = cfg["alerts"]["telegram"]
    if "callback_query" in u:
        cq = u["callback_query"]
        chat, cmd = cq["message"]["chat"]["id"], cq["data"]
        try:  # old button presses can no longer be acknowledged; still answer them below
            tg(cfg, "answerCallbackQuery", callback_query_id=cq["id"])
        except Exception:
            pass
    elif "text" in u.get("message", {}):
        chat, cmd = u["message"]["chat"]["id"], u["message"]["text"].strip()
    else:
        return

    def say(text, keyboard=True):
        extra = {"reply_markup": KEYBOARD} if keyboard else {}
        tg(cfg, "sendMessage", chat_id=chat, text=text[:4000], disable_web_page_preview=True, **extra)

    if not c.get("chat_id"):  # first-time setup helper
        say(f"Your chat id is {chat}\nPut it in config.json as \"chat_id\" and restart the tracker.", False)
        return
    if str(chat) != str(c["chat_id"]):  # ignore strangers
        return

    parts = cmd.lstrip("/").split()
    name = parts[0].split("@")[0].lower() if parts else ""
    if name in ("start", "menu", "help"):
        say("Pandora tracker ready. Tap a button, or send /target 55 to change your target price "
            "(/target 55 2 for item number 2).")
    elif name == "check":
        if fresh:
            say("Here is the check that just ran:", False)
        else:
            say("Checking now…", False)
            check_all(cfg)
        say(status_text(cfg))
    elif name == "status":
        say(status_text(cfg))
    elif name == "history":
        say(history_text(cfg))
    elif name == "pause":
        PAUSED.clear() if PAUSED.is_set() else PAUSED.set()
        say("⏸ Automatic checks paused." if PAUSED.is_set() else "▶ Automatic checks resumed.")
    elif name == "target":
        try:
            val = float(parts[1].replace(",", "."))
            idx = int(parts[2]) - 1 if len(parts) > 2 else 0
            item = cfg["items"][idx]
        except (IndexError, ValueError):
            say("Usage: /target 55  or  /target 55 2 (item number)")
            return
        item["target_price"] = val
        with open(CONFIG, encoding="utf-8") as f:  # edit the raw file so secrets from env never get saved
            raw = json.load(f)
        raw["items"][idx]["target_price"] = val
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2, ensure_ascii=False)
        state = load_state()
        state.get(item["url"], {}).pop("last_alerted_price", None)
        save_state(state)
        say(f"🎯 Target for item {idx + 1} set to {val:.2f} EUR")


def process_pending(cfg, fresh):
    """One-shot mode: answer button presses that arrived since the last run."""
    c = cfg.get("alerts", {}).get("telegram", {})
    if not (c.get("enabled") and c.get("bot_token")):
        return
    offset = load_state().get(META, {}).get("offset")
    params = {"timeout": 0, "allowed_updates": ["message", "callback_query"]}
    if offset:
        params["offset"] = offset
    try:
        updates = tg(cfg, "getUpdates", **params)["result"]
    except Exception as e:
        log(f"telegram poll error: {e}")
        return
    for u in updates:
        offset = u["update_id"] + 1
        try:
            handle_update(cfg, u, fresh=fresh)
        except Exception as e:
            log(f"telegram handler error: {e}")
    state = load_state()
    meta = state.setdefault(META, {})
    if offset:
        meta["offset"] = offset
    meta["paused"] = PAUSED.is_set()
    save_state(state)


def bot_loop(cfg):
    log("Telegram buttons active")
    offset = None
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
            if offset:
                params["offset"] = offset
            updates = tg(cfg, "getUpdates", **params)["result"]
        except Exception as e:
            log(f"telegram poll error: {e}")
            time.sleep(10)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            try:
                handle_update(cfg, u)
            except Exception as e:
                log(f"telegram handler error: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--history", action="store_true")
    a = ap.parse_args()

    cfg = load_cfg()

    if a.history:
        for url, st in load_state().items():
            if url == META:
                continue
            print(url)
            for h in st.get("history", []):
                print("  ", h["t"], h["price"])
        return
    if a.test:
        check_all(cfg, test=True)
        return
    if a.once:
        if load_state().get(META, {}).get("paused"):
            PAUSED.set()
        ran = not PAUSED.is_set()
        if ran:
            check_all(cfg)
        process_pending(cfg, fresh=ran)
        return

    interval = cfg.get("interval_minutes", 30)
    log(f"Tracking {len(cfg['items'])} item(s) every ~{interval} min. Ctrl+C to stop.")
    tgc = cfg.get("alerts", {}).get("telegram", {})
    if tgc.get("enabled") and tgc.get("bot_token"):
        threading.Thread(target=bot_loop, args=(cfg,), daemon=True).start()
    while True:
        try:
            if not PAUSED.is_set():
                check_all(cfg)
        except Exception as e:
            log(f"unexpected error: {e}")
        time.sleep(interval * 60 * random.uniform(0.9, 1.1))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
