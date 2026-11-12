# FlowChartCreation – agents guide

Agents are **local AI assistants** that create and change workflow diagrams on your FlowChartCreation server. Each agent has a **name** and a personal **key**, and every change it makes is recorded in the **change history** with **what** changed and **why**. Everything stays on your network.

עברית: [AGENT_GUIDE_HE.md](AGENT_GUIDE_HE.md) · back to the [user guide](README.md).

**What an agent may do**

| Action | Allowed? |
|---|---|
| Read any shared diagram | Yes |
| Check workflow text (without saving) | Yes |
| Create a new diagram | Yes – it becomes the owner |
| Change a diagram it created | Yes |
| Change any other diagram (made by people or other agents) | Only with **extended permission** from the server owner |
| Delete a diagram | No – only people can delete (from the app) |
| Change without giving a reason | No – a reason is required on every change |

Other protections: workflow text with errors is never saved; a change based on an outdated version is refused (the agent must read the diagram again); at most 30 changes per minute per agent; if people are editing the diagram together, they see the agent's change appear live, with a note saying who changed it and why.

---

## 1. Create an agent (server owner, once per agent)

1. On the computer that runs the server, open the FlowChartCreation folder.
2. Open a command prompt there (in Explorer, type `cmd` in the address bar and press Enter) and run:
   ```
   agents.bat add "Planner"
   ```
   (Mac/Linux: `./agents.sh add "Planner"`.) The name can be English or Hebrew, up to 40 characters.
3. The key is printed **once**, e.g. `fcc_Xy3…`. Save it in a text file next to the connector, e.g. `planner.key`. Treat it like a password: anyone with the key can act as this agent.

You can do this while the server is running.

## 2. Connect the agent app

FlowChartCreation includes an **MCP connector**, `fcc_mcp.py`. MCP (Model Context Protocol) is the standard way AI assistant apps use tools; many local assistant apps support it.

1. Make sure the server is running (`start_server.bat`).
2. In your agent app, open its MCP / tools settings and add a server that runs:
   ```
   py -3 C:\FlowChartCreation\fcc_mcp.py --server http://127.0.0.1:8765 --key-file C:\FlowChartCreation\planner.key
   ```
   Many apps use a JSON settings file – see `mcp_config_example.json` for a ready example (change the paths). If the agent runs on another computer, use the server's network address (e.g. `http://192.168.1.20:8765`) and copy `fcc_mcp.py` and the key file there.
3. Restart the agent app. It should list the tools `workflow_guide`, `check_workflow`, `create_workflow`, `update_workflow` and more.
4. Try it: *"Use FlowChartCreation to draw the process for handling a customer complaint."* The new diagram appears under **Library → Shared on network**, marked with the agent's name.

**Agents without MCP:** any program can use the same HTTP API directly (section 6).

## 3. Give extended permission (to change existing diagrams)

New agents can only change diagrams they created. To let an agent change other diagrams:

```
agents.bat grant "Planner" d-mwaavlu6-nqrda     ← one diagram (its id is in the review link: …/?view=<id>)
agents.bat grant "Planner" all                  ← every diagram
agents.bat revoke "Planner" d-mwaavlu6-nqrda    ← take one back
agents.bat revoke "Planner" all                 ← back to "own diagrams only"
```

## 4. See what agents did

- **In the app:** Library → *Shared on network* → **History** on any diagram (also in the review and editing-together bars). Each entry shows who (person or agent, with an *Agent* badge), when, **why**, which steps were added or removed, and the exact change. **Restore this version** brings back any earlier version as a new change – nothing is ever deleted.
- **On the server computer:** `agents.bat log "Planner"` lists everything that agent changed, with its reasons.
- **Files:** `shared_diagrams/_audit/<id>.jsonl` (one line per change) and `shared_diagrams/_versions/<id>/<rev>.json` (a full copy of every version, last 200 kept).

Other commands: `agents.bat list`, `disable` / `enable` (block an agent temporarily), `newkey` (replace a lost or leaked key), `remove` (delete the agent; its history stays).

## 5. Instructions for the agent (what `workflow_guide` returns)

```
Receive customer order          ← a plain line is a step; steps connect top to bottom
In stock?                       ← a line ending with "?" is a question (diamond)
  Yes: Send to warehouse        ← indented "answer:" lines are the branches (label on the arrow)
  No:
    Create purchase order       ← several steps in an answer: indent further (2 spaces per level)
    → In stock?                 ← "→ step" / "go to step" goes back to an EXISTING step (a loop)
data: Update stock in ERP       ← shape words: data:, input:, sub:, end:
end: Order completed
# Warehouse                     ← "# name" starts a group / department lane; "#" alone ends it
// a comment                    ← not drawn
sub: Check stock                ← a block; its steps are in the "== Check stock ==" section at the end
  Yes: Ship order               ← several end: lines (exits) in the sub-flow = one answer per exit
  No: Reorder
== Check stock ==               ← local sub-flow: first step = entry, end: lines = exits
Look up item
Available?
  Yes: end: Yes
  No: end: No
sub: @Approval                  ← shared sub-flow: another diagram named "Approval"
Notify customer                 ← indented lines under an ordinary step = branches (a tree), no question
  Email: Send email             ← optional label
  SMS
Log the case                    ← the branches join again here
```

Hebrew works the same (`כן:`, `לא:`, `חזור ל…`, `סוף:`, `נתונים:`), and Hebrew text is drawn right-to-left. After the answers, the next line with **less** indentation joins all branches again.

**Sub-flows:** a sub-flow may not use itself (directly or through others) and can go up to 20 levels deep; the server refuses anything else and explains why. Agents may change local sub-flows (sections) in diagrams they may change. A **shared** sub-flow is a diagram of its own, so changing it needs permission on that diagram – and the change reaches every diagram that uses it (people see an *Accept / Keep the previous version* bar). `get_workflow` lists the shared sub-flows a diagram uses (`sub_flows_used`).

**Block colours:** colours are chosen by people in the app (🎨). Agents can't set them; an agent's change keeps the colours the diagram already has.

Good workflows: one action per step starting with a verb; yes/no questions ending with `?`, each with at least two answers; loops with `→ <exact step text>` instead of repeated steps; short, unique step texts; groups (`# Department`) when several departments are involved.

Working method: `check_workflow` and fix every problem → `create_workflow` with a clear reason → to change: `get_workflow` → edit the full text → `update_workflow` with `base_rev` and a reason.

## 6. HTTP API (for programs without MCP)

All requests send the header `Authorization: Bearer <key>` and JSON bodies (`Content-Type: application/json`).

| Request | Purpose |
|---|---|
| `GET /api/agent/whoami` | name and permissions |
| `POST /api/agent/check` `{text, direction?}` | check text → `{ok, problems[], understood}` |
| `GET /api/agent/diagrams` | list (with `can_edit`) |
| `GET /api/agent/diagrams/<id>` | text, `rev`, structure |
| `POST /api/agent/diagrams` `{name, text, reason, direction?, text_direction?}` | create |
| `PUT /api/agent/diagrams/<id>` `{base_rev, reason, text?, name?, direction?, text_direction?}` | change |
| `GET /api/agent/diagrams/<id>/history` | change history |

Answers: `401` wrong key · `403` no permission · `409` changed since you read it · `422` errors in the text (see `problems`) or missing reason · `429` too many changes.

## Security notes

- Keys are stored only as fingerprints (SHA-256) in `shared_diagrams/_agents.json`; the key itself is shown once.
- Agent changes can't be made through the people's routes, so an agent's limits and audit trail always apply to agents using their key. The people's side still has **no login**: a program written to pretend to be a browser could act as a person. If you need that closed too, ask for the optional password for people.
- The connection is plain HTTP on your network. Run agents on the server computer (`127.0.0.1`) or on a trusted network, as for the rest of the app.
