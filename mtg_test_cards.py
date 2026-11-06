#!/usr/bin/env python3
"""
MTG Test-Play Card Printer
==========================
Type or paste a list of cards with quantities, e.g.:

    2x black lotus
    1x ancestral recall
    2x mox jet

Cards are looked up in the Scryfall bulk oracle database (downloaded once,
cached next to this script) and rendered as simple black-and-white test-play
cards at 100% scale (63 x 88 mm). Each card face has a solid black border
right at the edge, like a real Magic card; the body text is left-justified
and vertically centered in its box. Cards are tiled up to 9
per page, adjacent with only a 1 pt white line between them as a cutting
guide. Output is a PDF.

Dependencies: Python 3.8+ and reportlab (a pure-Python copy is bundled in
./vendor if you don't have it installed).
"""

import difflib
import glob
import gzip
import json
import os
import re
import shutil
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# ---------------------------------------------------------------------------
# reportlab: use the installed copy if available, otherwise the vendored one
# ---------------------------------------------------------------------------
try:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfgen import canvas as _rl_canvas
except ImportError:
    _vendor = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vendor')
    if os.path.isdir(_vendor) and _vendor not in sys.path:
        sys.path.insert(0, _vendor)
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfgen import canvas as _rl_canvas
    except ImportError:
        sys.stderr.write(
            "ERROR: the 'reportlab' package is required but was not found.\n"
            "Install it with:  pip install reportlab\n")
        raise

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MM = 72.0 / 25.4                      # points per millimetre
CARD_W = 63 * MM                      # Magic card width  (178.4 pt)
CARD_H = 88 * MM                      # Magic card height (249.3 pt)
MAX_PER_PAGE = 9                      # hard cap: 3 x 3 grid
QTY_NOTE_ABOVE = 20                   # warn above this per-line quantity

PAPER_SIZES = {
    'US Letter (8.5 x 11 in)': (612.0, 792.0),
    'US Legal (8.5 x 14 in)': (612.0, 1008.0),
    'A4 (210 x 297 mm)': (595.28, 841.89),
    'A3 (297 x 420 mm)': (841.89, 1190.55),
    'Tabloid (11 x 17 in)': (792.0, 1224.0),
}

# Scryfall requires BOTH a User-Agent and an Accept header on every request,
# otherwise it answers HTTP 400 Bad Request.
SCRYFALL_HEADERS = {
    'User-Agent': 'mtg-test-card-printer/1.0 (personal test-play tool)',
    'Accept': 'application/json',
}
BULK_LIST_URL = 'https://api.scryfall.com/bulk-data'

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(HERE, 'scryfall_oracle_cache.jsonl.gz')
SETTINGS_PATH = os.path.join(HERE, 'mtg_test_cards_settings.json')


def downloads_dir():
    """The system Downloads folder.

    On Windows this asks the shell (SHGetKnownFolderPath) so OneDrive
    redirection is honoured; elsewhere it falls back to ~/Downloads.
    """
    if sys.platform == 'win32':
        try:
            import ctypes
            from ctypes import wintypes

            class _GUID(ctypes.Structure):
                _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                            ('Data3', wintypes.WORD), ('Data4', ctypes.c_byte * 8)]

            # FOLDERID_Downloads = {374DE290-123F-4565-9164-39C4925E467B}
            folderid = _GUID(0x374DE290, 0x123F, 0x4565,
                             bytes.fromhex('916439C4925E467B'))

            shell = ctypes.windll.shell32
            shell.SHGetKnownFolderPath.argtypes = [
                ctypes.POINTER(_GUID), wintypes.DWORD, wintypes.HANDLE,
                ctypes.POINTER(wintypes.LPWSTR)]
            shell.SHGetKnownFolderPath.restype = wintypes.HRESULT
            buf = wintypes.LPWSTR()
            if (shell.SHGetKnownFolderPath(folderid, 0, None,
                                           ctypes.byref(buf)) == 0
                    and buf.value):
                path = buf.value
                ctypes.windll.ole32.CoTaskMemFree(buf)
                return path
        except Exception:
            pass
    path = os.path.join(os.path.expanduser('~'), 'Downloads')
    os.makedirs(path, exist_ok=True)
    return path

# Font sizes are MAXIMA: every text element renders at its maximum size and
# scales down (never below MIN_TEXT) only when the card face has no room.
# Sizes were scaled back a notch from the old layout so everything sits
# comfortably between the frame, the inner border and the rules.
NAME_FONT, NAME_SIZE = 'Helvetica-Bold', 10.5
COST_FONT, COST_SIZE = 'Helvetica', 9.5
TYPE_FONT, TYPE_SIZE = 'Helvetica-Oblique', 9.0
TEXT_FONT, TEXT_SIZE = 'Helvetica', 12.5
STAT_FONT, STAT_SIZE = 'Helvetica-Bold', 11.5
MIN_TEXT = 6.0                        # nothing ever renders smaller than this
FIT_EPS = 1e-6                        # pt, float-noise slack at an exact fit

# Card face layout (mm from the top edge of the card). Like a real Magic
# card, each face has a solid black border right at its edge; the zones
# below tile the area inside it: name / art / type line / text box / stat.
BORDER_W = 7         # pt, solid black border at the card edge
# Outer corners are square; only the inner corners of the frame are rounded.
INNER_R = 1.5                # mm, rounded inner corners of the black border
MARGIN_MM = 4.0        # card edge -> content boundary (rules / text)
GAP = 0.5              # pt: thinnest white line separating adjacent cards

# Zone proportions follow a real Magic card face: a tall art box, a slim
# type-line band, and a compact stat line at the bottom.
Z_NAME_LINE = 8.0      # rule under the name row
NAME_DROP = 1.5        # pt: nudge the name row down away from the border
Z_TYPE_TOP = 46.0      # rule above the type row (bottom of the art area)
Z_TYPE_BOT = 52.2      # rule below the type row (top of the text box)
TYPE_DROP = 1.0        # pt: nudge the type line down a hair
Z_TEXT_BOT = 79.4      # rule below the text box (top of the stat zone)
# stat zone: Z_TEXT_BOT .. (88 - MARGIN_MM)
PAD_X = (MARGIN_MM + 0.4) * MM   # horizontal padding for text inside the border
LINE_W = 1.0           # pt, weight of the separating rules
FACE_TAG_GAP = 1.5     # pt: white above the FRONT / BACK tag, under the name rule

# ---------------------------------------------------------------------------
# Card layouts (https://scryfall.com/docs/api/layouts)
#
# `layout` - NOT the mere presence of card_faces - decides how much physical
# cardstock a card needs and what frame its text sits in.  Scryfall uses
# card_faces for several one-sided layouts (split, flip, adventure, prepare),
# so counting faces is not a valid way to decide how many cards to print.
#
# Only these layouts are physically double sided -> two printed items:
DOUBLE_SIDED_LAYOUTS = ('transform', 'modal_dfc', 'double_faced_token',
                        'art_series', 'reversible_card')
# Two spells on ONE face, the lower half printed upside down (real split cards
# and flip cards): one printed item with a custom half-face frame.
HALF_FACE_LAYOUTS = ('split', 'flip')
# One face carrying an inset sub-panel in the text box (Adventure / Prepared
# spell): one printed item, main spell plus a small framed sub-box.
INSET_PANEL_LAYOUTS = ('adventure', 'prepare')
# One face whose rules text is printed as stacked sub-boxes inside the normal
# text box (levelers, Classes, Cases, Sagas): one printed item, custom frame.
STACKED_PANEL_LAYOUTS = ('leveler', 'class', 'case', 'saga')

# Corner labels for single-face frames that a plain card would misread.
LAYOUT_MARKERS = {
    'token': 'TOKEN', 'emblem': 'EMBLEM', 'planar': 'PLANE',
    'scheme': 'SCHEME', 'vanguard': 'VANGUARD', 'front_card': 'DECK INDICATOR',
    'host': 'HOST', 'augment': 'AUGMENT', 'art_series': 'ART SERIES',
    'prototype': 'PROTOTYPE', 'mutate': 'MUTATE', 'meld': 'MELD PART',
}

# Geometry of the custom frames (mm / font scale).
HALF_DIVIDER_MM = 44.0   # divider under the upper half (just above the type band)
HALF_SCALE = 0.62        # font scale inside a half face
INSET_W_MM, INSET_H_MM = 34.0, 18.0   # Adventure / Prepared sub-panel
INSET_SCALE = 0.70                   # font scale inside the sub-panel
PANEL_PAD_MM = 1.2                   # preferred white above a stacked-panel divider
MIN_PANEL_PAD_MM = 0.5               # ...shrunk to this only if text will not fit
PANEL_HDR_SCALE = 0.80               # font scale of a panel header ("I, II -")
PANEL_HDR_EXTRA_MM = 0.4             # white under a panel header
PANEL_BODY_PAD_MM = 0.3              # padding inside a stacked-panel body

# Zone offsets for the scaled-down faces of split/flip cards, in mm measured
# from the top of the half (real split halves keep a full-size name row and a
# short text box, so these are absolute rather than a scale of Z_*).
MINI_NAME_LINE = 8.0     # rule under the name row
MINI_TYPE_TOP = 23.5     # rule above the type band
MINI_TYPE_BOT = 29.5     # rule below the type band (top of the half's text box)
MINI_TEXT_BOT = 1.5      # bottom rule, measured up from the half's bottom edge


def load_settings():
    """Saved GUI preferences ({'paper_size':…, 'output_path':…}) or {} if absent.

    Stored as JSON next to the script (SETTINGS_PATH). Values that no longer
    make sense are dropped - a paper size that is not in PAPER_SIZES, a blank
    path - so an edited or half-written file can never break startup. A broken
    file simply means defaults.
    """
    try:
        with open(SETTINGS_PATH, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    prefs = {}
    paper = data.get('paper_size')
    if isinstance(paper, str) and paper in PAPER_SIZES:
        prefs['paper_size'] = paper
    path = data.get('output_path')
    if isinstance(path, str) and path.strip():
        prefs['output_path'] = path.strip()
    return prefs


def save_settings(paper_size=None, output_path=None):
    """Remember GUI preferences next to the script (best effort, never raises)."""
    data = load_settings()   # only valid, known keys survive a rewrite
    if paper_size in PAPER_SIZES:
        data['paper_size'] = paper_size
    if output_path and output_path.strip():
        data['output_path'] = output_path.strip()
    try:
        with open(SETTINGS_PATH, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, indent=1)
    except OSError:
        pass  # a read-only folder must not break generation


# ---------------------------------------------------------------------------
# Scryfall bulk oracle database (download once, cache locally)
# ---------------------------------------------------------------------------
def _http_get(url, timeout=90):
    import urllib.request
    req = urllib.request.Request(url, headers=SCRYFALL_HEADERS)
    return urllib.request.urlopen(req, timeout=timeout).read()


def bulk_file_url():
    """Ask Scryfall for today's oracle-cards bulk file URL."""
    data = json.loads(_http_get(BULK_LIST_URL))
    for item in data.get('data', []):
        if item.get('type') == 'oracle_cards':
            return item['jsonl_download_uri']
    raise RuntimeError("Scryfall's bulk-data list has no 'oracle_cards' entry")


_db_lock = threading.Lock()
_parse_lock = threading.Lock()   # serializes JSONL parsing (preload vs. worker)


def _find_local_bulk_file():
    """Find an already-downloaded oracle-cards bulk file next to the script."""
    cands = [CACHE_PATH] + sorted(glob.glob(os.path.join(HERE, 'oracle-cards*.jsonl.gz')))
    for p in cands:
        try:
            if os.path.isfile(p) and os.path.getsize(p) > 100_000:
                return p
        except OSError:
            continue
    return None


def ensure_database(progress=None):
    """Make sure the cached oracle database exists. Returns cache path.

    progress(done, total_bytes) may be called while downloading.
    """
    if os.path.isfile(CACHE_PATH) and os.path.getsize(CACHE_PATH) > 100_000:
        return CACHE_PATH
    with _db_lock:  # avoid two threads downloading at once
        if os.path.isfile(CACHE_PATH) and os.path.getsize(CACHE_PATH) > 100_000:
            return CACHE_PATH
        # Reuse a bulk file already sitting next to the script instead of
        # hitting the network (this also makes the app work offline).
        local = _find_local_bulk_file()
        if local and os.path.abspath(local) != os.path.abspath(CACHE_PATH):
            shutil.copyfile(local, CACHE_PATH)
            return CACHE_PATH
        import urllib.request
        try:
            url = bulk_file_url()
        except Exception as e:
            raise RuntimeError(
                f'Could not reach Scryfall ({e}).\n\n'
                'Check your internet connection (a corporate proxy can block it),\n'
                'or place an oracle-cards JSONL file from data.scryfall.io\n'
                'next to this script and restart.') from e
        tmp = CACHE_PATH + '.part'
        try:
            req = urllib.request.Request(url, headers=SCRYFALL_HEADERS)
            with urllib.request.urlopen(req, timeout=300) as resp, \
                    open(tmp, 'wb') as out:
                total = int(resp.headers.get('Content-Length') or 0)
                done = 0
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
            os.replace(tmp, CACHE_PATH)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return CACHE_PATH


def _norm(name):
    """Normalization used for matching: lowercase alphanumerics only, so
    'Urborg, Tomb of Yawgmoth' matches 'urborg tomb of yawgmoth'."""
    return re.sub(r'[^a-z0-9]', '', name.lower())


def _real_cards(cands):
    """Drop art (art-series) listings from a list of candidate cards.

    A typed name never means an art card: 'Hobgoblin' should not resolve to
    the Kaldheim art print 'Minion of the Mighty // Kobold', and typing a
    back face ('Ashen Reaper') must find the real Invasion of Azgol, not its
    art-series twin. Art prints stay reachable only through an exact full
    typing ('Minion of the Mighty // Kobold'), which is what resolve() does.
    """
    return [c for c in cands if not c.get('art_card')]


def load_database(path=CACHE_PATH):
    """Load the JSONL cache into {normalized_name: [card, ...]}.

    All listings under one name are kept (first listing first) so resolve()
    can warn when a typed name matches several different cards. Names whose
    normalized form is empty (e.g. '_____') fall back to their casefolded
    raw name as the key, so they stay findable by exact typing.
    """
    db = {}
    with gzip.open(path, 'rt', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                c = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = (c.get('name') or '').strip()
            if not name:
                continue
            key = _norm(name) or name.casefold()
            db.setdefault(key, []).append({
                'name': name,
                'mana_cost': c.get('mana_cost') or '',
                'type_line': c.get('type_line') or (c.get('card_types') and
                                                    ', '.join(c['card_types'])) or '',
                'power': c.get('power'),
                'toughness': c.get('toughness'),
                'loyalty': c.get('loyalty'),
                # Battle-type cards print a defense value instead of P/T.
                'defense': c.get('defense'),
                'oracle_text': (c.get('oracle_text') or '').strip(),
                'layout': c.get('layout') or 'normal',
                # Art (art-series) printings: art + name only, no cost,
                # type line or rules text. They are not playable cards,
                # so name matching must never pick one (they carry the
                # same names as real cards twice, 'X // X').
                'art_card': (c.get('layout') == 'art_series' or
                             c.get('frame') == 'art' or
                             c.get('set_type') == 'art_series'),
                # Two-faced cards (split, flip, transform, adventure,
                # prepare, art_series, two-faced tokens) carry a full copy
                # of every field per face; single-face cards: empty list.
                'faces': [
                    {
                        'name': f.get('name') or name,
                        'mana_cost': f.get('mana_cost') or '',
                        'type_line': (f.get('type_line') or
                                      (f.get('card_types') and
                                       ', '.join(f['card_types'])) or ''),
                        'power': f.get('power'),
                        'toughness': f.get('toughness'),
                        'loyalty': f.get('loyalty'),
                        'defense': f.get('defense'),
                        'oracle_text': (f.get('oracle_text') or '').strip(),
                        # Reversible cards nest two unrelated printings, each
                        # with its own layout.
                        'layout': f.get('layout') or '',
                    }
                    for f in (c.get('card_faces') or [])
                ],
                # Melds: the name of the melded result. Parts and results
                # both list it in all_parts; a result lists *itself*.
                'meld_result': next(
                    (p['name'] for p in (c.get('all_parts') or [])
                     if p.get('component') == 'meld_result'), None),
            })
    return db


# Face-name lookup index, cached per database object: {face_key: [card, ...]}
# where each card is the *full* multi-face record. Built lazily on first use.
# Stores (db, idx) so a recycled id() can never serve a stale index.
_FACE_INDEX = {}


def _face_index(db):
    """Map every face name of a multi-face card to its full card record.

    Art-series printings are skipped: they reuse real card names as faces,
    and being loaded first they would otherwise win the lookup and print a
    blank art card instead of the real spell.
    """
    hit = _FACE_INDEX.get(id(db))
    if hit is not None and hit[0] is db:
        return hit[1]
    idx = {}
    for cands in db.values():
        for card in cands:
            if card.get('art_card'):
                continue
            for face in card.get('faces') or []:
                key = _norm(face['name']) or face['name'].casefold()
                idx.setdefault(key, []).append(card)
    _FACE_INDEX[id(db)] = (db, idx)
    return idx


def _face_lookup(db, name):
    """Find multi-face cards by one of their face names.

    Lets the user type just the primary (front) side of a flip/transform/
split card and still get the whole card printed. Exact face-name matches
    win over normalized ones, mirroring the main lookup's preference.
    """
    idx = _face_index(db)
    cands = idx.get(name.casefold())
    if cands is None:
        cands = idx.get(_norm(name))
    return list(cands) if cands else []


SUGGEST_MAX = 4                     # spell suggestions offered per bad name
SUGGEST_CUTOFF = 0.75               # difflib similarity for a near match
_NAME_INDEX = {}                    # id(db) -> (db, ([keys], {key: name}))


def _name_index(db):
    """Cache ([normalized keys], {key: display name}) for spell suggestions.

    Built lazily once per database object, like _face_index; the stored db
    reference means a recycled id() can never serve a stale index.
    """
    hit = _NAME_INDEX.get(id(db))
    if hit is not None and hit[0] is db:
        return hit[1]
    names = {}                      # art-only keys excluded, see _real_cards
    for key, cands in db.items():
        real = _real_cards(cands)
        if real:
            names[key] = real[0]['name']
    idx = (list(names), names)
    _NAME_INDEX[id(db)] = (db, idx)
    return idx


def _suggest_names(db, name, n=SUGGEST_MAX):
    """Up to *n* card names closest to *name*, for a line that matched nothing.

    difflib over the ~38,700 normalized keys costs ~0.1 s per query, but only
    runs on the error path (a name that neither the main index nor any face
    name resolved), so a normal deck list never pays it.
    """
    key = _norm(name) or name.casefold()
    if len(key) < 3:
        return []      # punctuation-only names have no meaningful near match
    keys, names = _name_index(db)
    out = []
    for hit in difflib.get_close_matches(key, keys, n=n,
                                        cutoff=SUGGEST_CUTOFF):
        disp = names.get(hit)
        if disp and disp not in out:
            out.append(disp)
    return out


def _suggest_text(db, name):
    """' Did you mean …?' tail for the 'no card found' message ('' if none)."""
    tips = _suggest_names(db, name)
    if not tips:
        return ''
    shown = ', '.join('\u201c%s\u201d' % t for t in tips)
    return f' Did you mean {shown}?'


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------
QTY_RE = re.compile(r'^\s*(\d+)\s*(?:[xX*\u00d7]\s+|\s+)(.+)$')


def parse_input(text):
    """Parse lines like '2x black lotus' into [(lineno, qty, name), ...].

    A bare card name (no quantity) counts as 1. Returns (entries, errors);
    each entry carries its input line number so later error messages can
    point at the offending line.
    """
    entries, errors = [], []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip().strip('-*\u2022 ')
        if not line:
            continue
        m = QTY_RE.match(line)
        if m:
            qty, name = int(m.group(1)), m.group(2).strip()
        else:
            qty, name = 1, line
        name = re.sub(r'\s+', ' ', name).strip(' .;:,')
        if not name:
            errors.append(f'line {lineno}: no card name found')
            continue
        if qty < 1:
            errors.append(f'line {lineno}: quantity must be at least 1')
            continue
        entries.append((lineno, qty, name))
    return entries, errors


def resolve(entries, db):
    """Match parsed entries against the database.

    entries is a list of (lineno, qty, name) from parse_input(). Returns
    (cards, problems) where cards is a flat list of card dicts in deck order
    and problems is a list of human-readable strings identifying the
    offending input line: hard errors for names that match nothing, and
    notes when a typed name matched several different cards.

    Matching prefers an exact (case-insensitive) name over the normalized
    key, so typing 'Goblin Soldier' never silently picks up 'Goblin //
    Soldier'; the first listing wins when only the normalized form matches.

    If no card carries the typed name at all, the face names of
    multi-face cards are tried: typing just "Ulvenwald Captive" (the front)
    still finds and prints the whole card. A standalone card with that
    name always wins over a same-named face.

    Art (art-series) printings are ignored by matching: they carry no cost,
    type line or rules text and duplicate real card names, so 'Hobgoblin'
    finds nothing rather than the art twin of 'Minion of the Mighty', and a
    back-face name such as 'Ashen Reaper' finds Invasion of Azgol. An art
    print is still printable when typed by its exact full name.
    """
    cards, problems = [], []
    for lineno, qty, name in entries:
        cands = db.get(_norm(name)) or db.get(name.casefold())
        if cands:
            real = _real_cards(cands)
            if not real:
                # Only art prints carry this name: allow it on an exact
                # full typing ('Hobgoblin Bandit Lord // Hobgoblin') so the
                # art frame stays reachable, but never on a bare name.
                typed = name.casefold()
                real = [c for c in cands if c['name'].casefold() == typed]
            cands = real
        if not cands:
            # No card of this name: try matching one face of a
            # multi-face card (front-only names for flip/transform/split).
            cands = _face_lookup(db, name)
        if not cands:
            problems.append(
                f'line {lineno}: \u201c{name}\u201d (x{qty}): no card with this '
                'name was found. Please check the spelling.' +
                _suggest_text(db, name))
            continue
        typed = name.casefold()
        hit = next((c for c in cands if c['name'].casefold() == typed), None)
        if hit is None:
            hit = cands[0]
            if len(cands) > 1:
                shown = ', '.join('\u201c%s\u201d' % c['name'] for c in cands[:3])
                more = f' (+{len(cands) - 3} more)' if len(cands) > 3 else ''
                problems.append(
                    f'line {lineno}: \u201c{name}\u201d matches several cards; '
                    f'used \u201c{hit["name"]}\u201d. Also found: {shown}{more}.')
        for _ in range(qty):
            cards.append(hit)
    return cards, problems


# ---------------------------------------------------------------------------
# Card layout expansion (split / flip / transform / token / meld / ...)
# ---------------------------------------------------------------------------
def _base_item(src):
    """Copy the printable fields of a card/face record into a new item."""
    return {k: src.get(k) for k in ('name', 'mana_cost', 'type_line', 'power',
                                    'toughness', 'loyalty', 'defense',
                                    'layout', 'oracle_text')}


_PANEL_HEADERS = {          # regexes marking the start of a stacked sub-box
    'leveler': re.compile(r'^LEVEL\b'),
    'class': re.compile(r'^(?:\{[^}]*\}\s*)+:\s*Level\s+\d|^Level\s+\d'),
    'case': re.compile(r'^(To solve|Solved)\s*\u2014'),
    # Sagas also print merged chapters ("I, II \u2014 ..."): one panel each.
    'saga': re.compile(r'^(?:[IVX]{1,3}(?:\s*,\s*[IVX]{1,3})*)\s*\u2014'),
}


# Panel headings are printed bold and on their own line, so only the SHORT
# label part of a matching line may go in the heading: saga chapters ("I, II
# — ..."), Cases ("To solve — ...") and Class levels carry rules text on the
# same source line, which must fold into that panel's wrapped body.
_PANEL_LABEL = {
    'saga': re.compile(r'^((?:[IVX]{1,3}(?:\s*,\s*[IVX]{1,3})*)\s*\u2014)'
                       r'\s*(.*)$', re.DOTALL),
    'case': re.compile(r'^((?:To solve|Solved)\s*\u2014)\s*(.*)$', re.DOTALL),
}


def split_panels(text, kind):
    """Split one card's rules text into the stacked sub-boxes a real card prints.

    Returns [(label, body), ...]; an empty label means the unlabelled intro
    panel.  This only decides how the standard text box gets divided - the
    standard text box itself (fonts, wrapping, justification) is untouched.
    """
    hdr = _PANEL_HEADERS.get(kind)
    if not hdr or not text:
        return [('', text or '')]
    out, label, body = [], '', []
    for ln in text.split('\n'):
        s = ln.strip()
        if hdr.match(s):
            if label or body:
                out.append((label, '\n'.join(body).strip()))
            lab, rest = _panel_label(kind, s)
            label, body = lab, ([rest] if rest else [])
        else:
            body.append(ln)
    if label or body:
        out.append((label, '\n'.join(body).strip()))
    panels = [(a, b) for a, b in out if a or b]
    return panels or [('', text)]


def _panel_label(kind, line):
    """Split a panel's first source line into (bold label, remaining text).

    Lines that are entirely a label (LEVEL 2+, {3}{U}: Level 3) keep their
    whole text in the heading; lines with an em-dash label keep only the label
    there and return the rest so it can be wrapped as body text.
    """
    m = _PANEL_LABEL.get(kind)
    if m:
        g = m.match(line)
        if g:
            return g.group(1).strip(), g.group(2).strip()
    return line, ''


def expand_items(cards, db, notes=None):
    """Expand resolved cards into the flat list of card faces to print.

    The Scryfall `layout` field decides everything (see docs/api/layouts);
    counting card_faces does NOT: split, flip, adventure and prepare cards
    carry two card_faces but live on ONE piece of cardstock.

    * DOUBLE_SIDED_LAYOUTS (transform, modal_dfc, double_faced_token,
      art_series, reversible_card) - two physical sides -> the ONLY layouts
      that print two items, tagged FRONT / BACK.
    * HALF_FACE_LAYOUTS (split, flip) - two spells on one face, the lower one
      printed upside down: ONE item with a custom half-face frame.
    * INSET_PANEL_LAYOUTS (adventure, prepare) - main spell plus an inset
      sub-panel for the Adventure/Prepared spell: ONE item, custom frame.
    * STACKED_PANEL_LAYOUTS (leveler, class, case, saga) - one face whose
      rules text is printed as stacked sub-boxes: ONE item, custom frame.
    * meld - a meld part carries the melded result on its physical back, so
      part + result are two items; a meld result typed alone prints once and
      the auto-added result prints only once even when both halves of the
      meld pair are typed, or when the same card was also typed directly.
    * Everything else (normal, mutate, prototype, token, emblem, planar,
      scheme, vanguard, host, augment, front_card) - ONE item in the standard
      frame, with a corner marker where the frame would otherwise be ambiguous.

    `notes` (optional list) collects remarks about cards whose data cannot be
    reproduced faithfully (more than two halves, a missing meld result, ...).
    """
    items = []
    meld_results = set()      # results already printed by an earlier part
    typed_names = {_norm(c['name']) for c in cards}
    for card in cards:
        layout = card.get('layout') or 'normal'
        faces = card.get('faces') or []

        if layout in DOUBLE_SIDED_LAYOUTS and len(faces) >= 2:
            marker = ('TOKEN' if layout == 'double_faced_token' else
                      'ART SERIES' if layout == 'art_series' else None)
            for i, face in enumerate(faces[:2]):
                item = _base_item(face)
                if layout == 'art_series':
                    # Art series sides are art + name only: no type line.
                    item['type_line'] = ''
                if marker:
                    item['marker'] = marker
                item['face_tag'] = 'FRONT' if i == 0 else 'BACK'
                items.append(item)
            if len(faces) > 2 and notes is not None:
                notes.append(
                    f'\u201c{card["name"]}\u201d nests {len(faces)} faces but a '
                    'test card has two sides; only the first two are printed.')
            continue

        if layout in HALF_FACE_LAYOUTS and len(faces) >= 2:
            if len(faces) > 2:
                # The authentic frame puts exactly two spells on one face; a
                # joke card with more halves would be unreadable crammed in,
                # so every spell gets its own legible card instead of the
                # extras being dropped (a playtest deck still needs them all).
                prefix = 'SPLIT' if layout == 'split' else 'FLIP'
                for i, face in enumerate(faces):
                    item = _base_item(face)
                    item['marker'] = f'{prefix} {i + 1}/{len(faces)}'
                    item['face_tag'] = f'{i + 1}/{len(faces)}'
                    items.append(item)
                if notes is not None:
                    notes.append(
                        f'\u201c{card["name"]}\u201d has {len(faces)} halves; the '
                        'two-halves frame cannot hold them, so each spell prints '
                        'as its own card.')
                continue
            item = _base_item(card)
            item['frame'] = 'half'
            item['parts'] = [_base_item(f) for f in faces[:2]]
            items.append(item)
            continue

        if layout in INSET_PANEL_LAYOUTS and len(faces) >= 2:
            item = _base_item(faces[0])            # main spell
            item['inset'] = _base_item(faces[1])   # Adventure / Prepared panel
            items.append(item)
            continue

        if layout in STACKED_PANEL_LAYOUTS:
            item = _base_item(card)
            item['frame'] = 'panels'
            item['panels'] = split_panels(item.get('oracle_text'), layout)
            items.append(item)
            continue

        item = _base_item(card)
        if layout == 'token':
            item['marker'] = 'TOKEN'
        elif layout == 'front_card':
            item['marker'] = 'DECK INDICATOR'
        elif layout == 'meld':
            result_name = card.get('meld_result')
            if result_name and result_name != card['name']:
                # This is a meld part: its physical back is the result,
                # so print the part first, then the result (front -> back).
                item['marker'] = 'MELD PART'
                items.append(item)
                cands = _real_cards(db.get(_norm(result_name)) or
                                    db.get(result_name.casefold()))
                if cands and _norm(result_name) not in meld_results | typed_names:
                    # Both halves of a meld pair name the same result; one
                    # physical copy of it is enough.  An explicitly typed
                    # result card is untouched, so typing it twice still
                    # prints two copies.
                    res = _base_item(cands[0])
                    res['marker'] = 'MELD RESULT'
                    items.append(res)
                    meld_results.add(_norm(result_name))
                elif not cands and notes is not None:
                    notes.append(
                        f'\u201c{card["name"]}\u201d melds with \u201c{result_name}\u201d, '
                        'which was not found in the database.')
                continue
            item['marker'] = 'MELD RESULT'
        elif LAYOUT_MARKERS.get(layout):
            item['marker'] = LAYOUT_MARKERS[layout]
        items.append(item)
    return items


# ---------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------
def clean_mana_cost(cost):
    """'{1}{U}{B}' -> '1UB'  (black-and-white friendly)."""
    cost = cost.replace('{', '').replace('}', '')
    return re.sub(r'\s+', '', cost)


def wrap_text(text, font, size, max_width):
    """Word-wrap *text* to fit *max_width*; returns list of lines.

    A single token wider than the box (effectively impossible in MTG oracle
    text, but possible in a hand-typed name) is hard-split so no line ever
    overflows the card face.
    """
    def split_overlong(w):
        if pdfmetrics.stringWidth(w, font, size) <= max_width:
            return [w]
        parts, chunk = [], ''
        for ch in w:
            if chunk and pdfmetrics.stringWidth(chunk + ch, font, size) > max_width:
                parts.append(chunk)
                chunk = ch
            else:
                chunk += ch
        if chunk:
            parts.append(chunk)
        return parts or ['']

    lines = []
    for paragraph in text.split('\n'):
        words = [p for w in paragraph.split() for p in split_overlong(w)]
        if not words:
            lines.append('')
            continue
        cur = ''
        for w in words:
            trial = w if not cur else cur + ' ' + w
            if pdfmetrics.stringWidth(trial, font, size) <= max_width or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def compute_grid(paper_w_pt, paper_h_pt):
    """Lay out up to 3x3 cards on the page, centered.

    Adjacent cards are separated by the 1 pt GAP white line (cutting guide).
    Returns (cols, rows, ox, oy) where (ox, oy) is the bottom-left corner of
    the card block in points.
    """
    for cols in (3, 2, 1):
        for rows in (3, 2, 1):
            if cols * rows > MAX_PER_PAGE:
                continue
            block_w = cols * CARD_W + (cols - 1) * GAP
            block_h = rows * CARD_H + (rows - 1) * GAP
            if block_w <= paper_w_pt and block_h <= paper_h_pt:
                ox = (paper_w_pt - block_w) / 2.0
                oy = (paper_h_pt - block_h) / 2.0
                return cols, rows, ox, oy
    raise RuntimeError('Paper size is too small to fit even one card')


def fit_font_size(text, font, max_size, min_size, width, step=0.5):
    """Largest size (max_size down to min_size) at which *text* fits *width*.

    If the text is wider than *width* even at *min_size*, min_size is
    returned so nothing ever renders below the floor.
    """
    size = float(max_size)
    while size > min_size and pdfmetrics.stringWidth(text, font, size) > width:
        size -= step
    return max(size, min_size)


def truncate_to_fit(text, font, size, width):
    """If *text* is wider than *width* at *size*, shorten it with an ellipsis."""
    if pdfmetrics.stringWidth(text, font, size) <= width:
        return text
    ell = '\u2026'
    while text and \
            pdfmetrics.stringWidth(text + ell, font, size) > width:
        text = text[:-1]
    out = text.rstrip() + ell
    return out if pdfmetrics.stringWidth(out, font, size) <= width else ell


def fit_name_cost(name, cost, lw):
    """Sizes for a name (left) and mana cost (right) sharing one line.

    The name keeps its maximum size as long as possible; the cost is only
    shrunk when there is not enough room for it at its maximum size, so the
    two can never overlap. Only absurdly long names (a joke card or two)
    need the last resort, and that last resort still respects MIN_TEXT:
    the name stops at 6 pt and is shortened with an ellipsis there, so
    nothing in a name row ever renders below the documented floor.

    Returns (name_size, cost_size_or_None, name_text).
    """
    gap = 2.0 * MM                     # minimum space between name and cost
    if not cost:
        nsize = fit_font_size(name, NAME_FONT, NAME_SIZE, MIN_TEXT, lw)
        return nsize, None, truncate_to_fit(name, NAME_FONT, nsize, lw)
    c_floor_w = pdfmetrics.stringWidth(cost, COST_FONT, MIN_TEXT)
    nsize = float(NAME_SIZE)
    while nsize > MIN_TEXT:
        c_avail = lw - pdfmetrics.stringWidth(name, NAME_FONT, nsize) - gap
        if c_avail >= c_floor_w:
            return (nsize,
                    fit_font_size(cost, COST_FONT, COST_SIZE, MIN_TEXT, c_avail),
                    name)
        nsize -= 0.5
    # last resort at the floor: the cost still needs its minimum room, so the
    # name is shortened instead of shrinking below MIN_TEXT.
    ntext = truncate_to_fit(name, NAME_FONT, MIN_TEXT,
                            max(lw - gap - c_floor_w, 1.0))
    rest = lw - pdfmetrics.stringWidth(ntext, NAME_FONT, MIN_TEXT) - gap
    return (MIN_TEXT,
            fit_font_size(cost, COST_FONT, COST_SIZE, MIN_TEXT, rest), ntext)


def _note_short_name(warnings, name, ntext):
    """Record a name that had to be shortened with an ellipsis."""
    if warnings is not None and ntext != name:
        warnings.append(('name shortened', name))


# ---------------------------------------------------------------------------
# Text blocks and custom frames
#
# draw_text_block is the one standard text-box renderer: word-wrap, shrink to
# fit (never below MIN_TEXT), vertically center, visibly cut off with an
# ellipsis if it still overflows.  Standard cards call it with the standard
# zone bounds, so their text box is exactly as specified in SPEC.md 6.3; the
# custom frames below only hand it smaller sub-boxes of their own.
# ---------------------------------------------------------------------------
def draw_text_block(c, lx, width, box_top, box_bot, text, name,
                    truncated=None, font=TEXT_FONT, max_size=TEXT_SIZE,
                    min_size=MIN_TEXT, pad_mm=1.0):
    """Wrap *text* into the band between *box_top* and *box_bot* (points).

    Shrinks from *max_size* down to *min_size* until the wrapped block fits,
    centers it vertically inside the band and draws every line left-aligned.
    If it still does not fit at *min_size*, only the lines that fit are kept
    and the last one is cut with an ellipsis (a ('rules text cut off',
    *name*) pair is recorded in *truncated*).  Nothing ever renders below
    *min_size* except that cut.
    """
    face = pdfmetrics.getFont(font).face
    asc = face.ascent / 1000.0            # em above baseline (Helvetica .718)
    desc = -face.descent / 1000.0         # em below baseline (.207)
    pad = pad_mm * MM

    def fits(size, lines):
        line_h = size * 1.30
        block = (len(lines) - 1) * line_h + size * (asc + desc)
        # FIT_EPS: a caller that sizes its band from this very measurement
        # lands on the boundary exactly, and float noise would otherwise
        # report a cut-off that is not there.
        return block <= (box_top - box_bot) - 2 * pad + FIT_EPS

    # start at the maximum size; shrink until the wrapped text fits
    tsize = float(max_size)
    lines = wrap_text(text, font, tsize, width)
    while not fits(tsize, lines) and tsize > min_size:
        tsize -= 0.5
        lines = wrap_text(text, font, tsize, width)

    # even at the minimum size the text may overflow (huge rules text):
    # keep only as many lines as fit and visibly cut the last one
    if not fits(tsize, lines):
        line_h0 = tsize * 1.30
        avail = (box_top - box_bot) - 2 * pad
        max_lines = max(int((avail - tsize * (asc + desc)) // line_h0) + 1, 1)
        kept = lines[:max_lines]
        last = truncate_to_fit(kept[-1], font, tsize, width)
        if last == kept[-1]:
            last = kept[-1].rstrip() + '\u2026'   # still too wide: force a cut
        kept[-1] = last
        lines = kept
        if truncated is not None:
            truncated.append(('rules text cut off', name))

    # center the whole block vertically between the two rules
    line_h = tsize * 1.30
    block = (len(lines) - 1) * line_h + tsize * (asc + desc)
    first_baseline = box_top - (box_top - box_bot - block) / 2.0 \
                     - asc * tsize

    c.setFont(font, tsize)
    for i, ln in enumerate(lines):
        ty = first_baseline - i * line_h
        c.drawString(lx, ty, ln)   # left-justified
    return tsize


def draw_mini_face(c, x, y, w, h, data, scale=HALF_SCALE, warnings=None,
                   tag=None):
    """Draw a scaled-down card face inside the rect (x, y, w, h).

    *tag* is an optional corner label ('FRONT' / 'BACK') drawn bottom-left
    inside this half: on a flip card the two halves are different spells and
    only one of them is the front, so a playtester needs that marked.

    Used for the two halves of split/flip cards: no border is drawn here, the
    halves share the one bordered card face and are separated by a rule.  The
    standard zone proportions are kept but scaled to this half's height, and
    fonts use *scale* so they stay legible on a test-play card.
    """
    top = y + h
    h_mm = h / MM
    # Absolute zone offsets from the half's own top edge (a scaled copy of the
    # standard zones collapses to nothing on a 40 mm half).  If the half is
    # unusually short, squeeze the band positions proportionally.
    k = min(1.0, max(h_mm - MINI_TEXT_BOT - 2 * MINI_NAME_LINE,
                     MINI_TYPE_TOP) / (MINI_TYPE_TOP - MINI_NAME_LINE))
    y_name = MINI_NAME_LINE
    y_ttop = MINI_NAME_LINE + (MINI_TYPE_TOP - MINI_NAME_LINE) * k
    y_tbot = MINI_NAME_LINE + (MINI_TYPE_BOT - MINI_NAME_LINE) * k
    y_text_bot = h_mm - max(MINI_TEXT_BOT, 1.0)

    def H(y_mm):
        return top - y_mm * MM

    def rule(y_mm):
        c.setLineWidth(LINE_W)
        c.line(x + MARGIN_MM * MM, H(y_mm), x + w - MARGIN_MM * MM, H(y_mm))

    lx = x + PAD_X
    lw = w - 2 * PAD_X

    # --- name row (name left, mana cost right) -----------------------------
    name = data['name']
    cost = clean_mana_cost(data.get('mana_cost') or '')
    nsize, csize, ntext = fit_name_cost(name, cost, lw)
    _note_short_name(warnings, name, ntext)
    band_top = top - BORDER_W
    band_bot = H(Z_NAME_LINE)
    band_mid = (band_top + band_bot) / 2.0

    def _base(size, font):
        face = pdfmetrics.getFont(font).face
        asc = face.ascent / 1000.0
        desc = -face.descent / 1000.0
        return band_mid - (asc - desc) * size / 2.0 - NAME_DROP

    nsize = min(nsize, NAME_SIZE * scale)
    c.setFont(NAME_FONT, nsize)
    c.drawString(lx, _base(nsize, NAME_FONT), ntext)
    if cost:
        csize = min(csize, COST_SIZE * scale)
        c.setFont(COST_FONT, csize)
        c.drawRightString(x + w - PAD_X, _base(csize, COST_FONT), cost)
    rule(y_name)

    # --- blank art band, then the type line --------------------------------
    rule(y_ttop)
    tline = data.get('type_line') or ''
    if tline:
        tsize = fit_font_size(tline, TYPE_FONT, TYPE_SIZE * scale, MIN_TEXT, lw)
        c.setFont(TYPE_FONT, tsize)
        face = pdfmetrics.getFont(TYPE_FONT).face
        asc = face.ascent / 1000.0
        desc = -face.descent / 1000.0
        b_top, b_bot = H(y_ttop), H(y_tbot)
        tbase = (b_top + b_bot) / 2.0 - (asc - desc) * tsize / 2.0 - TYPE_DROP
        c.drawString(lx, tbase, tline)
    rule(y_tbot)

    # --- the half's own rules-text box -------------------------------------
    if data.get('oracle_text'):
        draw_text_block(c, lx, lw, H(y_tbot), H(y_text_bot),
                        data['oracle_text'], name, warnings,
                        max_size=TEXT_SIZE * scale, min_size=MIN_TEXT)

    # --- stat line of this half (bottom right) -----------------------------
    # Every face carries its OWN power/toughness, loyalty and defense, so a
    # flip card's back ('Nezumi Graverobber' 2/1 -> 'Nighteyes the Desecrator'
    # 4/2) prints its own numbers, and the back of a double-faced planeswalker
    # ('Ral, Monsoon Mage // Ral, Leyline Prodigy') prints its starting
    # loyalty counter - _stat_text() reads this face dict, never the parent.
    # The baseline is clamped above this half's own bottom padding so short
    # halves cannot push the numbers into the rules text or the border.
    stat = _stat_text(data)
    if stat:
        ssize = fit_font_size(stat, STAT_FONT, STAT_SIZE * scale, MIN_TEXT, lw)
        c.setFont(STAT_FONT, ssize)
        c.drawRightString(x + w - PAD_X,
                          max(y + MINI_TEXT_BOT * MM, y + 3.0 * MM * k), stat)

    # --- front/back label of this half (bottom left) -----------------------
    if tag:
        c.setFont(NAME_FONT, MIN_TEXT)
        c.drawString(x + PAD_X, y + 1.0 * MM, tag)


def _stat_text(data):
    """Power/toughness, else starting loyalty, else battle defense, else None."""
    p, t = data.get('power'), data.get('toughness')
    if p is not None or t is not None:
        return f'{p if p is not None else "*"}/{t if t is not None else "*"}'
    for key in ('loyalty', 'defense'):
        if data.get(key) is not None:
            return str(data[key])
    return None


def _draw_half_pair(c, x, y, item, truncated=None):
    """Split / flip frame: ONE bordered face, two spells, lower half upside down.

    This mirrors how real split and flip cards are printed: the second spell
    sits on the same cardstock turned 180 degrees, so cutting the card out
    gives one double-ended card rather than two separate ones.
    """
    c.saveState()
    c.setStrokeColorRGB(0, 0, 0)
    c.setFillColorRGB(0, 0, 0)
    c.rect(x, y, CARD_W, CARD_H, stroke=0, fill=1)
    c.setFillColorRGB(1, 1, 1)
    c.roundRect(x + BORDER_W, y + BORDER_W, CARD_W - 2 * BORDER_W,
                CARD_H - 2 * BORDER_W, INNER_R * MM, stroke=0, fill=1)
    c.setFillColorRGB(0, 0, 0)

    bw = BORDER_W
    div_y = y + CARD_H - HALF_DIVIDER_MM * MM      # divider between the halves
    c.setLineWidth(LINE_W)
    c.line(x + MARGIN_MM * MM, div_y,
           x + CARD_W - MARGIN_MM * MM, div_y)

    h_up = (y + CARD_H - bw) - div_y
    h_lo = div_y - (y + bw)
    parts = item.get('parts') or []
    # A flip card's halves really are its front and back (the game flips the
    # card over), so label them; a split card has both spells on one face and
    # needs no such tag.
    tags = ('FRONT', 'BACK') if item.get('layout') == 'flip' else (None, None)
    if parts:
        draw_mini_face(c, x + bw, div_y, CARD_W - 2 * bw, h_up,
                       parts[0], HALF_SCALE, truncated, tags[0])
    if len(parts) > 1:
        # the lower spell is printed upside down on the same face
        c.saveState()
        cx, cy = x + CARD_W / 2.0, y + bw + h_lo / 2.0
        c.translate(cx, cy)
        c.rotate(180)
        draw_mini_face(c, -(CARD_W - 2 * bw) / 2.0, -h_lo / 2.0,
                       CARD_W - 2 * bw, h_lo, parts[1], HALF_SCALE, truncated,
                       tags[1])
        c.restoreState()
    c.restoreState()


def _draw_panel_boxes(c, x_left, width, box_top, box_bot, panels, name,
                      truncated=None):
    """Divide the standard text box into the stacked sub-boxes of a real card.

    *panels* is [(header, body), ...] from split_panels(); each panel gets its
    own share of the box and its own divider rule, with the header printed in
    bold above the body.  Shares are sized from the measured wrapped height of
    every panel (not character counts), and the white above the dividers is
    spent down to MIN_PANEL_PAD_MM before any text is shrunk, so long sagas,
    Levels and Cases print every chapter instead of cutting text off.  The
    bounding rules of the standard text box are still drawn by draw_card,
    unchanged.
    """
    n = len(panels)
    box_h = box_top - box_bot
    pad = PANEL_BODY_PAD_MM * MM          # padding inside every sub-box

    def em(font):
        f = pdfmetrics.getFont(font).face
        return f.ascent / 1000.0, -f.descent / 1000.0

    # (header text, header size, header height, largest body size) per panel
    spec = []
    for head, body in panels:
        if head:
            hsize = fit_font_size(head, NAME_FONT, TEXT_SIZE * PANEL_HDR_SCALE,
                                  MIN_TEXT, width)
            asc, desc = em(NAME_FONT)
            spec.append((head, hsize, hsize * (asc + desc) +
                         PANEL_HDR_EXTRA_MM * MM, TEXT_SIZE * 0.92))
        else:
            spec.append(('', None, 0.0, TEXT_SIZE))

    # Band height each panel needs at its current body size: header plus the
    # wrapped body block plus that renderer's own padding (draw_text_block
    # measures a block as (lines - 1) * 1.30 * size + ascent + descent).
    sizes = [s[3] for s in spec]

    def need(k):
        head_txt, hsize, hdr_h, _ = spec[k]
        body = panels[k][1]
        if not body:
            return hdr_h + 2 * pad
        asc, desc = em(TEXT_FONT)
        lines = wrap_text(body, TEXT_FONT, sizes[k], width)
        block = (len(lines) - 1) * sizes[k] * 1.30 + sizes[k] * (asc + desc)
        return hdr_h + block + 2 * pad

    need_h = [need(k) for k in range(n)]

    # The white above each divider is the flexible resource: use as much of it
    # as fits (up to PANEL_PAD_MM) before shrinking any text.  Only when even
    # the minimum white cannot hold the stack do panels shrink, down to MIN_TEXT
    # and, if that still fails, a visible cut.
    gap = PANEL_PAD_MM * MM
    while True:
        free = box_h - sum(need_h)
        want = free / (n - 1) if n > 1 else PANEL_PAD_MM * MM
        gap = min(max(want, MIN_PANEL_PAD_MM * MM), PANEL_PAD_MM * MM)
        if sum(need_h) + gap * (n - 1) <= box_h:
            break
        k = max(range(n), key=lambda i: (sizes[i], need_h[i]))
        if sizes[k] <= MIN_TEXT:
            break           # nothing left to shrink: draw_text_block cuts
        sizes[k] -= 0.5
        need_h[k] = need(k)
    avail = box_h - gap * (n - 1)

    total_h = sum(need_h) or 1.0
    slack = max(avail - total_h, 0.0)

    top_i = box_top
    for k, (head, body) in enumerate(panels):
        head_txt, hsize, hdr_h, _ = spec[k]
        share = need_h[k] + slack * need_h[k] / total_h
        bot_i = box_bot if k == n - 1 else top_i - share
        if head_txt:
            asc, desc = em(NAME_FONT)
            c.setFont(NAME_FONT, hsize)
            c.drawString(x_left, top_i - asc * hsize - 0.3 * MM, head_txt)
            if body:
                draw_text_block(c, x_left, width, top_i - hdr_h, bot_i, body,
                                name, truncated, max_size=sizes[k],
                                min_size=MIN_TEXT, pad_mm=PANEL_BODY_PAD_MM)
        elif body:
            draw_text_block(c, x_left, width, top_i, bot_i, body, name,
                            truncated, max_size=sizes[k], min_size=MIN_TEXT,
                            pad_mm=PANEL_BODY_PAD_MM)
        if k + 1 < n:
            c.setLineWidth(LINE_W)
            c.line(x_left, bot_i - gap / 2.0, x_left + width,
                   bot_i - gap / 2.0)
        top_i = bot_i - gap


def _draw_inset_panel(c, x0, y0, w, h, data, warnings=None):
    """Adventure / Prepared sub-panel: a bordered box inside the text box."""
    c.saveState()
    c.setFillColorRGB(1, 1, 1)
    c.setStrokeColorRGB(0, 0, 0)
    c.setLineWidth(LINE_W)
    c.roundRect(x0, y0, w, h, 1.0 * MM, stroke=1, fill=1)
    c.setFillColorRGB(0, 0, 0)

    pad = 1.2 * MM
    ix, iw = x0 + pad, w - 2 * pad
    top_i, bot_i = y0 + h - pad, y0 + pad

    # header band: name left, mana cost right (like a mini card header)
    cost = clean_mana_cost(data.get('mana_cost') or '')
    nsize, csize, ntext = fit_name_cost(data['name'], cost, iw)
    _note_short_name(warnings, data['name'], ntext)
    nsize = min(nsize, NAME_SIZE * INSET_SCALE)
    face = pdfmetrics.getFont(NAME_FONT).face
    asc = face.ascent / 1000.0
    desc = -face.descent / 1000.0
    hdr_h = (top_i - bot_i) * 0.34
    base = top_i - (hdr_h - nsize * (asc + desc)) / 2.0 - asc * nsize
    c.setFont(NAME_FONT, nsize)
    c.drawString(ix, base, ntext)
    if cost:
        c.setFont(COST_FONT, min(csize or COST_SIZE, COST_SIZE * INSET_SCALE))
        c.drawRightString(ix + iw, base, cost)
    top_i -= hdr_h + 0.6 * MM

    tline = data.get('type_line') or ''
    if tline:
        tsize = fit_font_size(tline, TYPE_FONT, TYPE_SIZE * INSET_SCALE,
                              MIN_TEXT, iw)
        face = pdfmetrics.getFont(TYPE_FONT).face
        asc = face.ascent / 1000.0
        desc = -face.descent / 1000.0
        th = tsize * (asc + desc) + 0.8 * MM
        c.setFont(TYPE_FONT, tsize)
        c.drawString(ix, top_i - asc * tsize - 0.3 * MM, tline)
        top_i -= th

    if data.get('oracle_text'):
        draw_text_block(c, ix, iw, top_i, bot_i, data['oracle_text'],
                        data['name'], warnings, max_size=TEXT_SIZE * INSET_SCALE,
                        min_size=MIN_TEXT, pad_mm=0.3)
    c.restoreState()


def draw_card(c, x, y, card, truncated=None):
    """Draw one test-play card. (x, y) = bottom-left corner in points.

    If *truncated* is a list, a (reason, card name) pair is appended to it
    when the rules text was too long for the box and had to be cut off, or
    when the printed name had to be shortened with an ellipsis.
    """
    if card.get('frame') == 'half':
        _draw_half_pair(c, x, y, card, truncated)
        return

    top = y + CARD_H

    def H(y_mm):  # mm from top -> point coordinate
        return top - y_mm * MM

    c.saveState()
    c.setStrokeColorRGB(0, 0, 0)

    # solid black border right at the card edge, like the frame on a real
    # Magic card: adjacent cards meet across the GAP-wide white cutting
    # line. Filled in black with the white card face restored inside it;
    # the outer corners are square and only the inner corners of the frame
    # are rounded (INNER_R).
    c.setFillColorRGB(0, 0, 0)
    c.rect(x, y, CARD_W, CARD_H, stroke=0, fill=1)
    c.setFillColorRGB(1, 1, 1)
    c.roundRect(x + BORDER_W, y + BORDER_W, CARD_W - 2 * BORDER_W,
                CARD_H - 2 * BORDER_W, INNER_R * MM, stroke=0, fill=1)
    c.setFillColorRGB(0, 0, 0)   # text is drawn with the fill colour

    m = MARGIN_MM * MM
    lx = x + PAD_X
    lw = CARD_W - 2 * PAD_X

    def rule(y_mm):
        c.setLineWidth(LINE_W)
        c.line(x + m, H(y_mm), x + CARD_W - m, H(y_mm))

    # --- name row (name left, mana cost right) -----------------------------
    name = card['name']
    cost = clean_mana_cost(card['mana_cost'])
    nsize, csize, ntext = fit_name_cost(name, cost, lw)
    _note_short_name(truncated, name, ntext)
    # Baselines centred between the inner border edge and the name rule,
    # matching a real card's top band (name ~3.5-7 mm from the top edge),
    # nudged down a touch so the text doesn't crowd the thick border.
    band_top = top - BORDER_W             # inner border edge
    band_bot = H(Z_NAME_LINE)             # rule under the name row
    band_mid = (band_top + band_bot) / 2.0

    def _base(size, font):
        face = pdfmetrics.getFont(font).face
        asc = face.ascent / 1000.0        # em above baseline
        desc = -face.descent / 1000.0     # em below baseline
        return band_mid - (asc - desc) * size / 2.0 - NAME_DROP

    c.setFont(NAME_FONT, nsize)
    c.drawString(lx, _base(nsize, NAME_FONT), ntext)
    if cost:
        c.setFont(COST_FONT, csize)
        c.drawRightString(x + CARD_W - PAD_X, _base(csize, COST_FONT), cost)

    rule(Z_NAME_LINE)

    # --- art area: intentionally blank (between the two rules) -------------
    rule(Z_TYPE_TOP)

    # --- type line: vertically centered in its band -------------------------
    tline = card['type_line']
    tsize = fit_font_size(tline, TYPE_FONT, TYPE_SIZE, MIN_TEXT, lw)
    c.setFont(TYPE_FONT, tsize)
    face = pdfmetrics.getFont(TYPE_FONT).face
    asc = face.ascent / 1000.0            # em above baseline
    desc = -face.descent / 1000.0         # em below baseline
    band_top = H(Z_TYPE_TOP)              # rule above the band
    band_bot = H(Z_TYPE_BOT)              # rule below the band
    tbase = (band_top + band_bot) / 2.0 - (asc - desc) * tsize / 2.0 - TYPE_DROP
    c.drawString(lx, tbase, tline)

    rule(Z_TYPE_BOT)

    # --- text box: word-wrapped, vertically centered -----------------------
    # The standard renderer (draw_text_block) and the standard zone bounds are
    # used unchanged for every card.  Custom frames only change what happens
    # inside this box: stacked sub-boxes (leveler / class / case / saga) or a
    # reserved strip at the bottom for an Adventure / Prepared inset panel.
    if card.get('panels'):
        _draw_panel_boxes(c, lx, lw, H(Z_TYPE_BOT), H(Z_TEXT_BOT),
                          card['panels'], name, truncated)
    else:
        bot_mm = (Z_TEXT_BOT - INSET_H_MM) if card.get('inset') else Z_TEXT_BOT
        if card['oracle_text']:
            draw_text_block(c, lx, lw, H(Z_TYPE_BOT), H(bot_mm),
                            card['oracle_text'], name, truncated)

    rule(Z_TEXT_BOT)

    # --- stat line (power/toughness, or starting loyalty for
    #     planeswalkers; bottom right) ---------------------------------------
    stat = _stat_text(card)
    if stat:
        ssize = fit_font_size(stat, STAT_FONT, STAT_SIZE, MIN_TEXT, lw)
        c.setFont(STAT_FONT, ssize)
        c.drawRightString(x + CARD_W - PAD_X, y + 3.0 * MM, stat)

    # --- small corner labels (optional) -------------------------------------
    # TOKEN / DECK INDICATOR / MELD PART / MELD RESULT sit bottom-left on
    # the stat line's baseline; FRONT / BACK sits under the name rule,
    # right-aligned in the blank art band, kept clear of that rule's stroke.
    marker = card.get('marker')
    if marker:
        c.setFont(NAME_FONT, MIN_TEXT)
        c.drawString(lx, y + 3.0 * MM, marker)
    tag = card.get('face_tag')
    if tag:
        c.setFont(NAME_FONT, MIN_TEXT)
        # Baseline so the cap tops sit FACE_TAG_GAP below the name rule: the
        # rule's own half-stroke is counted, which a flat 4.0 pt drop was not,
        # and that let the caps clip into the line.
        asc = pdfmetrics.getFont(NAME_FONT).face.ascent / 1000.0
        c.drawRightString(x + CARD_W - PAD_X,
                          H(Z_NAME_LINE) - LINE_W / 2.0 - FACE_TAG_GAP
                          - asc * MIN_TEXT, tag)

    # --- Adventure / Prepared inset sub-panel -------------------------------
    # Sits in the strip that was reserved at the bottom-left of the text box,
    # so it can never cover the main spell's rules text.
    if card.get('inset'):
        gap = 1.0 * MM
        _draw_inset_panel(c, x + PAD_X,
                          H(Z_TEXT_BOT) + gap,
                          INSET_W_MM * MM,
                          INSET_H_MM * MM - 2 * gap,
                          card['inset'], truncated)

    c.restoreState()


def build_pdf(cards, paper_pt, out_path):
    """Render all cards to a PDF.

    Returns (pages, per_page, warnings) where each entry of *warnings* is a
    (reason, card name) pair: 'rules text cut off' when the text overflowed
    the box at MIN_TEXT, 'name shortened' when the name row needed one.
    """
    paper_w, paper_h = paper_pt
    cols, rows, ox, oy = compute_grid(paper_w, paper_h)
    per_page = cols * rows
    truncated = []

    c = _rl_canvas.Canvas(out_path, pagesize=(paper_w, paper_h))
    c.setTitle('MTG Test-Play Cards')
    c.setAuthor('mtg-test-card-printer')

    pages = 0
    for start in range(0, len(cards), per_page):
        chunk = cards[start:start + per_page]
        if pages:
            c.showPage()
        pages += 1
        for i, card in enumerate(chunk):
            r, col = divmod(i, cols)
            # a GAP-wide white line separates adjacent cards (cutting guide)
            x = ox + col * (CARD_W + GAP)
            y = oy + (rows - 1 - r) * (CARD_H + GAP)
            draw_card(c, x, y, card, truncated)
    c.save()
    return pages, per_page, truncated


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        self.db = None
        self._busy = False              # generation in progress (Python-side flag)
        root.title('MTG Test-Play Card Printer')
        root.geometry('720x560')
        root.minsize(640, 480)

        top = ttk.Frame(root, padding=10)
        top.pack(fill='x')

        prefs = load_settings()

        ttk.Label(top, text='Paper size:').pack(side='left')
        self.paper_var = tk.StringVar(
            value=prefs.get('paper_size', list(PAPER_SIZES)[0]))
        self.paper_cb = ttk.Combobox(
            top, textvariable=self.paper_var,
            values=list(PAPER_SIZES), width=24)
        self.paper_cb.pack(side='left', padx=(6, 18))

        ttk.Label(top, text='Output:').pack(side='left')
        self.out_var = tk.StringVar(value=prefs.get(
            'output_path') or os.path.join(
                downloads_dir(), 'mtg_test_cards.pdf'))
        ttk.Entry(top, textvariable=self.out_var, width=42).pack(side='left', padx=(6, 10))
        ttk.Button(top, text='Browse...', command=self.choose_output).pack(side='left')

        mid = ttk.Frame(root, padding=(10, 0))
        mid.pack(fill='both', expand=True)
        ttk.Label(mid, anchor='w',
                  text='One card per line: "quantity x name" (a bare name counts as 1):'
                  ).pack(fill='x')

        body = ttk.Frame(root, padding=10)
        body.pack(fill='both', expand=True)
        self.text = tk.Text(body, wrap='word', font=('Consolas', 10), height=14)
        sb = ttk.Scrollbar(body, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        self.text.insert('1.0',
                         '1x timewalk\n'
                         '1 ancestral recall\n'
                         '1* BLACK LOTUS\n'
                         '1x mox jet\n'
                         '1x mOx oPal\n'
                         '1x Mox Emerald\n'
                         '1x mox pearl\n'
                         '1x mox ruby\n'
                         '1x mox sapphire\n')
        self.text.bind('<F5>', lambda e: self.generate())

        bottom = ttk.Frame(root, padding=10)
        bottom.pack(fill='x')
        self.gen_btn = ttk.Button(bottom, text='Generate PDF  (F5)',
                                 command=self.generate)
        self.gen_btn.pack(side='left')
        self.status = tk.Label(bottom, text='', anchor='w', fg='#333')
        self.status.pack(side='left', padx=12, fill='x', expand=True)

        # warm up the database in the background so first generation is fast
        threading.Thread(target=self._preload_db, daemon=True).start()

    # -- helpers ------------------------------------------------------------
    def _set_status(self, msg):
        self.root.after(0, lambda: self.status.config(text=msg))

    def choose_output(self):
        path = filedialog.asksaveasfilename(
            defaultextension='.pdf', initialfile='mtg_test_cards.pdf',
            initialdir=downloads_dir(),
            filetypes=[('PDF files', '*.pdf')])
        if path:
            self.out_var.set(path)

    def _preload_db(self):
        try:
            ensure_database()
            with _parse_lock:      # same lock as the worker: parse once
                if self.db is None:
                    self.db = load_database()
            self._set_status(f'Ready. Scryfall database loaded '
                             f'({len(self.db):,} cards).')
        except Exception as e:
            self.db = None
            detail = str(e)
            self._set_status('Database not loaded yet (details in dialog).')
            def show():
                messagebox.showerror(
                    'Could not load the card database',
                    detail + '\n\nPressing "Generate PDF" will retry.')
            try:
                self.root.after(0, show)
            except tk.TclError:
                pass  # window already closed

    # -- main action ----------------------------------------------------------
    def generate(self):
        if self._busy:
            return
        entries, parse_errors = parse_input(self.text.get('1.0', 'end'))
        if not entries and not parse_errors:
            messagebox.showwarning('Nothing to do',
                                   'Type or paste at least one card first.')
            return
        self._busy = True
        self.gen_btn.state(['disabled'])
        threading.Thread(target=self._generate_worker,
                         args=(entries, parse_errors), daemon=True).start()

    def _generate_worker(self, entries, parse_errors):
        try:
            if self.db is None:
                self._set_status('Downloading Scryfall oracle database '
                                 '(first run only, ~25 MB)...')
                ensure_database(progress=self._dl_progress)
                with _parse_lock:      # don't double-parse against the preload
                    if self.db is None:
                        self._set_status('Loading card database...')
                        self.db = load_database()

            cards, problems = resolve(entries, self.db)
            problems = parse_errors + problems

            # A huge per-line quantity is legal but turns into a long stack of
            # sheets; say so rather than surprising the user with 100 pages.
            qty_notes = [f'line {ln}: x{q} copies'
                         for ln, q, _name in entries if q > QTY_NOTE_ABOVE]

            if not cards:
                raise RuntimeError(
                    'No valid cards to print.\n\n' + '\n'.join(problems))

            # Layout-driven expansion: only genuinely double-sided cards yield
            # two items; split/flip/adventure/prepare stay on one face with a
            # custom frame (see expand_items).
            notes = []
            items = expand_items(cards, self.db, notes)

            out_path = self.out_var.get().strip()
            if not out_path:
                raise RuntimeError(
                    'No output file set. Choose one with Browse... or type '
                    'a path in the Output field.')
            if os.path.isdir(out_path):
                raise RuntimeError(
                    f"'{out_path}' is a folder - pick a file name inside it.")
            if not out_path.lower().endswith('.pdf'):
                out_path += '.pdf'
            parent = os.path.dirname(os.path.abspath(out_path))
            if not os.path.isdir(parent):
                raise RuntimeError(
                    f"The folder '{parent}' does not exist - pick a valid "
                    'output path.')

            paper_name = self.paper_var.get()
            if paper_name not in PAPER_SIZES:
                raise RuntimeError(
                    f"'{paper_name}' is not one of the listed paper sizes - "
                    'pick one from the Paper size drop-down.')

            existed = os.path.isfile(out_path)
            pages, per_page, warnings = build_pdf(
                items, PAPER_SIZES[paper_name], out_path)
            save_settings(paper_name, out_path)  # remembered for next launch

            msg = (f'Wrote {out_path}\n{len(cards)} card(s), '
                   f'{len(items)} face(s) on {pages} page(s) '
                   f'({per_page}/page). Print at 100% scale for the real '
                   f'{CARD_W / MM:.2f} x {CARD_H / MM:.2f} mm card size.')
            if existed:
                msg += (f'\nNote: this replaced an existing file at '
                        f'{out_path}.')
            if problems:
                msg += ('\n\nSome lines could not be resolved:\n' +
                        '\n'.join(problems))
            if notes:
                msg += ('\n\nLayout notes:\n' + '\n'.join(
                    dict.fromkeys(notes)))
            if qty_notes:
                msg += ('\n\nLarge quantities requested (expect many pages):\n'
                        + '; '.join(qty_notes) + '.')
            for reason in ('rules text cut off', 'name shortened'):
                names = [n for kind, n in warnings if kind == reason]
                if not names:
                    continue
                if reason == 'rules text cut off':
                    msg += ('\n\nNote: the rules text was longer than the card '
                            'box and was cut off on: ' +
                            ', '.join(dict.fromkeys(names)) + '.')
                else:
                    msg += ('\n\nNote: these names were too long to print in '
                            'full at the 6 pt floor and were shortened with an '
                            'ellipsis: ' +
                            ', '.join(dict.fromkeys(names)) + '.')
            self.root.after(0, lambda: (
                self.status.config(text=f'Done - {len(items)} card face(s) '
                                        f'on {pages} page(s).'),
                messagebox.showinfo('PDF created', msg)))
        except Exception as e:
            def show():
                try:
                    self.status.config(text='Generation failed.')
                except tk.TclError:
                    pass
                messagebox.showerror('Generation failed', str(e))
            try:
                self.root.after(0, show)
            except tk.TclError:
                pass  # window already closed
        finally:
            self._busy = False
            try:
                self.root.after(0, lambda: self.gen_btn.state(['!disabled']))
            except tk.TclError:
                pass  # window already closed

    def _dl_progress(self, done, total):
        if total:
            self._set_status(f'Downloading database... {done // 1024} / '
                             f'{total // 1024} KB')
        else:
            self._set_status(f'Downloading database... {done // 1024} KB')


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use('vista' if os.name == 'nt' else 'clam')
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
