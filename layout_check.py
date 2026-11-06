"""Headless run of the app's pipeline over layout_review_list.txt.

Prints one line per printed card item (layout -> frame -> side), plus the
resolve problems, layout notes and truncated-text list, so a human can check
the PDF against what Scryfall says each layout should look like.

    python layout_check.py            # prints the table, writes layout_review.pdf
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import mtg_test_cards as app


def main():
    here = pathlib.Path(__file__).parent
    db = app.load_database(str(here / 'scryfall_oracle_cache.jsonl.gz'))
    entries, errors = app.parse_input((here / 'layout_review_list.txt')
                                      .read_text(encoding='utf-8'))
    cards, problems = app.resolve(entries, db)
    notes = []
    items = app.expand_items(cards, db, notes)

    print('input lines :', len(entries), '| resolved:', len(cards),
          '| printed items:', len(items))
    for p in problems:
        print('PROBLEM :', p)
    for n in notes:
        print('NOTE    :', n)
    print()
    for it in items:
        frame = it.get('frame') or ('inset' if it.get('inset') else 'standard')
        halves = len(it.get('parts') or [])
        panels = len(it.get('panels') or [])
        print(f"{it['layout']:19} {frame:9} "
              f"{it.get('face_tag') or '-':5} {it.get('marker') or '-':14} "
              f"halves={halves} panels={panels} text={len(it['oracle_text'] or ''):4} "
              f"{(it['name'] or '')[:40]}")

    pages, per_page, warnings = app.build_pdf(
        items, app.PAPER_SIZES['A4 (210 x 297 mm)'],
        str(here / 'layout_review.pdf'))
    print('PDF       :', f'{pages} page(s), {per_page}/page')
    for reason in ('rules text cut off', 'name shortened'):
        names = [n for kind, n in warnings if kind == reason]
        if names:
            print(f'{reason:19}:', ', '.join(dict.fromkeys(names)))


if __name__ == '__main__':
    main()
