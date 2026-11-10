#!/usr/bin/env python3
"""
FlowChartCreation – local network (LAN) server.

Serves FlowChartCreation.html to everyone on your network and stores shared
diagrams as JSON files in ./shared_diagrams. Python 3.8+ standard library only.

    python server.py                 # http://<your-ip>:8765
    python server.py --port 9000 --open
    python server.py --read-only     # reviewers can view, nobody can publish/delete

Reviewers open:  http://<your-ip>:8765/?view=<diagram-id>
Co-editors open: http://<your-ip>:8765/?live=<diagram-id>   (everyone types at the same time)

Live editing uses operational transformation (the same idea as Google Docs): every change is sent
as a small operation, the server puts concurrent operations in one order and adjusts them so all
editors end up with exactly the same text. Edits are saved to ./shared_diagrams a moment later.
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
from urllib.parse import parse_qs, unquote, urlparse

APP = "FlowChartCreation"
VERSION = "2.3.0"
HERE = Path(__file__).resolve().parent
HTML_FILE = HERE / "FlowChartCreation.html"
ID_RE = re.compile(r"^[a-z0-9-]{1,64}$")
MAX_BODY = 1_000_000          # bytes per request
MAX_CODE = 200_000            # characters of diagram code
MAX_NAME = 80
THEMES = {"dark", "default", "forest", "neutral"}
BGS = {"dark", "light", "transparent"}
MODES = {"simple", "mermaid"}
DIRS = {"TD", "LR", "RL", "BT"}


@dataclass
class Config:
    data_dir: Path
    read_only: bool
    lan_urls: list[str]


CFG: Config
LOCK = threading.Lock()

LIVE_MAX_HISTORY = 2000       # operations kept for late acknowledgements; older clients re-join
LIVE_CLIENT_TIMEOUT = 45      # seconds without polling → editor removed from the list
LIVE_POLL_WAIT = 25           # long-poll duration (seconds)
LIVE_SAVE_DELAY = 1.5         # seconds of quiet before a live document is written to disk
LIVE_MAX_CLIENTS = 50
PALETTE = ["#f97316", "#22c55e", "#3b82f6", "#e879f9", "#eab308", "#14b8a6", "#f43f5e", "#a78bfa", "#84cc16", "#06b6d4"]


# ----------------------------------------------------------------- text operations (OT)
# An operation is a list: positive int = keep n characters, negative int = delete n characters,
# string = insert it. Lengths count UTF-16 code units, exactly like JavaScript strings in the browser.
def u16(s: str) -> int:
    return len(s.encode("utf-16-le", "surrogatepass")) // 2


def to_units(s: str) -> list[int]:
    b = s.encode("utf-16-le", "surrogatepass")
    return [b[i] | (b[i + 1] << 8) for i in range(0, len(b), 2)]


def from_units(units: list[int]) -> str:
    b = bytearray()
    for u in units:
        b.append(u & 0xFF); b.append(u >> 8)
    return bytes(b).decode("utf-16-le", "surrogatepass")


class OpError(ValueError):
    pass


class Builder:
    def __init__(self) -> None:
        self.ops: list = []

    def retain(self, n: int) -> "Builder":
        if n <= 0:
            return self
        if self.ops and isinstance(self.ops[-1], int) and self.ops[-1] > 0:
            self.ops[-1] += n
        else:
            self.ops.append(n)
        return self

    def insert(self, s: str) -> "Builder":
        if not s:
            return self
        ops = self.ops
        if ops and isinstance(ops[-1], str):
            ops[-1] += s
        elif ops and isinstance(ops[-1], int) and ops[-1] < 0:
            # keep the canonical order "insert before delete"
            if len(ops) > 1 and isinstance(ops[-2], str):
                ops[-2] += s
            else:
                ops.insert(len(ops) - 1, s)
        else:
            ops.append(s)
        return self

    def delete(self, n: int) -> "Builder":
        if n <= 0:
            return self
        if self.ops and isinstance(self.ops[-1], int) and self.ops[-1] < 0:
            self.ops[-1] -= n
        else:
            self.ops.append(-n)
        return self


def check_op(op: object) -> list:
    if not isinstance(op, list) or len(op) > 100_000:
        raise OpError("operation must be a list")
    b = Builder()
    for c in op:
        if isinstance(c, bool):
            raise OpError("invalid component")
        if isinstance(c, int):
            if c > 0:
                b.retain(c)
            elif c < 0:
                b.delete(-c)
        elif isinstance(c, str):
            b.insert(c)
        else:
            raise OpError("invalid component")
    return b.ops


def base_len(op: list) -> int:
    return sum(abs(c) for c in op if isinstance(c, int))


def apply_op(units: list[int], op: list) -> list[int]:
    if base_len(op) != len(units):
        raise OpError("operation does not match the document length")
    out: list[int] = []
    i = 0
    for c in op:
        if isinstance(c, str):
            out.extend(to_units(c))
        elif c > 0:
            out.extend(units[i:i + c]); i += c
        else:
            i -= c
    return out


def transform(a: list, b: list) -> tuple[list, list]:
    """a and b were made on the same text. Returns (a', b') with apply(apply(t, a), b') == apply(apply(t, b), a')."""
    if base_len(a) != base_len(b):
        raise OpError("concurrent operations have different base lengths")
    ap, bp = Builder(), Builder()
    i = j = 0
    o1 = a[0] if a else None
    o2 = b[0] if b else None

    def n1():
        nonlocal i, o1
        i += 1; o1 = a[i] if i < len(a) else None

    def n2():
        nonlocal j, o2
        j += 1; o2 = b[j] if j < len(b) else None

    while o1 is not None or o2 is not None:
        if isinstance(o1, str):
            ap.insert(o1); bp.retain(u16(o1)); n1(); continue
        if isinstance(o2, str):
            ap.retain(u16(o2)); bp.insert(o2); n2(); continue
        if o1 is None or o2 is None:
            raise OpError("operations do not line up")
        if o1 > 0 and o2 > 0:
            if o1 > o2:
                m = o2; o1 -= o2; n2()
            elif o1 == o2:
                m = o2; n1(); n2()
            else:
                m = o1; o2 -= o1; n1()
            ap.retain(m); bp.retain(m)
        elif o1 < 0 and o2 < 0:
            if -o1 > -o2:
                o1 -= o2; n2()
            elif o1 == o2:
                n1(); n2()
            else:
                o2 -= o1; n1()
        elif o1 < 0 < o2:
            if -o1 > o2:
                m = o2; o1 += o2; n2()
            elif -o1 == o2:
                m = o2; n1(); n2()
            else:
                m = -o1; o2 += o1; n1()
            ap.delete(m)
        else:  # o1 > 0 > o2
            if o1 > -o2:
                m = -o2; o1 += o2; n2()
            elif o1 == -o2:
                m = o1; n1(); n2()
            else:
                m = o1; o2 += o1; n1()
            bp.delete(m)
    return ap.ops, bp.ops


# ----------------------------------------------------------------- live documents
META_KEYS = ("name", "mode", "dir", "textDir", "theme", "bg")


class LiveDoc:
    def __init__(self, doc_id: str, text: str, meta: dict, code: str) -> None:
        self.id = doc_id
        self.units = to_units(text)
        self.rev = 0                      # number of operations applied
        self.base = 0                     # revision of history[0]
        self.history: list[dict] = []     # {"op": [...], "cid": "..."}
        self.meta = meta
        self.meta_rev = 0
        self.code = code                  # last Mermaid code reported by an editor (for the shared copy)
        self.code_rev = 0
        self.clients: dict[str, dict] = {}
        self.presence_rev = 0
        self.cond = threading.Condition()
        self.dirty = False
        self.save_timer: threading.Timer | None = None
        self.next_color = 0

    @property
    def text(self) -> str:
        return from_units(self.units)

    def touch(self) -> None:
        self.cond.notify_all()

    def prune(self) -> None:
        now = time.time()
        gone = [c for c, v in self.clients.items() if now - v["seen"] > LIVE_CLIENT_TIMEOUT]
        for c in gone:
            del self.clients[c]
        if gone:
            self.presence_rev += 1

    def people(self) -> list[dict]:
        return [{"cid": c, "name": v["name"], "color": v["color"], "sel": v.get("sel")} for c, v in self.clients.items()]


LIVE: dict[str, LiveDoc] = {}
LIVE_LOCK = threading.Lock()


def live_get(doc_id: str, init: dict | None = None) -> LiveDoc | None:
    """Live document from memory, else from the shared file, else from the first editor's text."""
    with LIVE_LOCK:
        ld = LIVE.get(doc_id)
        if ld:
            return ld
        d = read_doc(doc_id)
        if d:
            mode = d.get("mode") if d.get("mode") in MODES else "mermaid"
            text = (d.get("src") or "") if mode == "simple" else (d.get("code") or "")
            meta = {"name": d.get("name") or doc_id, "mode": mode, "dir": d.get("dir") if d.get("dir") in DIRS else "TD",
                    "textDir": d.get("textDir") if d.get("textDir") in ("auto", "rtl", "ltr") else "auto",
                    "theme": d.get("theme") if d.get("theme") in THEMES else "dark", "bg": d.get("bg") if d.get("bg") in BGS else "dark"}
            ld = LiveDoc(doc_id, text, meta, d.get("code") or "")
        elif init:
            ld = LiveDoc(doc_id, init["text"], init["meta"], init["code"])
            ld.dirty = True
        else:
            return None
        LIVE[doc_id] = ld
        return ld


def clean_meta(m: object, base: dict | None = None) -> dict:
    out = dict(base or {"name": "Shared diagram", "mode": "simple", "dir": "TD", "textDir": "auto", "theme": "dark", "bg": "dark"})
    if isinstance(m, dict):
        name = m.get("name")
        if isinstance(name, str) and name.strip() and len(name.strip()) <= MAX_NAME and not re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
            out["name"] = name.strip()
        if m.get("mode") in MODES: out["mode"] = m["mode"]
        if m.get("dir") in DIRS: out["dir"] = m["dir"]
        if m.get("textDir") in ("auto", "rtl", "ltr"): out["textDir"] = m["textDir"]
        if m.get("theme") in THEMES: out["theme"] = m["theme"]
        if m.get("bg") in BGS: out["bg"] = m["bg"]
    return out


def live_schedule_save(ld: LiveDoc) -> None:
    """Called with ld.cond held. Write the shared copy after a short quiet period."""
    ld.dirty = True
    if ld.save_timer:
        ld.save_timer.cancel()
    ld.save_timer = threading.Timer(LIVE_SAVE_DELAY, live_save, args=(ld,))
    ld.save_timer.daemon = True
    ld.save_timer.start()


def live_save(ld: LiveDoc) -> None:
    with ld.cond:
        if not ld.dirty:
            return
        text, meta, code, author = ld.text, dict(ld.meta), ld.code, ", ".join(sorted({v["name"] for v in ld.clients.values()}))[:40]
        ld.dirty = False
    if len(text) > MAX_CODE:
        return
    simple = meta["mode"] == "simple"
    if not simple:
        code = text
    if not code.strip():
        code = "flowchart TD"   # placeholder until an editor reports the generated code
    with LOCK:
        current = read_doc(ld.id)
        now = time.time()
        doc = {"id": ld.id, "name": meta["name"], "code": code, "src": text if simple else None, "mode": meta["mode"],
               "dir": meta["dir"], "textDir": meta["textDir"], "theme": meta["theme"], "bg": meta["bg"],
               "author": author or (current or {}).get("author") or "live", "rev": (current.get("rev", 0) + 1) if current else 1,
               "created": current.get("created", now) if current else now, "updated": now, "live": True}
        try:
            write_doc_atomic(ld.id, doc)
        except OSError as e:
            sys.stderr.write(f"live save failed for {ld.id}: {e}\n")
            with ld.cond:
                ld.dirty = True


def live_save_all() -> None:
    for ld in list(LIVE.values()):
        if ld.save_timer:
            ld.save_timer.cancel()
        live_save(ld)


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
    m = {k: doc.get(k) for k in ("id", "name", "author", "rev", "updated", "theme", "bg", "mode")}
    ld = LIVE.get(doc.get("id") or "")
    m["editors"] = len(ld.clients) if ld else 0
    return m


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
    src = body.get("src")
    if src is not None and (not isinstance(src, str) or len(src) > MAX_CODE):
        return None, f"src must be a string of at most {MAX_CODE} characters"
    mode = body.get("mode") if body.get("mode") in MODES else "mermaid"
    if mode == "simple" and src is None:
        mode = "mermaid"
    base_rev = body.get("base_rev")
    if base_rev is not None and not isinstance(base_rev, int):
        return None, "base_rev must be an integer or null"
    return {
        "name": name.strip(),
        "code": code,
        "theme": body.get("theme") if body.get("theme") in THEMES else "dark",
        "bg": body.get("bg") if body.get("bg") in BGS else "dark",
        "src": src if mode == "simple" else None,
        "mode": mode,
        "dir": body.get("dir") if body.get("dir") in DIRS else "TD",
        "textDir": body.get("textDir") if body.get("textDir") in ("auto", "rtl", "ltr") else "auto",
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
            return self._send(200, {"app": APP, "version": VERSION, "read_only": CFG.read_only, "live": True,
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
        m = re.match(r"^/api/live/([a-z0-9-]{1,64})/poll$", path)
        if m:
            return self._live_poll(m.group(1))
        self._err(404, "not found")

    # -- live editing
    def _live_poll(self, doc_id: str) -> None:
        q = parse_qs(urlparse(self.path).query)
        def qint(k: str) -> int:
            try:
                return int(q.get(k, ["-1"])[0])
            except ValueError:
                return -1
        cid, rev, mrev, prev = q.get("cid", [""])[0], qint("rev"), qint("mrev"), qint("prev")
        ld = LIVE.get(doc_id)
        if not ld:
            return self._err(410, "live session ended – join again")
        deadline = time.time() + LIVE_POLL_WAIT
        with ld.cond:
            if cid not in ld.clients:
                return self._err(410, "not joined – join again")
            if rev < ld.base or rev > ld.rev:
                return self._err(409, "out of date – join again")
            while True:
                ld.clients[cid]["seen"] = time.time()
                ld.prune()
                if cid not in ld.clients:
                    return self._err(410, "not joined – join again")
                if ld.rev > rev or ld.meta_rev != mrev or ld.presence_rev != prev:
                    break
                left = deadline - time.time()
                if left <= 0:
                    break
                ld.cond.wait(min(left, 5))
            ops = ld.history[rev - ld.base:]
            res = {"rev": ld.rev, "ops": ops, "mrev": ld.meta_rev, "prev": ld.presence_rev, "people": ld.people()}
            if ld.meta_rev != mrev:
                res["meta"] = ld.meta
        self._send(200, res)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        m = re.match(r"^/api/live/([a-z0-9-]{1,64})/(join|op|sel|meta|code|leave)$", path)
        if not m:
            return self._err(404, "not found")
        doc_id, action = m.group(1), m.group(2)
        if CFG.read_only:
            return self._err(403, "server is read-only")
        try:
            body = self._read_json()
        except (ValueError, UnicodeDecodeError) as e:
            return self._err(400, f"invalid JSON: {e}")
        if not isinstance(body, dict):
            return self._err(400, "body must be a JSON object")
        if action == "join":
            return self._live_join(doc_id, body)
        ld = LIVE.get(doc_id)
        cid = body.get("cid")
        if not ld:
            return self._err(410, "live session ended – join again")
        with ld.cond:
            if cid not in ld.clients:
                return self._err(410, "not joined – join again")
            ld.clients[cid]["seen"] = time.time()
            if action == "leave":
                del ld.clients[cid]; ld.presence_rev += 1; ld.touch()
                return self._send(200, {"ok": True})
            if action == "sel":
                sel = body.get("sel")
                if isinstance(sel, list) and len(sel) == 2 and all(isinstance(x, int) and 0 <= x <= len(ld.units) for x in sel):
                    ld.clients[cid]["sel"] = sel; ld.presence_rev += 1; ld.touch()
                return self._send(200, {"ok": True})
            if action == "meta":
                ld.meta = clean_meta(body.get("meta"), ld.meta); ld.meta_rev += 1
                live_schedule_save(ld); ld.touch()
                return self._send(200, {"ok": True, "mrev": ld.meta_rev})
            if action == "code":
                code, at = body.get("code"), body.get("rev")
                if isinstance(code, str) and code.strip() and len(code) <= MAX_CODE and at == ld.rev:
                    ld.code = code; ld.code_rev = at; live_schedule_save(ld)
                return self._send(200, {"ok": True})
            # action == "op"
            seq = body.get("seq")
            if isinstance(seq, int) and seq <= ld.clients[cid].get("seq", 0):
                return self._send(200, {"ok": True, "rev": ld.rev, "dup": True})   # a retry of an op we already have
            rev = body.get("rev")
            if not isinstance(rev, int) or rev < ld.base or rev > ld.rev:
                return self._err(409, "out of date – join again")
            try:
                op = check_op(body.get("op"))
                for h in ld.history[rev - ld.base:]:
                    op, _ = transform(op, h["op"])
                units = apply_op(ld.units, op)
            except OpError as e:
                return self._err(409, f"operation rejected ({e}) – join again")
            if len(units) > MAX_CODE:
                return self._err(413, f"text too long (max {MAX_CODE} characters)")
            ld.units = units
            ld.history.append({"op": op, "cid": cid}); ld.rev += 1
            if isinstance(seq, int):
                ld.clients[cid]["seq"] = seq
            if len(ld.history) > LIVE_MAX_HISTORY:
                drop = len(ld.history) - LIVE_MAX_HISTORY
                del ld.history[:drop]; ld.base += drop
            sel = body.get("sel")
            if isinstance(sel, list) and len(sel) == 2 and all(isinstance(x, int) and 0 <= x <= len(ld.units) for x in sel):
                ld.clients[cid]["sel"] = sel; ld.presence_rev += 1
            live_schedule_save(ld); ld.touch()
            return self._send(200, {"ok": True, "rev": ld.rev})

    def _live_join(self, doc_id: str, body: dict) -> None:
        name = body.get("name") if isinstance(body.get("name"), str) else ""
        name = re.sub(r"[\x00-\x1f<>]", "", name).strip()[:40] or "Guest"
        init = None
        if isinstance(body.get("init"), dict):
            it = body["init"]
            text, code = it.get("text"), it.get("code")
            if isinstance(text, str) and len(text) <= MAX_CODE:
                init = {"text": text, "meta": clean_meta(it.get("meta")), "code": code if isinstance(code, str) and len(code) <= MAX_CODE else ""}
        ld = live_get(doc_id, init)
        if not ld:
            return self._err(404, "diagram not found – share it first")
        with ld.cond:
            ld.prune()
            if len(ld.clients) >= LIVE_MAX_CLIENTS:
                return self._err(429, "too many editors in this diagram")
            cid = os.urandom(8).hex()
            used = {v["color"] for v in ld.clients.values()}
            color = next((c for c in PALETTE if c not in used), PALETTE[ld.next_color % len(PALETTE)])
            ld.next_color += 1
            ld.clients[cid] = {"name": name, "color": color, "seen": time.time(), "sel": None}
            ld.presence_rev += 1
            if init and ld.dirty:
                live_schedule_save(ld)
            ld.touch()
            res = {"cid": cid, "color": color, "text": ld.text, "rev": ld.rev, "meta": ld.meta, "mrev": ld.meta_rev,
                   "prev": ld.presence_rev, "people": ld.people()}
        self._send(200, res)

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
                "id": doc_id, "name": clean["name"], "code": clean["code"], "src": clean["src"], "mode": clean["mode"], "dir": clean["dir"], "textDir": clean["textDir"],
                "theme": clean["theme"], "bg": clean["bg"],
                "author": clean["author"], "rev": (current.get("rev", 0) + 1) if current else 1,
                "created": current.get("created", now) if current else now, "updated": now,
            }
            try:
                write_doc_atomic(doc_id, doc)
            except OSError as e:
                return self._err(500, f"could not save: {e}")
        # A normal publish replaces the shared copy: a running live session restarts from it
        with LIVE_LOCK:
            ld = LIVE.pop(doc_id, None)
        if ld:
            with ld.cond:
                if ld.save_timer:
                    ld.save_timer.cancel()
                ld.dirty = False; ld.clients.clear(); ld.touch()
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
        with LIVE_LOCK:
            ld = LIVE.pop(doc_id, None)
        if ld:
            with ld.cond:
                if ld.save_timer:
                    ld.save_timer.cancel()
                ld.clients.clear(); ld.touch()
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
    print("  Live editing     : on (open a diagram with ?live=<id>)" if not a.read_only else "  Live editing     : off (read-only)")
    print("  Allow Python through the firewall (Private networks) if others can't connect.")
    print(f"  Press Ctrl+C to stop.\n{line}\n")
    if a.open:
        threading.Timer(0.6, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping – saving live diagrams…")
    finally:
        live_save_all()
        srv.server_close()
        print("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
