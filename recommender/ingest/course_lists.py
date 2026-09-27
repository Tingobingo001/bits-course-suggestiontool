"""Stage 1c: parse Bulletin Part IV 'List of Courses for B.E. / M.Sc. / B.Pharm. Programmes'.

This section gives, for every first-degree programme, its Discipline Core courses (CDCs)
and Discipline Electives (DELs), followed by institute-wide lists: the Humanities
elective pool (HUEL), project-type courses, other courses and audit courses.

Approach
--------
Rows are rebuilt from span positions (the table stores every cell as its own text line),
then walked in reading order by a small state machine:

  bold CAPITALS row            -> a programme heading (may wrap over two rows)
  'CORE COURSES'               -> following courses are that programme's CDCs
  'DISCIPLINE ELECTIVE COURSES'-> following courses are its DELs
  'Track - n: ...' / 'Pool - ...' -> DEL sub-group
  'Pool of Humanities courses' / 'Project Type Courses' / 'Other Courses' /
  'List of Audit Type Courses' -> institute-wide lists
  regular row starting with a code -> a course in the current list
  regular row without a code   -> wrapped title (right after a course) or prose
  'OR | OR' row                -> the next core course is an alternative for the same slot

Prose sentences that state limits ("at most", "maximum", "cannot") are kept as rules
with page references, for the policy layer.

Usage: python -m recommender.ingest.course_lists
"""
import json
from collections import Counter
import re

import pymupdf

from recommender.codes import CODE_RE, normalize_code
from recommender.config import PROCESSED, RAW, UNNAMED_PROGRAMME_NAMES
from recommender.ingest.pdf_utils import Span, span_rows
from recommender.schema import CoreSlot, CourseLists, ListedCourse, ProgrammeCourseList, SourceRef

PDF = RAW / "bulletin.pdf"
OUT = PROCESSED / "course_lists.json"

COLUMN_SPLIT = 258   # measured: no text span crosses x=258 on these pages
BODY = (40, 790)
START_HEADING = "List of Courses for B.E."
END_HEADING = "MINOR PROGRAMMES FOR FIRST"

NUMBER_RE = re.compile(r"^(\d{1,2}\*?|-)$")
SPECIAL_LISTS = {  # heading prefix -> list name
    "Pool of Humanities courses": "humanities_pool",
    "Project Type Courses": "project_courses",
    "Other Courses": "other_courses",
    "List of Audit Type Courses": "audit_courses",
}
UNNAMED = "(programme name not printed)"
RULE_WORDS = re.compile(r"\b(at most|maximum|cannot|can not|not allowed|limitation)", re.IGNORECASE)


def find_pages(doc) -> range:
    start = end = None
    for i, page in enumerate(doc):
        text = page.get_text()
        if start is None and START_HEADING in text and "CORE COURSES" in text:
            start = i
        elif start is not None and END_HEADING in text:
            end = i
            break
    if start is None or end is None:
        raise RuntimeError("Could not locate 'List of Courses' section")
    return range(start, end)


def parse_course_row(spans, page: int) -> ListedCourse | None:
    """'CS F213 | Object Oriented Programming | 3 | 1 | 4'  (code and title may share a span)."""
    text = " ".join(s.text.strip() for s in spans)
    m = CODE_RE.match(text.replace("*", "").strip())
    if not m:
        return None
    aliases = re.findall(r"\[alias:([^\]]+)\]", text)
    numbers = [s.text.strip() for s in spans if NUMBER_RE.match(s.text.strip())]
    words = [s.text.strip() for s in spans if not NUMBER_RE.match(s.text.strip())]
    rest = re.sub(r"\[alias:[^\]]+\]", "", " ".join(words))
    rest = CODE_RE.sub("", rest.replace("*", ""), count=1).strip(" /")
    rest = re.sub(r"\s+or$", "", rest, flags=re.IGNORECASE)  # 'Design Project or' (next row is the alternative)
    # when code, title and units share one span: 'BEGINNING CHINESE 3 0 3'
    trailing = re.search(r"(\s+(\d{1,2}|-)){1,3}$", rest)
    if trailing:
        if not numbers:
            numbers = trailing.group(0).split()
        rest = rest[:trailing.start()].strip()
    lec = prac = units = None
    variable = any(n.endswith("*") for n in numbers)
    vals = [None if n == "-" else int(n.rstrip("*")) for n in numbers]
    if len(vals) >= 3:
        lec, prac, units = vals[-3:]
    elif vals:
        units = vals[-1]
    return ListedCourse(
        course_code=normalize_code(m.group(0)), aliases=aliases, title=rest,
        lecture_hours=lec, practical_hours=prac, units=units, variable_units=variable, page=page,
    )


DEPT_ONLY_RE = re.compile(r"^[A-Z]{2,5}$")
NUMBER_ONLY_RE = re.compile(r"^[A-Z]\d{3}[A-Z]?$")
SLASHED_CODE_RE = re.compile(r"^([A-Z]{2,5}\s*[A-Z]\d{3}[A-Z]?)\s*/$")


def normalise_rows(rows: list[list[Span]]) -> list[list[Span]]:
    """Repair table cells that the PDF splits over two lines:
      'ECOM' / 'F321'          -> one code 'ECOM F321'
      'CS G514/' / 'SS G514'   -> one course 'CS G514' with alias 'SS G514'
    """
    out: list[list[Span]] = []
    i = 0
    while i < len(rows):
        row = rows[i]
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        first = row[0].text.strip()
        if nxt and abs(nxt[0].x0 - row[0].x0) < 6:
            nfirst = nxt[0].text.strip()
            if DEPT_ONLY_RE.match(first) and NUMBER_ONLY_RE.match(nfirst):
                code = row[0]._replace(text=f"{first} {nfirst}")
                out.append([code] + row[1:] + nxt[1:])
                i += 2
                continue
            if SLASHED_CODE_RE.match(first) and CODE_RE.match(nfirst):
                alias = row[0]._replace(text=f"{SLASHED_CODE_RE.match(first).group(1)} [alias:{normalize_code(nfirst)}]")
                out.append([alias] + row[1:] + nxt[1:])
                i += 2
                continue
        out.append(row)
        i += 1
    return out


def is_programme_heading(text: str, bold: bool) -> bool:
    letters = re.sub(r"[^A-Za-z]", "", text)
    return (bold and len(letters) >= 4 and text.upper() == text
            and not text.startswith(("CORE COURSES", "DISCIPLINE ELECTIVE")) and not CODE_RE.match(text))


def parse() -> CourseLists:
    doc = pymupdf.open(PDF)
    programmes: list[ProgrammeCourseList] = []
    special: dict[str, list[ListedCourse]] = {k: [] for k in SPECIAL_LISTS.values()}
    rules: list[dict] = []

    programme: ProgrammeCourseList | None = None
    mode: str | None = None        # "core" | "elective" | one of SPECIAL_LISTS values
    group: str | None = None       # DEL track / pool
    last: ListedCourse | None = None
    pending_or = False
    group_open = False                # last row was a bold Track/Pool heading
    pending_numbers: list[Span] = []  # L P U printed on an OR row, for the next course
    prev_heading = False           # previous row was a programme heading (for 2-row names)
    prose: list[str] = []
    prose_page = 0

    def flush_prose():
        nonlocal prose
        text = re.sub(r"\s+", " ", " ".join(prose)).strip()
        for sentence in re.split(r"(?<=[.:])\s+(?=[A-Z(])", text):
            if RULE_WORDS.search(sentence):
                rules.append({"text": sentence, "page": prose_page, "context": mode})
        prose = []

    for i in find_pages(doc):
        for spans in normalise_rows(span_rows(doc[i], COLUMN_SPLIT, BODY)):
            text = re.sub(r"\s+", " ", " ".join(s.text for s in spans)).strip()
            bold = all(s.bold for s in spans)
            if not text or re.fullmatch(r"(IV|V)-\d+|[ivx]+|IV|V", text):
                continue  # page numbers / running marks

            # --- headings -------------------------------------------------------
            special_list = next((v for k, v in SPECIAL_LISTS.items() if text.startswith(k)), None)
            if special_list:
                flush_prose()
                mode, group, programme, last, prev_heading = special_list, None, None, None, False
                continue
            if is_programme_heading(text, bold):
                flush_prose()
                pending_or = False
                if prev_heading and programme is not None:
                    programme.name += " " + text          # 2-row programme name
                else:
                    programme = ProgrammeCourseList(
                        name=text, source=SourceRef(document="bulletin.pdf", page=i + 1,
                                                    section="Part IV - List of Courses"))
                    programmes.append(programme)
                mode, group, last, prev_heading = None, None, None, True
                continue
            prev_heading = False
            if text.startswith("CORE COURSES"):
                if mode == "elective" or programme is None:
                    # a second core list with no programme name printed in between
                    programme = ProgrammeCourseList(
                        name=UNNAMED, source=SourceRef(document="bulletin.pdf", page=i + 1,
                                                       section="Part IV - List of Courses",
                                                       confidence="low"))
                    programmes.append(programme)
                mode, group, last, pending_or = "core", None, None, False
                continue
            if text.startswith("DISCIPLINE ELECTIVE"):
                mode, group, last, pending_or = "elective", None, None, False
                continue
            if re.match(r"(Track|Pool)\s*[-–]", text):
                group, last, pending_or = text, None, False
                group_open = bold    # a bold group heading may wrap onto the next row
                if programme is not None:
                    mode = "elective"
                continue
            if group_open and bold and not CODE_RE.match(text):
                group = f"{group} {text}"   # 'Pool – IV: Organizational Behaviour and' + 'Human Resource Management'
                continue
            group_open = False
            if "Course Title" in text or text.startswith("Course No"):
                continue  # column header row
            words = [s.text.strip() for s in spans if not NUMBER_RE.match(s.text.strip())]
            if words and all(w.upper() == "OR" for w in words):
                # 'OR | OR' between two courses; in vertically centred cells the NEXT
                # course's L P U can sit on this row ('or | or | 3 | 1 | 4')
                pending_or = True
                pending_numbers = [s for s in spans if NUMBER_RE.match(s.text.strip())]
                continue

            # --- course rows ----------------------------------------------------
            course = parse_course_row(spans, i + 1) if not bold or mode else None
            if course and course.units is None and pending_numbers:
                course = parse_course_row(spans + pending_numbers, i + 1)
            if course:
                pending_numbers = []
            if course and mode:
                flush_prose()
                course.group = group
                if mode == "core" and programme is not None:
                    if pending_or and programme.core:
                        programme.core[-1].options.append(course)
                    else:
                        programme.core.append(CoreSlot(options=[course]))
                elif mode == "elective" and programme is not None:
                    programme.electives.append(course)
                elif mode in special:
                    special[mode].append(course)
                last, pending_or = course, False
                continue

            # --- wrapped title or prose -----------------------------------------
            if last is not None and not bold and len(text) <= 40 and not text.endswith("."):
                last.title = f"{last.title} {text}".strip()
                continue
            if not prose:
                prose_page = i + 1
            prose.append(text)
            last = None
    flush_prose()
    for p in programmes:
        name_unnamed_programme(p, programmes)
    return CourseLists(programmes=programmes, rules=rules, **special)


def name_unnamed_programme(p: ProgrammeCourseList, programmes: list[ProgrammeCourseList]) -> None:
    """Apply an owner-confirmed name to a list printed without one (see config).

    Only if the configured prefix is the most common one among its core courses AND no
    other programme has a core course with that prefix (i.e. the prefix identifies it).
    """
    if p.name != UNNAMED:
        return
    prefixes = Counter(o.course_code.split()[0] for s in p.core for o in s.options)
    if not prefixes:
        return
    prefix, count = prefixes.most_common(1)[0]
    elsewhere = any(o.course_code.split()[0] == prefix
                    for q in programmes if q is not p for s in q.core for o in s.options)
    if prefix in UNNAMED_PROGRAMME_NAMES and not elsewhere:
        p.name = UNNAMED_PROGRAMME_NAMES[prefix]
        p.source.confidence = "medium"
        p.source.section = (f"{p.source.section} (name not printed; inferred: {count} of "
                            f"{sum(prefixes.values())} core courses are {prefix} and no other "
                            f"programme's core has {prefix} courses; confirmed by project owner)")


def run():
    lists = parse()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(lists.model_dump(), indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"programmes: {len(lists.programmes)}")
    for p in lists.programmes:
        alts = sum(1 for s in p.core if len(s.options) > 1)
        print(f"  {p.name[:60]:60} core {len(p.core):3} (alt {alts})  electives {len(p.electives):3}  p{p.source.page}")
    for name in ("humanities_pool", "project_courses", "other_courses", "audit_courses"):
        print(f"{name}: {len(getattr(lists, name))}")
    print(f"rules captured: {len(lists.rules)}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    run()
