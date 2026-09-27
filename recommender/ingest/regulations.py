"""Stage 1g: Academic Regulations 2023 + the timetable's registration instructions.

The Regulations are 65 pages of prose, numbered clause by clause ("3.13", "3.25 II").
Two outputs:

1. Every clause verbatim (number, section, page, text), so the agent can quote and cite
   "Clause 3.13 (p.15)" instead of paraphrasing from memory.
2. A short list of RULES the engine enforces (max units, extra electives, clash-free
   timetable, ...). Numbers in prose can't be extracted reliably by regex, so these are
   written by hand below - but each carries exact QUOTES from its clause, and the build
   checks that every quote really appears there. A typo in a rule, or a changed PDF,
   shows up as verified=False instead of silently wrong advice.

Clause numbers are printed in the right margin on the first line of each clause, after
a wide gap (in-text references like "see 3.25" sit further left or carry punctuation).

Usage: python -m recommender.ingest.regulations
"""
import json
import re

import pymupdf

from recommender.config import PROCESSED, RAW
from recommender.ingest.pdf_utils import group_rows
from recommender.schema import Clause, PolicyText, RegulationRule, Regulations, SourceRef

PDF = RAW / "Academic-Regulations-2023.pdf"
TIMETABLE_PDF = RAW / "timetable.pdf"
OUT = PROCESSED / "regulations.json"
DOC = "Academic-Regulations-2023.pdf"

CLAUSE_RE = re.compile(r"^\d{1,2}\.\d{2}[a-z]?$")
ROMAN = {"I", "II", "III", "IV"}
MARGIN_X = 380            # clause numbers sit right of this
MARGIN_GAP = 12           # ... and at least this far from the previous word
HEADER_Y = 60             # printed page number at the top of every page
SECTION_RE = re.compile(r"^(\d{1,2})\.\s+([A-Z][A-Za-z ,.\-]+)$")
STOP_SECTION = 12         # "12. Follow-Through Actions" is an administrative checklist

REGISTRATION_HEADING = "INSTRUCTIONS REGARDING REGISTRATION"


def normalise(text: str) -> str:
    """For quote matching: straight quotes, single spaces, case-insensitive."""
    text = text.translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'}))
    return re.sub(r"\s+", " ", text).strip().lower()


def section_titles(doc) -> dict[str, str]:
    """From the contents page: {'3': '3. Registration', ...}."""
    titles = {}
    for line in doc[2].get_text().splitlines():
        m = re.match(r"^\s*(\d{1,2})\.\s+(.+?)\s*\d*\s*$", line)
        if m:
            titles[m.group(1)] = f"{m.group(1)}. {m.group(2).strip()}"
    return titles


def clause_marker(row) -> str | None:
    """'3.13' / '3.25 II' if this row ends with a right-margin clause number."""
    last = row[-1]
    if last[4] in ROMAN and len(row) >= 2 and CLAUSE_RE.match(row[-2][4]):
        num, roman, before = row[-2], last, row[:-2]
        label = f"{num[4]} {roman[4]}"
    elif CLAUSE_RE.match(last[4]):
        num, before, label = last, row[:-1], last[4]
    else:
        return None
    if num[0] < MARGIN_X or (before and num[0] - before[-1][2] < MARGIN_GAP):
        return None
    return label


ITEM_RE = re.compile(r"\((?:i|ii|iii|iv)\)")


def table_start(row) -> float | None:
    """A two-column table row starts '(i) <course> ... (i) <requirement>': return the split x."""
    marks = [w for w in row if w[4] == "(i)"]
    return marks[1][0] - 5 if len(marks) == 2 else None


def crosses(row, split: float) -> bool:
    """True if this row is ordinary full-width text (words run across the column split)."""
    return any(w[0] < split < w[2] for w in row) or any(
        a[2] < split <= b[0] and b[0] - a[2] < 8 for a, b in zip(row, row[1:]))


def pair_columns(left: list[str], right: list[str]) -> str:
    """Zip the '(i) ... (ii) ...' items of both columns: '(i) PS I ... -> requires: Normally ...'."""
    def items(words):
        return [s.strip() for s in ITEM_RE.split(" ".join(words)) if s.strip()]
    lhs, rhs = items(left), items(right)
    pairs = [f"({'i' * (n + 1) if n < 3 else 'iv'}) {a} -> requires: {b}" for n, (a, b) in enumerate(zip(lhs, rhs))]
    return " ".join(pairs) if len(lhs) == len(rhs) else " ".join(left) + " || " + " ".join(right)


def parse_clauses(doc) -> list[Clause]:
    titles = section_titles(doc)
    clauses: list[Clause] = []
    current: dict | None = None
    section = ""
    table: dict | None = None          # {"split": x, "left": [...], "right": [...]} while inside a table

    def flush_table():
        nonlocal table
        if table and current:
            current["lines"].append(pair_columns(table["left"], table["right"]))
        table = None

    def close():
        flush_table()
        if current:
            clauses.append(Clause(clause=current["id"], section=section_of(current["id"]),
                                  text=re.sub(r"\s+", " ", " ".join(current["lines"])).strip(),
                                  source=SourceRef(document=DOC, page=current["page"], section=section_of(current["id"]))))

    def section_of(cid: str) -> str:
        return titles.get(cid.split(".")[0], section)

    for i in range(3, len(doc)):                           # pages 1-3: cover, title, contents
        for row in group_rows(doc[i].get_text("words"), tolerance=3):
            text = " ".join(w[4] for w in row)
            if row[0][1] < HEADER_Y and text.isdigit():
                continue                                    # printed page number
            m = SECTION_RE.match(text)
            if m and row[0][0] > 60:                         # centred section heading
                if int(m.group(1)) >= STOP_SECTION:
                    close()
                    return clauses
                section = titles.get(m.group(1), text)
                continue
            label = clause_marker(row)
            if table and (label or crosses(row, table["split"])):
                flush_table()
            split = table_start(row) if current and not label else None
            if split:
                table = {"split": split, "left": [], "right": []}
            if table:
                for w in row:
                    table["left" if w[0] < table["split"] else "right"].append(w[4])
                continue
            if label:
                close()
                cut = 2 if " " in label else 1
                current = {"id": label, "page": i + 1, "lines": [" ".join(w[4] for w in row[:-cut])]}
            elif current:
                current["lines"].append(text)
    close()
    return clauses


def registration_instructions(doc) -> tuple[list[PolicyText], int, str]:
    """Timetable 'VII. INSTRUCTIONS REGARDING REGISTRATION': numbered items 1-7."""
    pno = next(i for i, p in enumerate(doc) if REGISTRATION_HEADING in p.get_text())
    text = re.sub(r"\s+", " ", doc[pno].get_text(sort=True))
    body = text[text.find(REGISTRATION_HEADING) + len(REGISTRATION_HEADING):]
    body = re.split(r"\sVIII\.\s", body)[0]
    items = [s.strip() for s in re.split(r"\s(?=\d\.\s+[A-Z])", " " + body) if s.strip()]
    src = SourceRef(document="timetable.pdf", page=pno + 1, section="VII. Instructions regarding registration")
    return [PolicyText(topic="registration", text=re.sub(r"^\d\.\s*", "", s), source=src) for s in items], pno + 1, text


# (id, value, description, clause or None for the timetable page, [exact quotes])
RULES = [
    ("max_units_per_semester", 25, "Max units a first-degree student may register in a semester "
     "(deficiency/audit courses excluded).", "1.01", ["a first degree student can register is twenty-five"]),
    ("summer_max_courses", 3, "Max courses in a summer term.", "1.03", ["cannot be more than three"]),
    ("summer_max_units", 10, "Max units in a summer term, unless the semester-wise pattern differs.", "1.03",
     ["total number of units is not more than 10"]),
    ("min_units_first_degree", 144, "Minimum units for an integrated first degree.", "1.04",
     ["Integrated First Degree 144"]),
    ("course_cleared_by_any_grade", True, "A course is cleared by any letter grade (E included); reports "
     "such as NC, W, I, RC do not clear it.", "1.15", ["A course is deemed to have been cleared if the student obtains a grade"]),
    ("latest_performance_counts", True, "If a course is taken again, the latest performance decides whether it is cleared.",
     "1.16", ["determined by the latest performance"]),
    ("elective_overflow_to_open_elective", True, "Any elective beyond the Discipline and Humanities elective "
     "requirements counts as an Open Elective.", "2.05",
     ["any elective course will be treated as an Open Elective once the student's requirements"]),
    ("dual_degree_del_counts_as_opel", True, "For dual degree, Discipline Electives of one degree count as Open "
     "Electives of the other.", "2.05", ["counting the Discipline electives of one degree as Open Electives of the other degree"]),
    ("max_extra_electives", 4, "Electives a first-degree student may take over the prescribed number.", "2.08",
     ["upto a maximum number of four electives"]),
    ("max_higher_degree_courses_per_semester", 1, "Max one higher-degree course per semester, only above a CGPA "
     "set by AGC (threshold not stated in the supplied documents).", "2.08",
     ["maximum of one higher degree course per semester"]),
    ("higher_degree_course_category", "open_elective_unless_in_del_pool", "A higher-degree course counts as an Open "
     "Elective unless it is in the student's Discipline Elective pool.", "2.08",
     ["counted as an open elective unless the course is listed in the pool of discipline electives"]),
    ("practice_school_exclusive", True, "No other course can be taken alongside a Practice School course.", "2.10",
     ["There is no provision for taking other courses along with a Practice School component course"]),
    ("thesis_16_units_exclusive", True, "A 16-unit Thesis is full time; no other course alongside.", "2.10",
     ["16 units Thesis must pursue it exclusively full time"]),
    ("thesis_9_units_concurrent_limit", {"courses": 3, "units": 9}, "With a 9-unit Thesis, at most 3 other "
     "courses totalling at most 9 units.", "2.10", ["at most 3 courses (totaling at most 9 units)"]),
    ("prerequisites_must_be_met", True, "A course's prerequisites must be fulfilled before registering in it.", "3.13",
     ["should have fulfilled the prerequisite conditions"]),
    ("prior_preparation_named_courses", True, "Before a semester's prescribed courses, all named courses of earlier "
     "semesters must be cleared (the DCA may allow up to two missing if unrelated to core courses).", "3.14",
     ["All named courses in semesters and terms preceding this set of courses", "has not cleared at most two courses"]),
    ("other_discipline_course_prior_prep", "third_year_first_semester", "A core/elective course of another degree "
     "needs the prior preparation of the student's own 3rd-year 1st semester.", "3.15",
     ["preparation of the third year first semester"]),
    ("own_higher_degree_course_prior_prep", "first_set_of_core_courses", "A higher-degree course of the student's "
     "own discipline needs the first set of own discipline core courses (2nd year) cleared.", "3.15",
     ["After clearing first set of his/her own Discipline core courses"]),
    ("no_timetable_conflict", True, "The final semester programme must be free of timetable conflicts.", "3.19",
     ["should be free from any Timetable conflict"]),
    ("registration_order", ["backlog", "prescribed_semester_courses", "higher_level_or_repeat"], "Backlog courses "
     "come first, then the current semester's prescribed courses, then higher-level or repeat courses.", "3.25 I",
     ["(BL) is the first charge on his/her registration"]),
    ("repeat_only_if_in_programme", True, "A cleared course may be repeated to improve the grade only if it is part "
     "of the student's current programme.", "3.25 II", ["provided the course forms part of the current prescribed programme"]),
    ("not_repeatable", ["Practice School", "Thesis", "Seminar", "project courses"], "Courses that cannot be "
     "repeated (unless ACB requires it).", "3.25 II",
     ["project courses and other courses specifically so debarred in the Bulletin cannot, however, be repeated"]),
    ("electives_may_move", True, "Electives may be taken earlier or later than where the semester-wise pattern "
     "places them.", "3.25 IV", ["delay or advance taking the electives"]),
    ("grade_points", {"A": 10, "A-": 9, "B": 8, "B-": 7, "C": 6, "C-": 5, "D": 4, "E": 2},
     "Letter grades and their grade points.", "4.11",
     ["A Excellent 10", "A- Very Good 9", "B Good 8", "B- Above Average 7", "C Fair/Average 6",
      "C- Below Average 5", "D Poor 4", "E Exposed 2"]),
    ("withdrawn_ignored", True, "A W (withdrawn) is ignored; the previous performance, if any, counts.", "4.17",
     ["the W will be ignored"]),
    ("nc_compulsory_must_reregister", True, "An NC in a compulsory course must be cleared by registering again.",
     "4.20", ["required to again register in the same course"]),
    ("nc_elective_may_be_replaced", True, "An NC in an elective may be repeated or replaced by another elective.",
     "4.20", ["ignore it to choose another course"]),
    ("max_e_grades_per_semester", 1, "More than one E grade in a semester breaches the minimum academic "
     "requirements (first degree).", "5.02", ["not have secured more than one"]),
    ("min_cgpa", 4.5, "Minimum CGPA at the end of every semester (first degree).", "5.02",
     ["CGPA of at least 4.50 in the case of integrated first degree"]),
    ("max_minors", 1, "A student may be admitted to at most one minor.", "7.37",
     ["may be admitted to at most one minor program"]),
    ("minor_declaration", "end_of_second_year", "A minor must be declared at the end of the 2nd year.", "7.37",
     ["declare at the end of the 2nd year"]),
    ("audit_not_counted", True, "Audit courses never count towards programme requirements.", "7.35",
     ["cannot, even with a 'Satisfactory' grade, automatically claim acceptance"]),
    ("graduation_min_cgpa", 4.5, "Minimum CGPA to graduate (first degree).", "9.01",
     ["minimum CGPA of 4.50 in case of First Level Diploma/B.Sc./Integrated First Degree"]),
    ("minor_min_cgpa", 4.5, "Minimum CGPA in the courses applied to a minor.", "9.01a",
     ["minimum CGPA of 4.50 in the courses applied to the minor"]),
    # --- from the timetable, "VII. Instructions regarding registration"
    ("lunch_hours", [4, 5, 6], "At least one of periods 4, 5, 6 must be free every day for lunch.", None,
     ["provision for lunch hour on all days (lunch hours are 4, 5 and 6)"]),
    ("compre_must_not_clash", True, "No two registered courses may have their comprehensive exam at the same "
     "date and session.", None, ["Comprehensive Examination Dates are not clashing"]),
    ("max_extra_electives_timetable", 4, "Max four electives over the programme requirement; more only with AUGS "
     "permission, else RC.", None, ["maximum of four more electives than what is required"]),
    ("ineligible_registration_cancelled", True, "Registering in a course one is not eligible for leads to RC.",
     None, ["not eligible, their registration in it will be cancelled"]),
]


def build_rules(clauses: list[Clause], tt_page: int, tt_text: str) -> list[RegulationRule]:
    by_id = {c.clause: c for c in clauses}
    rules = []
    for rid, value, desc, cid, quotes in RULES:
        if cid:
            c = by_id.get(cid)
            haystack, src = (c.text, c.source) if c else ("", SourceRef(document=DOC, page=0, confidence="low"))
        else:
            haystack = tt_text
            src = SourceRef(document="timetable.pdf", page=tt_page, section="VII. Instructions regarding registration")
        ok = all(normalise(q) in normalise(haystack) for q in quotes)
        rules.append(RegulationRule(id=rid, value=value, description=desc, quotes=quotes,
                                    source=src, clause=cid, verified=ok))
    return rules


def parse() -> Regulations:
    clauses = parse_clauses(pymupdf.open(PDF))
    instructions, tt_page, tt_text = registration_instructions(pymupdf.open(TIMETABLE_PDF))
    return Regulations(clauses=clauses, rules=build_rules(clauses, tt_page, tt_text),
                       registration_instructions=instructions)


def run():
    result = parse()
    OUT.write_text(json.dumps(result.model_dump(), indent=1, ensure_ascii=False), encoding="utf-8")
    ids = [c.clause for c in result.clauses]
    dupes = sorted({c for c in ids if ids.count(c) > 1})
    print(f"clauses: {len(ids)}  (first {ids[0]}, last {ids[-1]})  duplicates: {dupes or 'none'}")
    bad = [r for r in result.rules if not r.verified]
    print(f"rules: {len(result.rules)}  verified: {len(result.rules) - len(bad)}")
    for r in bad:
        print(f"  NOT VERIFIED: {r.id} (clause {r.clause}) quotes {r.quotes}")
    print(f"registration instructions: {len(result.registration_instructions)}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    run()
