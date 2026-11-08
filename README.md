# FlowChartCreation 2.0

A self-contained diagram studio for creating and managing workflows with [Mermaid](https://mermaid.js.org) syntax — flowcharts, sequence, state, class and Gantt diagrams — in **Hebrew and English**.

Everything (Mermaid 10.9.3 and jsPDF 2.5.2 included) lives inside **one HTML file**. No internet connection, installation or build step is needed.

## Contents

| File | Purpose |
|---|---|
| `FlowChartCreation.html` | The app. Double-click to open in Chrome, Edge or Firefox. |
| `server.py` | Optional local-network server for sharing and reviewing (Python 3.8+, standard library only). |
| `start_server.bat` / `start_server.sh` | One-click launchers for the server (Windows / macOS-Linux). |
| `shared_diagrams/` | Created by the server; one JSON file per shared diagram. |

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

Click **Self-test** (header) to run the built-in checks: libraries loaded offline with no external requests, storage round-trip with Hebrew, save → verify → history → delete, rendering of every template, Hebrew label alignment, syntax-error line reporting, PNG and all four PDF modes, SVG validity, project-file validation (accepts good / rejects malformed or oversized files), name validation, and — when opened through the server — publish / read-back / conflict detection / delete. *Copy report* puts the results on the clipboard.

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
