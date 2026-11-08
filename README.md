# FlowChartCreation 2.2.2

A self-contained studio for creating and managing workflows in **Hebrew and English**. You describe the process in plain text — one step per line — and the diagram is drawn for you. The underlying [Mermaid](https://mermaid.js.org) code is generated automatically and stays out of the way (advanced users can view or edit it).

Everything (Mermaid 10.9.3 and jsPDF 2.5.2 included) lives inside **one HTML file**. No internet connection, installation or build step is needed.

## Contents

| File | Purpose |
|---|---|
| `FlowChartCreation.html` | The app. Double-click to open in Chrome, Edge or Firefox. |
| `server.py` | Optional local-network server for sharing and reviewing (Python 3.8+, standard library only). |
| `start_server.bat` / `start_server.sh` | One-click launchers for the server (Windows / macOS-Linux). |
| `verify_files.bat` / `verify_files.sh` | Checks every file against `CHECKSUMS.sha256` (SHA-256). Run it if the app shows a blank page or the "file is damaged" message. |
| `CHECKSUMS.sha256` | Expected SHA-256 of each file. |
| `shared_diagrams/` | Created by the server; one JSON file per shared diagram. |

## Writing a workflow (simple text)

```
Receive customer order            ← each line is a step; steps connect top to bottom
Check stock in ERP
In stock?                         ← a line ending with "?" is a question
  Yes: Send to warehouse          ← indented "answer:" lines are the branches
  No:
    Create purchase order         ← several steps in an answer: indent further
    → Check stock in ERP          ← "→" or "go to" jumps back to an existing step
data: Update SQL Server           ← shape words: data:, end:, input:, sub:
end: Order completed
```

| Write | Result |
|---|---|
| a plain line | step (the first one is drawn as the start oval, the last as the end oval) |
| line ending with `?` (or starting with `if` / `אם`) | question (diamond) |
| indented `Yes: …`, `No: …`, `Approved: …` | branches with that label on the arrow – any wording works |
| `→ step`, `go to step`, `חזור ל…` | dashed arrow back (or forward) to a step that already exists |
| `end: …` / `סוף: …`, `data: …` / `נתונים: …`, `input: …` / `קלט: …`, `sub: …` | end, database, input/output, sub-process shapes |
| `# Warehouse` | group / swim lane for the following lines (`#` alone ends it) |
| `// …` | comment (not drawn) |

After the answers, the next line with less indentation joins all branches again. Hebrew and English keywords both work in either interface language.

**Helpers:** the buttons above the text (Step, Question, Go back to…, Data, End, Group) insert the right line with the right indentation; *Go back to…* lets you pick the step from a list. **Enter** keeps the indentation (and goes one level deeper after a question or an `answer:`); Enter on an empty line steps one level back. **Click a shape** in the diagram to jump to its line. Mistakes are explained in plain language with the line number and a "did you mean…" suggestion, and the line is highlighted. The **Guide** button shows a cheat sheet; the direction box sets the flow (↓ → ← ↑).

**Mermaid code (advanced):** the `</>` button shows the generated code read-only, with *Copy* and *Edit as Mermaid…*. Editing as Mermaid switches that diagram to code mode (badge "Advanced"); *Back to simple text* returns to your text (the code version is kept in History). Sequence, Gantt, class and state diagrams are available as Mermaid templates under *Templates → Advanced*.

## Features

**1. Saving your work**
- Every change is autosaved in the browser (status pill in the header shows *Saved hh:mm*). Each save is read back and verified.
- **Library** (Ctrl+O): all your diagrams — open, rename, duplicate, delete, search.
- **History**: up to 20 automatic snapshots per diagram (taken every ~2 minutes of editing, and before any template/restore replaces your code).
- **Save as…** (Ctrl+Shift+S) asks for the file name (in Chrome/Edge: the normal Windows *Save as* window, so you also choose the folder) and writes a portable `.flowchart` file; the chosen name also becomes the diagram's name. PNG/PDF/SVG exports ask for a file name the same way. **Backup all** writes the whole library to one `.json`. Open either with *Open file* or by dragging onto the window. Old v1 `.flowchart` files and plain `.mmd` files are accepted.
- Browser storage is per browser and per address: `file://…` and `http://…:8765` keep separate libraries. Use *Backup all* / *Restore backup* to move between them or between computers.

**2. Hebrew & English**
- Full UI in both languages with proper RTL layout (button in the header; remembered).
- Hebrew labels in diagrams render with correct direction and punctuation, including mixed Hebrew + English (`בדיקת מלאי ב-ERP`, `יש במלאי? (Stock > 0)`).
- Tips: use English node IDs (`A`, `step1`) with Hebrew labels; use `flowchart RL` for right-to-left flow; wrap labels containing `( ) : "` in double quotes: `A["טקסט (X)"]`.

**3. Export without losing data**
- Diagrams are rendered with real SVG text (no HTML labels), so exports contain exactly what you see.
- **PNG** at 2×/3×/4× resolution. **PDF** options: exact diagram size (1 page), A4 or A3 fit-to-page, or **A4 real size split over several pages** for large workflows. **SVG** (vector) and **.mmd** (source) too.
- Optional title + date header (Hebrew supported).
- Checks before export: the code must render without errors (so a file never shows an older diagram than your code). Checks after: image not blank, valid PNG/PDF signature, and very large diagrams are automatically scaled to stay within browser limits (you're told when this happens).

**4. Review & share on the local network**
1. Run `start_server.bat` (Windows) or `./start_server.sh` (macOS/Linux). It prints addresses like `http://192.168.1.20:8765`.
2. Open that address, work as usual, press **Share**, enter your name → you get a review link.
3. Anyone on the same network opens the link: a read-only **review mode** that updates live (every 4 s) when you publish again. Reviewers can export or *Copy to my library* to edit their own copy.
4. If someone else published in between, you're asked to overwrite or load their version — nothing is silently lost. The server also keeps the previous version as `.json.bak`, and deleted diagrams go to `shared_diagrams/_deleted/`.

Server options: `--port 9000`, `--read-only` (view only), `--host 127.0.0.1` (this PC only), `--data <folder>`.
If others can't connect, allow Python through the Windows firewall for **Private** networks. There is no login — run it only on networks you trust.

## Verification

**Nothing to install.** The app needs only a modern browser (Chrome, Edge, Firefox); all libraries are inside the HTML file. The optional server needs Python 3.8+ with its standard library only – there is no `requirements.txt` because there are no third-party packages.

**If the page stays blank or reports "This file is damaged":** the file was corrupted in transfer (e.g. a CRC error while unzipping or copying). Download the ZIP again, use *Extract All*, and run `verify_files.bat` – every line should say `OK`. Some corporate security/DLP tools rewrite or block large HTML files with embedded scripts; if a fresh copy still fails the check, copy it through a different route or ask IT to allow it.

**Copying through Word-based tools (e.g. CodeDoc):** the HTML contains no control characters, so it survives a Word/.docx round trip byte-for-byte (verified with CodeDoc export → import).


Click **Self-test** (header) to run the built-in checks: workflow-text compiler (branches, loops, Hebrew keywords, groups, error messages, every template), libraries loaded offline with no external requests, storage round-trip with Hebrew, save → verify → history → delete, rendering of every template, Hebrew label alignment, syntax-error line reporting, PNG and all four PDF modes, SVG validity, project-file validation (accepts good / rejects malformed or oversized files), name validation, and — when opened through the server — publish / read-back / conflict detection / delete. *Copy report* puts the results on the clipboard.

## Shortcuts

| Keys | Action |
|---|---|
| Ctrl+Enter | Render now |
| Ctrl+S | Save (to browser) |
| Ctrl+Shift+S | Save as… (choose file name) |
| Ctrl+O | Library |
| Mouse wheel | Zoom around the cursor |
| Left-drag (or middle-drag) | Move the diagram |
| Double-click | Fit to window |
| Shift+wheel | Scroll |
| + / − / 0 / arrows | Zoom / fit / move (click the diagram first) |
| Tab / Shift+Tab | Indent / outdent |

---

## בעברית בקצרה

- **פתיחה:** לחיצה כפולה על `FlowChartCreation.html` – עובד ללא אינטרנט.
- **שמירה:** כל שינוי נשמר אוטומטית בדפדפן, עם ספרייה והיסטוריית גרסאות. „שמירה לקובץ” ו„גיבוי הכל” לשמירה חיצונית.
- **ייצוא:** PNG ברזולוציה גבוהה, PDF (בגודל התרשים, A4/A3, או A4 בגודל אמיתי מפוצל לעמודים), SVG.
- **שיתוף ברשת:** הפעילו `start_server.bat`, לחצו „שיתוף” ושלחו את הקישור. הסוקרים רואים עדכונים בזמן אמת.
- **בדיקה עצמית:** כפתור „בדיקה עצמית” מריץ את כל בדיקות האימות.

## License

MIT – see `LICENSE`. Bundled third-party libraries: see `THIRD_PARTY_LICENSES.txt`.
