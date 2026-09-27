"""Stage 2: course handouts -> evaluation scheme, policies and topics (regex pass).

Each handout ("Course Handout - Part II") follows a loose template: numbered sections
(Course Description, Scope, Text Books, Course Plan, Evaluation Scheme, Make-up,
Attendance, Grading, Chamber Consultation, Notices) and an evaluation TABLE.

What becomes DATA vs TEXT:
  - numbers that preferences need: component weights, midsem/compre share, open/closed
    book, attendance % -> structured fields;
  - policy prose (make-up conditions, grading/NC rules) -> kept as quoted text with its
    page. Turning "make-up only on genuine medical grounds" into yes/no by regex would be
    guessing; the agent quotes the text instead.

Evaluation table: read with PyMuPDF's table detector. Header labels are often shifted
one column from the numbers, so columns are identified by CONTENT: the weight column is
the one whose numbers sum to ~100 (headers only break ties). Built-in check: weights
must total 100 +- 1, otherwise the handout is flagged (and later sent to the LLM fallback).

Usage: python -m recommender.ingest.handouts
"""
import json
import re
from collections import Counter

import pymupdf

from recommender.codes import find_codes
from recommender.config import PROCESSED, RAW_HANDOUTS, TEXT_HANDOUTS
from recommender.schema import EvaluationComponent, Handout, HandoutSection, SourceRef

OUT = PROCESSED / "handouts.json"
PAGE_RE = re.compile(r"^===== PAGE (\d+) =====$")

COURSE_NO_RE = re.compile(r"Course\s*(?:No|Number|Code)\.?\s*[:\-]?\s*(.+)", re.IGNORECASE)
TITLE_RE = re.compile(r"Course\s*Title\s*[:\-]?\s*(.+)", re.IGNORECASE)
IC_RE = re.compile(r"Instructor[\s-]*in[\s-]*charge\s*[:\-]?\s*(.+)", re.IGNORECASE)
PROJECT_RE = re.compile(r"project|thesis|seminar|dissertation|independent study|practice school|reading course",
                        re.IGNORECASE)
PROJECT_NUMBERS = {"F266", "F366", "F367", "F376", "F377", "F491"}   # study/lab/design project numbers

# ---------------------------------------------------------------- sections

HEADING_RE = re.compile(r"^\s*(\d{1,2})\s*[.)]\s*([A-Za-z][^:\n]{2,70}?)\s*(?::.*)?$")
TOPICS = [                                   # first match wins
    ("makeup", r"make[\s-]*up"),
    ("attendance", r"attendance"),
    ("evaluation", r"evaluation|assessment"),
    ("grading", r"grading|\bnc\b|passing|criteri"),
    ("description", r"course description"),
    ("outcomes", r"outcome"),
    ("scope", r"scope|objective"),
    ("textbooks", r"text\s*-?\s*books?|reference"),
    ("course_plan", r"course plan|lecture plan|plan of work|lecture[\s-]*wise|schedule|course content|topics"),
    ("consultation", r"chamber|consultation|office hour"),
    ("notices", r"notice"),
    ("academic_honesty", r"honesty|integrity|conduct|plagiarism"),
]
FALLBACK_TOPICS = {"makeup": r"make[\s-]*up", "attendance": r"attendance"}   # searched in body text if no heading


def topic_of(heading: str) -> str:
    h = heading.lower()
    return next((t for t, p in TOPICS if re.search(p, h)), "other")


def read_pages(path) -> list[tuple[int, str]]:
    """Lines of the extracted text with their page numbers."""
    out, page = [], 1
    for line in path.read_text(encoding="utf-8").splitlines():
        m = PAGE_RE.match(line)
        if m:
            page = int(m.group(1))
        elif line.strip():
            out.append((page, line.rstrip()))
    return out


def parse_sections(lines: list[tuple[int, str]]) -> list[HandoutSection]:
    """Split at numbered headings ('10. Make-up Policy:'); text runs to the next heading."""
    sections: list[HandoutSection] = []
    for page, line in lines:
        m = HEADING_RE.match(line)
        if m and len(m.group(2).split()) <= 8:
            heading = m.group(2).strip()
            rest = line.split(":", 1)[1].strip() if ":" in line else ""
            sections.append(HandoutSection(topic=topic_of(heading), heading=heading, text=rest, page=page))
        elif sections:
            sections[-1].text += "\n" + line.strip()
    for s in sections:
        s.text = s.text.strip()
    have = {s.topic for s in sections}
    for topic, pattern in FALLBACK_TOPICS.items():   # policy stated without its own heading
        if topic in have:
            continue
        for i, (page, line) in enumerate(lines):
            if re.search(pattern, line, re.IGNORECASE):
                text = " ".join(l.strip() for _, l in lines[i:i + 4])
                sections.append(HandoutSection(topic=topic, heading="(found in text)", text=text, page=page))
                break
    return sections


ATTENDANCE_PCT_RE = re.compile(
    r"(\d{2,3})\s*%\s*(?:of\s+)?(?:the\s+)?(?:attendance|classes|lectures|sessions)"
    r"|attendance[^.%]{0,80}?(\d{2,3})\s*%", re.IGNORECASE)


def attendance_percent(sections: list[HandoutSection]) -> int | None:
    for s in sections:
        if s.topic == "attendance":
            m = ATTENDANCE_PCT_RE.search(s.text)
            if m:
                pct = int(m.group(1) or m.group(2))
                if 50 <= pct <= 100:
                    return pct
    return None

# ---------------------------------------------------------------- evaluation table

KINDS = [                                    # first match wins
    ("compre", r"compre|comprehensive|\bcomp\.|end[\s-]*sem|final\s+exam"),
    ("midsem", r"mid[\s-]*(?:sem|term|test)|\bmid\b"),
    ("quiz", r"quiz|(?:class|surprise|lecture|internal|declared|announced)\s*tests?"),
    ("lab", r"\blab|practical|experiment"),
    ("tutorial", r"tutorial"),
    ("assignment", r"assignment|home[\s-]*work|take[\s-]*home"),
    ("project", r"project"),
    ("seminar", r"seminar|presentation"),
    ("viva", r"viva"),
    ("participation", r"participation|attendance|class\s*(?:performance|activit)"),
    ("report", r"report"),
]
COMPONENT_WORDS = re.compile(r"mid|compre|quiz|exam|test|assignment|project|lab|tutorial|seminar|viva", re.IGNORECASE)
NOTE_RE = re.compile(r"^\s*(?:[#*†]|note\b|\(?\*)", re.IGNORECASE)
TOTAL_RE = re.compile(r"^\s*(?:grand\s+)?total\b", re.IGNORECASE)

ROLE_HEADERS = {
    "weight": r"weight|wt\b|%|marks",
    "duration": r"duration",
    "date": r"date|time|schedule",
    "nature": r"nature|remark|comment|mode|book",
}
ROLE_CONTENT = {
    "duration": r"\bmin|\bhour|\bhr|\d\s*h\b",
    "date": r"\d{1,2}\s*/\s*\d{1,2}|tba|announc|\b[AF]N\d?\b|week|<test",
    "nature": r"book|\bcb\b|\bob\b|take[\s-]*home",
}


def clean(cell) -> str:
    return re.sub(r"\s+", " ", cell or "").replace("", "").strip()


def parse_weight(cell: str) -> float | None:
    """'30%' -> 30, '50 (25%)' -> 25 (the % number wins), '20+10' -> 30, '30 (60)' -> 30."""
    pct = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", cell)
    if pct:
        return float(pct.group(1))
    plus = re.fullmatch(r"\s*(\d{1,3}(?:\.\d+)?)\s*\+\s*(\d{1,3}(?:\.\d+)?)\s*", cell)
    if plus:
        return float(plus.group(1)) + float(plus.group(2))
    num = re.match(r"\s*(\d{1,3}(?:\.\d+)?)\b", cell)
    return float(num.group(1)) if num else None


def component_kind(name: str) -> str:
    n = name.lower()
    return next((k for k, p in KINDS if re.search(p, n)), "other")


def open_book(text: str) -> str | None:
    t = text.lower()
    is_open = bool(re.search(r"open|\bob\b", t))
    is_closed = bool(re.search(r"\bclose[d]?\b|\bcb\b", t))          # "Closed Book" and "Close Book"
    if is_open and (is_closed or "partly" in t or "part" in t):
        return "partly"
    return "open" if is_open else "closed" if is_closed else None


def candidate_tables(doc) -> list[tuple[int, list[list[str]]]]:
    """Tables mentioning weights and exam-like components, each merged with its continuation
    on the next page (a table whose top is near the top of that page, same column count)."""
    found = []
    tables = {i: page.find_tables().tables for i, page in enumerate(doc)}
    for i, ts in tables.items():
        for t in ts:
            rows = [[clean(c) for c in r] for r in t.extract()]
            flat = " ".join(" ".join(r) for r in rows).lower()
            if not (re.search(r"weight|marks|%|wt\b", flat) and COMPONENT_WORDS.search(flat)):
                continue
            nxt = tables.get(i + 1, [])
            if t is ts[-1] and nxt and nxt[0].bbox[1] < 160 and len(nxt[0].extract()[0]) == len(rows[0]):
                cont = [[clean(c) for c in r] for r in nxt[0].extract()]
                if not re.search(r"weight|component", " ".join(cont[0]).lower()):
                    rows += cont
            found.append((i + 1, rows))
    return found


def analyse_table(rows: list[list[str]]) -> tuple[list[EvaluationComponent], float | None, list[str]]:
    """Identify columns by content, then read one component per row."""
    ncols = len(rows[0])
    header_end = next((k for k, r in enumerate(rows) if any(COMPONENT_WORDS.search(c) for c in r)
                       and any(parse_weight(c) is not None for c in r)), 0)
    header = [" ".join(rows[k][c] for k in range(header_end)).lower() for c in range(ncols)]
    data = [r for r in rows[header_end:] if any(r)]

    # name column: the one holding component words most often
    name_col = max(range(ncols), key=lambda c: sum(bool(COMPONENT_WORDS.search(r[c])) for r in data))

    # weight column: numbers summing closest to 100 (header 'weight' breaks ties)
    def col_sum(c):
        vals = [parse_weight(r[c]) for r in data if r[name_col] and not TOTAL_RE.match(r[name_col])
                and not NOTE_RE.match(r[name_col])]
        return sum(v for v in vals if v is not None), sum(v is not None for v in vals)
    scores = []
    for c in range(ncols):
        if c == name_col:
            continue
        total, n = col_sum(c)
        if n:
            scores.append((abs(total - 100) > 1, not re.search(ROLE_HEADERS["weight"], header[c]), c))
    notes = []
    if not scores:
        return [], None, ["evaluation table has no numeric weight column"]
    weight_col = min(scores)[2]

    def role_col(role):
        best, best_score = None, 0.0
        for c in range(ncols):
            if c in (name_col, weight_col):
                continue
            score = 2.0 * bool(re.search(ROLE_HEADERS[role], header[c]))
            if role in ROLE_CONTENT and data:
                score += 3.0 * sum(bool(re.search(ROLE_CONTENT[role], r[c], re.IGNORECASE)) for r in data) / len(data)
            if score > best_score:
                best, best_score = c, score
        return best
    cols = {role: role_col(role) for role in ("duration", "date", "nature")}

    # the name can spill over merged columns: join every non-role, non-numeric cell (drops S.No.)
    role_cols = {weight_col} | {c for c in cols.values() if c is not None}
    text_cols = [c for c in range(ncols) if c not in role_cols]

    def row_name(r):
        return " ".join(r[c] for c in text_cols if r[c] and not re.fullmatch(r"[\d.\s]+", r[c]))

    comps: list[EvaluationComponent] = []
    for r in data:
        name = row_name(r)
        w = parse_weight(r[weight_col])
        if TOTAL_RE.match(name) or (not name and TOTAL_RE.match(" ".join(r))):
            continue
        if NOTE_RE.match(name) and w is None:
            notes.append(name)
            continue
        if name and w is None:
            if comps and not any(r[c] for c in role_cols):
                comps[-1].name += " " + name         # wrapped component name
            continue
        if w is None or not name:
            continue          # a nameless row is a wrapped cell, a sub-part or an unlabelled total
        get = lambda role: (r[cols[role]] or None) if cols[role] is not None else None
        nature = get("nature")
        comps.append(EvaluationComponent(
            name=name or "(unnamed)", kind=component_kind(name), weight=w, duration=get("duration"),
            date=get("date"), nature=nature,
            open_book=open_book(nature if nature else " ".join(x for k, x in enumerate(r) if k != weight_col))))
    total = sum(c.weight for c in comps) if comps else None
    marks_only = "marks" in header[weight_col] and not re.search(r"weight|wt\b|%", header[weight_col])
    if total and abs(total - 100) > 1 and marks_only:
        for c in comps:                      # weights printed as marks: convert to % of the total
            c.weight = round(c.weight * 100 / total, 2)
        notes.append(f"weights converted from marks (total {total:g})")
        total = 100.0
    return comps, total, notes


def best_evaluation(doc):
    """The candidate table whose weights come closest to 100."""
    best = None
    for page, rows in candidate_tables(doc):
        comps, total, notes = analyse_table(rows)
        if not comps:
            continue
        key = abs((total or 0) - 100)
        if best is None or key < best[0]:
            best = (key, page, comps, total, notes)
    return best

# ---------------------------------------------------------------- per handout

STOPWORDS = {"and", "the", "for", "with", "of", "in", "to", "an", "introduction"}
_titles: dict[str, list[str]] | None = None


def reference_titles(code: str) -> list[str]:
    """Titles the catalogue and the timetable give for a code (loaded once)."""
    global _titles
    if _titles is None:
        _titles = {}
        for name, key, field in (("courses.json", "course_code", "title"), ("timetable.json", "course_code", "title")):
            path = PROCESSED / name
            if path.exists():
                for r in json.loads(path.read_text(encoding="utf-8")):
                    _titles.setdefault(r[key], []).append(r[field])
    return _titles.get(code, [])


def handout_codes(text: str, file_code: str | None) -> tuple[list[str], list[str]]:
    """Which courses a handout is for. The printed 'Course No.' and the file name usually agree;
    when they don't, the printed TITLE decides:
      - file-name code whose title matches -> kept (renumbered course, e.g. BIO U101 = BIO F101);
      - printed code whose known title does NOT match -> dropped as a misprint (the CS F215
        'Digital Design' handout prints 'Course No. ... F342');
      - file-name code whose title doesn't match -> not used, flagged (wrong document in the file)."""
    cm = COURSE_NO_RE.search(text)
    printed = list(dict.fromkeys(find_codes(cm.group(1)))) if cm else []
    if not printed:
        return ([file_code] if file_code else []), []
    if not file_code or file_code in printed:     # file name and printed number agree: trust them
        return printed, []
    header = text[cm.start():cm.start() + 250] + " " + ((TITLE_RE.search(text) or [None, ""])[1])
    bad_numbers = {c.split()[1] for c in printed if reference_titles(c) and not same_title(c, header)}
    kept = [c for c in printed if c.split()[1] not in bad_numbers]   # a misprinted group goes as a whole
    flags = [f"printed course number {c} does not match the handout's title - ignored as a misprint"
             for c in printed if c not in kept]
    if file_code and file_code not in kept:
        if same_title(file_code, header):
            kept.insert(0, file_code)
        else:
            flags.append(f"file name says {file_code}, but the handout is for {', '.join(printed)} "
                         f"(titles differ) - not used for {file_code}")
    return kept, flags


def same_title(code: str, header: str) -> bool:
    """Do most words of the code's known title appear in the handout header?"""
    head = header.lower()
    for t in reference_titles(code):
        words = [w for w in re.findall(r"[a-z]{3,}", t.lower()) if w not in STOPWORDS]
        if words and sum(w in head for w in words) / len(words) >= 0.6:
            return True
    return False


def parse_handout(pdf_path) -> Handout:
    txt_path = TEXT_HANDOUTS / f"{pdf_path.stem}.txt"
    lines = read_pages(txt_path)
    text = "\n".join(l for _, l in lines)
    m = re.match(r"\d+_([A-Z]+)_([A-Z]\d{3}[A-Z]?)", pdf_path.stem)
    file_code = f"{m.group(1)} {m.group(2)}" if m else None
    src = SourceRef(document=f"handouts/{pdf_path.name}", page=1, section="Course Handout - Part II")

    if len(text) < 200:
        return Handout(file=pdf_path.name, course_codes=[file_code] if file_code else [], extracted_by="none",
                       source=src.model_copy(update={"confidence": "low"}),
                       needs_verification=["scanned handout: no text layer"])

    codes, flags = handout_codes(text, file_code)
    title = (TITLE_RE.search(text) or [None, ""])[1].strip()
    ic = re.sub(r"\(.*?\)|\S+@\S+", "", (IC_RE.search(text) or [None, ""])[1]).strip(" ,:;-")
    sections = parse_sections(lines)
    project = any(c.split()[1] in PROJECT_NUMBERS for c in codes) or bool(PROJECT_RE.search(title))

    h = Handout(file=pdf_path.name, course_codes=codes, title=title, instructor_in_charge=ic,
                sections=sections, attendance_min_percent=attendance_percent(sections),
                project_type=project, source=src, needs_verification=flags)

    best = best_evaluation(pymupdf.open(pdf_path))
    if best:
        _, page, comps, total, notes = best
        h.evaluation_notes = notes
        problem = evaluation_problem(comps)
        if problem:
            h.evaluation, h.evaluation_page, h.weights_total = comps, page, total
            h.needs_verification.append(problem)
        else:
            set_evaluation(h, comps, page)
    elif not project:
        h.needs_verification.append("no evaluation table found")
    return h


EVALUATION_FLAGS = ("evaluation", "no evaluation", "scanned", "LLM fallback")


def evaluation_flagged(flags: list[str]) -> bool:
    """Flags about the evaluation scheme (what the LLM fallback can fix), as opposed to notes
    about course numbers."""
    return any(f.startswith(EVALUATION_FLAGS) for f in flags)


def evaluation_problem(comps: list[EvaluationComponent]) -> str | None:
    """The checks every evaluation must pass, whoever extracted it (regex or LLM)."""
    if not comps:
        return "no evaluation components"
    total = sum(c.weight or 0 for c in comps)
    if abs(total - 100) > 1:
        return f"evaluation weights total {total:g}, not 100"
    if any(not re.search(r"[A-Za-z]", c.name) for c in comps):
        return "evaluation component names not readable (matrix-style table)"
    return None


def set_evaluation(h: Handout, comps: list[EvaluationComponent], page: int | None) -> None:
    """Store a validated evaluation and derive the preference fields from it."""
    h.evaluation, h.evaluation_page = comps, page
    h.weights_total = sum(c.weight for c in comps)
    h.midsem_weight = sum(c.weight for c in comps if c.kind == "midsem")
    h.compre_weight = sum(c.weight for c in comps if c.kind == "compre")
    h.continuous_weight = round(h.weights_total - h.midsem_weight - h.compre_weight, 2)
    kinds = {c.open_book for c in comps}
    h.has_open_book = True if kinds & {"open", "partly"} else False if kinds == {"closed"} else None


def parse() -> list[Handout]:
    return [parse_handout(p) for p in sorted(RAW_HANDOUTS.glob("*.pdf"))]


def run():
    handouts = parse()
    OUT.write_text(json.dumps([h.model_dump() for h in handouts], indent=1, ensure_ascii=False), encoding="utf-8")
    ok = [h for h in handouts if h.weights_total is not None and abs(h.weights_total - 100) <= 1]
    print(f"handouts: {len(handouts)}  evaluation ok (sum 100): {len(ok)}  "
          f"flagged: {sum(1 for h in handouts if h.needs_verification)}  "
          f"project-type without table: {sum(1 for h in handouts if h.project_type and not h.evaluation)}")
    print("flags:", Counter(re.sub(r"\d[\d.]*", "N", f) for h in handouts for f in h.needs_verification).most_common())
    print("section topics:", Counter(s.topic for h in handouts for s in h.sections if s.topic != "other").most_common())
    print("kinds:", Counter(c.kind for h in handouts for c in h.evaluation).most_common())
    print(f"attendance % stated: {sum(1 for h in handouts if h.attendance_min_percent)}  "
          f"open-book somewhere: {sum(1 for h in handouts if h.has_open_book)}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    run()
