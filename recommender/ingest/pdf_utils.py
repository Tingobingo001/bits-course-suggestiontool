"""Small PDF helpers shared by the parsers (timetable, equivalents, bulletin, ...)."""
from typing import NamedTuple

Word = tuple  # PyMuPDF word: (x0, y0, x1, y1, text, block, line, word_no)

BOLD_FLAG = 16  # PyMuPDF span flag for bold text


class Span(NamedTuple):
    """One run of same-font text. Field order matches PyMuPDF words (x0, y0, ..., text)
    so group_rows() works on both."""
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    bold: bool


def group_rows(words: list[Word], tolerance: float = 4) -> list[list[Word]]:
    """Group words into visual rows by y, each row sorted left-to-right.

    Words within `tolerance` points vertically are the same row; this absorbs
    wrapped titles that sit a point or two off the main row.
    """
    rows: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w[1], w[0])):
        if rows and abs(w[1] - rows[-1][0][1]) <= tolerance:
            rows[-1].append(w)
        else:
            rows.append([w])
    return [sorted(r, key=lambda w: w[0]) for r in rows]


def page_spans(page, y_range: tuple[float, float]) -> list[Span]:
    """All non-blank text spans on a page within a vertical band, with bold flag."""
    y0, y1 = y_range
    return [
        Span(*s["bbox"], s["text"], bool(s["flags"] & BOLD_FLAG))
        for b in page.get_text("dict")["blocks"]
        for ln in b.get("lines", [])
        for s in ln["spans"]
        if s["text"].strip() and y0 <= s["bbox"][1] < y1
    ]


def span_rows(page, split_x: float, y_range: tuple[float, float],
              tolerance: float = 2) -> list[list[Span]]:
    """Visual rows of a two-column page in reading order (left column, then right).

    Rows are rebuilt from span positions rather than the PDF's own text lines, which
    are unreliable (table cells and some headers are stored one word/cell per line).
    """
    spans = page_spans(page, y_range)
    rows: list[list[Span]] = []
    for x0, x1 in ((0, split_x), (split_x, page.rect.width)):
        rows += group_rows([s for s in spans if x0 <= s.x0 < x1], tolerance)
    return rows
