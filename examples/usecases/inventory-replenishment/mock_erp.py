"""A tiny stand-in ERP for trying the use case without a real one (`python mock_erp.py`).

It serves `GET /stock` from `data/erp_stock.json` and accepts `POST /purchase-orders`, which it
appends to `out/erp_purchase_orders.jsonl`. Both need the header `X-Api-Key` with the value of
`ERP_KEY` (default `demo-key`). Start it, then run the use case in another terminal with
`ERP_URL=http://127.0.0.1:8099` and `ERP_KEY=demo-key`. It listens on localhost only.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REQUIRED = {"supplier_id", "sku", "quantity", "unit_price"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict[str, object]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        if self.headers.get("X-Api-Key") == os.environ.get("ERP_KEY", "demo-key"):
            return True
        self._send(401, {"error": "missing or wrong X-Api-Key"})
        return False

    def do_GET(self) -> None:
        if self._authorized():
            if self.path.split("?")[0] != "/stock":
                self._send(404, {"error": "not found"})
            else:
                stock = (HERE / "data" / "erp_stock.json").read_text(encoding="utf-8")
                self._send(200, json.loads(stock))

    def do_POST(self) -> None:
        if not self._authorized():
            return
        if self.path != "/purchase-orders":
            self._send(404, {"error": "not found"})
            return
        order = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if not order.keys() >= REQUIRED:
            self._send(422, {"error": f"{', '.join(sorted(REQUIRED))} are required"})
            return
        log = HERE / "out" / "erp_purchase_orders.jsonl"
        log.parent.mkdir(exist_ok=True)
        number = f"PO-{7001 + (len(log.read_text().splitlines()) if log.exists() else 0)}"
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"po_number": number, **order}) + "\n")
        self._send(201, {"po_number": number, "status": "created"})

    def log_message(self, format: str, *args: object) -> None:
        """Stay quiet."""


def make_server(port: int = 8099) -> ThreadingHTTPServer:
    """The server, bound to localhost only (port 0 picks a free one)."""

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    server = make_server()
    print(f"sample ERP on http://127.0.0.1:{server.server_port} (Ctrl+C to stop)")
    server.serve_forever()
