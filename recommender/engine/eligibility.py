"""Eligibility (brief Sec 5 steps 2-4): which offered courses can this student take, and what
would each one count as for them?

Deterministic. Each offered course gets:
  status   eligible | warning (allowed, but something must be checked) | ineligible | done
  reasons  why, each citing the clause / page it comes from
  category what it counts as for THIS student (per degree): backlog, CDC, GIR, DEL, HUEL, OPEL, extra
  plus timetable (sections, midsem/compre) and handout facts for the agent to explain.

Checks (Regulations / Bulletin / timetable):
  already cleared, directly or via an equivalent ........ 1.15, E4
  'not allowed if X done' exclusions ..................... Bulletin Part VI
  prerequisites (None = not stated -> 'could not be verified') 3.13
  another degree's discipline course: prior preparation of 3rd year 1st semester ... 3.15(b)(i)
  own-discipline higher-degree course: first set of own core (year-2 CDCs) ........... 3.15(b)(ii)
  higher-degree courses: max one per semester, CGPA threshold not supplied ........... 2.08
  own chart's courses of a later semester: all named courses of earlier semesters ..... 3.14(iii)
  PS / thesis / project-type courses: allotted, not freely registered ..... 2.10, timetable p.116
"""
import re
from typing import Literal

from pydantic import BaseModel, Field

from recommender.engine.requirements import RequirementReport, Slot, analyse
from recommender.ingest.handouts import evaluation_flagged
from recommender.schema import ExamSlot, StudentProfile
from recommender.store import Store, get_store

Status = Literal["eligible", "warning", "ineligible", "done"]
HIGHER_DEGREE_RE = re.compile(r"^[A-Z]+ G\d")               # G-series codes = higher-degree courses
PROJECT_NUMBERS = re.compile(r" F(266|366|367|376|377|491)$")


class SectionInfo(BaseModel):
    section: str
    kind: str
    schedule: str                          # "M W F 2"
    instructors: list[str]


class CourseOption(BaseModel):
    code: str
    title: str
    units: int | None
    status: Status
    reasons: list[str] = Field(default_factory=list)      # ineligible / done: why
    warnings: list[str] = Field(default_factory=list)     # allowed, but check this
    unverified: list[str] = Field(default_factory=list)   # facts the supplied documents don't state (brief Sec 7)
    categories: dict[str, str] = Field(default_factory=dict)  # degree -> "CDC (Y3 S1)", "DEL", "OPEL", ...
    minor: str | None = None               # "Minor in X (Core Courses)" if it counts for the student's minor
    prerequisites: str = ""                # as printed, or "not stated in the supplied documents"
    sections: list[SectionInfo] = Field(default_factory=list)
    midsem: ExamSlot | None = None
    compre: ExamSlot | None = None
    handout: dict | None = None            # evaluation summary from the handout (see handout_summary)
    description: str = ""


# ---------------------------------------------------------------- helpers

def slot_label(s: Slot) -> str:
    return f"Y{s.year} S{s.semester}" if s.year else "year/semester not given"


def is_before(s: Slot, year: int, sem: int) -> bool:
    """Slot scheduled before the current semester (summer counts after semester 2)."""
    order = {1: 1, 2: 2, "summer": 3}
    return s.year is not None and (s.year, order.get(s.semester, 0)) < (year, order[sem])


def prior_prep_missing(report: RequirementReport, degree_idx: int, year: int, sem: int) -> list[str]:
    """Named/CDC courses of semesters before (year, sem) not yet cleared (clause 3.14(iii))."""
    d = report.degrees[degree_idx]
    return ["/".join(s.options) for s in d.slots
            if s.kind in ("named", "cdc") and not s.done_via and s.semester != "summer" and is_before(s, year, sem)]


def handout_summary(store: Store, code: str) -> dict | None:
    h = store.handout(code)
    if not h:
        return None
    sec = {s.topic: s for s in h.sections}
    verified = h.weights_total is not None and not evaluation_flagged(h.needs_verification)
    return {
        "file": h.file,
        "evaluation": [{"name": c.name, "kind": c.kind, "weight": c.weight, "nature": c.nature, "date": c.date}
                       for c in h.evaluation] if verified else None,
        "midsem_weight": h.midsem_weight, "compre_weight": h.compre_weight,
        "continuous_weight": h.continuous_weight, "has_open_book": h.has_open_book,
        "evaluation_notes": h.evaluation_notes,
        "makeup_policy": sec["makeup"].text[:600] if "makeup" in sec else None,
        "attendance_policy": sec["attendance"].text[:400] if "attendance" in sec else None,
        "attendance_min_percent": h.attendance_min_percent,
        "extracted_by": h.extracted_by,
        "verified": verified,
        "page": h.evaluation_page,
    }


def section_infos(offers) -> list[SectionInfo]:
    out = []
    for t in offers:
        for s in t.sections:                 # cancelled sections are already dropped by the parser
            out.append(SectionInfo(section=s.code, kind=s.type, schedule=s.raw_schedule or "",
                                   instructors=list(s.instructors)))
    return out


# ---------------------------------------------------------------- per course

def categorise(code: str, report: RequirementReport, store: Store, profile: StudentProfile) -> dict[str, str]:
    """What the course would count as, per degree, given what is still open."""
    out = {}
    same = store.same_course(code)
    year, sem = report.study_year, report.semester
    for d in report.degrees:
        slot = next((s for s in d.remaining_slots() if same & set(s.options)), None)
        if slot:
            kind = {"cdc": "CDC", "named": "GIR", "ps_thesis": "PS/Thesis"}[slot.kind]
            tag = "backlog " if is_before(slot, year, sem) else ""
            out[d.programme] = f"{tag}{kind} ({slot_label(slot)})"
            continue
        prog = store.programme(d.programme)
        cats = {c.category: c for c in d.categories}
        del_codes = {e.course_code for e in prog.electives} | {a for e in prog.electives for a in e.aliases}
        huel = {h.course_code for h in store.course_lists.humanities_pool}
        own = {o.course_code for s in prog.core for o in s.options} | del_codes
        if same & del_codes and not cats["DEL"].complete:
            out[d.programme] = "DEL"
        elif "HUEL" in cats and same & huel and not same & own and not cats["HUEL"].complete:
            out[d.programme] = "HUEL"
        elif not cats["OPEL"].complete:
            out[d.programme] = "OPEL"
        else:
            out[d.programme] = "extra elective (beyond requirements; max 4 extra, clause 2.08)"
    return out


def minor_tag(code: str, profile: StudentProfile, store: Store) -> str | None:
    if not profile.minor:
        return None
    same = store.same_course(code)
    for m in store.minors.minors:
        if profile.minor.lower() in m.name.lower():
            for g in m.groups:
                if same & ({c.course_code for c in g.courses} | {a for c in g.courses for a in c.aliases}):
                    return f"{m.name} ({g.label})"
    return None


def check_course(code: str, profile: StudentProfile, report: RequirementReport, store: Store) -> CourseOption:
    offers = store.offering(code)
    course = store.course(code)
    title = course.title if course else offers[0].title
    units = offers[0].units if offers and offers[0].units else (course.units if course else None)
    opt = CourseOption(code=code, title=title, units=units, status="eligible",
                       sections=section_infos(offers), midsem=offers[0].midsem if offers else None,
                       compre=offers[0].compre if offers else None, handout=handout_summary(store, code),
                       description=(course.description[:500] if course else ""))
    cleared = report.cleared
    year, sem = report.study_year, report.semester
    blocks, warns = opt.reasons, opt.warnings

    # 1. already done (directly or via an equivalent) - 1.15; equivalents block re-recommendation (E4)
    done = sorted(store.same_course(code) & set(cleared))
    if done:
        opt.status = "done"
        blocks.append(f"Already cleared {'as ' + done[0] if done[0] != code else ''}".strip()
                      + " - repeating to improve a grade is possible only if it is part of your programme (3.25 II).")
        return opt

    # 2. exclusion ('those who have done X are not allowed')
    if course and course.excluded_if_completed:
        hit = [x for x in course.excluded_if_completed if store.same_course(x) & set(cleared)]
        if hit:
            blocks.append(f"Not allowed after {hit[0]}: {course.restriction_text} (Bulletin p.{course.source.page}).")

    # 3. prerequisites - 3.13
    if course is None or course.prerequisites is None:
        opt.prerequisites = "not stated in the supplied documents"
        opt.unverified.append("Prerequisites could not be verified: not stated in the supplied Bulletin/timetable "
                              "(timetable section VI refers to the AUGSD website).")
    elif course.prerequisites:
        opt.prerequisites = course.prerequisite_text
        met = [p for p in course.prerequisites if store.same_course(p) & set(cleared)]
        missing = [p for p in course.prerequisites if p not in met]
        if (course.prerequisite_logic == "any" and not met) or (course.prerequisite_logic == "all" and missing):
            need = " or ".join(missing) if course.prerequisite_logic == "any" else ", ".join(missing)
            blocks.append(f"Prerequisite not met: {need} (clause 3.13; Bulletin p.{course.source.page}).")
    else:
        opt.prerequisites = "none"

    # 4. categories for this student (needed by the prior-preparation checks)
    opt.categories = categorise(code, report, store, profile)
    opt.minor = minor_tag(code, profile, store)
    own_lists = set()
    for d in report.degrees:
        prog = store.programme(d.programme)
        own_lists |= {o.course_code for s in prog.core for o in s.options} | {e.course_code for e in prog.electives}
    same = store.same_course(code)
    in_own = bool(same & own_lists)
    in_own_chart = any(same & set(s.options) for d in report.degrees for s in d.slots)

    # 5. higher-degree courses - 2.08, 3.15(b)(ii)
    if HIGHER_DEGREE_RE.match(code):
        warns.append("Higher-degree course: at most one per semester, and only above a CGPA set by AGC "
                     "(threshold not in the supplied documents) - clause 2.08.")
        if in_own:
            missing = [m for i in range(len(report.degrees)) for m in prior_prep_missing(report, i, 3, 1)]
            if missing:
                blocks.append("Needs the first set of your discipline core courses (2nd year) cleared "
                              f"first (clause 3.15(b)(ii)); missing: {', '.join(missing[:6])}.")

    # 6. another degree's discipline course - 3.15(b)(i)
    other_discipline = any(same & ({o.course_code for s in p.core for o in s.options} | {e.course_code for e in p.electives})
                           for p in store.course_lists.programmes) and not in_own and not in_own_chart
    huel_pool = {h.course_code for h in store.course_lists.humanities_pool}
    if other_discipline and not (same & huel_pool) and not HIGHER_DEGREE_RE.match(code):
        missing = [m for i in range(len(report.degrees)) for m in prior_prep_missing(report, i, 3, 1)]
        if year < 3 or missing:
            blocks.append("Another degree's discipline course: needs the prior preparation of your 3rd year "
                          "1st semester (clause 3.15(b)(i))"
                          + (f"; not yet cleared: {', '.join(missing[:6])}." if missing else "."))

    # 7. own chart's courses of this/later semesters - prior preparation 3.14(iii)
    for i, d in enumerate(report.degrees):
        slot = next((s for s in d.remaining_slots() if same & set(s.options)), None)
        if slot and slot.year and not is_before(slot, year, sem) and slot.semester != "summer":
            missing = prior_prep_missing(report, i, slot.year, slot.semester)
            if len(missing) > 2:
                blocks.append(f"Scheduled for {slot_label(slot)}: all named courses of earlier semesters must be "
                              f"cleared first (clause 3.14(iii)); {len(missing)} missing, e.g. {', '.join(missing[:4])}.")
            elif missing:
                warns.append(f"Scheduled for {slot_label(slot)}: {', '.join(missing)} from earlier semesters not "
                             "cleared - the DCA may allow up to two (clause 3.14(iii)).")
            if (slot.year, slot.semester) != (year, sem):
                warns.append(f"Higher-level course (your chart places it in {slot_label(slot)}): allowed only if "
                             "you also register all backlog and current-semester courses (clause 3.25 III).")

    # 8. PS / thesis / project-type courses are allotted - 2.10, timetable p.116 item 3
    if code in ("BITS F221", "BITS F412") or (code.startswith("BITS F4") and code.endswith("T")):
        blocks.append("Practice School / thesis: allotted through the PS Division / AUGSD, not a regular "
                      "registration (clause 2.10).")
    elif PROJECT_NUMBERS.search(code):
        warns.append("Project-type course: needs allotment by Associate Dean, AUGS Division (timetable p.116, item 3).")

    if not opt.handout or not opt.handout["verified"]:
        opt.unverified.append("Evaluation scheme could not be verified from the handout.")
    opt.status = "ineligible" if blocks else "warning" if warns else "eligible"
    return opt


# ---------------------------------------------------------------- entry points

def check_all(profile: StudentProfile, report: RequirementReport | None = None,
              store: Store | None = None) -> list[CourseOption]:
    """Every in-scope course offered this semester, checked for this student."""
    store = store or get_store()
    report = report or analyse(profile, store)
    return [check_course(code, profile, report, store) for code in sorted(store.offered_codes())]


PRIORITY = ["backlog CDC", "backlog GIR", "CDC", "GIR", "DEL", "HUEL", "OPEL", "extra"]


def priority(opt: CourseOption) -> int:
    """Lower = more urgent: backlog first (3.25 I), then this semester's named courses, then electives."""
    ranks = [next((i for i, p in enumerate(PRIORITY) if c.startswith(p)), len(PRIORITY))
             for c in opt.categories.values()]
    return min(ranks, default=len(PRIORITY))
