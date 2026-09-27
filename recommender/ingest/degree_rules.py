"""Stage 1e: institute-wide degree rules from Bulletin Part IV (IV-1, IV-2).

IV-1 has the table "The category-wise structure of each program": for each category
(Humanities Electives, Core, Open Electives, ...) the units and number of courses
required. These NUMBERS become structured data, so the engine never hard-codes them.

IV-2 states rules in prose: which courses make up the General Institutional
Requirement, the Humanities 'heads', thesis options and the dual-degree principles.
These are kept as text with page references: the engine implements them in code and
cites this text in its answers.

Usage: python -m recommender.ingest.degree_rules
"""
import json
import re

import pymupdf

from recommender.config import PROCESSED, RAW
from recommender.ingest.pdf_utils import group_rows, page_spans, span_rows
from recommender.schema import CategoryRequirement, DegreeRules, PolicyText, Range, SourceRef

PDF = RAW / "bulletin.pdf"
OUT = PROCESSED / "degree_rules.json"

TABLE_HEADING = "The category-wise structure of each program"
UNITS_X = (270, 370)      # x-range of the "Number of Units Required" column
COURSES_X = (370, 460)    # x-range of the "Number of Courses Required" column
PROSE_SPLIT = 265         # column split on the prose page (right column starts at x ~ 269)


def parse_range(text: str) -> Range:
    """'8' -> 8..8;  '6 to 9' -> 6..9;  '129 (min)' -> 129..None."""
    nums = [int(n) for n in re.findall(r"\d+", text)]
    if not nums:
        return Range()
    if "min" in text:
        return Range(min=nums[0])
    return Range(min=nums[0], max=nums[-1])


def find_table_page(doc) -> int:
    for i, page in enumerate(doc):
        if TABLE_HEADING in page.get_text() and "General Institutional Requirement" in page.get_text():
            return i
    raise RuntimeError("category table not found")


def parse_table(page) -> list[CategoryRequirement]:
    rows = group_rows(page_spans(page, (0, page.rect.height)), tolerance=3)
    heading_y = next(r[0].y0 for r in rows if TABLE_HEADING in " ".join(s.text for s in r))
    out, group, after_or = [], None, False
    for row in rows:
        if row[0].y0 <= heading_y:
            continue                                    # prose above the table
        label = " ".join(s.text.strip() for s in row if s.x0 < UNITS_X[0]).strip()
        units = " ".join(s.text.strip() for s in row if UNITS_X[0] <= s.x0 < UNITS_X[1])
        courses = " ".join(s.text.strip() for s in row if COURSES_X[0] <= s.x0 < COURSES_X[1])
        if label == "OR":
            after_or = True                             # next row is an alternative to the previous
            continue
        if re.match(r"\((I|II|III|IV)\)", label):
            group = label                               # "(I) General Institutional Requirement"
            if not units:
                continue
            category = re.sub(r"^\(\w+\)\s*", "", label)   # "(III) Open Electives 15 to 27 ..."
        elif label.startswith("Course-work") or label == "Total":
            group, category = "Overall", label          # institute-wide totals
        else:
            category = label
        if not units or not group:
            continue
        if after_or and out:
            category = f"{category} (alternative to {out[-1].category})"
            group = out[-1].group
        after_or = False
        out.append(CategoryRequirement(group=group, category=category,
                                       units=parse_range(units), courses=parse_range(courses)))
    return out


# numbered / bulleted items on the prose page, grouped by topic keywords
TOPICS = [
    ("humanities_heads", r"under the head of\s*Humanities|Languages and Literature"),
    ("general_institutional_requirement", r"General Institutional Requirement"),
    ("thesis", r"thesis could be|Thesis courses|thesis for 9 units|Thesis must"),
    ("dual_degree", r"dual|Dual|Discipline Requirements of each|open elective requirement"),
    ("overall_minimum", r"minimum\s+number of courses and units"),
]


def parse_prose(page, pno: int) -> list[PolicyText]:
    lines = [" ".join(s.text.strip() for s in row) for row in span_rows(page, PROSE_SPLIT, (40, page.rect.height - 40))]
    text = re.sub(r"\s+", " ", " ".join(lines))
    text = re.sub(r"(\w)- (\w)", r"\1\2", text)
    # split into numbered items "1. ...", "2. ..." and bullets "o ..." / "▪ ..."
    items = [i.strip() for i in re.split(r"\s(?=\d\.\s+[A-Z])|\s(?=[o▪]\s+[A-Z])", text) if len(i.strip()) > 30]
    out = []
    for item in items:
        topic = next((t for t, pat in TOPICS if re.search(pat, item)), "other")
        out.append(PolicyText(topic=topic, text=item,
                              source=SourceRef(document="bulletin.pdf", page=pno, section="Part IV - IV-2")))
    return out


def run():
    doc = pymupdf.open(PDF)
    i = find_table_page(doc)
    rules = DegreeRules(
        category_requirements=parse_table(doc[i]),
        policies=parse_prose(doc[i + 1], i + 2),
        source=SourceRef(document="bulletin.pdf", page=i + 1, section="Part IV - IV-1 category-wise structure"),
    )
    OUT.write_text(json.dumps(rules.model_dump(), indent=1, ensure_ascii=False), encoding="utf-8")
    for r in rules.category_requirements:
        print(f"  {r.group[:38]:38} {r.category[:40]:40} units {r.units.min}-{r.units.max}  courses {r.courses.min}-{r.courses.max}")
    for p in rules.policies:
        print(f"  [{p.topic}] {p.text[:110]}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    run()
