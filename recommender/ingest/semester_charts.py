"""Stage 1d: parse the semester-wise charts in Bulletin Part IV.

Each chart shows, for one degree (or one dual-degree pair), which courses and elective
slots are taken in which year and semester, plus totals such as
"Discipline Core - 48 Units (14 Courses)". The engine uses this to tell a student which
CDCs are due now and how many DEL units a programme needs.

Approach
--------
Rows are rebuilt from span positions. A chart page is split into YEAR BLOCKS by
delimiter rows: the "First Semester | U | Second Semester" header, the bold unit-total
row that closes each year ("18 | 19"), and the "Summer" row. Each block's year comes from
the Roman numeral printed in it (I-V). Inside a block, x decides the semester (left half
= first, right half = second). A half-row with a course code becomes a course entry; a
half-row mentioning Humanities/Open/Discipline electives becomes an elective slot; "or"
marks the next entry as an alternative.

Each single-degree chart is linked to the course list (course_lists.json) whose CDCs
overlap most with the chart's courses - data-driven, no hand-written name mapping.

Usage: python -m recommender.ingest.semester_charts   (after course_lists)
"""
import json
import re

import pymupdf

from recommender.codes import find_codes
from recommender.config import PROCESSED, RAW
from recommender.ingest.pdf_utils import group_rows, page_spans
from recommender.schema import ChartEntry, ChartTotals, SemesterChart, SourceRef

PDF = RAW / "bulletin.pdf"
OUT = PROCESSED / "semester_charts.json"

SEMESTER_SPLIT = 262          # x between the first- and second-semester columns
YEAR_LABEL_MAX_X = 80         # Roman numeral year labels sit at x ~ 65-72
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6}
UNITS_RE = re.compile(r"^(\d{1,2}\s*(\*|\(min\))?|\(min\)|\d{1,2}\s*to\s*\d{1,2}|\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2})$")
SLOT_WORDS = re.compile(r"Humanities|Open|Discipline|Elective", re.IGNORECASE)
# title ends at "Programme", or (General Studies) right after "Stream" before the "Year" header
TITLE_SINGLE_RE = re.compile(r"Semester-wise Pattern for Students Admitted to\s+(.+?(?:Stream)?)\s*(?:Programme|(?=Ye\s?a\s?r\b))", re.S)
BARE_NUMBER_RE = re.compile(r"^[A-Z]\d{3}[A-Z]?$")   # 'F425T' printed without its department
TITLE_DUAL_RE = re.compile(r"composite Dual Degree Programmes\s*\(?\s*(M\.\s?Sc\..+?with.+?)\)?\s*$", re.S | re.M)
TOTAL_CORE_RE = re.compile(r"Discipline\s+Core\s*-?\s*(\d+)\s*Units?\s*\((\d+)\s*Courses?\)", re.S)
TOTAL_DEL_RE = re.compile(r"Discipline\s+Electives?\s*-?\s*(\d+)\s*Units?\s*(\(min\))?\s*-?\s*\(?(\d+)\s*Courses?", re.S)


def chart_title(page_text: str) -> tuple[str, str] | None:
    """('single'|'dual', title) or None if the page isn't a programme chart."""
    flat = re.sub(r"\s+", " ", page_text)
    m = TITLE_DUAL_RE.search(re.sub(r"[ \t]+", " ", page_text))
    if "composite Dual Degree" in flat and m and " with " in m.group(1):
        return "dual", re.sub(r"\s+", " ", m.group(1)).strip(" ()")
    m = TITLE_SINGLE_RE.search(flat)
    if m:
        return "single", m.group(1).strip()
    return None


def is_delimiter(row) -> bool:
    text = " ".join(s.text.strip() for s in row)
    if "First Semester" in text or text.startswith("Year") or text.startswith("Summer"):
        return True
    return all(s.bold for s in row) and all(UNITS_RE.match(s.text.strip()) for s in row)


def parse_half(spans, year: int, semester, pending_or: bool,
               prev_dept: str | None = None) -> tuple[list[ChartEntry], bool]:
    """Entries from one semester column of one row; returns (entries, or_seen)."""
    words = [s.text.strip() for s in spans if s.text.strip()]
    if not words:
        return [], pending_or
    text = " ".join(words)
    if re.fullmatch(r"(or\s*)+", text, re.IGNORECASE):
        return [], True
    if words[0].lower() == "or":          # 'or | F425T | Thesis' - alternative to the line above
        pending_or, words = True, words[1:]
        if not words:
            return [], True
    if BARE_NUMBER_RE.match(words[0]) and prev_dept:
        words[0] = f"{prev_dept} {words[0]}"   # department implied by the line above
    units = next((w for w in reversed(words) if UNITS_RE.match(w)), None)
    label = " ".join(w for w in words if w != units)
    codes = find_codes(label)
    if codes:
        return [ChartEntry(year=year, semester=semester, kind="course", course_code=c, text=c,
                           units=units, alternative_to_previous=pending_or and k == 0)
                for k, c in enumerate(codes)], False
    if units and SLOT_WORDS.search(label):
        return [ChartEntry(year=year, semester=semester, kind="elective_slot", text=label,
                           units=units, alternative_to_previous=pending_or)], False
    return [], pending_or   # wrapped title text etc.


def parse_chart(page, pno: int, kind: str, title: str) -> SemesterChart:
    chart = SemesterChart(title=title, kind=kind,
                          source=SourceRef(document="bulletin.pdf", page=pno,
                                           section="Part IV - Semester-wise Pattern"))
    rows = group_rows(page_spans(page, (0, page.rect.height - 40)), tolerance=2)

    # 1. cut into blocks at delimiter rows
    blocks, current = [], []
    for row in rows:
        if is_delimiter(row):
            if current:
                blocks.append(current)
            current = []
            text = " ".join(s.text.strip() for s in row)
            if text.startswith("Summer"):
                blocks.append([row])            # summer row is its own block
            continue
        current.append(row)
    if current:
        blocks.append(current)

    # 2. each block: find its year label, then read both semester columns
    last_year = 0
    for block in blocks:
        text = " ".join(s.text.strip() for r in block for s in r)
        if text.startswith("Summer"):
            codes = find_codes(text)
            chart.entries.append(ChartEntry(year=max(last_year, 1), semester="summer", kind="other",
                                            course_code=codes[0] if codes else None,
                                            text=re.sub(r"^Summer\s*", "", text)))
            continue
        labels = [s.text.strip() for r in block for s in r
                  if s.x0 < YEAR_LABEL_MAX_X and s.text.strip() in ROMAN]
        if labels:
            year = ROMAN[labels[0]]
        elif find_codes(text) and last_year:
            year = last_year + 1                  # year label not printed (BBA year III)
            chart.needs_verification.append(f"year {year} label not printed; inferred from order")
        else:
            continue                              # title / footer text
        last_year = year
        if "Same as First degree" in text:
            chart.years_same_as_first_degree.append(year)
            continue
        pending = {1: False, 2: False}
        for row in block:
            for sem in (1, 2):
                half = [s for s in row if (s.x0 < SEMESTER_SPLIT) == (sem == 1)
                        and not (s.x0 < YEAR_LABEL_MAX_X and s.text.strip() in ROMAN)]
                prev = [e for e in chart.entries if e.semester == sem and e.course_code]
                prev_dept = prev[-1].course_code.split()[0] if prev else None
                entries, pending[sem] = parse_half(half, year, sem, pending[sem], prev_dept)
                chart.entries += entries

    # 3. totals printed under the chart
    flat = re.sub(r"\s+", " ", page.get_text())
    if m := TOTAL_CORE_RE.search(flat):
        chart.totals.core_units, chart.totals.core_courses = int(m.group(1)), int(m.group(2))
    if m := TOTAL_DEL_RE.search(flat):
        chart.totals.elective_units = int(m.group(1))
        chart.totals.electives_are_minimum = bool(m.group(2))
        chart.totals.elective_courses = int(m.group(3))
    return chart


def link_course_lists(charts: list[SemesterChart]) -> None:
    """Link each single-degree chart to the course list whose CDCs it overlaps most."""
    path = PROCESSED / "course_lists.json"
    if not path.exists():
        return
    lists = json.loads(path.read_text(encoding="utf-8"))["programmes"]
    cores = {p["name"]: {o["course_code"] for s in p["core"] for o in s["options"]} for p in lists}
    for chart in charts:
        if chart.kind != "single":
            continue
        codes = {e.course_code for e in chart.entries if e.course_code}
        title_words = set(re.findall(r"[A-Z]{3,}", chart.title.upper()))
        best, score, best_key = None, 0.0, (0.0, 0)
        for name, core in cores.items():
            s = len(core & codes) / len(core) if core else 0   # share of the list's CDCs in the chart
            # tie-break on shared title words: a specialisation chart has the same CDCs as the base
            # degree, so "...with Specialization in Aerospace" must pick the list naming Aerospace
            key = (round(s, 3), len(title_words & set(re.findall(r"[A-Z]{3,}", name))))
            if key > best_key:
                best, score, best_key = name, s, key
        chart.course_list, chart.course_list_overlap = best, round(score, 2)
        if score < 0.8:
            chart.needs_verification.append(f"weak link to course list '{best}' ({score:.0%} of its CDCs in chart)")


def validate(charts: list[SemesterChart]) -> None:
    for c in charts:
        if c.kind == "single" and c.totals.core_courses is None:
            c.needs_verification.append("core totals not printed under chart")
        if not c.entries and not c.years_same_as_first_degree:
            c.needs_verification.append("no entries parsed")


def run():
    doc = pymupdf.open(PDF)
    charts = []
    for i, page in enumerate(doc):
        if i < 200 or i > 320:        # Part IV only (a quick pre-filter; titles decide)
            continue
        found = chart_title(page.get_text())
        if found:
            charts.append(parse_chart(page, i + 1, *found))
    link_course_lists(charts)
    validate(charts)
    OUT.write_text(json.dumps([c.model_dump() for c in charts], indent=1, ensure_ascii=False), encoding="utf-8")

    single = [c for c in charts if c.kind == "single"]
    print(f"charts: {len(charts)}  (single {len(single)}, dual {len(charts) - len(single)})")
    for c in single:
        n = sum(1 for e in c.entries if e.kind == "course")
        print(f"  {c.title[:52]:52} courses {n:2}  -> {str(c.course_list)[:34]:34} {c.course_list_overlap}  "
              f"core {c.totals.core_courses}/{c.totals.core_units}u  DEL {c.totals.elective_courses}/{c.totals.elective_units}u")
    print(f"flagged: {[(c.title[:40], c.needs_verification) for c in charts if c.needs_verification][:8]}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    run()
