#!/usr/bin/env python3
"""
FlowChartCreation – local network (LAN) server.

Serves FlowChartCreation.html to everyone on your network and stores shared
diagrams as JSON files in ./shared_diagrams. Python 3.8+ standard library only.

    python server.py                 # http://<your-ip>:8765
    python server.py --port 9000 --open
    python server.py --read-only     # reviewers can view, nobody can publish/delete

Reviewers open:  http://<your-ip>:8765/?view=<diagram-id>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

APP = "FlowChartCreation"
VERSION = "2.0.0"
HERE = Path(__file__).resolve().parent
HTML_FILE = HERE / "FlowChartCreation.html"
ID_RE = re.compile(r"^[a-z0-9-]{1,64}$")
MAX_BODY = 1_000_000          # bytes per request
MAX_CODE = 200_000            # characters of diagram code
MAX_NAME = 80
THEMES = {"dark", "default", "forest", "neutral"}
BGS = {"dark", "light", "transparent"}


@dataclass
class Config:
    data_dir: Path
    read_only: bool
    lan_urls: list[str]


CFG: Config
LOCK = threading.Lock()


# ----------------------------------------------------------------- storage
def doc_path(doc_id: str) -> Path:
    return CFG.data_dir / f"{doc_id}.json"


def read_doc(doc_id: str) -> dict | None:
    p = doc_path(doc_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_doc_atomic(doc_id: str, doc: dict) -> None:
    """Write to a temp file, fsync, keep previous as .bak, then atomically replace."""
    target = doc_path(doc_id)
    fd, tmp = tempfile.mkstemp(dir=CFG.data_dir, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if target.exists():
            os.replace(target, target.with_suffix(".json.bak"))
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    # verification: read back
    back = read_doc(doc_id)
    if not back or back.get("code") != doc["code"] or back.get("rev") != doc["rev"]:
        raise OSError("verification after write failed")


def meta(doc: dict) -> dict:
    return {k: doc.get(k) for k in ("id", "name", "author", "rev", "updated", "theme", "bg")}


def validate_body(body: object) -> tuple[dict | None, str | None]:
    if not isinstance(body, dict):
        return None, "body must be a JSON object"
    code, name = body.get("code"), body.get("name")
    if not isinstance(code, str) or not code.strip():
        return None, "code must be a non-empty string"
    if len(code) > MAX_CODE:
        return None, f"code too long (max {MAX_CODE} characters)"
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_NAME:
        return None, f"name must be 1-{MAX_NAME} characters"
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
        return None, "name contains invalid characters"
    author = body.get("author") if isinstance(body.get("author"), str) else "anonymous"
    base_rev = body.get("base_rev")
    if base_rev is not None and not isinstance(base_rev, int):
        return None, "base_rev must be an integer or null"
    return {
        "name": name.strip(),
        "code": code,
        "theme": body.get("theme") if body.get("theme") in THEMES else "dark",
        "bg": body.get("bg") if body.get("bg") in BGS else "dark",
        "author": (author.strip() or "anonymous")[:40],
        "base_rev": base_rev,
        "force": bool(body.get("force")),
    }, None


# ----------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP}/{VERSION}"

    # -- helpers
    def _send(self, status: int, payload: object | bytes, ctype: str = "application/json; charset=utf-8") -> None:
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _err(self, status: int, msg: str, **extra) -> None:
        self._send(status, {"error": msg, **extra})

    def _read_json(self) -> object:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            raise ValueError("empty or too large body")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _doc_id(self, path: str) -> str | None:
        doc_id = unquote(path[len("/api/diagrams/"):])
        return doc_id if ID_RE.match(doc_id) else None

    def log_message(self, fmt: str, *args) -> None:  # concise log
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {self.client_address[0]} {fmt % args}\n")

    # -- routes
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/FlowChartCreation.html"):
            try:
                return self._send(200, HTML_FILE.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                return self._err(500, f"{HTML_FILE.name} not found next to server.py")
        if path == "/favicon.ico":
            self.send_response(204); self.end_headers(); return
        if path == "/api/health":
            return self._send(200, {"app": APP, "version": VERSION, "read_only": CFG.read_only,
                                    "lan_urls": CFG.lan_urls, "time": time.time()})
        if path == "/api/diagrams":
            items = []
            for p in CFG.data_dir.glob("*.json"):
                d = read_doc(p.stem)
                if d:
                    items.append(meta(d))
            items.sort(key=lambda d: d.get("updated") or 0, reverse=True)
            return self._send(200, {"items": items})
        if path.startswith("/api/diagrams/"):
            doc_id = self._doc_id(path)
            if not doc_id:
                return self._err(400, "invalid id")
            d = read_doc(doc_id)
            return self._send(200, d) if d else self._err(404, "not found")
        self._err(404, "not found")

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        if not path.startswith("/api/diagrams/"):
            return self._err(404, "not found")
        if CFG.read_only:
            return self._err(403, "server is read-only")
        doc_id = self._doc_id(path)
        if not doc_id:
            return self._err(400, "invalid id")
        try:
            body = self._read_json()
        except (ValueError, UnicodeDecodeError) as e:
            return self._err(400, f"invalid JSON: {e}")
        clean, err = validate_body(body)
        if err:
            return self._err(422, err)
        with LOCK:
            current = read_doc(doc_id)
            if current and not clean["force"] and clean["base_rev"] != current.get("rev"):
                return self._err(409, "conflict: diagram changed since your last publish", current=meta(current))
            now = time.time()
            doc = {
                "id": doc_id, "name": clean["name"], "code": clean["code"], "theme": clean["theme"], "bg": clean["bg"],
                "author": clean["author"], "rev": (current.get("rev", 0) + 1) if current else 1,
                "created": current.get("created", now) if current else now, "updated": now,
            }
            try:
                write_doc_atomic(doc_id, doc)
            except OSError as e:
                return self._err(500, f"could not save: {e}")
        self._send(200, meta(doc))

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        if not path.startswith("/api/diagrams/"):
            return self._err(404, "not found")
        if CFG.read_only:
            return self._err(403, "server is read-only")
        doc_id = self._doc_id(path)
        if not doc_id:
            return self._err(400, "invalid id")
        with LOCK:
            p = doc_path(doc_id)
            if not p.exists():
                return self._err(404, "not found")
            trash = CFG.data_dir / "_deleted"
            trash.mkdir(exist_ok=True)
            os.replace(p, trash / f"{doc_id}-{int(time.time())}.json")  # recoverable
        self._send(200, {"ok": True})


# ----------------------------------------------------------------- startup
def lan_addresses() -> list[str]:
    ips: list[str] = []
    try:  # primary outbound interface (no packets are actually sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def main() -> int:
    global CFG
    ap = argparse.ArgumentParser(description="FlowChartCreation LAN server")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (0.0.0.0 = all interfaces, 127.0.0.1 = this PC only)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data", default=str(HERE / "shared_diagrams"), help="folder for shared diagrams")
    ap.add_argument("--read-only", action="store_true", help="disable publishing and deleting")
    ap.add_argument("--open", action="store_true", help="open the app in the default browser")
    a = ap.parse_args()

    # startup verification checks
    if sys.version_info < (3, 8):
        print("ERROR: Python 3.8 or newer is required."); return 1
    if not HTML_FILE.is_file():
        print(f"ERROR: {HTML_FILE.name} must be in the same folder as server.py ({HERE})"); return 1
    data_dir = Path(a.data).resolve()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".write-test"; probe.write_text("ok", encoding="utf-8"); probe.unlink()
    except OSError as e:
        print(f"ERROR: cannot write to data folder {data_dir}: {e}"); return 1
    port = a.port
    while not port_free(a.host, port) and port < a.port + 20:
        port += 1
    if port != a.port:
        print(f"Port {a.port} is busy – using {port} instead.")

    ips = lan_addresses() if a.host in ("0.0.0.0", "") else ([a.host] if a.host != "127.0.0.1" else [])
    CFG = Config(data_dir=data_dir, read_only=a.read_only, lan_urls=[f"http://{ip}:{port}" for ip in ips])
    srv = ThreadingHTTPServer((a.host, port), Handler)
    srv.daemon_threads = True

    line = "=" * 64
    print(f"\n{line}\n  {APP} {VERSION} – local network server{'  [READ-ONLY]' if a.read_only else ''}\n{line}")
    print(f"  On this computer : http://localhost:{port}")
    for u in CFG.lan_urls:
        print(f"  On your network  : {u}")
    if not CFG.lan_urls and a.host != "127.0.0.1":
        print("  (no LAN address detected – check that you're connected to a network)")
    print(f"  Shared diagrams  : {data_dir}")
    print("  Allow Python through the firewall (Private networks) if others can't connect.")
    print(f"  Press Ctrl+C to stop.\n{line}\n")
    if a.open:
        threading.Timer(0.6, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
