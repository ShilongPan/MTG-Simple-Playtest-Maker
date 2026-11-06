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
      → parse_input() → resolve() → expand_items() → build_pdf() → result dialog
Shared state: self.db (card dict), _db_lock (download only), _parse_lock
(serializes the ~25 MB load_database() parse between the two threads).
```

Pure functions (unit-testable): `parse_input`, `resolve`, `load_database`,
`ensure_database`, `clean_mana_cost`, `wrap_text`, `compute_grid`,
`fit_font_size`, `truncate_to_fit`, `fit_name_cost`, `split_panels`,
`expand_items`, `draw_text_block`, `draw_card`, `build_pdf`.

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
   `{normalized_name: [card, ...]}`; every listing under one name is kept,
   first-listing-first, so resolution can warn about collisions. card =
   `name, mana_cost, type_line` (falls back to joined `card_types`),
   `power, toughness, loyalty, defense, oracle_text`, plus the layout fields of
   §6.5: `layout`, `faces` (a full copy of every printable field per entry of
   the JSON's `card_faces`, each face with its own `layout`; empty for
   one-sided cards) and `meld_result` (the melded card's name from `all_parts`).
   Malformed lines are skipped; names whose normalized form is empty (e.g.
   `_____`) fall back to their casefolded raw name as key, so they stay
   findable by exact typing.
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
to `qty` card copies **in input order**. Matching prefers an exact
(case-insensitive) name over the normalized key, so typing `Goblin Soldier`
never silently picks up `Goblin // Soldier`; when only the normalized form
matches and several cards share it, the first listing wins and a note names the
others (closes REVIEW.md 1.2/1.3). If no card carries the typed name, the face
names of multi-face cards are tried, so typing the front half alone (`Ulvenwald
Captive`) still prints the whole card — a standalone card of that name always
wins over a same-named face. Art (art-series) printings never answer a name
search: `load_database()` flags them with `art_card` (`layout == 'art_series'`,
`frame == 'art'` or `set_type == 'art_series'` - 2,411 of the 39,287 records,
in 45 sets named "… Art Series"), `_face_index()` skips them and `_real_cards()`
filters every main-index hit, so `Hobgoblin` finds nothing instead of the art
print of *Minion of the Mighty* and a back-face name (`Ashen Reaper`) finds
Invasion of Azgol rather than its art twin; an art print is still printable when
typed by its exact full name (`Hobgoblin Bandit Lord // Hobgoblin`), which keeps
the `ART SERIES` frame reachable. Unresolved entries produce a problem string
`line N: “name” (xQ): no card with this name was found…`. Spelling help: a name
that matched neither the main index nor any face name gets up to `SUGGEST_MAX = 4`
nearest names appended as ` Did you mean "X", "Y"?` by `_suggest_names()` /
`_suggest_text()` - difflib over the normalized keys at `SUGGEST_CUTOFF = 0.75`,
key index cached per DB object like `_face_index`, ~0.1 s per bad name on the
38,766-key DB and therefore paid only on the error path. Generation proceeds if
at least one card resolved; all problems are reported in the completion dialog.
Zero resolved cards → hard error listing all problems.

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
right with a 2 mm gap; otherwise it shrinks the name in 0.5 pt steps down to
`MIN_TEXT = 6.0` pt and never below it, so the §6 floor holds for the name row
as well. A name still too wide *at that floor* is shortened with an ellipsis by
`truncate_to_fit`, which tests `text + ellipsis` inside its loop (testing only
the shortened candidate overshoots by one glyph and collapses the whole name to
a bare ellipsis); the cost keeps its own minimum room, so the two can never
overlap. 340 DB names take this path - all joke/set-gag names such as the
141-character *Our Market Research Shows…* card - and all print at 6 pt with a
readable prefix. Baselines are vertically centered in the band using each font's
ascent/descent metrics.

### 6.2 Type line
Vertically centered in its band, shrunk (≥ 6 pt) to fit the content width.

### 6.3 Text box
Oracle text word-wrapped at the current size; the whole block is vertically
centered between the bounding rules with 1 mm padding. If it doesn't fit, the
size shrinks in 0.5 pt steps (re-wrapping each step) down to 6 pt (`MIN_TEXT`);
`FIT_EPS = 1e-6` pt of slack counts an exactly-full band as fitting, so a caller
that sizes its band from this very measurement never reports a phantom cut.
All lines are **left-aligned** at the content inset (the justified renderer is
gone — its negative word gap caused overlapping words, REVIEW.md 1.5).
*Overflow is bounded:* if the text still does not fit at `MIN_TEXT`, only the
lines that fit are kept and the last one is cut with an ellipsis; that card's
name is collected in `build_pdf()`'s third return value and reported in the
completion dialog (closes REVIEW.md 1.1 / §2 "per-card quality warnings").

### 6.4 Stat line
Bottom-right. `P/T` if either is present (`*` for a missing half); else
starting loyalty; else battle `defense`; else omitted (`_stat_text`). Shrunken
to fit, ≥ 6 pt.

### 6.5 Layout-driven frames (Scryfall `layout`, not `card_faces`)

How much cardstock a card needs and which frame its text sits in is decided by
the Scryfall **`layout`** field (docs/api/layouts). Counting `card_faces` is
wrong: `split`, `flip`, `adventure` and `prepare` carry two faces but live on
ONE piece of cardstock. `expand_items()` maps layout → printed items + frame:

| Layout(s) | Items | Frame |
|---|---|---|
| `transform`, `modal_dfc`, `double_faced_token`, `art_series`, `reversible_card` (`DOUBLE_SIDED_LAYOUTS`) | **2**, tagged FRONT/BACK | standard; `TOKEN` marker for two-faced tokens, `ART SERIES` marker and a blank type line for art cards; > 2 nested faces print the first two plus a layout note. An `ART SERIES` face prints its name and nothing else (`oracle_text` is empty in Scryfall for these): the blank text box is genuine, not lost text, and such faces are never reported as truncated; a bare name search never picks one
up, see §5 |
| `split`, `flip` (`HALF_FACE_LAYOUTS`) | 1 (or `len(faces)` when > 2) | **half**: both spells on the one bordered face, divided by a rule at `HALF_DIVIDER_MM = 44.0`; each half drawn by `draw_mini_face` with its own absolute zone offsets (`MINI_NAME_LINE = 8.0`, `MINI_TYPE_TOP = 23.5`, `MINI_TYPE_BOT = 29.5`, `MINI_TEXT_BOT = 1.5`) and fonts scaled by `HALF_SCALE = 0.62`; the lower half is rotated 180° like a real split/flip card. A `flip` card's
halves are additionally tagged `FRONT` / `BACK` bottom-left inside each half
(`draw_mini_face(tag=...)`) because the game really turns that card over, and
every half prints its own stat line from its own face record: a flip back is a
different creature (*Nezumi Graverobber* 2/1 → *Nighteyes the Desecrator* 4/2,
*Curse of the Fire Penguin* → *Curse of the Fire Penguin Creature* 6/5) and the
back of a double-faced planeswalker carries its starting loyalty counter (*Ral,
Monsoon Mage* 1/3 → *Ral, Leyline Prodigy* 2), so `_stat_text()` is fed the face
dict, never the parent card. The half's stat baseline is clamped to
`max(y + MINI_TEXT_BOT * MM, y + 3.0 * MM * k)` so a short half cannot push the
numbers into its rules text or the border. The authentic frame holds *exactly* two spells, so a card with more halves (the joke set `Smelt // Herd // Saw`, `Who // What // When // Where // Why`) instead prints one standard card per spell, each marked `SPLIT i/n` / `FLIP i/n` and tagged `i/n`; the extra spells are never dropped silently, only reflowed |
| `adventure`, `prepare` (`INSET_PANEL_LAYOUTS`) | 1 | standard + inset: main spell as usual, Adventure/Prepared spell in an `INSET_W_MM × INSET_H_MM = 34 × 18` mm rounded box at the bottom-left of the text box (`_draw_inset_panel`, fonts × `INSET_SCALE = 0.70`); the main text box's bottom is raised so nothing overlaps |
| `leveler`, `class`, `case`, `saga` (`STACKED_PANEL_LAYOUTS`) | 1 | **panels**: `split_panels()` cuts the rules text into sub-boxes at `_PANEL_HEADERS` regexes, `_panel_label()` keeps only short labels (`I, II —`, `To solve —`, `LEVEL 2+`) bold and folds the rest into wrapped body text; `_draw_panel_boxes` measures every panel's true wrapped height (header plus body block, exactly as `draw_text_block` measures it) and sizes each share from that measurement — the white above the dividers is spent down from `PANEL_PAD_MM = 1.2` mm to `MIN_PANEL_PAD_MM = 0.5` mm *before* any text shrinks, so a three-chapter card prints every chapter instead of losing text; headers at × `PANEL_HDR_SCALE = 0.80` with `PANEL_HDR_EXTRA_MM = 0.4` mm under them, bodies padded `PANEL_BODY_PAD_MM = 0.3` mm, separated by 1 pt rules |
| `meld` | part → 2 (MELD PART then MELD RESULT via its `meld_result`); result alone → 1 | standard + marker; the auto-added result prints at most once per run — both halves of a meld pair name the same result, and typing that result card yourself counts as the copy — so no face is duplicated; a missing partner emits a layout note |
| everything else (`normal`, `mutate`, `prototype`, `token`, `emblem`, `planar`, `scheme`, `vanguard`, `host`, `augment`, `front_card`) | 1 | standard + bottom-left corner marker from `LAYOUT_MARKERS` (TOKEN, EMBLEM, PLANE, SCHEME, VANGUARD, DECK INDICATOR, HOST, AUGMENT, ART SERIES, PROTOTYPE, MUTATE, MELD PART) |

The standard text box of §6.3 is shared unchanged: `draw_text_block()` is the
only renderer and normal cards pass it the §6 zone bounds, fonts and padding;
the custom frames above only hand it smaller sub-boxes of their own.
`expand_items(cards, db, notes)` appends human-readable remarks to `notes`,
which the completion dialog prints under "Layout notes:".

Every frame reports its own overflow: the same warning list is threaded into
`draw_card`, `draw_mini_face` (split/flip halves) and `_draw_inset_panel`
(Adventure/Prepared), so a cut in any of them - or a name shortened at the §6.1
floor (`_note_short_name`) - reaches `build_pdf()` as a `(reason, name)` pair.
Nothing is ever cut silently.

Panel stacks obey the same overflow rule as §6.3: a stack that still does not
fit at `MIN_TEXT` is cut with an ellipsis and named in the completion dialog.
The measured allocation is what makes that rare — of the test set only sagas and
classes with ~330–380 characters of rules text sit at the 6 pt floor, and every
chapter label (`I, II —`, `{3}{U}: Level 2`, `Solved —`) still prints in full.

## 7. Page layout and pagination

- Paper sizes offered: US Letter, US Legal, A4, A3, Tabloid (portrait).
- `compute_grid` picks the largest of 3×3 → 3×2/2×3 → … → 1×1 that fits the
  paper with a 1 pt white gap (`GAP`) between adjacent cards; the block is
centered. Hard cap `MAX_PER_PAGE = 9`. Paper too small for one card →
RuntimeError.
- Cards fill pages row-major, top-left first; each page holds up to
`cols × rows` cards. PDF metadata: title "MTG Test-Play Cards",
author "mtg-test-card-printer".
- Quantity is unbounded on purpose (any `qty >= 1` is legal), but a per-line
quantity above `QTY_NOTE_ABOVE = 20` adds a "Large quantities requested"
section to the completion dialog so a 30x or 1000x line never silently turns
into a stack of sheets: `30x Sol Ring` + `2x Lightning Bolt` prints 32 faces on
4 pages and names the x30 line.

## 8. Output handling

- Default path: system Downloads folder (`SHGetKnownFolderPath` on Windows so
  OneDrive redirection is honoured; `~/Downloads` elsewhere) /
  `mtg_test_cards.pdf`. Editable in the GUI, or chosen via Browse…
- A missing `.pdf` extension is appended (case-insensitive check).
- Existing files are overwritten without a confirmation prompt, but the
   completion dialog says so: "Note: this replaced an existing file at …" is
   added when the target already existed.
- Completion dialog, in order: path, card count, face count, page count,
  cards/page, then the reminder "Print at 100% scale for the real 63.00 x
  88.00 mm card size." (`CARD_W / MM` × `CARD_H / MM`, so the PDF is never
  accidentally rescaled by the printer), the replaced-file notice, unresolved-
  line problems, `Layout notes:`, "Large quantities requested (expect many
  pages):" and quality warnings grouped by reason - "rules text was longer
  than the card box and was cut off on …" and "these names were too long to
  print in full at the 6 pt floor and were shortened with an ellipsis …".
  Failures surface as an error dialog with the exception text; the Generate
  button re-enables in all cases.

## 9. GUI behaviour

- Window 720×560 (min 640×480); widgets: paper-size combo (**editable** - the
  pick is validated before rendering, a value outside `PAPER_SIZES` raises the
  friendly "'X' is not one of the listed paper sizes" RuntimeError instead of a
  KeyError deep in `build_pdf`), output entry + Browse, card-list text box
  (Consolas 10, pre-filled with an example deck), Generate button, status label.
- Preferences persist: `load_settings()` / `save_settings()` keep `paper_size`
  and `output_path` in `mtg_test_cards_settings.json` next to the script
  (`SETTINGS_PATH`), written after each successful generation and read back at
  startup. Invalid values are dropped on read (unknown paper size, blank path)
  and a corrupt or non-dict file falls back to defaults; only known keys are
  ever rewritten.
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
| Bad output path | Blank field, an existing directory or a missing parent folder each raise their own message before reportlab is reached; a missing `.pdf` extension is appended |
| Corrupt cache line | Line skipped during load |
| Interrupted download | `.part` cleaned up; next run re-downloads |
