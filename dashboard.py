import html
from http.server import BaseHTTPRequestHandler, HTTPServer
from pandora_tracker import load_state

def render(state):
    rows = []
    for url, item in state.items():
        if url == "__meta__":
            for catalog in item.get("catalogs", {}).values():
                for p in catalog.get("products", {}).values():
                    rows.append((p["name"], p["price"], "Sale" if p["sale"] else ""))
        elif "last_price" in item:
            rows.append((item.get("name") or url, item["last_price"], f"Lowest: {item.get('lowest', 0):.2f}"))
    body = "".join(f"<tr><td>{html.escape(name)}</td><td>€{price:.2f}</td><td>{html.escape(status)}</td></tr>" for name, price, status in rows)
    return f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>PriceTracker</title><style>body{{font:16px system-ui;background:#101827;color:#eef2ff;margin:5vw}}table{{width:100%;border-collapse:collapse;background:#1e293b}}td,th{{padding:14px;text-align:left;border-bottom:1px solid #334155}}h1{{color:#5eead4}}input{{padding:12px;width:90%;margin-bottom:20px}}</style><h1>PriceTracker</h1><p>{len(rows)} tracked products and variants</p><input placeholder="Search products" oninput="document.querySelectorAll('tbody tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(this.value.toLowerCase()))"><table><thead><tr><th>Product</th><th>Price</th><th>Status</th></tr></thead><tbody>{body}</tbody></table>"""

class Dashboard(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/":
            self.send_error(404)
            return
        data = render(load_state()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8080), Dashboard).serve_forever()
