"""
Workflow text -> Mermaid flowchart (Python twin of simple.js inside FlowChartCreation.html).

The server uses this to check agents' workflow text and to produce the Mermaid code stored with each
diagram. It must give exactly the same result as the browser; tests run both on the same inputs.

    Receive order                 plain line          = step (connected from the line above)
    In stock?                     ends with "?"       = decision (also "if ..." / "אם ...")
      Yes: Ship                   indented "answer:"  = branch (answer becomes the arrow label)
      No:
        Create purchase order     deeper lines        = steps inside that branch
        -> In stock?              "→ / -> / go to"    = jump/loop back to an existing step
    data: SQL Server              shape words         = data / input / sub / start / end
    # Warehouse                   "#"                 = group (swim lane) until next "#"
    // note                       "//"                = comment
"""
from __future__ import annotations

import re

# JavaScript's \s and String.trim() whitespace (Python's differ slightly) – keeps results identical to the browser
_WS = "\t\n\x0b\x0c\r                  　﻿"
S = "[" + re.escape(_WS) + "]"


def jtrim(s: str) -> str:
    return s.strip(_WS)


def jtrim_start(s: str) -> str:
    return s.lstrip(_WS)


KW = {
    "start": ["start", "begin", "התחלה"],
    "end": ["end", "stop", "finish", "סוף", "סיום"],
    "data": ["data", "db", "database", "storage", "נתונים", "מסד", "מסד נתונים", "מאגר"],
    "io": ["input", "output", "io", "קלט", "פלט"],
    "sub": ["sub", "subprocess", "תת תהליך", "תת-תהליך"],
}
KW_LOOKUP = {w: shape for shape, words in KW.items() for w in words}
JUMP_RE = re.compile(
    r"^(?:→|->|=>|⟶|go" + S + r"*to" + S + r"+|goto" + S + r"+|back" + S + r"+to" + S + r"+|return" + S + r"+to" + S + r"+|jump" + S + r"+to" + S + r"+"
    r"|חזור" + S + r"+ל-?|חזרה" + S + r"+ל-?|עבור" + S + r"+ל-?|מעבר" + S + r"+ל-?|לך" + S + r"+ל-?|חזרה" + S + r"+אל" + S + r"+|עבור" + S + r"+אל" + S + r"+)"
    + S + r"*(.+)$", re.I)
IF_RE = re.compile(r"^(?:if|אם)" + S + r"+(.+)$", re.I)
COLON_24 = re.compile(r"^([^:]{1,24}):" + S + r"*(.*)$")
COLON_40 = re.compile(r"^([^:]{1,40}):" + S + r"*(.*)$")
DIRS = ["TD", "LR", "RL", "BT"]
SECTION_RE = re.compile("^==" + S + "*(.+?)" + S + "*==$")
DEPTH_DEFAULT, DEPTH_CEILING = 5, 20

MESSAGES = {
    "jumpNotFound": "Line {n}: there is no step called “{x}” to go back to.",
    "didYouMean": " Did you mean “{s}”?",
    "noBranches": "Line {n}: the question “{x}” has no answers yet. Add indented lines such as “Yes: …” and “No: …”.",
    "unreachable": "Line {n}: no arrow leads to “{x}” (it comes right after a “go to” or an end).",
    "jumpNoSource": "Line {n}: nothing leads to this “go to”.",
    "subPlain": "Line {n}: there is no sub-flow called “{x}” – it is drawn as a plain sub-process box.",
    "subMissingShared": "Line {n}: there is no shared sub-flow called “{x}”.",
    "subNeedsAnswers": "Line {n}: the sub-flow “{x}” has several exits – write one answer for each under it: {s}.",
    "subExitMissing": "Line {n}: the sub-flow “{x}” has an exit “{s}” that has no answer here.",
    "subExitUnknown": "Line {n}: “{x}” is not an exit of this sub-flow.",
    "subSingleExit": "Line {n}: the sub-flow “{x}” has only one exit, so it needs no answers.",
    "subDuplicate": "Line {n}: there is already a sub-flow called “{x}”.",
    "subCycle": "Line {n}: “{x}” uses itself: {s}.",
    "subUnused": "Line {n}: the sub-flow “{x}” is never used.",
    "subTooDeep": "The sub-flows go {x} levels deep – the maximum is {s}.",
    "subDepthRaised": "The depth limit of this flow was raised from {x} to {s} levels.",
}
NO_SUGGEST = {"subNeedsAnswers", "subExitMissing", "subSingleExit", "subCycle", "subTooDeep", "subDepthRaised"}


def norm(s: str) -> str:
    s = str(s).lower()
    s = re.sub(r"[‪-‮⁦-⁩]", "", s)
    s = re.sub("[\"'״׳`]", "", s)
    s = re.sub(S + "+", " ", s)
    return jtrim(s)


def classify(raw: str) -> dict:
    text = jtrim(raw)
    j = JUMP_RE.match(text)
    if j:
        return {"type": "jump", "target": jtrim(j.group(1))}
    kw_alone = KW_LOOKUP.get(re.sub(r"[.!:]$", "", norm(text), count=1))
    if kw_alone == "end":
        return {"type": "node", "shape": "end", "text": re.sub(r"^[a-z]", lambda m: m.group(0).upper(), text, count=1)}
    m = COLON_24.match(text)
    if m:
        shape = KW_LOOKUP.get(norm(m.group(1)))
        if shape:
            body = jtrim(m.group(2))
            if shape == "end" and not body:
                return {"type": "node", "shape": "end", "text": jtrim(m.group(1))}
            if body:
                return {"type": "node", "shape": shape, "text": body, "explicit": True}
    iff = IF_RE.match(text)
    if iff:
        return {"type": "decision", "text": re.sub(r"\?*$", "?", jtrim(iff.group(1)), count=1)}
    if re.search(r"[?？]" + S + r"*$", text):
        return {"type": "decision", "text": text}
    return {"type": "node", "shape": "step", "text": text}


def branch_label(text: str) -> dict | None:
    m = COLON_40.match(text)
    if not m:
        return None
    if norm(m.group(1)) in KW_LOOKUP:
        return None
    if JUMP_RE.match(text):
        return None
    return {"label": jtrim(m.group(1)), "rest": jtrim(m.group(2))}


def _u16len(s: str) -> int:
    return len(s.encode("utf-16-le", "surrogatepass")) // 2


def similarity(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if a == b:
        return 1.0
    if _u16len(a) < 2 or _u16len(b) < 2:
        return 0.0
    def bg(s: str) -> dict:
        m: dict = {}
        for i in range(len(s) - 1):
            k = s[i:i + 2]; m[k] = m.get(k, 0) + 1
        return m
    A, B = bg(a), bg(b)
    inter = sum(min(v, B.get(k, 0)) for k, v in A.items())
    return (2 * inter) / (len(a) + len(b) - 2)


def wrap(text: str, max_len: int = 26) -> list[str]:
    words = re.split(S + "+", text)
    lines, cur = [], ""
    for w in words:
        if cur and _u16len(cur + " " + w) > max_len:
            lines.append(cur); cur = w
        else:
            cur = cur + " " + w if cur else w
    if cur:
        lines.append(cur)
    return lines


def esc(s: str) -> str:
    return str(s).replace("#", "#35;").replace('"', "#quot;").replace("<", "#lt;").replace(">", "#gt;")


def label(s: str, max_len: int) -> str:
    return "<br/>".join(esc(x) for x in wrap(s, max_len))


def hebrew_ratio(text: str) -> float:
    he = len(re.findall("[֐-׿]", str(text)))
    lat = len(re.findall("[A-Za-z]", str(text)))
    return he / (he + lat) if he + lat else 0.0


def best_name(x: str, names: list) -> str:
    best, bs = "", 0.35
    for n in names or []:
        s_ = similarity(n, x)
        if s_ > bs:
            bs, best = s_, n
    return best


def compile_workflow(src: str, dir: str = "TD", rtl: bool = False, wrap_at: int = 26, only: str | None = None, id_prefix: str = "",
                     sub_info: dict | None = None, section_names: list | None = None, steps_word: str | None = None) -> dict:
    d = dir if dir in DIRS else "TD"
    if rtl and d == "LR":
        d = "RL"
    issues: list[dict] = []
    nodes: list[dict] = []
    edges: list[dict] = []
    jumps: list[dict] = []
    groups: list[dict] = []
    group_of_line = None

    # ---- 1. tokenize ----
    lines: list[dict] = []
    section = None
    for i, raw in enumerate(re.sub(r"\r\n?", "\n", str(src or "")).split("\n")):
        no = i + 1
        expanded = raw.replace("\t", "    ")
        text = jtrim(expanded)
        sh = SECTION_RE.match(text)
        if sh:
            section = jtrim(sh.group(1)); group_of_line = None
            continue
        if section != only:
            continue
        if not text or text.startswith("//"):
            continue
        if text.startswith("#"):
            name = jtrim(re.sub(r"^#+" + S + "*", "", text, count=1))
            if name:
                group_of_line = {"id": "g" + str(len(groups) + 1), "name": name, "line": no}
                groups.append(group_of_line)
            else:
                group_of_line = None
            continue
        lines.append({"no": no, "indent": _u16len(expanded) - _u16len(jtrim_start(expanded)), "text": text, "group": group_of_line})
    if not lines:
        return {"ok": False, "empty": True, "code": "", "layoutCode": "", "loops": [], "issues": [{"level": "info", "code": "empty", "line": 0}],
                "nodes": nodes, "edges": edges, "blocks": [], "ends": [], "lineOf": {}, "stepNames": []}

    # ---- 2. parse (indentation tree) ----
    def child_end(i: int, to: int) -> int:
        j = i + 1
        while j < to and lines[j]["indent"] > lines[i]["indent"]:
            j += 1
        return j

    def make_items(text: str, L: dict, c_from: int, c_to: int, own: bool = True) -> list[dict]:
        c = classify(text)
        if c["type"] == "decision":
            return [{**c, "line": L["no"], "group": L["group"], "branches": parse_branches(c_from, c_to, L)}]
        if c["type"] == "node" and c["shape"] == "sub" and c_from < c_to and any(x["indent"] == lines[c_from]["indent"] and branch_label(x["text"]) for x in lines[c_from:c_to]):
            return [{**c, "line": L["no"], "group": L["group"], "branches": parse_branches(c_from, c_to, L)}]
        item = {**c, "line": L["no"], "group": L["group"]}
        # a step written on its own line with several indented lines under it = branches (a tree); they rejoin like answers
        if own and c["type"] == "node" and c["shape"] != "end" and c_from < c_to:
            kids = [x for x in lines[c_from:c_to] if x["indent"] == lines[c_from]["indent"]]
            if len(kids) > 1 or branch_label(lines[c_from]["text"]):
                return [{**item, "tree": True, "branches": parse_tree(c_from, c_to)}]
        return [item, *parse_seq(c_from, c_to)] if c_from < c_to else [item]

    def parse_tree(frm: int, to: int) -> list[dict]:
        out, i = [], frm
        while i < to:
            L, j = lines[i], child_end(i, to)
            b = branch_label(L["text"])
            if b:
                out.append({"label": b["label"], "line": L["no"], "items": make_items(b["rest"], L, i + 1, j, False) if b["rest"] else parse_seq(i + 1, j)})
            else:
                out.append({"label": "", "line": L["no"], "items": make_items(L["text"], L, i + 1, j)})
            i = j
        return out

    def parse_seq(frm: int, to: int) -> list[dict]:
        out, i = [], frm
        while i < to:
            j = child_end(i, to)
            out.extend(make_items(lines[i]["text"], lines[i], i + 1, j))
            i = j
        return out

    def parse_branches(frm: int, to: int, dec_line: dict) -> list[dict]:
        out, i = [], frm
        while i < to:
            L, j = lines[i], child_end(i, to)
            b = branch_label(L["text"])
            if b:
                out.append({"label": b["label"], "line": L["no"], "items": make_items(b["rest"], L, i + 1, j, False) if b["rest"] else parse_seq(i + 1, j)})
            else:
                items = make_items(L["text"], L, i + 1, j)
                last = out[-1] if out else None
                if last and last.get("unlabeled"):
                    last["items"].extend(items)
                else:
                    out.append({"label": "", "unlabeled": True, "line": L["no"], "items": items})
            i = j
        if not out:
            issues.append({"level": "warn", "code": "noBranches", "line": dec_line["no"], "x": dec_line["text"]})
        return out

    tree = parse_seq(0, len(lines))

    # ---- 3. build graph ----
    counter = [0]

    def add_node(it: dict, shape: str) -> dict:
        counter[0] += 1
        n = {"id": id_prefix + "n" + str(counter[0]), "text": it["text"], "shape": shape, "line": it["line"], "group": it["group"]}
        if shape == "sub":
            shared = it["text"].startswith("@")
            nm = jtrim(re.sub(r"^@" + S + "*", "", it["text"], count=1)) if shared else jtrim(it["text"])
            n["sub"] = {"name": nm, "shared": shared}; n["text"] = nm
        nodes.append(n)
        return n

    def connect(entries: list[dict], to: str) -> None:
        for e in entries:
            edges.append({"from": e["from"], "to": to, "label": e.get("label") or ""})

    def build(items: list[dict], entries: list[dict]) -> list[dict]:
        for it in items:
            if it["type"] == "jump":
                jumps.append({"entries": list(entries), "target": it["target"], "line": it["line"]}); entries = []; continue
            if not entries and nodes and not it.get("_first"):
                issues.append({"level": "info", "code": "unreachable", "line": it["line"], "x": it["text"]})
            if it["type"] == "decision" or it.get("branches") is not None:
                dn = add_node(it, "decision" if it["type"] == "decision" else it["shape"]); connect(entries, dn["id"])
                if dn.get("sub"):
                    dn["sub"]["answers"] = [] if it.get("tree") else [{"label": b["label"], "line": b["line"]} for b in it["branches"]]
                exits: list[dict] = []
                if not it["branches"]:
                    exits.append({"from": dn["id"]})
                for br in (list(reversed(it["branches"])) if rtl else it["branches"]):
                    if not br["items"]:
                        exits.append({"from": dn["id"], "label": br["label"]})
                    else:
                        exits.extend(build(br["items"], [{"from": dn["id"], "label": br["label"]}]))
                entries = exits; continue
            n = add_node(it, it["shape"]); connect(entries, n["id"])
            entries = [] if it["shape"] == "end" else [{"from": n["id"]}]
        return entries

    if tree:
        tree[0]["_first"] = True
    build(tree, [])

    # ---- 4. resolve jumps ----
    def k_of(s: str) -> str:
        return re.sub(r"[?.!]$", "", norm(s), count=1)

    for jmp in jumps:
        t = k_of(jmp["target"])
        cands = [{"n": n, "k": k_of(n["text"])} for n in nodes]
        hit = next((c for c in cands if c["k"] == t), None) \
            or next((c for c in cands if c["k"].startswith(t) and _u16len(t) >= 3), None) \
            or next((c for c in cands if t in c["k"] and _u16len(t) >= 4), None)
        if not hit:
            ranked = sorted(({"c": c, "s": similarity(c["k"], t)} for c in cands), key=lambda r: -r["s"])
            best = ranked[0] if ranked else None
            issues.append({"level": "error", "code": "jumpNotFound", "line": jmp["line"], "x": jmp["target"],
                           "s": best["c"]["n"]["text"] if best and best["s"] > 0.35 else ""})
            continue
        if not jmp["entries"]:
            issues.append({"level": "warn", "code": "jumpNoSource", "line": jmp["line"], "x": jmp["target"]})
        for e in jmp["entries"]:
            edges.append({"from": e["from"], "to": hit["n"]["id"], "label": e.get("label") or "", "jump": True})

    # ---- 5. automatic start / end ovals ----
    outgoing = {e["from"] for e in edges}
    for i, n in enumerate(nodes):
        if n["shape"] != "step":
            continue
        if i == 0:
            n["shape"] = "start"
        elif n["id"] not in outgoing:
            n["shape"] = "end"

    # ---- 5b. sub-flow blocks: exits must match the sub-flow's ends ----
    blocks = [{"id": n["id"], "name": n["sub"]["name"], "shared": n["sub"]["shared"], "line": n["line"], "answers": n["sub"].get("answers") or []} for n in nodes if n.get("sub")]
    info = sub_info or {}
    for b in blocks:
        si = info.get(("@" if b["shared"] else "") + norm(b["name"]))
        if not si:
            if b["shared"]:
                issues.append({"level": "error", "code": "subMissingShared", "line": b["line"], "x": b["name"], "s": best_name(b["name"], [v["name"] for v in info.values() if v.get("shared")])})
            elif section_names:
                s2 = best_name(b["name"], section_names)
                issues.append({"level": "warn" if s2 else "info", "code": "subPlain", "line": b["line"], "x": b["name"], "s": s2})
            continue
        b["target"] = si
        exits = si.get("exits") or []
        if len(exits) >= 2:
            got = [norm(a["label"]) for a in b["answers"]]
            if not b["answers"]:
                issues.append({"level": "error", "code": "subNeedsAnswers", "line": b["line"], "x": b["name"], "s": " / ".join(exits)})
            else:
                for x in exits:
                    if norm(x) not in got:
                        issues.append({"level": "error", "code": "subExitMissing", "line": b["line"], "x": b["name"], "s": x})
                for a in b["answers"]:
                    if not any(norm(x) == norm(a["label"]) for x in exits):
                        issues.append({"level": "error", "code": "subExitUnknown", "line": a["line"], "x": a["label"], "s": best_name(a["label"], exits)})
        elif any(a["label"] for a in b["answers"]):
            issues.append({"level": "error", "code": "subSingleExit", "line": b["line"], "x": b["name"], "s": exits[0] if exits else ""})
    ends = list(dict.fromkeys(n["text"] for n in nodes if n["shape"] == "end"))

    # ---- 6. emit Mermaid ----
    SH = {"step": ("[", "]"), "start": ("([", "])"), "end": ("([", "])"), "decision": ("{", "}"), "data": ("[(", ")]"), "io": ("[/", "/]"), "sub": ("[[", "]]")}

    def decl(n: dict) -> str:
        a, b = SH.get(n["shape"], SH["step"])
        return f'{n["id"]}{a}"{label(n["text"], max(14, wrap_at - 6) if n["shape"] == "decision" else wrap_at)}"{b}'

    def ldecl(n: dict, ind: str) -> list[str]:
        if not n.get("sub"):
            return [ind + decl(n)]
        b = next((x for x in blocks if x["id"] == n["id"]), None)
        tgt = b.get("target") if b else None
        steps = tgt.get("steps") if tgt else None
        txt = ("⊕ " if tgt else "") + n["text"]
        extra = ("<br/>" + esc(str(steps) + " " + (steps_word or "steps"))) if steps is not None else ""
        return [f'{ind}{n["id"]}[["{label(txt, wrap_at)}{extra}"]]']

    def comment(n: dict) -> list[str]:
        return [f'    %% sub-flow: {"@" if n["sub"]["shared"] else ""}{n["sub"]["name"]}'] if n.get("sub") else []

    out, lay = [f"flowchart {d}"], [f"flowchart {d}"]
    for n in nodes:
        if not n["group"]:
            out += comment(n) + ["    " + decl(n)]; lay += ldecl(n, "    ")
    for g in groups:
        members = [n for n in nodes if n["group"] is g]
        if not members:
            continue
        out.append(f'    subgraph {g["id"]}["{esc(g["name"])}"]'); lay.append(f'    subgraph {id_prefix}{g["id"]}["{esc(g["name"])}"]')
        for n in members:
            out += ["    " + c for c in comment(n)] + ["        " + decl(n)]; lay += ldecl(n, "        ")
        out.append("    end"); lay.append("    end")
    fwd: dict[str, list[str]] = {}
    for e in edges:
        if not e.get("jump"):
            fwd.setdefault(e["from"], []).append(e["to"])

    def reaches(a: str, b: str) -> bool:
        seen, st = {a}, [a]
        while st:
            x = st.pop()
            if x == b:
                return True
            for y in fwd.get(x, []):
                if y not in seen:
                    seen.add(y); st.append(y)
        return False

    loops, layout = [], lay
    for e in edges:
        lab = f'|"{label(e["label"], 18)}"|' if e["label"] else ""
        line = f'    {e["from"]} {"-.->" if e.get("jump") else "-->"}{lab} {e["to"]}'
        out.append(line)
        if e.get("jump") and reaches(e["to"], e["from"]):
            loops.append({"from": e["from"], "to": e["to"], "label": e["label"] or ""})
        else:
            layout.append(line)
    cls = {"start": [], "end": [], "decision": []}
    for n in nodes:
        if n["shape"] in cls:
            cls[n["shape"]].append(n["id"])
    tail = []
    if cls["start"]:
        tail.append("    class " + ",".join(cls["start"]) + " fcStart")
    if cls["end"]:
        tail.append("    class " + ",".join(cls["end"]) + " fcEnd")
    tail += ["    classDef fcStart stroke:#10b981,stroke-width:2px", "    classDef fcEnd stroke:#f43f5e,stroke-width:2px"]
    out += tail
    linked = [b["id"] for b in blocks if b.get("target")]
    if cls["start"]:
        layout.append("    class " + ",".join(cls["start"]) + " fcStart")
    if cls["end"]:
        layout.append("    class " + ",".join(cls["end"]) + " fcEnd")
    if linked:
        layout.append("    class " + ",".join(linked) + " fcSub")
    layout += ["    classDef fcStart stroke:#10b981,stroke-width:2px", "    classDef fcEnd stroke:#f43f5e,stroke-width:2px", "    classDef fcSub stroke:#a78bfa,stroke-width:3px"]
    return {"ok": not any(i["level"] == "error" for i in issues), "code": "\n".join(out), "layoutCode": "\n".join(layout), "loops": loops,
            "rtl": bool(rtl), "issues": issues, "nodes": nodes, "edges": edges, "blocks": blocks, "ends": ends, "linked": linked,
            "lineOf": {n["id"]: n["line"] for n in nodes},
            "stepNames": [n["text"] for n in nodes]}


def messages(result: dict) -> list[dict]:
    """Issues as plain-language English messages: [{level, line, message}]."""
    out = []
    for i in result.get("issues", []):
        if i["code"] == "empty":
            continue
        msg = MESSAGES[i["code"]].replace("{n}", str(i["line"])).replace("{x}", i.get("x", "")).replace("{s}", i.get("s", ""))
        if i.get("s") and i["code"] not in NO_SUGGEST:
            msg += MESSAGES["didYouMean"].replace("{s}", i["s"])
        out.append({"level": i["level"], "line": i["line"], "message": msg})
    return out


def summary(result: dict) -> dict:
    """What the checker understood – for agents to confirm the structure before saving."""
    names = {n["id"]: n["text"] for n in result.get("nodes", [])}
    return {
        "steps": [{"id": n["id"], "text": n["text"], "shape": n["shape"], "line": n["line"]} for n in result.get("nodes", [])],
        "arrows": [{"from": names.get(e["from"]), "to": names.get(e["to"]), "label": e["label"], "loop": bool(e.get("jump"))} for e in result.get("edges", [])],
        "counts": {"steps": len(result.get("nodes", [])), "arrows": len(result.get("edges", [])), "loops": len(result.get("loops", []))},
    }


def sections_of(src: str) -> tuple[list[dict], list[dict]]:
    out, issues, seen = [], [], set()
    for i, raw in enumerate(re.sub(r"\r\n?", "\n", str(src or "")).split("\n")):
        m = SECTION_RE.match(jtrim(raw))
        if not m:
            continue
        name = jtrim(m.group(1)); k = norm(name)
        if k in seen:
            issues.append({"level": "error", "code": "subDuplicate", "line": i + 1, "x": name})
        else:
            seen.add(k); out.append({"name": name, "line": i + 1})
    return out, issues


def compile_doc(src: str, dir: str = "TD", rtl: bool = False, shared: dict | None = None, depth_limit: int | None = None,
                self_name: str | None = None, steps_word: str | None = None) -> dict:
    """Whole document (main flow + local sub-flows) – same rules as compileDoc() in the browser."""
    sections, sec_issues = sections_of(src)
    names = [x["name"] for x in sections]
    base = {"dir": dir, "rtl": rtl, "section_names": names, "steps_word": steps_word}
    pfx = lambda i: "s" + str(i + 1) + "_"
    raw = {x["name"]: compile_workflow(src, only=x["name"], id_prefix=pfx(i), **base) for i, x in enumerate(sections)}
    main_raw = compile_workflow(src, **base)
    sub_info: dict = {}
    for x in sections:
        r = raw[x["name"]]
        sub_info[norm(x["name"])] = {"name": x["name"], "shared": False, "exits": r.get("ends") or [], "steps": len(r.get("nodes") or []), "line": x["line"]}
    for v in (shared or {}).values():
        if v and v.get("name"):
            sub_info["@" + norm(v["name"])] = {**v, "shared": True}
    comp = lambda name: main_raw if name is None else raw[name]
    def local_kids(name):
        return list(dict.fromkeys(sub_info[norm(b["name"])]["name"] for b in comp(name).get("blocks") or [] if not b["shared"] and norm(b["name"]) in sub_info))
    def shared_kids(name):
        return list(dict.fromkeys(sub_info["@" + norm(b["name"])]["name"] for b in comp(name).get("blocks") or [] if b["shared"] and ("@" + norm(b["name"])) in sub_info))
    issues = list(sec_issues)
    cyc: set = set()
    for x in sections:
        path = [x["name"]]
        def walk(n):
            for k in local_kids(n):
                if k == x["name"]:
                    key = "|".join(sorted(path))
                    if key not in cyc:
                        cyc.add(key); issues.append({"level": "error", "code": "subCycle", "line": x["line"], "x": x["name"], "s": " → ".join(path + [k])})
                    continue
                if k in path or len(path) > DEPTH_CEILING + 2:
                    continue
                path.append(k); walk(k); path.pop()
        walk(x["name"])
    all_comps = [None] + names
    for n in all_comps:
        for b in comp(n).get("blocks") or []:
            si = sub_info.get("@" + norm(b["name"])) if b["shared"] else None
            if si and si.get("cycle"):
                issues.append({"level": "error", "code": "subCycle", "line": b["line"], "x": b["name"],
                               "s": (self_name or "…") + " → " + si["name"] + " → … → " + (self_name or "…")})
    used: dict = {}
    st = [None]
    while st:
        n = st.pop()
        for k in local_kids(n):
            if k not in used:
                used[k] = True; st.append(k)
    for x in sections:
        if x["name"] not in used:
            issues.append({"level": "warn", "code": "subUnused", "line": x["line"], "x": x["name"]})
    memo: dict = {}
    def depth_of(n, guard):
        if n in memo:
            return memo[n]
        if n in guard or len(guard) > DEPTH_CEILING + 2:
            return 1
        guard.add(n)
        d = 1
        for k in local_kids(n):
            d = max(d, 1 + depth_of(k, guard))
        for k in shared_kids(n):
            d = max(d, 1 + ((sub_info.get("@" + norm(k)) or {}).get("depth") or 1))
        guard.discard(n); memo[n] = d
        return d
    depth = depth_of(None, set())
    limit = min(DEPTH_CEILING, max(1, depth_limit or DEPTH_DEFAULT))
    if depth > DEPTH_CEILING:
        issues.append({"level": "error", "code": "subTooDeep", "line": 1, "x": str(depth), "s": str(DEPTH_CEILING)})
    elif depth > limit:
        issues.append({"level": "info", "code": "subDepthRaised", "line": 1, "x": str(limit), "s": str(depth)})
    full = {**base, "sub_info": sub_info}
    main = compile_workflow(src, **full)
    out = {x["name"]: compile_workflow(src, only=x["name"], id_prefix=pfx(i), **full) for i, x in enumerate(sections)}
    seen, all_issues = set(), []
    for it in issues + main["issues"] + [i for x in sections for i in out[x["name"]]["issues"]]:
        if it["code"] == "empty":
            continue
        k = "\u0001".join([it["level"], it["code"], str(it["line"]), it.get("x", "") or "", it.get("s", "") or ""])
        if k not in seen:
            seen.add(k); all_issues.append(it)
    all_issues.sort(key=lambda i: i["line"])
    uses_shared = list(dict.fromkeys(b["name"] for n in all_comps for b in comp(n).get("blocks") or [] if b["shared"]))
    return {"ok": not any(i["level"] == "error" for i in all_issues), "main": main, "sections": out, "sectionList": sections, "subInfo": sub_info,
            "issues": all_issues, "uses": {"local": list(used), "shared": uses_shared}, "depth": depth, "depthLimit": limit,
            "depthNeeded": depth if limit < depth <= DEPTH_CEILING else None}
