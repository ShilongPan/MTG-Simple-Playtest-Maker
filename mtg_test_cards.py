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
                'oracle_text': (c.get('oracle_text') or '').strip(),
                'layout': c.get('layout') or 'normal',
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
                        'oracle_text': (f.get('oracle_text') or '').strip(),
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
    """Map every face name of a multi-face card to its full card record."""
    hit = _FACE_INDEX.get(id(db))
    if hit is not None and hit[0] is db:
        return hit[1]
    idx = {}
    for cands in db.values():
        for card in cands:
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
    """
    cards, problems = [], []
    for lineno, qty, name in entries:
        cands = db.get(_norm(name)) or db.get(name.casefold())
        if not cands:
            # No card of this name: try matching one face of a
            # multi-face card (front-only names for flip/transform/split).
            cands = _face_lookup(db, name)
        if not cands:
            problems.append(
                f'line {lineno}: \u201c{name}\u201d (x{qty}): no card with this '
                'name was found. Please check the spelling.')
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
    return {k: src[k] for k in ('name', 'mana_cost', 'type_line', 'power',
                                'toughness', 'loyalty', 'oracle_text')}


def expand_items(cards, db):
    """Expand resolved cards into the flat list of card faces to print.

    Scryfall's layouts fall into three groups:

    * Two physical faces (split, flip, transform, adventure, prepare,
      art_series, two-faced tokens): each face is printed as its own test
      card, tagged FRONT / BACK so you know which side of the real card it
      is. Token faces additionally carry a TOKEN marker.
    * Melds: a meld part is one physical card whose back is the melded
      result; both are printed (part tagged MELD PART, result tagged MELD
      RESULT). A meld result typed on its own prints alone.
    * Everything else (normal, saga, class, case, leveler, vanguard,
      planar, scheme, emblem, mutate, prototype, augment, host, token,
      front_card): one face in the standard frame; tokens and deck
      indicators get a small corner marker.
    """
    items = []
    for card in cards:
        layout = card.get('layout') or 'normal'
        faces = card.get('faces') or []

        if len(faces) >= 2:
            token_marker = ('TOKEN' if layout in (
                'token', 'double_faced_token', 'two-faced_token') else None)
            for i, face in enumerate(faces):
                item = _base_item(face)
                if token_marker:
                    item['marker'] = token_marker
                item['face_tag'] = 'FRONT' if i == 0 else 'BACK'
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
                cands = (db.get(_norm(result_name)) or
                         db.get(result_name.casefold()))
                if cands:
                    res = _base_item(cands[0])
                    res['marker'] = 'MELD RESULT'
                    items.append(res)
                continue
            item['marker'] = 'MELD RESULT'
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
    while len(text) > 1 and \
            pdfmetrics.stringWidth(text[:-1].rstrip() + ell, font, size) > width:
        text = text[:-1]
    out = text.rstrip() + ell
    return out if pdfmetrics.stringWidth(out, font, size) <= width else ell


def fit_name_cost(name, cost, lw):
    """Sizes for a name (left) and mana cost (right) sharing one line.

    The name keeps its maximum size as long as possible; the cost is only
    shrunk when there is not enough room for it at its maximum size, so the
    two can never overlap. Only absurdly long names (a joke card or two)
    need the last resort: shrinking to 3 pt and shortening with an ellipsis.

    Returns (name_size, cost_size_or_None, name_text).
    """
    gap = 2.0 * MM                     # minimum space between name and cost
    if not cost:
        nsize = fit_font_size(name, NAME_FONT, NAME_SIZE, MIN_TEXT, lw)
        return nsize, None, truncate_to_fit(name, NAME_FONT, nsize, lw)
    c_floor_w = pdfmetrics.stringWidth(cost, COST_FONT, MIN_TEXT)
    nsize = float(NAME_SIZE)
    while nsize > 3.0:
        c_avail = lw - pdfmetrics.stringWidth(name, NAME_FONT, nsize) - gap
        if c_avail >= c_floor_w:
            return (nsize,
                    fit_font_size(cost, COST_FONT, COST_SIZE, MIN_TEXT, c_avail),
                    name)
        nsize -= 0.5
    # last resort: below the normal floor, and shorten the name if needed
    c_avail = lw - pdfmetrics.stringWidth(name, NAME_FONT, 3.0) - gap
    if c_avail >= c_floor_w:
        return 3.0, fit_font_size(cost, COST_FONT, COST_SIZE, MIN_TEXT, c_avail), name
    ntext = truncate_to_fit(name, NAME_FONT, 3.0,
                            lw - gap - c_floor_w)
    rest = lw - pdfmetrics.stringWidth(ntext, NAME_FONT, 3.0) - gap
    return 3.0, fit_font_size(cost, COST_FONT, COST_SIZE, MIN_TEXT, rest), ntext


def draw_card(c, x, y, card, truncated=None):
    """Draw one test-play card. (x, y) = bottom-left corner in points.

    If *truncated* is a list, the card name is appended to it when the
    rules text was too long for the box and had to be cut off.
    """
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

    # --- text box: left-justified, vertically centered ----------------------
    text = card['oracle_text']
    if text:
        face = pdfmetrics.getFont(TEXT_FONT).face
        asc = face.ascent / 1000.0            # em above baseline (Helvetica .718)
        desc = -face.descent / 1000.0         # em below baseline (.207)
        box_top = H(Z_TYPE_BOT)               # rule above the text box
        box_bot = H(Z_TEXT_BOT)               # rule below the text box

        # keep a little padding between the text and the rules bounding
        # the box, even when the text nearly fills it
        pad = 1.0 * MM

        def fits(size, lines):
            line_h = size * 1.30
            block = (len(lines) - 1) * line_h + size * (asc + desc)
            return block <= (box_top - box_bot) - 2 * pad

        # start at the maximum size; shrink until the wrapped text fits
        tsize = float(TEXT_SIZE)
        lines = wrap_text(text, TEXT_FONT, tsize, lw)
        while not fits(tsize, lines) and tsize > MIN_TEXT:
            tsize -= 0.5
            lines = wrap_text(text, TEXT_FONT, tsize, lw)

        # even at the minimum size the text may overflow (huge rules text):
        # keep only as many lines as fit and visibly cut the last one
        if not fits(tsize, lines):
            line_h0 = tsize * 1.30
            avail = (box_top - box_bot) - 2 * pad
            max_lines = max(int((avail - tsize * (asc + desc)) // line_h0) + 1, 1)
            kept = lines[:max_lines]
            last = truncate_to_fit(kept[-1], TEXT_FONT, tsize, lw)
            if last == kept[-1]:
                last = kept[-1].rstrip() + '\u2026'   # still too wide: force a cut
            kept[-1] = last
            lines = kept
            if truncated is not None:
                truncated.append(card['name'])

        # center the whole block vertically between the two rules
        line_h = tsize * 1.30
        block = (len(lines) - 1) * line_h + tsize * (asc + desc)
        first_baseline = box_top - (box_top - box_bot - block) / 2.0 \
                         - asc * tsize

        c.setFont(TEXT_FONT, tsize)
        for i, ln in enumerate(lines):
            ty = first_baseline - i * line_h
            c.drawString(lx, ty, ln)   # left-justified

    rule(Z_TEXT_BOT)

    # --- stat line (power/toughness, or starting loyalty for
    #     planeswalkers; bottom right) ---------------------------------------
    p, t = card['power'], card['toughness']
    if p is not None or t is not None:
        stat = f'{p if p is not None else "*"}/{t if t is not None else "*"}'
    elif card.get('loyalty') is not None:
        stat = str(card['loyalty'])
    else:
        stat = None
    if stat:
        ssize = fit_font_size(stat, STAT_FONT, STAT_SIZE, MIN_TEXT, lw)
        c.setFont(STAT_FONT, ssize)
        c.drawRightString(x + CARD_W - PAD_X, y + 3.0 * MM, stat)

    # --- small corner labels (optional) -------------------------------------
    # TOKEN / DECK INDICATOR / MELD PART / MELD RESULT sit bottom-left on
    # the stat line's baseline; FRONT / BACK sits just under the name rule,
    # right-aligned in the blank art band.
    marker = card.get('marker')
    if marker:
        c.setFont(NAME_FONT, MIN_TEXT)
        c.drawString(lx, y + 3.0 * MM, marker)
    tag = card.get('face_tag')
    if tag:
        c.setFont(NAME_FONT, MIN_TEXT)
        c.drawRightString(x + CARD_W - PAD_X, H(Z_NAME_LINE) - 4.0, tag)

    c.restoreState()


def build_pdf(cards, paper_pt, out_path):
    """Render all cards to a PDF.

    Returns (pages, per_page, truncated) where *truncated* lists the names
    of cards whose rules text was too long and got cut off.
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

        ttk.Label(top, text='Paper size:').pack(side='left')
        self.paper_var = tk.StringVar(value=list(PAPER_SIZES)[0])
        self.paper_cb = ttk.Combobox(
            top, textvariable=self.paper_var,
            values=list(PAPER_SIZES), state='readonly', width=24)
        self.paper_cb.pack(side='left', padx=(6, 18))

        ttk.Label(top, text='Output:').pack(side='left')
        self.out_var = tk.StringVar(value=os.path.join(
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

            if not cards:
                raise RuntimeError(
                    'No valid cards to print.\n\n' + '\n'.join(problems))

            # Split two-faced cards into one item per face and add meld
            # results, so every physical card side gets its own test card.
            items = expand_items(cards, self.db)

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

            existed = os.path.isfile(out_path)
            pages, per_page, truncated = build_pdf(
                items, PAPER_SIZES[self.paper_var.get()], out_path)

            msg = (f'Wrote {out_path}\n{len(cards)} card(s), '
                   f'{len(items)} face(s) on {pages} page(s) ({per_page}/page).')
            if existed:
                msg += (f'\nNote: this replaced an existing file at '
                        f'{out_path}.')
            if problems:
                msg += ('\n\nSome lines could not be resolved:\n' +
                        '\n'.join(problems))
            if truncated:
                names = ', '.join(dict.fromkeys(truncated))
                msg += ('\n\nNote: the rules text was longer than the card '
                        f'box and was cut off on: {names}.')
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
