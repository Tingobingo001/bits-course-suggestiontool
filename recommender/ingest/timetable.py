"""Stage 1a: parse the course-wise timetable into structured JSON.

Approach
--------
The timetable is a fixed-column table. Plain text extraction loses column
boundaries, so we read each word together with its (x, y) position:

  1. Detect column boundaries from each page's header labels (HEADER_ANCHORS),
     then bucket every word into a column by its x-coordinate.
  2. Group words into visual rows by y-coordinate (with a small tolerance,
     because wrapped titles sit a point or two off the main row).
  3. Classify each row:
       - new course     -> has a comp code + course code
       - extra section  -> has a section code (L2, T1, P3...) but no course code
       - co-instructor  -> only the instructor column is filled
       - noise          -> headers, footers, notes
  4. Decode "M W F 9" into meetings [(M,9), (W,9), (F,9)].
  5. Validate and write data/processed/timetable.json.

Usage: python -m recommender.ingest.timetable
"""
import json
import re

import pymupdf

from recommender.codes import normalize_code
from recommender.config import PROCESSED, RAW
from recommender.ingest.pdf_utils import group_rows
from recommender.schema import ExamSlot, Meeting, Section, SourceRef, TimetableCourse

PDF = RAW / "timetable.pdf"
OUT = PROCESSED / "timetable.json"

# Column boundaries are detected on each page from the header labels, so a new
# semester's timetable with a shifted layout still parses. Each column's left edge is
# (label x + offset); offsets were measured once against the 2026-27 timetable.
#   column: (header label, which occurrence of that label left-to-right, offset)
HEADER_ANCHORS = {
    "code": ("COURSE", 0, -3),
    "title": ("COURSE", 1, -3),
    "lec": ("L", 0, -9),
    "prac": ("P", 0, -8),
    "tut": ("T", 0, -6),
    "s": ("S", 0, -5),
    "units": ("U/C", 0, -4),
    "sec": ("SEC", 0, -4),
    "instr": ("INSTRUCTOR-IN-CHARGE", 0, -4),
    "room": ("ROOM", 0, 5),
    "sched": ("DAYS", 0, -5),
    "midsem": ("DATE", 0, -4),
    "compre": ("DATE", 1, -5),
}
# Fallback if a header can't be read (values measured on the 2026-27 timetable).
DEFAULT_COLUMNS = [
    ("comp", 0), ("code", 75), ("title", 135), ("lec", 300), ("prac", 325), ("tut", 345),
    ("s", 360), ("units", 375), ("sec", 398), ("instr", 434), ("room", 578), ("sched", 624),
    ("midsem", 695), ("compre", 752),
]
ROW_TOLERANCE = 4  # points; rows are ~14 pt apart, wrapped titles drift 1-2 pt

DAYS = {"M", "T", "W", "TH", "F", "S"}
SECTION_RE = re.compile(r"^([LTP])\d+[A-Z]*$")  # L1, T2, P3, T1AJ, L2RM ...
DATE_RE = re.compile(r"^\d{2}/\d{2}$")
ROOM_FRAG_RE = re.compile(r"^\d*\((M|T|W|Th|F|S)\b|^(M|T|W|Th|F|S)?\)$|^\d+\([A-Za-z ]+\)$")  # '6107(M', 'W)', '1223(Th)'; not 'Name(RS)'


def column_of(x: float, columns: list[tuple[str, float]]) -> str:
    name = columns[0][0]
    for col, left in columns:
        if x >= left:
            name = col
    return name


def header_bottom(words) -> float:
    """y just below the column-header block ("U/C" is its lowest label); 0 if no header."""
    ys = [w[1] for w in words if w[4] == "U/C"]
    return max(ys) + 12 if ys else 0


def detect_columns(words, top: float) -> list[tuple[str, float]] | None:
    """Column left edges from this page's header labels; None if the page has no full header."""
    header = sorted((w for w in words if w[1] <= top), key=lambda w: w[0])
    columns = [("comp", 0.0)]
    for col, (label, nth, offset) in HEADER_ANCHORS.items():
        xs = [w[0] for w in header if w[4] == label]
        if len(xs) <= nth:
            return None
        columns.append((col, xs[nth] + offset))
    return columns


def page_rows(page, columns) -> tuple[list[tuple[float, dict[str, str]]], list[tuple[str, float]]]:
    """Return the page body as (y, {column: text}) rows, skipping the header block,
    plus the columns used (detected here, or carried over from the previous page)."""
    words = page.get_text("words")
    top = header_bottom(words)
    columns = (detect_columns(words, top) if top else None) or columns
    rows = group_rows([w for w in words if w[1] > top], ROW_TOLERANCE)
    result = []
    for row in rows:
        cells: dict[str, list[str]] = {}
        for w in row:
            cells.setdefault(column_of(w[0], columns), []).append(w[4])
        result.append((row[0][1], {k: " ".join(v) for k, v in cells.items()}))
    return result, columns


def is_room_fragment(row: dict[str, str]) -> bool:
    """Per-day rooms like '6107(M W) 1223(Th)' are printed stacked over 2-3 lines.
    Rooms don't affect recommendations, so we don't reconstruct them - we flag them."""
    tokens = " ".join(row.values()).split()
    return bool(tokens) and all(ROOM_FRAG_RE.search(t) for t in tokens)


def parse_schedule(text: str) -> list[Meeting]:
    """'M W F 9' -> M9 W9 F9;  'M W 5 Th 10' -> M5 W5 Th10;  'W 8 9 10' -> W8 W9 W10."""
    meetings, days, hours = [], [], []

    def flush():
        for d in days:
            for h in hours:
                meetings.append(Meeting(day="Th" if d == "TH" else d, hour=h))

    for tok in text.split():
        t = tok.upper()
        if t in DAYS:
            if hours:  # a day after hours starts a new group
                flush()
                days, hours = [], []
            days.append(t)
        elif t.isdigit():
            hours.append(int(t))
    flush()
    return meetings


def parse_exam(text: str | None) -> ExamSlot | None:
    if not text:
        return None
    parts = text.split()
    if parts and DATE_RE.match(parts[0]):
        return ExamSlot(date=parts[0], session=" ".join(parts[1:]))
    return None


def to_int(text: str | None) -> int | None:
    return int(text) if text and text.strip().isdigit() else None


def is_cancelled(row: dict[str, str]) -> bool:
    # the PDF spells it "CANCLED"
    return any(w in row.get("instr", "").upper() for w in ("CANCLED", "CANCELLED"))


def make_section(row: dict[str, str]) -> Section:
    sec = row["sec"].split()[0]
    sched = row.get("sched")
    # a wide room like '6108(M)' can spill left into the instructor column
    instr_tokens = row.get("instr", "").split()
    spill = [t for t in instr_tokens if ROOM_FRAG_RE.search(t)]
    instr = " ".join(t for t in instr_tokens if t not in spill)
    room = " ".join(spill + ([row["room"]] if row.get("room") else [])) or None
    return Section(
        code=sec,
        type=sec[0],
        instructors=[instr] if instr else [],
        room=room,
        meetings=parse_schedule(sched) if sched else [],
        raw_schedule=sched,
    )


def parse() -> tuple[list[TimetableCourse], list[str]]:
    doc = pymupdf.open(PDF)
    courses: list[TimetableCourse] = []
    skipped: list[str] = []
    current: TimetableCourse | None = None
    cancelled_rows: set[int] = set()  # courses with at least one CANCLED row
    columns = DEFAULT_COLUMNS

    for pno, page in enumerate(doc, start=1):
        if "COURSEWISE" not in page.get_text() and current is None:
            continue  # before the course table starts
        if "SUGGESTIONS FOR CHOOSING ELECTIVES" in page.get_text():
            break     # course table has ended

        rows, columns = page_rows(page, columns)
        for _, row in rows:
            if is_room_fragment(row):
                continue  # stacked per-day room text; the section is flagged in validate()
            comp = row.get("comp", "").split()
            code = normalize_code(row.get("code", ""))
            sec = row.get("sec", "").split()
            is_section = bool(sec) and SECTION_RE.match(sec[0])

            # 1. New course row
            if comp and comp[0].isdigit() and code:
                current = TimetableCourse(
                    comp_code=int(comp[0]),
                    course_code=code,
                    title=row.get("title", "").strip(),
                    lecture_hours=to_int(row.get("lec")),
                    practical_hours=to_int(row.get("prac")),
                    units=to_int(row.get("units")),
                    midsem=parse_exam(row.get("midsem")),
                    compre=parse_exam(row.get("compre")),
                    source=SourceRef(document="timetable.pdf", page=pno, section="II. Coursewise Timetable"),
                )
                courses.append(current)
                if is_cancelled(row):
                    cancelled_rows.add(id(current))
                elif is_section:
                    current.sections.append(make_section(row))

            # 2. Another section of the current course
            elif is_section and current:
                if is_cancelled(row):
                    cancelled_rows.add(id(current))
                else:
                    current.sections.append(make_section(row))

            # 3. Co-instructor line (instructor column only)
            elif current and current.sections and set(row) == {"instr"}:
                current.sections[-1].instructors.append(row["instr"])

            # 4. Wrapped title (title column only, directly under a course with no title)
            elif current and set(row) == {"title"} and not current.title:
                current.title = row["title"].strip()

            # 5. Anything with a course code we didn't understand is logged, not guessed
            elif code and "Note" not in row.get("comp", ""):
                skipped.append(f"p{pno}: {row}")

    # a course is cancelled only when it had a CANCLED row and no section survived
    for c in courses:
        c.cancelled = id(c) in cancelled_rows and not c.sections
    return courses, skipped


def validate(courses: list[TimetableCourse]) -> None:
    """Attach needs_verification notes instead of silently accepting bad data."""
    for c in courses:
        if c.cancelled:
            continue
        if c.units is None:
            c.needs_verification.append("units not parsed")
        if not c.sections:
            c.needs_verification.append("no sections parsed")
        for s in c.sections:
            if s.room and any(ch in s.room.replace("(RS)", "") for ch in "()"):
                c.needs_verification.append(f"{s.code}: per-day rooms only partially parsed ('{s.room}')")
            if s.raw_schedule and not s.meetings:
                c.needs_verification.append(f"{s.code}: could not decode schedule '{s.raw_schedule}'")


def run():
    courses, skipped = parse()
    validate(courses)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps([c.model_dump() for c in courses], indent=1, ensure_ascii=False),
        encoding="utf-8",
    )
    active = [c for c in courses if not c.cancelled]
    flagged = [c for c in active if c.needs_verification]
    print(f"courses parsed:        {len(courses)}")
    print(f"  cancelled:           {len(courses) - len(active)}")
    print(f"  active:              {len(active)}")
    print(f"  sections:            {sum(len(c.sections) for c in active)}")
    print(f"  with midsem slot:    {sum(1 for c in active if c.midsem)}")
    print(f"  needs verification:  {len(flagged)}")
    print(f"rows skipped:          {len(skipped)}")
    for s in skipped[:10]:
        print("   ", s[:160])
    print(f"-> {OUT}")


if __name__ == "__main__":
    run()
