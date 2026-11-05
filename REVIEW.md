# MTG Proxy Creator — Review Findings

Review of `mtg_test_cards.py` (read-only; nothing was altered). Items are grouped
by severity. "Verified" means the behaviour was reproduced against the live
Scryfall bulk DB (~38,700 cards) during this review.

## 1. Bugs / functional defects

| # | Item | Detail |
|---|------|--------|
| 1.1 | **Text-box overflow is not bounded** (verified) | `draw_card` shrinks body text to `MIN_TEXT` (6 pt) and then keeps drawing every wrapped line, so cards with long oracle text spill below the `Z_TEXT_BOT` rule into the stat line / across the bottom border. 177 DB cards overflow at the minimum size, e.g. *Baldur's Gate Wilderness* (1489 ch), *Bureaucracy* (774), *Booster Blitz* (763), *Dungeon of the Mad Mage* (707). No line cap, no clipping, no ellipsis, and the completion dialog never warns. |
| 1.2 | **Silent name shadowing from normalization** (verified) | `_norm()` strips all punctuation and spaces, so distinct cards collide: 12 collision pairs exist in the DB (e.g. *Clear the Mind* vs *Clear, the Mind*), and user input is silently redirected (*Goblin Soldier* → *Goblin // Soldier*). First listing wins; no ambiguity warning or candidate list. |
| 1.3 | **Cards whose names normalize to empty are unprintable** (verified) | DB entries named `_____` / `______` are dropped by `load_database` (`if not key: continue`) and can never be resolved. Harmless for most decks, but a user typing such a name gets a generic "no card found" with no hint that the card exists in the DB. |
| 1.4 | **`MIN_TEXT` floor is violated by design** | `fit_name_cost()`'s last resort renders names at 3 pt (below the documented 6 pt floor: "nothing ever renders smaller than this") and truncates them with an ellipsis. Only absurdly long names are affected, but it contradicts the stated invariant; pick one behaviour and document it. |
| 1.5 | **Unbreakable over-long tokens** (edge) | `wrap_text()` never splits a single token wider than `max_width` (it is emitted as its own line past the box), and `draw_justified()` then computes a *negative* word gap for such lines, so words overlap. Rare in MTG oracle text, but unhandled. |
| 1.6 | **Empty / bad output path fails with a raw exception** | If the Output field is blank or a directory, `build_pdf` raises deep inside reportlab and the user sees a "Generation failed" dialog with a traceback-ish message instead of "please pick a valid file". |
| 1.7 | **No lock around DB parsing** (minor race) | `_db_lock` guards only the download. If the preload thread and a Generate click overlap, `load_database()` can run twice concurrently (double ~25 MB parse) and `self.db` is written from two threads. Benign today (identical results) but worth a lock or a single shared future. |
| 1.8 | **Silent overwrite of the default PDF** | Every generation writes to the same `mtg_test_cards.pdf` with no confirmation, timestamp option, or "file replaced" notice. |

## 2. Missing features (candidates)

- **No direct print path** — PDF only; the user must open it and print at 100 % scale. A "Print…" action (or at least a reminder to print at 100 %) would close the loop.
- **No ambiguity assistance** — for item 1.2: show near-matches / all cards sharing the normalized key when resolution is not exact on the raw name.
- **Per-card quality warnings are invisible** — overflow (1.1), 3 pt names (1.4) and truncation should surface in the completion dialog or status bar (e.g. "2 cards rendered below minimum size").
- **No settings persistence** — paper size and output path reset to defaults on every launch.
- **No list import/export** — drag-and-drop of a `.txt` deck file, or saving the entered list; only manual typing/pasting into the text box.
- **No sanity cap on quantity** — `1000x black lotus` produces ~112 pages with no warning.
- **Mana colors are flattened by design** (`{1}{U}` → `1U`, `{W}/{U}` → `W/U`) — fine for B/W printing, but a legend or colored-cost annotation option would help identify mana requirements at the table.
- **No deck metadata in the PDF** (deck name, date) beyond the fixed title "MTG Test-Play Cards".

## 3. Edge cases to confirm / document (mostly OK as-is)

- Quantity formats accepted: `2x name`, `2 x name`, `2 * name`, `2 × name`, bare name = 1, bullet/`-` prefixes stripped. **Not** accepted: `2xname` (no space after the letter) — it is treated as a card name and fails to resolve. Document the grammar.
- `0x name` → friendly "quantity must be at least 1" error (handled).
- Case-insensitive matching, diacritic-stripped matching (`Ætherling` ↔ `aetherling`) — both sides normalize identically, verified working.
- Star P/T (`*/*`, `*/5`) and loyalty fallback render correctly (verified); cards with **both** P/T and loyalty show P/T only — document as intended precedence.
- Zero-cost `{0}` → `0`; `X` costs pass through unchanged — fine.
- Very small paper: `compute_grid` raises a clear RuntimeError for paper that can't fit one card — OK, but the GUI surfaces it as a generic failure dialog; acceptable.
- Offline operation: a local `oracle-cards*.jsonl.gz` next to the script is reused without network (verified) — good; keep this behaviour documented.

## 4. Leftover files to remove (test/review artifacts)

Safe to delete — none are referenced by `mtg_test_cards.py`:

- **Notes:** `analysis_notes.txt`, `review_report.txt`
- **Throwaway scripts:** `inspect_bulk.py`, `layout_test.py`, `probe_scryfall.py`, `studio_exec_mhefm6az.py`, `test_power_nine.py`
- **Test PDFs:** `bug_pages.pdf`, `creature_check.pdf`, `current_state.pdf`, `glyph_test.pdf`, `glyph_test2.pdf`, `layout_test.pdf`, `minus_test.pdf`, `power_nine_test.pdf`, `test_borders.pdf`, `test_cards.pdf`, `test_type_center.pdf`
- **Test images/text:** `_after.png`, `_before.png`, `_full_readout.txt`, `_koth.png`, `creature_check.png`, `current_state.png`, `layout_after.png`, `layout_jace.png`, `layout_test.png`, `preview.png`, `preview_full.png`, `pw_check.png`, `test_borders.png`, `test_borders_full.png`, `test_card.png`, `test_card_zoom.png`, `test_cards_preview.png`, `test_type_band.png`, `test_type_center.png`, `zoom_after.png`, `zoom_bolt.png`, `zoom_jace.png`, `zoom_lotus.png`
- **Caches/temp (optional):** `__pycache__/`, `unsloth-tmp/`, empty `Downloads/` dir. Keep one DB copy for offline use: `scryfall_oracle_cache.jsonl.gz` is the script's cache path — delete `oracle-cards.jsonl.gz` if you want, or both (they re-download on demand).
- **Keep:** `mtg_test_cards.py`, `vendor/` (bundled reportlab fallback referenced in the docstring), and this pair of documents (`REVIEW.md`, `SPEC.md`).

No stray output was found in the real system Downloads folder (`%USERPROFILE%\Downloads\mtg_test_cards.pdf` does not exist).
