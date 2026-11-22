"""Category scanning for Shopify stores, using their public product catalog."""
import json, urllib.parse, time
from decimal import Decimal, InvalidOperation

def scan(url, fetch, max_pages=10):
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or u.hostname not in {"pandorastore.lv", "www.pandorastore.lv"}:
        raise ValueError("Currently supported category store: pandorastore.lv")
    path = u.path.rstrip("/")
    if path not in ("", "/collections/all") and not (path.startswith("/collections/") and len(path.split("/")) == 3):
        raise ValueError("Use a collection URL or the store homepage")
    endpoint = (path if path != "/collections/all" else "") + "/products.json"
    found = {}
    for page in range(1, max_pages + 1):
        data = json.loads(fetch(urllib.parse.urlunsplit((u.scheme,u.netloc,endpoint,f"limit=250&page={page}",""))))
        products = data["products"]
        for p in products:
            for v in p.get("variants", []):
                try:
                    price = Decimal(str(v["price"]))
                    old = Decimal(str(v.get("compare_at_price")))
                    sale = old > price > 0
                except (InvalidOperation, TypeError, KeyError):
                    try:
                        price = Decimal(str(v["price"]))
                    except (InvalidOperation, TypeError, KeyError):
                        continue
                    old, sale = None, False
                if not price.is_finite() or price <= 0:
                    continue
                key = str(v["id"])
                found[key] = dict(name=p["title"] + (" / " + v["title"] if v.get("title") not in (None,"Default Title") else ""), url=f"{u.scheme}://{u.netloc}/products/{p['handle']}?variant={key}", price=float(price), original=float(old) if sale else None, sale=sale, in_stock=v.get("available"))
        if len(products) < 250:
            return found, True
        time.sleep(1)
    return found, False

def changes(previous, current, minimum=5):
    events = []
    for key, p in current.items():
        old = previous.get(key)
        if old is None:
            if p["sale"] and (p["original"]-p["price"])/p["original"]*100 >= minimum:
                events.append(("Sale discovered", p))
            continue
        if p["sale"] and old.get("sale") is not True and (p["original"]-p["price"])/p["original"]*100 >= minimum:
            events.append(("Sale started" if old.get("sale") is False else "Sale discovered", p))
        elif old["price"] > p["price"] and (old["price"]-p["price"])/old["price"]*100 >= minimum:
            events.append(("Price dropped", p))
        elif p["in_stock"] is True and old.get("in_stock") is False:
            events.append(("Back in stock", p))
    return events

def check_categories(cfg, state, fetch, notify, log, now, test=False):
    catalogs = state.setdefault("__meta__", {}).setdefault("catalogs", {})
    for c in cfg.get("categories", []):
        url = c["url"]
        record = catalogs.setdefault(url, {})
        try:
            products, complete = scan(url, fetch, min(20,max(1,int(c.get("max_pages",10)))))
            if not products:
                raise ValueError("No products returned; previous snapshot preserved")
            events = changes(record.get("products", {}), products, c.get("min_drop_percent",5))
            log(f"Category: {len(products)} variants; {len(events)} events; complete={complete}")
            if test:
                continue
            for offset in range(0,len(events),8):
                lines = [f"{kind}: {p['name'][:100]} — EUR {p['price']:.2f}\n{p['url']}" for kind,p in events[offset:offset+8]]
                notify(cfg,"Category deals", "\n\n".join(lines),url)
            record.update(products=products if complete else {**record.get("products",{}),**products}, checked=now(), complete=complete, error=None)
        except Exception as e:
            log(f"Category scan failed: {e}")
            if not test:
                record["error"] = str(e)[:200]
