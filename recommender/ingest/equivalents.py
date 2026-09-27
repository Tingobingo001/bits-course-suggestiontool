"""Stage 1a+: parse the timetable's 'List of Equivalent Courses'.

Why it matters
--------------
- Completed-course checks: a student who passed IS F213 has effectively done CS F213,
  so CS F213 must not be recommended again.
- Catalogue fallback: ECON F315 has no bulletin description, but its equivalent
  FIN F315 does.

Two sources
-----------
1. PRINTED (high confidence): group words into rows by y, take every course code in
   the row; the first is the course, the rest are its equivalents. Rows are never
   chained (A=B in one row and B=C in another does NOT make A=C here).
2. HANDOUTS (high confidence): a handout whose "Course No." line names several codes
   ("BITS F482/ECON F414") is one course under several codes - stated by the course
   itself. Rule requested by the project owner.
3. INFERRED CROSS-LISTS (medium confidence): the timetable prints some cross-listed
   courses as separate rows, e.g. ECON F315 and FIN F315 - same class, different
   department codes - and the printed list omits them. We infer a cross-list only when
   title, lecture schedule, room and compre slot ALL match (decision confirmed with
   the project owner; slot+room alone also merges unrelated courses sharing a room).

Usage: python -m recommender.ingest.equivalents
"""
import json
import re

import pymupdf

from recommender.codes import find_codes
from recommender.config import NEW_ADMISSIONS_MIN_COMP_CODE, PROCESSED, RAW, TEXT_HANDOUTS
from recommender.ingest.handouts import handout_codes
from recommender.ingest.pdf_utils import group_rows
from recommender.schema import Equivalence, SourceRef

PDF = RAW / "timetable.pdf"
OUT = PROCESSED / "equivalents.json"

START_MARKER = "LIST OF EQUIVALENT COURSES"
TITLE_X = (140, 280)  # x-range of the COURSE TITLE column


def table_pages(doc) -> list[int]:
    """0-based pages from the list's heading until the column header stops appearing."""
    pages, inside = [], False
    for i, page in enumerate(doc):
        text = page.get_text()
        if START_MARKER in text:
            inside = True
        if inside:
            if "EQUIVALENT" not in text:
                break
            pages.append(i)
    return pages


def parse() -> list[Equivalence]:
    doc = pymupdf.open(PDF)
    rows: list[Equivalence] = []
    for i in table_pages(doc):
        for row in group_rows(doc[i].get_text("words")):
            text = " ".join(w[4] for w in row)
            codes = find_codes(text)
            if len(codes) < 1 or "EQUIVALENT" in text:
                continue
            course = codes[0]
            others = list(dict.fromkeys(c for c in codes[1:] if c != course))
            title = " ".join(w[4] for w in row if TITLE_X[0] <= w[0] < TITLE_X[1])
            rows.append(Equivalence(
                course_code=course, title=title, equivalents=others,
                source=SourceRef(document="timetable.pdf", page=i + 1, section="IX. List of Equivalent Courses"),
            ))
    return rows


COURSE_NO_RE = re.compile(r"Course\s*(?:No|Number|Code)\.?\s*[:\-]?\s*(.+)", re.IGNORECASE)


def from_handouts() -> list[Equivalence]:
    """Handouts whose 'Course No.' line lists more than one code."""
    out = []
    for path in sorted(TEXT_HANDOUTS.glob("*.txt")):
        text = path.read_text(encoding="utf-8")
        m = COURSE_NO_RE.search(text)
        if not m:
            continue
        # printed codes minus misprints (same title rule as the handout parser: the CS F215
        # 'Digital Design' handout prints 'EEE / ECE / INSTR / CS F342')
        fm = re.match(r"\d+_([A-Z]+)_([A-Z]\d{3}[A-Z]?)", path.stem)
        valid = set(handout_codes(text, f"{fm.group(1)} {fm.group(2)}" if fm else None)[0])
        codes = [c for c in dict.fromkeys(find_codes(m.group(1))) if c in valid]
        if len(codes) < 2:
            continue
        title_m = re.search(r"Course\s*Title\s*:?\s*(.+)", path.read_text(encoding="utf-8"), re.IGNORECASE)
        for code in codes:
            out.append(Equivalence(
                course_code=code,
                title=title_m.group(1).strip() if title_m else "",
                equivalents=[c for c in codes if c != code],
                basis="handout",
                source=SourceRef(document=f"handouts/{path.stem}.pdf", page=1, section="Course No."),
            ))
    return out


def unit_conflicts(rows: list[Equivalence], timetable: list[dict]) -> list[list[str]]:
    """Equivalence groups whose codes carry different units in the timetable - worth a human look."""
    units = {t["course_code"]: t["units"] for t in timetable if t["units"] is not None}
    conflicts = []
    for r in rows:
        group = sorted({r.course_code, *r.equivalents})
        if len({units[c] for c in group if c in units}) > 1 and group not in conflicts:
            conflicts.append(group)
    return conflicts


def normalize_title(title: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", title.upper())


def infer_crosslists(timetable: list[dict]) -> list[Equivalence]:
    """Courses printed as separate rows but taught as one class."""
    groups: dict[tuple, list[dict]] = {}
    for t in timetable:
        if t["cancelled"] or not t["sections"] or t["comp_code"] >= NEW_ADMISSIONS_MIN_COMP_CODE:
            continue
        lec = t["sections"][0]
        if not lec["raw_schedule"] or not t["compre"]:
            continue
        key = (normalize_title(t["title"]), lec["raw_schedule"], lec["room"],
               t["compre"]["date"], t["compre"]["session"])
        groups.setdefault(key, []).append(t)

    out = []
    for members in groups.values():
        codes = list(dict.fromkeys(m["course_code"] for m in members))
        if len(codes) < 2:
            continue
        for m in members:
            out.append(Equivalence(
                course_code=m["course_code"], title=m["title"],
                equivalents=[c for c in codes if c != m["course_code"]],
                basis="inferred_crosslist",
                source=SourceRef(document="timetable.pdf", page=m["source"]["page"], section="II. Coursewise Timetable", confidence="medium"),
            ))
    return out


def run():
    rows = parse()
    print(f"printed records:             {len(rows)}")
    handout_rows = from_handouts()
    print(f"handout records:             {len(handout_rows)}")
    rows += handout_rows
    tt_path = PROCESSED / "timetable.json"
    if tt_path.exists():
        timetable = json.loads(tt_path.read_text(encoding="utf-8"))
        inferred = infer_crosslists(timetable)
        print(f"inferred cross-list records: {len(inferred)}")
        rows += inferred
        for group in unit_conflicts(rows, timetable):
            print(f"  WARNING units differ across equivalent codes: {group}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps([r.model_dump() for r in rows], indent=1, ensure_ascii=False), encoding="utf-8")

    no_equiv = [r.course_code for r in rows if not r.equivalents]
    print(f"total records:           {len(rows)}")
    print(f"  total equivalent codes: {sum(len(r.equivalents) for r in rows)}")
    print(f"  rows listing no other code: {len(no_equiv)} {no_equiv[:8]}")

    # how many offered courses without a bulletin description can borrow one via an equivalent?
    cat_path, tt_path = PROCESSED / "courses.json", PROCESSED / "timetable.json"
    if cat_path.exists() and tt_path.exists():
        catalogue = {c["course_code"] for c in json.loads(cat_path.read_text(encoding="utf-8"))}
        offered = {t["course_code"] for t in json.loads(tt_path.read_text(encoding="utf-8"))
                   if not t["cancelled"] and t["comp_code"] < NEW_ADMISSIONS_MIN_COMP_CODE}
        equiv: dict[str, set[str]] = {}
        for r in rows:
            equiv.setdefault(r.course_code, set()).update(r.equivalents)
        missing = offered - catalogue
        recovered = {m for m in missing if equiv.get(m, set()) & catalogue}
        print(f"in-scope offered courses: {len(offered)}, without description: {len(missing)}")
        print(f"  recovered via equivalents: {len(recovered)} {sorted(recovered)}")
        print(f"  still missing: {sorted(missing - recovered)}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    run()
