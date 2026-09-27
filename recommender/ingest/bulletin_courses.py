"""Stage 1b: parse the course catalogue (Bulletin Part VI, on-campus descriptions).

Approach
--------
Part VI is printed in two columns. Plain text extraction interleaves them, so:

  1. Find Part VI's page range from its own headings (no hard-coded page numbers).
  2. Split every page into a LEFT and RIGHT column and rebuild visual rows from text
     span positions (PDF text lines are unreliable: some headers are stored one word
     per line), keeping each span's font.
  3. Course headers are BOLD, descriptions regular. A header = a bold row starting
     with "DEPT F123", plus any following bold rows (wrapped title, units "3 0 3").
     A course code inside regular text (e.g. a prerequisite line) is never a header.
  4. Everything between two headers is the first course's description; department
     headings (from Part VI's contents page) set the department and section reference.
  5. Pull out an explicit "Pre-requisite: ..." sentence if present. If there isn't
     one we store None (= not stated), never [] (= no prerequisites).

Usage: python -m recommender.ingest.bulletin_courses
"""
import json
import re
from typing import NamedTuple

import pymupdf

from recommender.codes import find_codes, normalize_code
from recommender.config import PROCESSED, RAW
from recommender.ingest.pdf_utils import span_rows
from recommender.schema import Course, SourceRef

PDF = RAW / "bulletin.pdf"
OUT = PROCESSED / "courses.json"

COLUMN_SPLIT = 297       # x between the two columns (page width 595)
BODY = (40, 780)         # y-range without running header / page-number footer

HEADER_RE = re.compile(r"^([A-Z]{2,5})\s+([A-Z]{1,2}\d{3}[A-Z]?)\s*(.*)$")
UNIT_SPAN_RE = re.compile(r"^\s*(\d{1,2})(?:\s+(\d{1,2})\s+(\d{1,2}))?\s*(\*?)\s*$")  # '3 0 3', '4', '4*'
TRAILING_UNITS_RE = re.compile(r"(?:^|\s)\(?(\d{1,2})(?:\s+(\d{1,2})\s+(\d{1,2}))?\s*(\*?)\)?\s*$")  # 'Title  3 1 4', 'Title 4*', 'Title (5*)'
MAX_TITLE_CONTINUATION = 50  # chars; wrapped title lines are short ("Management"), description lines aren't
EXCLUSION_RE = re.compile(  # "Those who have done MATH F471 are not allowed to take this course"
    r"(?:Those|Students)\s+who\s+have\s+(?:done|taken|completed|passed)\s+(.+?)\s+"
    r"(?:are|will)\s+not\s+(?:be\s+)?(?:allowed|permitted|eligible)\s+to\s+(?:take|register)[^.]*\.?",
    re.IGNORECASE,
)
PREREQ_RE = re.compile(r"Pre-?requisites?\s*:\s*(.+?)(?:\.|$)", re.IGNORECASE)  # colon required: skips "is a prerequisite for X"


def find_part_range(doc) -> tuple[int, int]:
    """0-based [start, end) of the on-campus course descriptions."""
    start = end = None
    for i, page in enumerate(doc):
        text = page.get_text()
        if start is None and "COURSE DESCRIPTIONS" in text and "(On-Campus)" in text and "PART VI" in text:
            start = i
        elif start is not None and "PART VII" in text and "COURSE DESCRIPTIONS" in text:
            end = i
            break
    if start is None or end is None:
        raise RuntimeError("Could not locate Bulletin Part VI")
    return start, end


def department_headings(doc, start: int, end: int) -> set[str]:
    """Department names from Part VI's contents page, e.g. 'Computer Science'.
    They appear as stand-alone lines in the body and must not leak into descriptions."""
    names = set()
    for i in range(start, min(start + 3, end)):
        for line in doc[i].get_text().splitlines():
            m = re.match(r"^(.+?)\s*\.{5,}", line.strip())
            if m:
                names.add(m.group(1).strip())
    return names


class Row(NamedTuple):
    page: int                  # 1-based PDF page
    text: str
    bold: bool                 # every non-blank span on the row is bold
    units: tuple | None        # (lecture, practical, units, variable) if a bold units span is on the row
    title_text: str            # the row's bold text minus any units span


def column_rows(doc, start: int, end: int) -> list[Row]:
    """Visual rows in reading order (page1-left, page1-right, page2-left ...), with font info.

    Rows are rebuilt from span positions rather than PDF text lines, because some headers
    are stored one word per text line ('ECON' / 'F412' / 'Security' ...).
    """
    out = []
    for i in range(start, end):
        for row in span_rows(doc[i], COLUMN_SPLIT, BODY):
            # Units appear either as their own span ('3 0 3') or at the end of the
            # title span after a wide gap ('AN F211 Fluid Mechanics   3 1 4').
            units, title_parts, title_bold = None, [], []
            for s in row:
                text = s.text
                m = UNIT_SPAN_RE.match(text) or TRAILING_UNITS_RE.search(text)
                if m and units is None:
                    units = unit_tuple(m)
                    text = text[:m.start()] if m.re is TRAILING_UNITS_RE else ""
                if text.strip():
                    title_parts.append(text.strip())
                    title_bold.append(s.bold)
            out.append(Row(
                page=i + 1,
                text=re.sub(r"\s+", " ", " ".join(s.text for s in row)).strip(),
                bold=bool(title_bold) and all(title_bold),  # units may be in regular font
                units=units,
                title_text=re.sub(r"\s+", " ", " ".join(title_parts)).strip(),
            ))
    return out


def plain_header(rows: list[Row], i: int, headings: set[str]) -> bool:
    """A few headers are printed in regular font. Accept a regular row as a header only if
    it starts with a code followed by a Capitalised title, isn't a prerequisite/equivalence
    line (no ':' or '/'), and follows the end of a sentence or a department heading.
    (Rejects sentences like 'BITS F412 is a required course ...'.)"""
    row = rows[i]
    m = HEADER_RE.match(row.text)
    if not m or not m.group(3)[:1].isupper() or ":" in row.text or "/" in row.text or len(row.text) > 80:
        return False
    prev = rows[i - 1].text if i > 0 else ""
    return prev.endswith(".") or prev in headings


def is_prerequisite_line(rows: list[Row], i: int, m: re.Match) -> bool:
    """'EEE F311: Communication systems' under a 'Pre-requisite:' label - sometimes printed bold."""
    prev = rows[i - 1].text if i > 0 else ""
    return m.group(3).startswith(":") or bool(re.match(r"Pre-?requisites?\s*:?\s*$", prev, re.IGNORECASE))


def is_header_continuation(row: Row) -> bool:
    """A wrapped title line (short, bold) or a units-only row (any font)."""
    if HEADER_RE.match(row.text):
        return False
    if row.units and not row.title_text:
        return True
    return row.bold and len(row.title_text) <= MAX_TITLE_CONTINUATION


def unit_tuple(m: re.Match) -> tuple:
    """'3 1 4' -> (3, 1, 4, False);  '4*' -> (None, None, 4, True)."""
    a, b, c, star = m.groups()
    if b is not None:
        return int(a), int(b), int(c), bool(star)
    return None, None, int(a), bool(star)


def prerequisite_logic(text: str, codes: list[str]):
    """'A OR B' -> ('any', None); 'A AND B' / 'A, B' -> ('all', None); mixed -> flagged."""
    has_or = re.search(r"\bOR\b|/", text, re.IGNORECASE)
    has_and = re.search(r"\bAND\b|&", text, re.IGNORECASE)
    if not codes:
        return None, "prerequisite sentence found but no course codes extracted"
    if has_or and has_and:
        return "all", "prerequisite mixes AND/OR - logic needs manual check"
    return ("any" if has_or else "all"), None


def join_lines(lines: list[str]) -> str:
    """Join column lines, repairing words hyphenated across line breaks ('ma-' + 'chine')."""
    text = ""
    for line in lines:
        if text.endswith("-") and line[:1].islower():
            text = text[:-1] + line
        else:
            text = f"{text} {line}" if text else line
    return re.sub(r"\s+", " ", text).strip()


def parse() -> list[Course]:
    doc = pymupdf.open(PDF)
    start, end = find_part_range(doc)
    headings = department_headings(doc, start, end)
    rows = column_rows(doc, start, end)

    courses: list[Course] = []
    body: list[str] = []
    current: Course | None = None
    group: list[Course] = []  # consecutive headers with no text yet ("CS F366 Lab Project / CS F367 Lab Project")

    def close():
        if current is None:
            return
        if not body:
            group.append(current)  # shares the description printed after the group
            return
        description = join_lines(body)
        m = PREREQ_RE.search(description)
        excl = EXCLUSION_RE.search(description)
        for c in group + [current]:
            c.description = description
            if excl:
                c.restriction_text = excl.group(0)
                c.excluded_if_completed = [x for x in find_codes(excl.group(1)) if x != c.course_code]
            if m:
                c.prerequisite_text = m.group(0)
                c.prerequisites = list(dict.fromkeys(p for p in find_codes(m.group(1)) if p != c.course_code))
                c.prerequisite_logic, issue = prerequisite_logic(m.group(1), c.prerequisites)
                if issue:
                    c.needs_verification.append(issue)
        group.clear()

    department: str | None = None  # the Part VI heading we are currently under
    i = 0
    while i < len(rows):
        row = rows[i]
        prev_bold = i > 0 and rows[i - 1].bold

        # Department heading: a contents-page name that follows description text.
        # (After a bold title line, "Management" is a wrapped title, not a heading.)
        if row.text in headings and not prev_bold:
            department = row.text
            i += 1
            continue

        # Course header: a bold row starting with a code, plus up to 2 short bold
        # continuation rows (wrapped title and/or units).
        m = HEADER_RE.match(row.text) if (row.bold or plain_header(rows, i, headings)) else None
        if m and not is_prerequisite_line(rows, i, m):
            close()
            block = [row]
            j = i + 1
            while j < len(rows) and j - i <= 2 and is_header_continuation(rows[j]):
                block.append(rows[j])
                j += 1
            code = normalize_code(f"{m.group(1)} {m.group(2)}")
            first_title = HEADER_RE.match(row.title_text)
            title_parts = [first_title.group(3) if first_title else row.title_text]
            title_parts += [r.title_text for r in block[1:] if r.title_text]
            title = re.sub(r"(\w)- (\w)", r"\1\2", join_lines(title_parts))  # 'Instrumenta- tion'
            title = re.sub(r"\s*\*$", "", title)                              # footnote marker
            unit_range = re.search(r"\s(\d{1,2}-\d{1,2})$", title)            # thesis: '17-20'
            if unit_range:
                title = title[:unit_range.start()]
            units = next((r.units for r in block if r.units), None)
            lec, prac, u, variable = units or (None, None, None, False)
            current = Course(
                course_code=code, title=title, department=department,
                lecture_hours=lec, practical_hours=prac, units=u, variable_units=variable,
                source=SourceRef(document="bulletin.pdf", page=row.page,
                                 section=f"Part VI - {department}" if department else "Part VI"),
            )
            if unit_range:
                current.needs_verification.append(f"units given as a range: {unit_range.group(1)}")
            elif units is None:
                current.needs_verification.append("units not found in header")
            if not row.bold:
                current.needs_verification.append("header detected without bold font")
            courses.append(current)
            body = []
            i = j
            continue

        if current is not None:
            body.append(row.text)
        i += 1
    close()
    return courses


def validate(courses: list[Course]) -> dict:
    """Flag suspicious records and cross-check coverage against the timetable."""
    seen: dict[str, Course] = {}
    dups = []
    for c in courses:
        if len(c.description) < 40 and "same as" not in c.description.lower():
            c.needs_verification.append("very short or missing description")
        if c.lecture_hours is not None and c.units is not None and c.lecture_hours + c.practical_hours > 20:
            c.needs_verification.append("implausible L/P hours")
        if c.course_code in seen:
            dups.append(c.course_code)
        seen[c.course_code] = c

    report = {"duplicates": sorted(set(dups))}
    tt_path = PROCESSED / "timetable.json"
    if tt_path.exists():
        offered = {t["course_code"] for t in json.loads(tt_path.read_text(encoding="utf-8")) if not t["cancelled"]}
        report["offered"] = len(offered)
        report["offered_without_description"] = sorted(offered - set(seen))
    return report


def run():
    courses = parse()
    report = validate(courses)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps([c.model_dump() for c in courses], indent=1, ensure_ascii=False), encoding="utf-8")

    print(f"courses parsed:          {len(courses)}")
    print(f"  with L-P-U breakup:    {sum(1 for c in courses if c.lecture_hours is not None)}")
    print(f"  variable units (*):    {sum(1 for c in courses if c.variable_units)}")
    print(f"  prerequisite stated:   {sum(1 for c in courses if c.prerequisites is not None)}")
    print(f"  needs verification:    {sum(1 for c in courses if c.needs_verification)}")
    print(f"  duplicate codes:       {len(report['duplicates'])} {report['duplicates'][:8]}")
    if "offered" in report:
        missing = report["offered_without_description"]
        print(f"timetable coverage:      {report['offered'] - len(missing)}/{report['offered']} offered courses have a description")
        print(f"  missing sample:        {missing[:15]}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    run()
