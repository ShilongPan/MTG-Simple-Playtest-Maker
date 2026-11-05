# Specification — MTG Test-Play Card Printer (`mtg_test_cards.py`)

Single-file desktop tool (Python 3.8+, tkinter + reportlab) that turns a
typed/pasted list of Magic card names into a print-ready PDF of black-and-white
test-play cards at true 1:1 scale.

## 1. Purpose and non-goals

**Purpose.** Produce cut-out test-play proxies for playtesting: correct
63 × 88 mm card size, name, mana cost, type line, rules text and P/T (or
starting loyalty) on each face, laid out up to 9 per sheet with white cutting
lines between cards.

**Non-goals.** No colour art, no frame flavour, no printing directly from the
app (PDF output only), no multi-language support, no online card search UI —
all data comes from a local bulk database.

## 2. Architecture

```
main() ── tk.Tk + ttk theme ── App(root)
App
 ├─ GUI thread: text input, paper-size combo, output path, Generate (F5), status bar
 ├─ preload thread (daemon): ensure_database() → load_database() → self.db
 └─ worker thread (daemon, per Generate click):
      ensure_database(progress) → load_database()
      → parse_input() → resolve() → build_pdf() → result dialog
Shared state: self.db (card dict), _db_lock (download only).
```

Pure functions (unit-testable): `parse_input`, `resolve`, `load_database`,
`ensure_database`, `clean_mana_cost`, `wrap_text`, `compute_grid`,
`fit_font_size`, `truncate_to_fit`, `fit_name_cost`, `draw_justified`,
`draw_card`, `build_pdf`.

## 3. Card database (Scryfall bulk oracle data)

1. **Acquisition** — `ensure_database()`:
   - Cache file: `<script dir>/scryfall_oracle_cache.jsonl.gz`; valid if it
     exists and is > 100 KB.
   - Otherwise, in order: reuse any local `oracle-cards*.jsonl.gz` next to the
     script (copied to the cache path) → ask
     `https://api.scryfall.com/bulk-data` for the `oracle_cards` JSONL URL →
     download in 256 KB chunks (300 s timeout, optional progress callback)
     to `<cache>.part`, then atomically `os.replace` into place. `.part` is
     removed on failure.
   - Both Scryfall requests send the mandatory `User-Agent` and `Accept`
     headers (Scryfall returns HTTP 400 without them).
   - The download section is serialized by a process-wide lock; a failed
     network fetch raises a RuntimeError with offline-workaround guidance.
2. **Loading** — `load_database()` streams the gzipped JSONL into
   `{normalized_name: card}` where card = `name, mana_cost, type_line` (falls
   back to joined `card_types`), `power, toughness, loyalty, oracle_text`.
   Malformed lines are skipped; **first listing wins** on duplicate keys;
   entries whose name normalizes to empty are dropped.
3. **Normalization** — `_norm()`: lowercase, keep only `[a-z0-9]`. This makes
   matching case-, punctuation- and diacritic-insensitive (e.g.
   `Urborg, Tomb of Yawgmoth` ↔ `urborg tomb of yawgmoth`).

## 4. Input format

One card per line in the text box:

| Line | Result |
|------|--------|
| `2x black lotus` / `2 x name` / `2 * name` / `2 × name` | quantity 2 of *name* |
| `black lotus` (bare name) | quantity 1 |
| leading `-`, `*`, `•` bullet markers | stripped, line still parsed |
| blank line | ignored |
| `0x name` | parse error: "quantity must be at least 1" |
| punctuation-only line | parse error: "no card name found" |

Names have internal whitespace collapsed and trailing `.;:,` stripped.
Each entry keeps its input line number so later diagnostics can cite it.
(Not accepted: `2xname` without a space after the letter — see REVIEW.md §3.)

## 5. Resolution

`resolve(entries, db)` looks each entry up by normalized name and expands it
to `qty` card copies **in input order**. Unresolved entries produce a problem
string `line N: “name” (xQ): no card with this name was found…`. Generation
proceeds if at least one card resolved; all problems are reported in the
completion dialog. Zero resolved cards → hard error listing all problems.

## 6. Card face layout (all values in pt unless noted)

Constants: `MM = 72/25.4`; card `63 × 88 mm` (178.4 × 249.3 pt).

- **Border:** solid black rounded rectangle at the card edge, 7 pt wide,
corner radius 2.0 mm (assumed physical corner cut 2.5 mm), white face
restored inside it.
- **Zones** (mm from top edge, separated by 1 pt rules inset 4.0 mm):
  - name row: top … `8.0`
  - art area (intentionally blank): `8.0` … `46.0`
  - type band: `46.0` … `52.2`
  - text box: `52.2` … `79.4`
  - stat zone: `79.4` … bottom margin
- **Text padding:** content inset `(4.0 + 0.4) mm` horizontally.
- Fonts/sizes (maximums; all shrink to fit, floor `MIN_TEXT = 6 pt`):
  name Helvetica-Bold 10.5 · cost Helvetica 9.5 · type Helvetica-Oblique 9.0
  · text Helvetica 12.5 · stat Helvetica-Bold 11.5.

### 6.1 Name row
Name left, mana cost right (`{1}{U}` → `1U`, braces/whitespace removed).
`fit_name_cost` keeps the name at maximum size as long as the cost fits to its
right with a 2 mm gap; otherwise shrinks the name in 0.5 pt steps down to 3 pt,
after which the name is truncated with an ellipsis if still needed. Baselines
are vertically centered in the band using each font's ascent/descent metrics.

### 6.2 Type line
Vertically centered in its band, shrunk (≥ 6 pt) to fit the content width.

### 6.3 Text box
Oracle text word-wrapped at the current size; the whole block is vertically
centered between the bounding rules with 1 mm padding. If it doesn't fit, the
size shrinks in 0.5 pt steps (re-wrapping each step) down to 6 pt.
All lines are **fully justified** (word gaps stretched/squeezed to the content
width) except the final line and single-word lines, which stay left-aligned.
*Known limitation:* text that still doesn't fit at 6 pt overflows past the
bottom rule (see REVIEW.md 1.1).

### 6.4 Stat line
Bottom-right. `P/T` if either is present (`*` for a missing half); else
starting loyalty; else omitted. Shrunken to fit, ≥ 6 pt.

## 7. Page layout and pagination

- Paper sizes offered: US Letter, US Legal, A4, A3, Tabloid (portrait).
- `compute_grid` picks the largest of 3×3 → 3×2/2×3 → … → 1×1 that fits the
  paper with a 1 pt white gap (`GAP`) between adjacent cards; the block is
centered. Hard cap `MAX_PER_PAGE = 9`. Paper too small for one card →
RuntimeError.
- Cards fill pages row-major, top-left first; each page holds up to
`cols × rows` cards. PDF metadata: title "MTG Test-Play Cards",
author "mtg-test-card-printer".

## 8. Output handling

- Default path: system Downloads folder (`SHGetKnownFolderPath` on Windows so
  OneDrive redirection is honoured; `~/Downloads` elsewhere) /
  `mtg_test_cards.pdf`. Editable in the GUI, or chosen via Browse…
- A missing `.pdf` extension is appended (case-insensitive check).
- Existing files are overwritten without confirmation.
- Completion dialog: path, card count, page count, cards/page, plus any
  unresolved-line problems. Failures surface as an error dialog with the
  exception text; the Generate button re-enables in all cases.

## 9. GUI behaviour

- Window 720×560 (min 640×480); widgets: paper-size combo (readonly), output
  entry + Browse, card-list text box (Consolas 10, pre-filled with an example
  deck), Generate button, status label.
- **F5** triggers generation. Double-click guard: Generate is disabled while a
  worker runs and re-enabled in `finally`.
- On startup a daemon thread warms the database; on success the status bar
  shows "Ready … (N cards)"; on failure it says so and offers a retry via
  Generate (a deferred error dialog is shown unless the window is already
closed).
- Download progress updates the status bar in KB.
- ttk theme: `vista` on Windows, else `clam`, falling back silently if absent.

## 10. Dependencies and portability

- Python stdlib + tkinter; **reportlab** — installed copy preferred,
fallback to a pure-Python copy in `./vendor` (added to `sys.path`); missing
both → clear install hint on stderr.
- Network access only for the one-time bulk download (or a local
  `oracle-cards*.jsonl.gz` file works fully offline).
- Windows-specific: Downloads-folder resolution via COM GUID; everything else
  is cross-platform.

## 11. Error-handling summary

| Situation | Behaviour |
|-----------|-----------|
| No Scryfall reachability at first run | Status hint + error dialog with offline instructions; Generate retries |
| Line doesn't resolve | Card skipped, line cited in completion dialog |
| Nothing resolves | Hard failure dialog listing all problems |
| Empty input | "Nothing to do" warning, no worker started |
| Bad output path | Failure dialog with exception text (see REVIEW.md 1.6) |
| Corrupt cache line | Line skipped during load |
| Interrupted download | `.part` cleaned up; next run re-downloads |
