#!/usr/bin/env python3
"""
FlowChartCreation – MCP connector for agents (local AI assistants).

Lets any MCP-capable agent app create and change workflow diagrams on your FlowChartCreation server,
under the agent's own name, with every change recorded in the audit trail (what and why).
Python 3.8+ standard library only. Talks to server.py; nothing leaves your network.

Setup (see AGENT_GUIDE.md):
  1. On the server computer:  python server.py agent add "Planner"   → copy the key it prints
  2. In your agent app's MCP settings add a server that runs:
        python fcc_mcp.py --server http://127.0.0.1:8765 --key-file planner.key
     (or set the environment variables FCC_SERVER and FCC_AGENT_KEY)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

NAME, VERSION = "flowchartcreation", "2.6.0"
PROTOCOL = "2025-06-18"

GUIDE = """FlowChartCreation workflow text – one step per line, the diagram draws itself.

  Receive customer order          ← a plain line is a step; steps connect top to bottom
  In stock?                       ← a line ending with "?" is a question (diamond)
    Yes: Send to warehouse        ← indented "answer:" lines are the branches (label on the arrow)
    No:
      Create purchase order       ← several steps in an answer: indent further (2 spaces per level)
      → In stock?                 ← "→ step" / "go to step" goes back to an EXISTING step (a loop)
  data: Update stock in ERP       ← shape words: data:, input:, sub:, end:
  end: Order completed            ← end: marks an end (the last step becomes an end automatically)
  # Warehouse                     ← "# name" starts a group / department lane; "#" alone ends it
  // a comment                    ← not drawn

Hebrew works the same: "כן:", "לא:", "חזור ל…", "סוף:", "נתונים:" – Hebrew text is drawn right-to-left.
After the answers, the next line with LESS indentation joins all branches again.

Branches without a question (a tree): indent several lines under an ordinary step.
  Notify customer
    Email: Send email             ← optional label on the arrow
    SMS
  Log the case                    ← the branches join again here (like answers)

Sub-flows (a block with its own inner flow):
  sub: Check stock                ← a block; its steps are in the "== Check stock ==" section at the END of the text
    Yes: Ship order               ← if the sub-flow has several end: lines (exits), write one answer per exit
    No: Reorder
  == Check stock ==               ← a local sub-flow: first step = entry, end: lines = exits
  Look up item
  Available?
    Yes: end: Yes
    No: end: No
  sub: @Approval                  ← a SHARED sub-flow: another diagram called "Approval" (see list_workflows)
  Rules: a sub-flow may not use itself (directly or through others); up to 20 levels deep.
  Changing a shared sub-flow changes it for every diagram that uses it – it needs permission on that diagram.

Good workflows:
  • one action per step, starting with a verb ("Check stock", not "Stock")
  • questions are yes/no and end with "?"; every question has at least two answers
  • loop back with "→ <exact step text>" instead of repeating steps
  • keep step texts short (under ~6 words) and unique – loops find steps by their text
  • use groups (# Department) when several departments are involved
  • for a long process, keep the main flow short and move details into sub-flows (== Name == sections)

How to work:
  1. check_workflow(text) – fix every problem it reports; look at "understood" to confirm the structure
  2. create_workflow(name, text, reason) – a new diagram; you may change the ones you create
  3. To change one: get_workflow(id) → edit the text → update_workflow(id, base_rev=<rev you read>, text, reason)
     Changing diagrams you did not create needs extended permission from the server owner.
  4. Always give a short, specific reason – it is shown to people in the change history.
"""

TOOLS = [
    {"name": "workflow_guide", "description": "Read first: how to write FlowChartCreation workflow text and how to use these tools.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "whoami", "description": "Your agent name and what you are allowed to change.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "check_workflow", "description": "Check workflow text without saving. Returns problems (with line numbers and suggestions) and the steps/arrows it understood.",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "Workflow text (one step per line, see workflow_guide)"},
         "direction": {"type": "string", "enum": ["TD", "LR", "RL", "BT"], "description": "Flow direction (default TD = top to bottom)"}},
         "required": ["text"], "additionalProperties": False}},
    {"name": "list_workflows", "description": "List shared diagrams: id, name, owner, rev, and whether you may change it.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_workflow", "description": "Read one diagram: its workflow text, rev (needed for update_workflow) and the structure it understood.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False}},
    {"name": "create_workflow", "description": "Create a new shared diagram from workflow text. Fails with the list of problems if the text has errors.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "Diagram name (1-80 characters, no < > : \" / \\ | ? *)"},
         "text": {"type": "string", "description": "Workflow text"},
         "reason": {"type": "string", "description": "Why you are creating it – shown in the change history"},
         "direction": {"type": "string", "enum": ["TD", "LR", "RL", "BT"]},
         "text_direction": {"type": "string", "enum": ["auto", "rtl", "ltr"], "description": "auto = right-to-left when the text is mostly Hebrew"}},
         "required": ["name", "text", "reason"], "additionalProperties": False}},
    {"name": "update_workflow", "description": "Change a diagram (only ones you created, unless you have extended permission). Send the full new text. base_rev must be the rev you read with get_workflow.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "base_rev": {"type": "integer", "description": "rev from get_workflow"},
         "reason": {"type": "string", "description": "Why you are making this change – shown in the change history"},
         "text": {"type": "string", "description": "The complete new workflow text"},
         "name": {"type": "string"}, "direction": {"type": "string", "enum": ["TD", "LR", "RL", "BT"]},
         "text_direction": {"type": "string", "enum": ["auto", "rtl", "ltr"]}},
         "required": ["id", "base_rev", "reason"], "additionalProperties": False}},
    {"name": "workflow_history", "description": "Change history of a diagram: who changed it, when, what and why.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False}},
]


class Api:
    def __init__(self, server: str, key: str) -> None:
        self.server = server.rstrip("/"); self.key = key

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.server + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.key,
                                              "User-Agent": f"fcc-mcp/{VERSION}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8"))
            except ValueError:
                return e.code, {"error": f"HTTP {e.code}"}
        except (urllib.error.URLError, OSError) as e:
            return 0, {"error": f"cannot reach the FlowChartCreation server at {self.server} ({e}). Is start_server running?"}


def q(s: str) -> str:
    return urllib.parse.quote(str(s), safe="")


def run_tool(api: Api, name: str, args: dict) -> tuple[str, bool]:
    if name == "workflow_guide":
        return GUIDE, False
    if name == "whoami":
        st, j = api.call("GET", "/api/agent/whoami")
    elif name == "check_workflow":
        st, j = api.call("POST", "/api/agent/check", {k: args[k] for k in ("text", "direction") if k in args})
    elif name == "list_workflows":
        st, j = api.call("GET", "/api/agent/diagrams")
    elif name == "get_workflow":
        st, j = api.call("GET", "/api/agent/diagrams/" + q(args.get("id", "")))
    elif name == "create_workflow":
        st, j = api.call("POST", "/api/agent/diagrams", {k: args[k] for k in ("name", "text", "reason", "direction", "text_direction") if k in args})
        if st == 200:
            for k in ("review_link", "edit_together_link"):
                if k in j:
                    j[k] = api.server + j[k]
    elif name == "update_workflow":
        body = {k: args[k] for k in ("base_rev", "reason", "text", "name", "direction", "text_direction") if k in args}
        st, j = api.call("PUT", "/api/agent/diagrams/" + q(args.get("id", "")), body)
    elif name == "workflow_history":
        st, j = api.call("GET", "/api/agent/diagrams/" + q(args.get("id", "")) + "/history")
    else:
        return f"unknown tool {name}", True
    return json.dumps(j, ensure_ascii=False, indent=1), st != 200


def main() -> int:
    ap = argparse.ArgumentParser(description="FlowChartCreation MCP connector (stdio)")
    ap.add_argument("--server", default=os.environ.get("FCC_SERVER", "http://127.0.0.1:8765"))
    ap.add_argument("--key-file", default=os.environ.get("FCC_AGENT_KEY_FILE"), help="file containing the agent key")
    a = ap.parse_args()
    key = os.environ.get("FCC_AGENT_KEY", "")
    if a.key_file:
        try:
            key = open(a.key_file, encoding="utf-8").read().strip()
        except OSError as e:
            print(f"fcc_mcp: cannot read key file: {e}", file=sys.stderr)
    if not key:
        print("fcc_mcp: no agent key (use --key-file or FCC_AGENT_KEY); tools will report this.", file=sys.stderr)
    api = Api(a.server, key)
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    def send(msg: dict) -> None:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n"); sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}); continue
        if not isinstance(msg, dict):
            continue
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:          # notification (e.g. notifications/initialized) – nothing to answer
            continue
        try:
            if method == "initialize":
                res = {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                       "capabilities": {"tools": {"listChanged": False}},
                       "serverInfo": {"name": NAME, "version": VERSION},
                       "instructions": "Create and change FlowChartCreation workflow diagrams. Call workflow_guide first, check_workflow before saving, and always give a reason."}
            elif method == "ping":
                res = {}
            elif method == "tools/list":
                res = {"tools": TOOLS}
            elif method == "tools/call":
                if not key:
                    text, err = "No agent key configured. Ask the server owner to run: server.py agent add <name>, then start fcc_mcp.py with --key-file.", True
                else:
                    text, err = run_tool(api, params.get("name", ""), params.get("arguments") or {})
                res = {"content": [{"type": "text", "text": text}], "isError": err}
            elif method in ("resources/list", "prompts/list"):
                res = {method.split("/")[0]: []}
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}); continue
            send({"jsonrpc": "2.0", "id": mid, "result": res})
        except Exception as e:  # noqa: BLE001 – never crash the connector
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
