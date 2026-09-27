"""Stage 1f: parse Bulletin Part IV 'Minor Programmes for First Degree Students' (IV-129-141).

IV-129 states the general rules for every minor (courses/units, overlap with the major,
GPA, when to declare). Each following minor is a bordered table:

  Minor in X | Description | Courses & Units Req. | Core Courses / Electives groups -> courses

The group label ("Core Courses", "Electives (Science Pool) 01 (min)") is printed
vertically centred beside its group, so its text position does NOT tell where the group
starts or ends. The drawn table borders do: a horizontal border that crosses the LABEL
column separates two groups (inside a group, row borders only span the course columns).
So PyMuPDF's table detector gives us the cells, and the borders crossing the label column
cut the table into bands; every course row belongs to the band its centre falls in.

Usage: python -m recommender.ingest.minors
"""
import json
import re

import pymupdf

from recommender.codes import CODE_RE, normalize_code
from recommender.config import PROCESSED, RAW
from recommender.ingest.pdf_utils import span_rows
from recommender.schema import ListedCourse, Minor, MinorGroup, MinorProgrammes, PolicyText, SourceRef

PDF = RAW / "bulletin.pdf"
OUT = PROCESSED / "minors.json"

START_HEADING = "MINOR PROGRAMMES FOR FIRST"
END_HEADING = "2+2 INTERNATIONAL"
PROSE_SPLIT = 258
MINOR_RE = re.compile(r"Minor in [^\n]+")


def find_pages(doc) -> range:
    start = next(i for i, p in enumerate(doc) if START_HEADING in p.get_text() and "Requirements for a minor" in p.get_text())
    end = next(i for i in range(start + 1, len(doc)) if END_HEADING in doc[i].get_text())
    return range(start, end)


def cell(c) -> str:
    return re.sub(r"\s+", " ", c or "").strip()


def parse_units_cell(text: str) -> tuple[int | None, int | None]:
    """'06 courses (min) 18 units (min)' -> (6, 18)."""
    courses = re.search(r"(\d+)\s*courses?", text, re.IGNORECASE)
    units = re.search(r"(\d+)\s*units?", text, re.IGNORECASE)
    return (int(courses.group(1)) if courses else None, int(units.group(1)) if units else None)


def parse_code_cell(text: str) -> tuple[str | None, list[str]]:
    """'AN F311' -> ('AN F311', []);  'EEE/INSTR F432' -> ('EEE F432', ['INSTR F432'])."""
    m = re.match(r"^([A-Z]{2,5}(?:\s*/\s*[A-Z]{2,5})+)\s+([A-Z]\d{3}[A-Z]?)$", text)
    if m:
        depts = [d.strip() for d in m.group(1).split("/")]
        codes = [f"{d} {m.group(2)}" for d in depts]
        return codes[0], codes[1:]
    m = CODE_RE.search(text)
    return (normalize_code(m.group(0)), []) if m else (None, [])


WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def parse_group_label(text: str) -> MinorGroup:
    """'Electives (Science Pool) 01 (min)' -> min 1;  'Electives (Any two)' -> min 2."""
    m = re.search(r"(\d+)\s*\(min\)|minimum of (\d+)|any (\w+)", text, re.IGNORECASE)
    n = None
    if m:
        raw = next(g for g in m.groups() if g)
        n = int(raw) if raw.isdigit() else WORD_NUMBERS.get(raw.lower())
    return MinorGroup(label=text, is_core=text.lower().startswith("core"), min_courses=n)


def title_above(page, table_top: float) -> str | None:
    """A minor's name printed just above its table instead of inside it."""
    text = page.get_text(clip=pymupdf.Rect(0, max(0, table_top - 40), page.rect.width, table_top))
    m = MINOR_RE.search(text)
    return m.group(0).strip() if m else None


def title_below(page, table_bottom: float) -> str | None:
    """A minor's name printed at the bottom of a page, its table starting on the next page."""
    m = MINOR_RE.search(page.get_text(clip=pymupdf.Rect(0, table_bottom, page.rect.width, page.rect.height)))
    return m.group(0).strip() if m else None


def label_column_borders(page, table) -> tuple[float, list[float]]:
    """(right edge of the label column, y of every horizontal border crossing it)."""
    x0, y0, x1, y1 = table.bbox
    label_right = min(c[2] for c in table.cells if c and c[0] <= x0 + 2)
    ys = set()
    for drawing in page.get_drawings():
        for item in drawing["items"]:
            if item[0] == "l":
                a, b = item[1], item[2]
                if abs(a.y - b.y) < 1 and min(a.x, b.x) <= x0 + 3 and max(a.x, b.x) > x0 + 20:
                    ys.add(round(a.y, 1))
            elif item[0] == "re" and item[1].height < 2:
                r = item[1]
                if r.x0 <= x0 + 3 and r.x1 > x0 + 20:
                    ys.add(round(r.y0, 1))
    return label_right, sorted(y for y in ys if y0 - 1 <= y <= y1 + 1)


def parse_minor_table(page, table, minor: Minor, pno: int) -> None:
    rows = table.extract()
    label_right, borders = label_column_borders(page, table)
    words = page.get_text("words")

    def band_of(y: float) -> int:
        return sum(1 for b in borders if b <= y)

    def band_text(k: int, x_max: float | None = None) -> str:
        lo = borders[k - 1] if k > 0 else table.bbox[1]
        hi = borders[k] if k < len(borders) else table.bbox[3]
        ws = [w for w in words if lo <= (w[1] + w[3]) / 2 < hi and table.bbox[0] <= w[0] < (x_max or table.bbox[2])]
        return " ".join(w[4] for w in sorted(ws, key=lambda w: (round(w[1]), w[0])))

    groups: dict[int, MinorGroup] = {}
    pending_or = False
    for r, row in enumerate(rows):
        c = [cell(x) for x in row]
        y_mid = (table.rows[r].bbox[1] + table.rows[r].bbox[3]) / 2
        k = band_of(y_mid)
        label = band_text(k, label_right)
        if label.startswith("Description") and not minor.description:
            minor.description = re.sub(r"^Description\s*", "", band_text(k))
            continue
        if label.startswith("Courses & Units"):
            minor.min_courses, minor.min_units = parse_units_cell(band_text(k))
            continue
        if len(c) > 1 and c[1].lower() == "or":
            pending_or = True
            continue
        code, aliases = parse_code_cell(c[1]) if len(c) > 1 else (None, [])
        if not code:
            continue
        if k not in groups:
            if not label and minor.groups:
                groups[k] = minor.groups[-1]          # group continues from the previous table/page
            else:
                groups[k] = parse_group_label(label or "(unlabelled group)")
                minor.groups.append(groups[k])
        nums = (c[3:6] + ["", "", ""])[:3] if len(c) >= 6 else ["", "", ""]
        units_txt = nums[2].rstrip("*")
        groups[k].courses.append(ListedCourse(
            course_code=code, aliases=aliases, title=c[2] if len(c) > 2 else "",
            lecture_hours=int(nums[0]) if nums[0].isdigit() else None,
            practical_hours=int(nums[1]) if nums[1].isdigit() else None,
            units=int(units_txt) if units_txt.isdigit() else None,
            variable_units=nums[2].endswith("*"), alternative_to_previous=pending_or, page=pno))
        pending_or = False


def general_rules(page, pno: int) -> list[PolicyText]:
    lines = [" ".join(s.text.strip() for s in r) for r in span_rows(page, PROSE_SPLIT, (40, page.rect.height - 40))]
    text = re.sub(r"\s+", " ", " ".join(lines))
    start = text.find("Requirements for a minor")
    items = re.split(r"\s(?=[●o]\s)", text[start:]) if start >= 0 else []
    return [PolicyText(topic="minor", text=i.strip(" ●o"), source=SourceRef(document="bulletin.pdf", page=pno,
                                                                          section="Part IV - Minor Programmes"))
            for i in items if len(i) > 40 and re.search(r"\d|must|may|No course|at most", i)]


def parse() -> MinorProgrammes:
    doc = pymupdf.open(PDF)
    pages = find_pages(doc)
    rules = general_rules(doc[pages.start], pages.start + 1)
    minors: list[Minor] = []
    carried_title: str | None = None       # title printed at the bottom of the previous page
    for i in pages:
        page = doc[i]
        tables = page.find_tables().tables
        for n, table in enumerate(tables):
            first = cell(table.extract()[0][0])
            name = (first if first.startswith("Minor in") else
                    title_above(page, table.bbox[1]) or (carried_title if n == 0 else None))
            if name and (not minors or minors[-1].name != name):
                minors.append(Minor(name=name, source=SourceRef(document="bulletin.pdf", page=i + 1,
                                                                section="Part IV - Minor Programmes")))
            elif not minors:
                continue                                  # a table before the first minor
            parse_minor_table(page, table, minors[-1], i + 1)
        carried_title = title_below(page, tables[-1].bbox[3]) if tables else None
    for m in minors:
        if not any(g.is_core for g in m.groups):
            m.needs_verification.append("no core group found")
        if m.min_courses is None:
            m.needs_verification.append("course/unit requirement not found")
    return MinorProgrammes(minors=minors, general_rules=rules)


def run():
    result = parse()
    OUT.write_text(json.dumps(result.model_dump(), indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"minors: {len(result.minors)}")
    for m in result.minors:
        groups = ", ".join(f"{'core' if g.is_core else g.label[:28]}:{len(g.courses)}"
                           + (f"(min {g.min_courses})" if g.min_courses else "") for g in m.groups)
        print(f"  {m.name[:44]:44} req {m.min_courses}c/{m.min_units}u  [{groups}] {m.needs_verification or ''}")
    print(f"general rules: {len(result.general_rules)}")
    for r in result.general_rules:
        print(f"   - {r.text[:120]}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    run()
