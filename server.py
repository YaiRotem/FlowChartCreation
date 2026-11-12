#!/usr/bin/env python3
"""
FlowChartCreation – local network (LAN) server.

Serves FlowChartCreation.html to everyone on your network and stores shared
diagrams as JSON files in ./shared_diagrams. Python 3.8+ standard library only.

    python server.py                 # http://<your-ip>:8765
    python server.py --port 9000 --open
    python server.py --read-only     # reviewers can view, nobody can publish/delete

Agents (local AI assistants) use /api/agent/... with a personal key – see AGENT_GUIDE.md:
    python server.py agent add "Planner"          # create an agent and print its key (once)
    python server.py agent grant "Planner" <id>   # extended permission: may change that existing diagram
    python server.py agent list | log "Planner" | revoke | remove | newkey

Reviewers open:  http://<your-ip>:8765/?view=<diagram-id>
Co-editors open: http://<your-ip>:8765/?live=<diagram-id>   (everyone types at the same time)

Live editing uses operational transformation (the same idea as Google Docs): every change is sent
as a small operation, the server puts concurrent operations in one order and adjusts them so all
editors end up with exactly the same text. Edits are saved to ./shared_diagrams a moment later.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import hmac
import secrets
from collections import Counter, deque
import ipaddress
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workflow_text import DEPTH_CEILING, compile_doc, compile_workflow, hebrew_ratio, messages as wf_messages, norm as wf_norm, summary as wf_summary  # noqa: E402

APP = "FlowChartCreation"
VERSION = "2.6.0"
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
    allowed_hosts: set[str]


CFG: Config
LOCK = threading.Lock()

LIVE_MAX_HISTORY = 2000       # operations kept for late acknowledgements; older clients re-join
LIVE_CLIENT_TIMEOUT = 45      # seconds without polling → editor removed from the list
LIVE_POLL_WAIT = 25           # long-poll duration (seconds)
LIVE_SAVE_DELAY = 1.5         # seconds of quiet before a live document is written to disk
LIVE_MAX_CLIENTS = 50
LIVE_MAX_DOCS = 200           # live sessions kept in memory at once
LIVE_MAX_POLLS = 300          # simultaneous waiting connections (protects the server from being flooded)
MAX_SHARED = 1000             # shared diagram files in the data folder
POLLS = 0
POLLS_LOCK = threading.Lock()
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
META_KEYS = ("name", "mode", "dir", "textDir", "theme", "bg", "colors")


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
        self.editors_since_save: set[str] = set()
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
                    "theme": d.get("theme") if d.get("theme") in THEMES else "dark", "bg": d.get("bg") if d.get("bg") in BGS else "dark",
                    "colors": clean_colors(d.get("colors"))}
            ld = LiveDoc(doc_id, text, meta, d.get("code") or "")
        elif init:
            if shared_count() >= MAX_SHARED:
                raise LimitError(f"the server already holds {MAX_SHARED} shared diagrams – delete some first")
            ld = LiveDoc(doc_id, init["text"], init["meta"], init["code"])
            ld.dirty = True
        else:
            return None
        if len(LIVE) >= LIVE_MAX_DOCS:   # drop idle, saved sessions from memory (they reload from disk when needed)
            for k, v in list(LIVE.items()):
                if not v.clients and not v.dirty:
                    del LIVE[k]
            if len(LIVE) >= LIVE_MAX_DOCS:
                raise LimitError("too many live sessions at once – try again later")
        LIVE[doc_id] = ld
        return ld


class LimitError(Exception):
    pass


def shared_count() -> int:
    return sum(1 for _ in CFG.data_dir.glob("*.json"))


COLOR_TYPES = ("start", "step", "decision", "end", "data", "io", "sub")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def clean_colors(v: object) -> dict | None:
    """Block colours of a chart: {block type: {"fill": "#rrggbb", "stroke": "#rrggbb"}} (only valid entries are kept)."""
    if not isinstance(v, dict):
        return None
    out: dict = {}
    for k in COLOR_TYPES:
        x = v.get(k)
        if isinstance(x, dict):
            e = {c: x[c].lower() for c in ("fill", "stroke") if isinstance(x.get(c), str) and HEX_RE.match(x[c])}
            if e:
                out[k] = e
    return out or None


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
        if "colors" in m: out["colors"] = clean_colors(m.get("colors"))
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
        text, meta, code = ld.text, dict(ld.meta), ld.code
        editors = sorted(set(ld.editors_since_save) or {v["name"] for v in ld.clients.values()})
        author = ", ".join(editors)[:40]
        ld.editors_since_save = set()
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
        if current and text_of(current) == (text if simple else code) and current.get("name") == meta["name"] and current.get("dir") == meta["dir"] \
                and current.get("textDir") == meta["textDir"] and current.get("theme") == meta["theme"] and current.get("bg") == meta["bg"] \
                and current.get("colors") == meta.get("colors"):
            if code.strip() and code != current.get("code"):   # only the generated code is newer: refresh it, no new version
                try:
                    write_doc_atomic(ld.id, {**current, "code": code})
                except OSError:
                    pass
            return
        doc = {"id": ld.id, "name": meta["name"], "code": code, "src": text if simple else None, "mode": meta["mode"],
               "dir": meta["dir"], "textDir": meta["textDir"], "theme": meta["theme"], "bg": meta["bg"], "colors": meta.get("colors"),
               "author": author or (current or {}).get("author") or "live", "rev": (current.get("rev", 0) + 1) if current else 1,
               "owner": (current or {}).get("owner") or ("human:" + (author or "live")),
               "created": current.get("created", now) if current else now, "updated": now, "live": True,
               "subLinks": (current or {}).get("subLinks") or {}, "depthLimit": (current or {}).get("depthLimit")}
        try:
            chk = check_subflows(doc, ld.id)
            doc["subLinks"], doc["depthLimit"] = chk["subLinks"], chk["depthLimit"]
        except Exception as e:  # noqa: BLE001 – a check must never lose typing
            sys.stderr.write(f"sub-flow check skipped for {ld.id}: {e}\n")
        try:
            commit_doc(ld.id, doc, current, {"type": "human", "name": author or "live"}, "live-edit", "")
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


# ----------------------------------------------------------------- versions + audit trail
MAX_VERSIONS = 200            # full copies kept per diagram (oldest dropped first)
AUDIT_LOCK = threading.Lock()


def text_of(doc: dict | None) -> str:
    if not doc:
        return ""
    return (doc.get("src") or "") if doc.get("mode") == "simple" else (doc.get("code") or "")


def steps_of(doc: dict | None) -> list[str]:
    if not doc or doc.get("mode") != "simple":
        return []
    try:
        return [n["text"] for n in compile_workflow(doc.get("src") or "", doc.get("dir") or "TD")["nodes"]]
    except Exception:  # noqa: BLE001 – a summary must never block a save
        return []


def change_summary(prev: dict | None, new: dict | None) -> tuple[dict, list[str]]:
    a, b = text_of(prev).splitlines(), text_of(new).splitlines()
    diff = list(difflib.unified_diff(a, b, "before", "after", lineterm="", n=1))
    added = sum(1 for x in diff if x.startswith("+") and not x.startswith("+++"))
    removed = sum(1 for x in diff if x.startswith("-") and not x.startswith("---"))
    sa, sb = Counter(steps_of(prev)), Counter(steps_of(new))
    out = {"lines_added": added, "lines_removed": removed,
           "steps_added": list((sb - sa).elements())[:50], "steps_removed": list((sa - sb).elements())[:50],
           "steps_total": sum(sb.values())}
    if prev and new and prev.get("name") != new.get("name"):
        out["renamed"] = {"from": prev.get("name"), "to": new.get("name")}
    if prev and new and prev.get("dir") != new.get("dir"):
        out["direction"] = {"from": prev.get("dir"), "to": new.get("dir")}
    return out, diff[:400]


def versions_dir(doc_id: str) -> Path:
    return CFG.data_dir / "_versions" / doc_id


def version_save(doc: dict) -> None:
    d = versions_dir(doc["id"]); d.mkdir(parents=True, exist_ok=True)
    target = d / f"{int(doc['rev'])}.json"
    tmp = d / f".tmp-{secrets.token_hex(4)}"
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, target)
    files = sorted(d.glob("*.json"), key=lambda p: int(p.stem) if p.stem.isdigit() else -1)
    for old in files[:-MAX_VERSIONS]:
        old.unlink(missing_ok=True)


def version_read(doc_id: str, rev: int) -> dict | None:
    p = versions_dir(doc_id) / f"{int(rev)}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    except (OSError, json.JSONDecodeError):
        return None


def audit_path(doc_id: str) -> Path:
    return CFG.data_dir / "_audit" / f"{doc_id}.jsonl"


def audit_append(doc_id: str, entry: dict) -> None:
    p = audit_path(doc_id); p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with AUDIT_LOCK:
        with open(p, "a", encoding="utf-8") as f:
            f.write(line); f.flush(); os.fsync(f.fileno())


def audit_read(doc_id: str, limit: int = 1000) -> list[dict]:
    p = audit_path(doc_id)
    if not p.is_file():
        return []
    out = []
    with AUDIT_LOCK:
        for line in p.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def commit_doc(doc_id: str, doc: dict, prev: dict | None, actor: dict, action: str, reason: str = "") -> dict:
    """Write a diagram (caller holds LOCK), keep a full version copy and append an audit entry: who, what, why."""
    if prev and not version_read(doc_id, prev.get("rev", 0)) and isinstance(prev.get("rev"), int):
        try:
            version_save(prev)   # first change since 2.4: keep the starting point too
        except OSError:
            pass
    write_doc_atomic(doc_id, doc)
    try:
        version_save(doc)
    except OSError as e:
        sys.stderr.write(f"version copy failed for {doc_id}: {e}\n")
    summ, diff = change_summary(prev, doc)
    entry = {"time": time.time(), "when": time.strftime("%Y-%m-%d %H:%M:%S"), "diagram": doc_id, "name": doc.get("name"),
             "rev": doc.get("rev"), "actor": actor, "action": action, "reason": reason, "summary": summ, "diff": diff}
    try:
        audit_append(doc_id, entry)
    except OSError as e:
        sys.stderr.write(f"AUDIT WRITE FAILED for {doc_id}: {e}\n")
    return entry


# ----------------------------------------------------------------- agents (local AI assistants)
AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9\u0590-\u05FF][A-Za-z0-9\u0590-\u05FF _.-]{0,39}$")
AGENTS_LOCK = threading.Lock()
_AGENTS = {"mtime": None, "data": {"agents": {}}}
AGENT_RATE = 30               # changes per minute per agent
_AGENT_HITS: dict[str, deque] = {}


def agents_path() -> Path:
    return CFG.data_dir / "_agents.json"


def agents_load() -> dict:
    """Agent registry (re-read when the file changes, so `server.py agent ...` works while the server runs)."""
    p = agents_path()
    with AGENTS_LOCK:
        try:
            mt = p.stat().st_mtime_ns
        except OSError:
            _AGENTS.update(mtime=None, data={"agents": {}}); return _AGENTS["data"]
        if mt != _AGENTS["mtime"]:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("agents"), dict):
                    raise ValueError("bad format")
            except (OSError, ValueError, json.JSONDecodeError) as e:
                sys.stderr.write(f"cannot read {p.name}: {e} – agents disabled until it is fixed\n")
                data = {"agents": {}}
            _AGENTS.update(mtime=mt, data=data)
        return _AGENTS["data"]


def agents_save(data: dict) -> None:
    p = agents_path(); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def agent_by_key(key: str) -> tuple[str, dict] | None:
    if not key or len(key) > 200:
        return None
    h = key_hash(key)
    for name, a in agents_load()["agents"].items():
        if not a.get("disabled") and hmac.compare_digest(str(a.get("key_sha256", "")), h):
            return name, a
    return None


def agent_can_edit(name: str, a: dict, doc: dict) -> bool:
    if doc.get("owner") == "agent:" + name:
        return True
    ext = a.get("extended")
    return ext == "all" or (isinstance(ext, list) and doc.get("id") in ext)


def agent_rate_ok(name: str) -> bool:
    now = time.time(); q = _AGENT_HITS.setdefault(name, deque())
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= AGENT_RATE:
        return False
    q.append(now); return True


def units_diff(a: list[int], b: list[int]) -> list:
    """Smallest single-region operation that turns text a into text b (same format as the live-editing ops)."""
    p, n = 0, min(len(a), len(b))
    while p < n and a[p] == b[p]:
        p += 1
    q = 0
    while q < n - p and a[len(a) - 1 - q] == b[len(b) - 1 - q]:
        q += 1
    bld = Builder().retain(p).delete(len(a) - p - q).insert(from_units(b[p:len(b) - q])).retain(q)
    return bld.ops


# ----------------------------------------------------------------- sub-flows (the server is the authority)
STRUCTURAL = {"subCycle", "subTooDeep", "subDuplicate"}   # never saved, whoever sends them


def all_docs() -> list[dict]:
    out = []
    for p in CFG.data_dir.glob("*.json"):
        if ID_RE.match(p.stem):
            d = read_doc(p.stem)
            if d and d.get("id") == p.stem:
                out.append(d)
    return out


class SubResolver:
    """Resolves `sub: @Name` references against the shared diagrams and works out each one's facts
    (exits, steps, depth, and whether it leads back to the chart being checked)."""

    def __init__(self) -> None:
        self.docs = {d["id"]: d for d in all_docs()}
        self.by_name: dict[str, str] = {}
        for d in sorted(self.docs.values(), key=lambda d: d.get("created") or 0):
            self.by_name.setdefault(wf_norm(d.get("name") or ""), d["id"])
        self.facts_memo: dict[str, dict] = {}

    def refs(self, doc: dict) -> list[str]:
        if doc.get("mode") != "simple":
            return []
        return compile_doc(doc.get("src") or "", doc.get("dir") or "TD")["uses"]["shared"]

    def resolve(self, name: str, links: dict) -> str | None:
        link = (links or {}).get(wf_norm(name))
        if link and link.get("id") in self.docs:
            return link["id"]
        return self.by_name.get(wf_norm(name))

    def children(self, doc: dict) -> list[str]:
        out = []
        for nm in self.refs(doc):
            i = self.resolve(nm, doc.get("subLinks") or {})
            if i and i not in out:
                out.append(i)
        return out

    def reaches(self, start: str, target: str | None) -> bool:
        if not target:
            return False
        seen, st = {start}, [start]
        while st:
            x = st.pop()
            if x == target:
                return True
            d = self.docs.get(x)
            for k in (self.children(d) if d else []):
                if k not in seen:
                    seen.add(k); st.append(k)
        return False

    def facts(self, doc_id: str, guard: frozenset = frozenset()) -> dict:
        if doc_id in self.facts_memo:
            return self.facts_memo[doc_id]
        d = self.docs[doc_id]
        if d.get("mode") != "simple" or doc_id in guard or len(guard) > DEPTH_CEILING + 2:
            return {"exits": [], "steps": 0, "depth": 1}
        shared = self.shared_map(d, doc_id, guard | {doc_id})[0]
        r = compile_doc(d.get("src") or "", d.get("dir") or "TD", shared=shared)
        f = {"exits": r["main"].get("ends") or [], "steps": len(r["main"].get("nodes") or []), "depth": r["depth"]}
        self.facts_memo[doc_id] = f
        return f

    def shared_map(self, doc: dict, self_id: str | None, guard: frozenset = frozenset()) -> tuple[dict, dict]:
        """For compile_doc: {text name: facts} and the resolved links {norm name: {id, rev, name}}."""
        shared, links = {}, {}
        for nm in self.refs(doc):
            i = self.resolve(nm, doc.get("subLinks") or {})
            if not i:
                continue
            t = self.docs[i]
            cyc = bool(self_id) and (i == self_id or self.reaches(i, self_id))
            f = {"exits": [], "steps": 0, "depth": 1} if cyc else self.facts(i, guard)
            shared[nm] = {"name": nm, "id": i, "rev": t.get("rev"), "current_name": t.get("name"), **f, "cycle": cyc}
            links[wf_norm(nm)] = {"id": i, "rev": ((doc.get("subLinks") or {}).get(wf_norm(nm)) or {}).get("rev") or t.get("rev")}
        return shared, links

    def used_by(self, doc_id: str) -> list[dict]:
        return [{"id": d["id"], "name": d.get("name")} for d in self.docs.values() if d["id"] != doc_id and doc_id in self.children(d)]


def check_subflows(doc: dict, self_id: str | None) -> dict:
    """Run the whole-document check for a (new) diagram version. Returns the result + filled-in subLinks / depthLimit."""
    if doc.get("mode") != "simple":
        return {"ok": True, "issues": [], "structural": [], "subLinks": doc.get("subLinks") or {}, "depthLimit": doc.get("depthLimit")}
    res = SubResolver()
    if self_id:
        res.docs[self_id] = {**doc, "id": self_id}
    shared, links = res.shared_map(doc, self_id)
    rtl = doc.get("textDir") == "rtl" or (doc.get("textDir") != "ltr" and hebrew_ratio(doc.get("src") or "") > 0.5)
    r = compile_doc(doc.get("src") or "", doc.get("dir") or "TD", rtl, shared=shared, depth_limit=doc.get("depthLimit"), self_name=doc.get("name"))
    structural = [i for i in r["issues"] if i["code"] in STRUCTURAL]
    return {"ok": r["ok"], "result": r, "issues": r["issues"], "structural": structural, "shared": shared,
            "subLinks": {**(doc.get("subLinks") or {}), **links}, "depthLimit": r["depthNeeded"] or doc.get("depthLimit"),
            "depthRaised": r["depthNeeded"]}


def meta(doc: dict) -> dict:
    m = {k: doc.get(k) for k in ("id", "name", "author", "rev", "updated", "theme", "bg", "mode", "owner")}
    m["rev"] = m["rev"] if isinstance(m["rev"], int) else 0            # files may have been edited by hand
    m["updated"] = m["updated"] if isinstance(m["updated"], (int, float)) else 0
    for k in ("name", "author", "owner"):
        m[k] = m[k] if isinstance(m[k], str) else ""
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
        "colors": clean_colors(body.get("colors")),
        "author": (author.strip() or "anonymous")[:40],
        "base_rev": base_rev,
        "force": bool(body.get("force")),
        "subLinks": clean_sub_links(body.get("subLinks")),
        "depthLimit": body.get("depthLimit") if isinstance(body.get("depthLimit"), int) and 1 <= body.get("depthLimit") <= DEPTH_CEILING else None,
    }, None


def clean_sub_links(v: object) -> dict:
    """{normalised sub-flow name: {"id": shared diagram id, "rev": the version this chart last accepted}}"""
    out: dict = {}
    if isinstance(v, dict):
        for k, x in list(v.items())[:200]:
            if isinstance(k, str) and len(k) <= 120 and isinstance(x, dict) and isinstance(x.get("id"), str) and ID_RE.match(x["id"]):
                out[k] = {"id": x["id"], "rev": x["rev"] if isinstance(x.get("rev"), int) else None}
    return out


# ----------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP}/{VERSION}"
    timeout = 60   # drop connections that stall (slow or half-open clients)

    # -- helpers
    def _send(self, status: int, payload: object | bytes, ctype: str = "application/json; charset=utf-8") -> None:
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")   # no hidden frames (clickjacking)
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'; connect-src 'self'")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
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

    # -- protection against other websites (CSRF) and DNS rebinding
    def _host_ok(self) -> bool:
        """The Host header must be an IP address or a known name of this computer. A web page that tricks the
        browser with a look-alike domain (DNS rebinding) sends its own domain name here and is refused."""
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        name = host[1:host.index("]")] if host.startswith("[") and "]" in host else host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        try:
            ipaddress.ip_address(name)
            return True
        except ValueError:
            return name in CFG.allowed_hosts

    def _guard(self, write: bool) -> bool:
        if not self._host_ok():
            self._err(421, "unknown host name – open the app with the address that the server window shows "
                           "(or start the server with --allow-host <name>)")
            return False
        if write:
            host = (self.headers.get("Host") or "").strip().lower()
            origin = self.headers.get("Origin")
            if origin is not None:
                o = urlparse(origin.strip().lower())
                if origin.strip().lower() == "null" or o.netloc != host:
                    self._err(403, "request from another website refused")
                    return False
            elif not urlparse(self.path).path.startswith("/api/agent/"):
                # People change diagrams through the app in a browser (which always sends Origin). Programs – including
                # agents – must use /api/agent/ with their key, so their permissions and audit trail apply.
                self._err(403, "changes must come from the app in a browser; agents must use /api/agent/ with their key")
                return False
            if self.command in ("POST", "PUT"):
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype != "application/json":
                    self._err(415, "Content-Type must be application/json")
                    return False
        return True

    def _doc_id(self, path: str) -> str | None:
        doc_id = unquote(path[len("/api/diagrams/"):])
        return doc_id if ID_RE.match(doc_id) else None

    def log_message(self, fmt: str, *args) -> None:  # concise log
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {self.client_address[0]} {fmt % args}\n")

    # -- routes
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._guard(False):
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/FlowChartCreation.html"):
            try:
                return self._send(200, HTML_FILE.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                return self._err(500, f"{HTML_FILE.name} not found next to server.py")
        if path == "/favicon.ico":
            self.send_response(204); self.end_headers(); return
        m = re.match(r"^/docs/([a-z0-9_]{1,40})\.png$", path)   # pictures for the guides shown inside the app
        if m:
            f = HERE / "docs" / f"{m.group(1)}.png"
            try:
                return self._send(200, f.read_bytes(), "image/png") if f.is_file() else self._err(404, "not found")
            except OSError:
                return self._err(404, "not found")
        if path == "/api/health":
            return self._send(200, {"app": APP, "version": VERSION, "read_only": CFG.read_only, "live": True,
                                    "lan_urls": CFG.lan_urls, "time": time.time()})
        if path == "/api/diagrams":
            items = []
            for p in CFG.data_dir.glob("*.json"):
                if not ID_RE.match(p.stem):
                    continue
                d = read_doc(p.stem)
                if d and d.get("id") == p.stem:
                    items.append(meta(d))
            items.sort(key=lambda d: d.get("updated") or 0, reverse=True)
            return self._send(200, {"items": items})
        m = re.match(r"^/api/diagrams/([a-z0-9-]{1,64})/usedby$", path)
        if m:
            return self._send(200, {"used_by": SubResolver().used_by(m.group(1))})
        m = re.match(r"^/api/diagrams/([a-z0-9-]{1,64})/audit$", path)
        if m:
            return self._send(200, {"entries": audit_read(m.group(1))})
        m = re.match(r"^/api/diagrams/([a-z0-9-]{1,64})/versions/(\d{1,9})$", path)
        if m:
            v = version_read(m.group(1), int(m.group(2)))
            return self._send(200, v) if v else self._err(404, "version not kept")
        if path == "/api/audit":
            q = parse_qs(urlparse(self.path).query); who = (q.get("agent", [""])[0] or "").strip()
            entries = []
            for f in (CFG.data_dir / "_audit").glob("*.jsonl") if (CFG.data_dir / "_audit").is_dir() else []:
                if ID_RE.match(f.stem):
                    entries += [e for e in audit_read(f.stem) if not who or (e.get("actor", {}).get("type") == "agent" and e["actor"].get("name") == who)]
            entries.sort(key=lambda e: e.get("time") or 0, reverse=True)
            return self._send(200, {"entries": entries[:500]})
        if path.startswith("/api/agent/"):
            return self._agent_get(path)
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
        global POLLS
        with POLLS_LOCK:
            if POLLS >= LIVE_MAX_POLLS:
                return self._err(503, "server busy – retrying")
            POLLS += 1
        try:
            self._live_poll_inner(doc_id)
        finally:
            with POLLS_LOCK:
                POLLS -= 1

    def _live_poll_inner(self, doc_id: str) -> None:
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
        if not self._guard(True):
            return
        path = urlparse(self.path).path
        if path.startswith("/api/agent/"):
            return self._agent_write(path)
        mr = re.match(r"^/api/diagrams/([a-z0-9-]{1,64})/restore$", path)
        if mr:
            return self._restore(mr.group(1))
        if path == "/api/check":
            return self._check()
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
            ld.editors_since_save.add(ld.clients[cid]["name"])
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
        try:
            ld = live_get(doc_id, init)
        except LimitError as e:
            return self._err(507, str(e))
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

    # -- sub-flow check for the app (the server's verdict + facts about shared sub-flows)
    def _check(self) -> None:
        try:
            body = self._read_json()
        except (ValueError, UnicodeDecodeError) as e:
            return self._err(400, f"invalid JSON: {e}")
        if not isinstance(body, dict) or not isinstance(body.get("src"), str) or len(body["src"]) > MAX_CODE:
            return self._err(422, "src must be workflow text")
        self_id = body.get("selfId") if isinstance(body.get("selfId"), str) and ID_RE.match(body["selfId"]) else None
        doc = {"mode": "simple", "src": body["src"], "dir": body.get("dir") if body.get("dir") in DIRS else "TD",
               "textDir": body.get("textDir") if body.get("textDir") in ("auto", "rtl", "ltr") else "auto",
               "name": body.get("name") if isinstance(body.get("name"), str) else "", "subLinks": clean_sub_links(body.get("subLinks")),
               "depthLimit": body.get("depthLimit") if isinstance(body.get("depthLimit"), int) else None}
        chk = check_subflows(doc, self_id)
        r = chk["result"]
        self._send(200, {"ok": chk["ok"], "issues": [{k: i.get(k, "") for k in ("level", "code", "line", "x", "s")} for i in chk["issues"]],
                         "shared": chk["shared"], "subLinks": chk["subLinks"], "depth": r["depth"], "depthNeeded": r["depthNeeded"],
                         "usedBy": SubResolver().used_by(self_id) if self_id else []})

    # -- versions
    def _restore(self, doc_id: str) -> None:
        if CFG.read_only:
            return self._err(403, "server is read-only")
        try:
            body = self._read_json()
        except (ValueError, UnicodeDecodeError) as e:
            return self._err(400, f"invalid JSON: {e}")
        rev = body.get("rev") if isinstance(body, dict) else None
        who = (body.get("author") if isinstance(body, dict) and isinstance(body.get("author"), str) else "") .strip()[:40] or "anonymous"
        if not isinstance(rev, int):
            return self._err(422, "rev must be a number")
        self._end_live(doc_id, flush=True)
        with LOCK:
            current, old = read_doc(doc_id), version_read(doc_id, rev)
            if not current or not old:
                return self._err(404, "diagram or version not found")
            now = time.time()
            doc = {**old, "id": doc_id, "rev": current.get("rev", 0) + 1, "updated": now, "author": who,
                   "owner": current.get("owner") or old.get("owner"), "created": current.get("created", now)}
            doc.pop("live", None)
            try:
                commit_doc(doc_id, doc, current, {"type": "human", "name": who}, "restore", f"restored version {rev}")
            except OSError as e:
                return self._err(500, f"could not save: {e}")
        self._send(200, meta(doc))

    def _end_live(self, doc_id: str, flush: bool) -> None:
        with LIVE_LOCK:
            ld = LIVE.pop(doc_id, None)
        if not ld:
            return
        if flush:
            if ld.save_timer:
                ld.save_timer.cancel()
            live_save(ld)
        with ld.cond:
            if ld.save_timer:
                ld.save_timer.cancel()
            ld.dirty = False; ld.clients.clear(); ld.touch()

    # -- agents
    def _agent(self) -> tuple[str, dict] | None:
        auth = self.headers.get("Authorization") or ""
        key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        hit = agent_by_key(key)
        if not hit:
            self._err(401, "unknown or missing agent key (header: Authorization: Bearer <key>)")
            return None
        return hit

    def _agent_doc_view(self, name: str, a: dict, d: dict, full: bool) -> dict:
        v = {"id": d["id"], "name": d.get("name"), "rev": d.get("rev"), "owner": d.get("owner") or "", "mode": d.get("mode"),
             "direction": d.get("dir") or "TD", "text_direction": d.get("textDir") or "auto", "updated": d.get("updated"),
             "can_edit": agent_can_edit(name, a, d) and d.get("mode") == "simple"}
        if full:
            v["text"] = text_of(d)
            v["sub_flows_used"] = [{"name": k, "id": x.get("id")} for k, x in (d.get("subLinks") or {}).items()]
            if d.get("mode") == "simple":
                v["understood"] = wf_summary(compile_workflow(d.get("src") or "", d.get("dir") or "TD"))
        return v

    def _agent_get(self, path: str) -> None:
        hit = self._agent()
        if not hit:
            return
        name, a = hit
        if path == "/api/agent/whoami":
            ext = a.get("extended")
            return self._send(200, {"name": name, "server": VERSION, "read_only": CFG.read_only,
                                    "may_create": not CFG.read_only,
                                    "may_change": "diagrams you created" + (" and every other diagram" if ext == "all" else
                                                  f" and these diagrams: {', '.join(ext)}" if isinstance(ext, list) and ext else " only")})
        if path == "/api/agent/diagrams":
            items = []
            for p in CFG.data_dir.glob("*.json"):
                d = read_doc(p.stem) if ID_RE.match(p.stem) else None
                if d and d.get("id") == p.stem:
                    items.append(self._agent_doc_view(name, a, d, False))
            items.sort(key=lambda d: d.get("updated") or 0, reverse=True)
            return self._send(200, {"items": items})
        m = re.match(r"^/api/agent/diagrams/([a-z0-9-]{1,64})$", path)
        if m:
            d = read_doc(m.group(1))
            return self._send(200, self._agent_doc_view(name, a, d, True)) if d else self._err(404, "diagram not found")
        m = re.match(r"^/api/agent/diagrams/([a-z0-9-]{1,64})/history$", path)
        if m:
            return self._send(200, {"entries": [{k: e.get(k) for k in ("when", "rev", "actor", "action", "reason", "summary")} for e in audit_read(m.group(1))][-100:]})
        self._err(404, "not found")

    def _agent_write(self, path: str) -> None:
        hit = self._agent()
        if not hit:
            return
        name, a = hit
        try:
            body = self._read_json()
        except (ValueError, UnicodeDecodeError) as e:
            return self._err(400, f"invalid JSON: {e}")
        if not isinstance(body, dict):
            return self._err(400, "body must be a JSON object")
        if path == "/api/agent/check" and self.command == "POST":
            text = body.get("text")
            if not isinstance(text, str) or len(text) > MAX_CODE:
                return self._err(422, f"text must be a string of at most {MAX_CODE} characters")
            direction = body.get("direction") if body.get("direction") in DIRS else "TD"
            f = {"text": text, "dir": direction, "textDir": body.get("text_direction") if body.get("text_direction") in ("auto", "rtl", "ltr") else "auto", "name": ""}
            r, probs = self._agent_compile(f)
            return self._send(200, {"ok": r["ok"] and not r.get("empty"), "problems": probs, "understood": wf_summary(r)})
        if CFG.read_only:
            return self._err(403, "server is read-only")
        reason = body.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return self._err(422, "reason is required: say in a sentence why you are making this change")
        reason = re.sub(r"[\x00-\x08\x0b-\x1f]", " ", reason).strip()[:500]
        if not agent_rate_ok(name):
            return self._err(429, f"too many changes – at most {AGENT_RATE} per minute")
        if path == "/api/agent/diagrams" and self.command == "POST":
            return self._agent_create(name, body, reason)
        m = re.match(r"^/api/agent/diagrams/([a-z0-9-]{1,64})$", path)
        if m and self.command == "PUT":
            return self._agent_update(name, a, m.group(1), body, reason)
        self._err(404, "not found")

    def _agent_fields(self, body: dict, base: dict | None) -> tuple[dict | None, str | None]:
        """Validate the fields an agent may set; returns the new diagram values or an error message."""
        base = base or {}
        out = {"name": base.get("name"), "text": text_of(base) if base else None, "dir": base.get("dir") or "TD", "textDir": base.get("textDir") or "auto"}
        if "name" in body or not base:
            nm = body.get("name")
            if not isinstance(nm, str) or not nm.strip() or len(nm.strip()) > MAX_NAME or re.search(r'[<>:"/\\|?*\x00-\x1f]', nm):
                return None, f"name must be 1-{MAX_NAME} characters without < > : \" / \\ | ? *"
            out["name"] = nm.strip()
        if "text" in body or not base:
            tx = body.get("text")
            if not isinstance(tx, str) or not tx.strip() or len(tx) > MAX_CODE:
                return None, f"text must be non-empty workflow text of at most {MAX_CODE} characters"
            out["text"] = tx.replace("\r\n", "\n").replace("\r", "\n")
        if "direction" in body:
            if body["direction"] not in DIRS:
                return None, "direction must be TD, LR, RL or BT"
            out["dir"] = body["direction"]
        if "text_direction" in body:
            if body["text_direction"] not in ("auto", "rtl", "ltr"):
                return None, "text_direction must be auto, rtl or ltr"
            out["textDir"] = body["text_direction"]
        return out, None

    def _agent_compile(self, f: dict, doc_id: str | None = None, base: dict | None = None) -> tuple[dict, list[dict]]:
        doc = {"mode": "simple", "src": f["text"], "dir": f["dir"], "textDir": f["textDir"], "name": f["name"],
               "subLinks": (base or {}).get("subLinks") or {}, "depthLimit": (base or {}).get("depthLimit")}
        chk = check_subflows(doc, doc_id)
        r = chk["result"]
        out = {**r["main"], "ok": r["ok"], "issues": r["issues"], "subLinks": chk["subLinks"], "depthLimit": chk["depthLimit"], "depthRaised": chk.get("depthRaised")}
        return out, wf_messages({"issues": r["issues"]})

    def _agent_create(self, name: str, body: dict, reason: str) -> None:
        f, err = self._agent_fields(body, None)
        if err:
            return self._err(422, err)
        doc_id = "a-" + format(int(time.time() * 1000), "x")[-8:] + "-" + secrets.token_hex(3)
        r, probs = self._agent_compile(f, doc_id)
        if not r["ok"] or r.get("empty"):
            return self._err(422, "the workflow text has errors – fix them and try again", problems=probs)
        now = time.time()
        doc = {"id": doc_id, "name": f["name"], "code": r["code"], "src": f["text"], "mode": "simple", "dir": f["dir"], "textDir": f["textDir"],
               "theme": "dark", "bg": "dark", "author": f"{name} (agent)", "owner": "agent:" + name, "rev": 1, "created": now, "updated": now,
               "subLinks": r["subLinks"], "depthLimit": r["depthLimit"]}
        with LOCK:
            if shared_count() >= MAX_SHARED:
                return self._err(507, f"the server already holds {MAX_SHARED} shared diagrams")
            try:
                commit_doc(doc_id, doc, None, {"type": "agent", "name": name}, "create", reason)
            except OSError as e:
                return self._err(500, f"could not save: {e}")
        self._send(200, {"id": doc_id, "rev": 1, "name": doc["name"], "warnings": probs,
                         "review_link": f"/?view={doc_id}", "edit_together_link": f"/?live={doc_id}",
                         "understood": wf_summary(r)})

    def _agent_update(self, name: str, a: dict, doc_id: str, body: dict, reason: str) -> None:
        base_rev = body.get("base_rev")
        if not isinstance(base_rev, int):
            return self._err(422, "base_rev is required: the rev you read with get_workflow (protects other people's newer changes)")
        current = read_doc(doc_id)
        if not current:
            return self._err(404, "diagram not found")
        if current.get("mode") != "simple":
            return self._err(409, "this diagram is in Mermaid-code mode – agents can only change workflow-text diagrams")
        if not agent_can_edit(name, a, current):
            return self._err(403, f"no permission to change “{current.get('name')}”: it was not created by you. "
                                  f"The server owner can allow it with:  server.py agent grant \"{name}\" {doc_id}")
        f, err = self._agent_fields(body, current)
        if err:
            return self._err(422, err)
        r, probs = self._agent_compile(f, doc_id, current)
        if not r["ok"] or r.get("empty"):
            return self._err(422, "the workflow text has errors – fix them and try again", problems=probs)
        with LIVE_LOCK:
            ld = LIVE.get(doc_id)
        if ld:   # people are (or were) editing together: save their typing first, so nothing of theirs is overwritten
            if ld.save_timer:
                ld.save_timer.cancel()
            live_save(ld)
        lock_cm = ld.cond if ld else threading.Lock()
        with lock_cm:
            with LOCK:
                current = read_doc(doc_id)
                if not current or current.get("rev") != base_rev:
                    return self._err(409, "the diagram changed since you read it – read it again (get_workflow) and redo your change",
                                     current_rev=(current or {}).get("rev"))
                if ld and from_units(ld.units) != text_of(current):
                    return self._err(409, "people are typing in this diagram right now – read it again and retry in a moment")
                now = time.time()
                doc = {**current, "name": f["name"], "src": f["text"], "code": r["code"], "dir": f["dir"], "textDir": f["textDir"],
                       "rev": current.get("rev", 0) + 1, "updated": now, "author": f"{name} (agent)", "subLinks": r["subLinks"], "depthLimit": r["depthLimit"]}
                doc.pop("live", None)
                summ_entry = None
                try:
                    summ_entry = commit_doc(doc_id, doc, current, {"type": "agent", "name": name}, "update", reason)
                except OSError as e:
                    return self._err(500, f"could not save: {e}")
            if ld and ld.clients:   # show the change to everyone in the session, like another editor typing
                new_units = to_units(f["text"])
                op = units_diff(ld.units, new_units)
                if base_len(op) == len(ld.units) and op != [len(ld.units)] and op:
                    ld.units = apply_op(ld.units, op)
                    ld.history.append({"op": op, "cid": "agent:" + name, "agent": name, "reason": reason}); ld.rev += 1
                    if len(ld.history) > LIVE_MAX_HISTORY:
                        drop = len(ld.history) - LIVE_MAX_HISTORY
                        del ld.history[:drop]; ld.base += drop
                new_meta = {**ld.meta, "name": doc["name"], "dir": doc["dir"], "textDir": doc["textDir"]}
                if new_meta != ld.meta:
                    ld.meta = new_meta; ld.meta_rev += 1
                ld.code = r["code"]; ld.dirty = False
                ld.touch()
            elif ld:
                with LIVE_LOCK:
                    LIVE.pop(doc_id, None)
        self._send(200, {"id": doc_id, "rev": doc["rev"], "warnings": probs, "summary": (summ_entry or {}).get("summary"), "understood": wf_summary(r)})

    def do_PUT(self) -> None:
        if not self._guard(True):
            return
        path = urlparse(self.path).path
        if path.startswith("/api/agent/"):
            return self._agent_write(path)
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
            if not current and shared_count() >= MAX_SHARED:
                return self._err(507, f"the server already holds {MAX_SHARED} shared diagrams – delete some first")
            if current and not clean["force"] and clean["base_rev"] != current.get("rev"):
                return self._err(409, "conflict: diagram changed since your last publish", current=meta(current))
            now = time.time()
            doc = {
                "id": doc_id, "name": clean["name"], "code": clean["code"], "src": clean["src"], "mode": clean["mode"], "dir": clean["dir"], "textDir": clean["textDir"],
                "theme": clean["theme"], "bg": clean["bg"], "colors": clean["colors"],
                "author": clean["author"], "rev": (current.get("rev", 0) + 1) if current else 1,
                "owner": (current or {}).get("owner") or ("human:" + clean["author"]),
                "created": current.get("created", now) if current else now, "updated": now,
                "subLinks": clean["subLinks"], "depthLimit": clean["depthLimit"] or (current or {}).get("depthLimit"),
            }
            chk = check_subflows(doc, doc_id)
            if chk["structural"]:
                return self._err(422, "the sub-flows can't be saved like this", problems=wf_messages({"issues": chk["structural"]}))
            doc["subLinks"], doc["depthLimit"] = chk["subLinks"], chk["depthLimit"]
            reason = "overwrote a newer version" if current and clean["force"] and clean["base_rev"] != current.get("rev") else ""
            if chk.get("depthRaised"):
                reason = (reason + "; " if reason else "") + f"depth limit raised to {chk['depthRaised']}"
            try:
                commit_doc(doc_id, doc, current, {"type": "human", "name": clean["author"]}, "publish" if current else "create", reason)
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
        if not self._guard(True):
            return
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
            users = SubResolver().used_by(doc_id)
            if users:
                return self._err(409, "this diagram is used as a sub-flow by: " + ", ".join(u["name"] or u["id"] for u in users[:10]) +
                                 " – remove it from those charts first", used_by=users)
            trash = CFG.data_dir / "_deleted"
            trash.mkdir(exist_ok=True)
            gone = read_doc(doc_id)
            os.replace(p, trash / f"{doc_id}-{int(time.time())}.json")  # recoverable
            try:
                audit_append(doc_id, {"time": time.time(), "when": time.strftime("%Y-%m-%d %H:%M:%S"), "diagram": doc_id, "name": (gone or {}).get("name"),
                                      "rev": (gone or {}).get("rev"), "actor": {"type": "human", "name": "?"}, "action": "delete",
                                      "reason": "moved to _deleted", "summary": {}, "diff": []})
            except OSError:
                pass
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


def agent_cli(argv: list[str]) -> int:
    """server.py agent <command> … – manage agents (works while the server is running)."""
    global CFG
    ap = argparse.ArgumentParser(prog="server.py agent", description="Manage agents (local AI assistants) that may create and change diagrams.")
    ap.add_argument("--data", default=str(HERE / "shared_diagrams"), help="folder for shared diagrams (same as the server's)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add", help="create an agent and print its key"); p_add.add_argument("name")
    p_add.add_argument("--extended", metavar="all|ID[,ID]", help="also allow changing existing diagrams (all, or a list of ids)")
    sub.add_parser("list", help="list agents and their permissions")
    p_gr = sub.add_parser("grant", help="extended permission: allow changing an existing diagram (or all)"); p_gr.add_argument("name"); p_gr.add_argument("what", help="diagram id, or all")
    p_rv = sub.add_parser("revoke", help="take back extended permission"); p_rv.add_argument("name"); p_rv.add_argument("what", nargs="?", default="all", help="diagram id, or all (default)")
    p_rm = sub.add_parser("remove", help="delete an agent (its changes stay in the audit trail)"); p_rm.add_argument("name")
    p_nk = sub.add_parser("newkey", help="replace an agent's key (the old one stops working)"); p_nk.add_argument("name")
    p_dis = sub.add_parser("disable", help="temporarily block an agent"); p_dis.add_argument("name")
    p_en = sub.add_parser("enable", help="unblock an agent"); p_en.add_argument("name")
    p_log = sub.add_parser("log", help="show what an agent changed and why"); p_log.add_argument("name"); p_log.add_argument("--last", type=int, default=30)
    a = ap.parse_args(argv)
    data_dir = Path(a.data).resolve(); data_dir.mkdir(parents=True, exist_ok=True)
    CFG = Config(data_dir=data_dir, read_only=False, lan_urls=[], allowed_hosts=set())
    reg = json.loads(json.dumps(agents_load()))
    agents = reg.setdefault("agents", {})
    def find(nm: str) -> str | None:
        return next((k for k in agents if k.lower() == nm.strip().lower()), None)
    def new_key() -> str:
        return "fcc_" + secrets.token_urlsafe(24)
    if a.cmd == "add":
        nm = a.name.strip()
        if not AGENT_NAME_RE.match(nm):
            print("Name: 1-40 letters, digits, spaces, . _ - (start with a letter or digit)."); return 1
        if find(nm):
            print(f"An agent called '{find(nm)}' already exists (use newkey to get a new key)."); return 1
        ext = None
        if a.extended:
            ext = "all" if a.extended.strip().lower() == "all" else [x.strip() for x in a.extended.split(",") if ID_RE.match(x.strip())]
        key = new_key()
        agents[nm] = {"key_sha256": key_hash(key), "extended": ext, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "disabled": False}
        agents_save(reg)
        print(f"\nAgent '{nm}' created. Its key (shown only now – copy it into the agent's settings):\n\n    {key}\n")
        print("It may create new diagrams and change the ones it creates." + (" Extended: " + (ext if ext == "all" else ", ".join(ext)) if ext else ""))
        return 0
    if a.cmd == "list":
        if not agents:
            print("No agents yet. Create one with:  server.py agent add \"Name\""); return 0
        for k, v in agents.items():
            ext = v.get("extended"); ext_s = "all diagrams" if ext == "all" else (", ".join(ext) if ext else "own diagrams only")
            print(f"  {k:<24} {'DISABLED ' if v.get('disabled') else ''}may change: {ext_s}   (created {v.get('created', '?')})")
        return 0
    nm = find(a.name)
    if not nm:
        print(f"No agent called '{a.name}'. See:  server.py agent list"); return 1
    v = agents[nm]
    if a.cmd == "grant":
        if a.what.lower() == "all":
            v["extended"] = "all"
        else:
            if not ID_RE.match(a.what) or not doc_path(a.what).is_file():
                print(f"No shared diagram with id '{a.what}'."); return 1
            if v.get("extended") != "all":
                v["extended"] = sorted(set((v.get("extended") or []) + [a.what]))
        agents_save(reg); print(f"'{nm}' may now change: {'all diagrams' if v['extended'] == 'all' else ', '.join(v['extended'])}"); return 0
    if a.cmd == "revoke":
        if a.what.lower() == "all":
            v["extended"] = None
        elif v.get("extended") == "all":
            print("This agent may change all diagrams. Use 'revoke NAME all' first, then grant single diagrams."); return 1
        else:
            v["extended"] = [x for x in (v.get("extended") or []) if x != a.what] or None
        agents_save(reg)
        ext = v.get("extended")
        print(f"'{nm}' now may change: {'all diagrams' if ext == 'all' else ', '.join(ext) if ext else 'own diagrams only'}"); return 0
    if a.cmd == "remove":
        del agents[nm]; agents_save(reg); print(f"Agent '{nm}' removed. Its past changes stay in the audit trail."); return 0
    if a.cmd == "newkey":
        key = new_key(); v["key_sha256"] = key_hash(key); agents_save(reg)
        print(f"\nNew key for '{nm}' (the old key no longer works):\n\n    {key}\n"); return 0
    if a.cmd in ("disable", "enable"):
        v["disabled"] = a.cmd == "disable"; agents_save(reg); print(f"Agent '{nm}' {a.cmd}d."); return 0
    if a.cmd == "log":
        entries = []
        adir = data_dir / "_audit"
        for f in adir.glob("*.jsonl") if adir.is_dir() else []:
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("actor", {}).get("type") == "agent" and e["actor"].get("name") == nm:
                    entries.append(e)
        entries.sort(key=lambda e: e.get("time") or 0)
        if not entries:
            print(f"'{nm}' has not changed anything yet."); return 0
        for e in entries[-a.last:]:
            s_ = e.get("summary") or {}
            bits = [f"+{s_.get('lines_added', 0)}/-{s_.get('lines_removed', 0)} lines"]
            if s_.get("steps_added"): bits.append("new steps: " + ", ".join(s_["steps_added"][:5]))
            if s_.get("steps_removed"): bits.append("removed: " + ", ".join(s_["steps_removed"][:5]))
            print(f"{e.get('when')}  {e.get('action'):<7} {e.get('name')!s:<28} rev {e.get('rev')}  – {e.get('reason')}\n{'':21}{'; '.join(bits)}")
        return 0
    return 1


def main() -> int:
    global CFG
    if len(sys.argv) > 1 and sys.argv[1] == "agent":
        return agent_cli(sys.argv[2:])
    ap = argparse.ArgumentParser(description="FlowChartCreation LAN server")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (0.0.0.0 = all interfaces, 127.0.0.1 = this PC only)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data", default=str(HERE / "shared_diagrams"), help="folder for shared diagrams")
    ap.add_argument("--read-only", action="store_true", help="disable publishing and deleting")
    ap.add_argument("--open", action="store_true", help="open the app in the default browser")
    ap.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                    help="extra host name people may use in the address, e.g. a DNS name for this PC (IP addresses always work)")
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
    names = {"localhost"}
    try:
        hn = socket.gethostname().lower()
        names |= {hn, hn + ".local", socket.getfqdn().lower()}
    except OSError:
        pass
    names |= {h.strip().lower() for h in a.allow_host if h.strip()}
    CFG = Config(data_dir=data_dir, read_only=a.read_only, lan_urls=[f"http://{ip}:{port}" for ip in ips], allowed_hosts=names)
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
    n_agents = len(agents_load()["agents"])
    print(f"  Agents           : {n_agents} registered (manage with: agents.bat / server.py agent ...)")
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
