"""Small PDF helpers shared by the table parsers (timetable, equivalents, ...)."""

Word = tuple  # PyMuPDF word: (x0, y0, x1, y1, text, block, line, word_no)


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
