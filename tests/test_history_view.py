"""History view logic (no display needed): list paging math + the offset->Tk
index mapper that fixes the editor "random stuff" glitch (Issue C).

These exercise the PURE helpers extracted from the widget, so they run without a
Tk display. The bug being locked down: red "changed" tags were computed from flat
Python char offsets but applied via per-token Tk .index() calls, which DRIFTED on
multi-line / wrapped text -> red landed on text the user never edited.
"""

from voiceflow.ui import history_view as H


# --------------------------------------------------------------- paging (B.2)
def test_page_bounds_first_page():
    # 319 records, none shown yet, step 40 -> render 0..40, 279 remain.
    assert H._page_bounds(319, 0, 40) == (0, 40, 279)


def test_page_bounds_next_page():
    assert H._page_bounds(319, 40, 40) == (40, 80, 239)


def test_page_bounds_last_partial_page():
    # 50 total, 40 shown, step 40 -> render 40..50, 0 remain (hide Load more).
    assert H._page_bounds(50, 40, 40) == (40, 50, 0)


def test_page_bounds_all_fit_first_page():
    assert H._page_bounds(10, 0, 40) == (0, 10, 0)


def test_page_bounds_nothing_left():
    assert H._page_bounds(40, 40, 40) == (40, 40, 0)


# ----------------------------------------------- offset->index mapper (C)
def test_line_starts_single_line():
    assert H._line_starts("hello world") == [0]


def test_line_starts_multiline():
    # "a\nbb\nccc" -> line starts at 0, 2, 5.
    assert H._line_starts("a\nbb\nccc") == [0, 2, 5]


def test_flat_to_index_single_line():
    ls = H._line_starts("hello world")
    assert H._flat_to_index(ls, 0) == "1.0"
    assert H._flat_to_index(ls, 6) == "1.6"


def test_flat_to_index_multiline_maps_to_correct_line():
    s = "line one\nline two\nline three"
    ls = H._line_starts(s)
    # offset of "three" on the 3rd line.
    off = s.index("three")
    assert H._flat_to_index(ls, off) == "3.5"     # "line " is 5 chars on line 3


# ----------------------------------------------- change spans / red diff (C)
def test_change_spans_single_line_edit():
    # orig "the cat sat", edit the middle word -> only that token highlighted.
    spans = H._build_change_spans("the cat sat", "the dog sat")
    assert spans == [("1.4", "1.7")]              # "dog" at cols 4..7 on line 1


def test_change_spans_no_change_is_empty():
    assert H._build_change_spans("hello world", "hello world") == []


def test_change_spans_multiline_maps_to_right_line():
    """THE bug: a change on line 3 must map to a 3.col span, NOT a drifted 1.col.
    Original (newlines) vs an edit on the third line."""
    orig = "first line\nsecond line\nthird line"
    cur = "first line\nsecond line\nTHIRD line"
    spans = H._build_change_spans(orig, cur)
    assert len(spans) == 1
    start, end = spans[0]
    assert start.startswith("3.")                 # line 3, not line 1
    assert start == "3.0" and end == "3.5"        # "THIRD" at cols 0..5


def test_change_spans_recapitalization_is_flagged():
    # Cased diff: 'big air' -> 'Big Air' must highlight (so it gets learned).
    spans = H._build_change_spans("big air", "Big Air")
    assert len(spans) == 2                         # both tokens changed case
