"""Requirement analysis (brief Sec 5 step 1): what has a student completed, and what remains?

Deterministic - no LLM. Every item carries the page/clause it comes from.

Per degree:
  * named slots from the semester chart (GIR courses, PS-I/PS-II or thesis alternatives) and
    CDC slots from the course list (CDC_SOURCE decision: the list defines CDCs, the chart only
    gives year/semester; list-vs-chart conflicts are shown as `disputed` / notes);
  * elective categories DEL, HUEL, OPEL with the minimum units/courses.
Cleared courses fill slots first; the rest count as DEL (programme's elective list), then HUEL
(humanities pool, not own discipline - Bulletin IV-p.335), then OPEL - clause 2.05: anything
beyond the DEL/HUEL requirement is an open elective.

"Cleared" (Regulations 1.15-1.16, 4.17): the LATEST performance in a course is a letter grade
(E included); W is ignored; NC/I/RC/GA are not cleared. An equivalent code counts (assumption E5,
shown as "via X").

Dual degree (Bulletin IV-2): GIR and HUEL are met once (first degree); each degree's CDCs and
DELs are met separately, a course satisfying both counts for both; a DEL of one degree can fill
the other's OPEL (falls out naturally: it is 'anything else' for that degree).
"""
import re
from typing import Literal

from pydantic import BaseModel, Field

from recommender.codes import normalize_code
from recommender.config import MAX_SUPPORTED_BATCH
from recommender.schema import SourceRef, StudentProfile
from recommender.store import Store, get_store

SEMESTER_YEAR, SEMESTER = 2026, 1          # First Semester 2026-27 (the supplied timetable)
NOT_CLEARED = {"NC", "I", "RC", "RRA", "DP", "GA"}
PS_CODES = {"BITS F221", "BITS F412"}      # Practice School I / II


class Slot(BaseModel):
    """One named requirement: a course, or 'X or Y' alternatives."""
    kind: Literal["cdc", "named", "ps_thesis", "disputed"]
    options: list[str]
    title: str = ""
    year: int | None = None
    semester: int | str | None = None
    units: int | None = None
    done_via: str | None = None           # the cleared code that satisfies it (maybe an equivalent)
    in_progress: str | None = None        # registered this semester, not yet cleared
    note: str | None = None
    source: SourceRef


class CategoryStatus(BaseModel):
    category: Literal["DEL", "HUEL", "OPEL"]
    required_units: int
    required_courses: int
    done_units: int = 0
    done_courses: int = 0
    courses: list[str] = Field(default_factory=list)
    slots_in_chart: list[str] = Field(default_factory=list)   # "Y3 S1: Discipline Electives 3(min)"
    source: SourceRef

    @property
    def remaining_units(self) -> int:
        return max(0, self.required_units - self.done_units)

    @property
    def remaining_courses(self) -> int:
        return max(0, self.required_courses - self.done_courses)

    @property
    def complete(self) -> bool:
        return self.remaining_units == 0 and self.remaining_courses == 0


class DegreeReport(BaseModel):
    programme: str
    chart: str | None = None
    slots: list[Slot] = Field(default_factory=list)
    categories: list[CategoryStatus] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def remaining_slots(self) -> list[Slot]:
        return [s for s in self.slots if not s.done_via and s.kind != "disputed"]


class RequirementReport(BaseModel):
    study_year: int                        # year of study in First Semester 2026-27
    semester: int = SEMESTER
    cleared: dict[str, str | None]         # code -> grade, latest performance only
    in_progress: list[str] = Field(default_factory=list)   # registered this semester
    degrees: list[DegreeReport] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    caveat: str = "Requirements are based on Bulletin 2025-26 and Academic Regulations 2023."


# ---------------------------------------------------------------- helpers

def cleared_courses(profile: StudentProfile) -> dict[str, str | None]:
    """Latest performance per course (list order = chronological); keep only cleared ones."""
    latest: dict[str, str | None] = {}
    for c in profile.completed:
        grade = (c.grade or "").strip().upper() or None
        if grade == "W":                   # 4.17: a withdrawal is ignored
            continue
        latest[normalize_code(c.code)] = grade
    return {code: g for code, g in latest.items() if g not in NOT_CLEARED}


def done_via(code: str, cleared: dict, store: Store) -> str | None:
    """The cleared code (this one or an equivalent) that satisfies `code`."""
    if code in cleared:
        return code
    return next((c for c in sorted(store.equivalents(code)) if c in cleared), None)


def units_of(code: str, store: Store) -> int:
    """Units of the code actually taken (E6): catalogue, else timetable, else 3 with no guarantee."""
    course = store.courses.get(code)
    if course and course.units:
        return course.units
    offer = store.offering(code)
    if offer and offer[0].units:
        return offer[0].units
    return 3


def category_row(store: Store, name: str):
    return next(r for r in store.degree_rules.category_requirements if r.category.startswith(name))


# ---------------------------------------------------------------- slots

def chart_slots(chart, core_codes: set[str], chart_only: set[str], store: Store) -> list[Slot]:
    """Group chart course entries into slots; 'X or Y' in the same semester cell = one slot."""
    slots: list[Slot] = []
    last_in_cell: dict[tuple, Slot] = {}
    for e in chart.entries:
        if e.kind == "elective_slot" or not e.course_code:
            continue
        cell = (e.year, e.semester)
        if e.alternative_to_previous and cell in last_in_cell:
            last_in_cell[cell].options.append(e.course_code)
            continue
        code = e.course_code
        is_thesis = code.startswith("BITS F4") and code.endswith("T")
        kind = ("cdc" if code in core_codes else "disputed" if code in chart_only
                else "ps_thesis" if code in PS_CODES or is_thesis else "named")
        slot = Slot(kind=kind, options=[code], year=e.year, semester=e.semester,
                    units=int(e.units) if e.units and e.units.isdigit() else None,
                    source=chart.source.model_copy(update={"section": chart.title}))
        if kind == "disputed":
            slot.note = ("Bulletin conflict: the semester chart lists this as core but the programme's "
                         "course list does not; not counted as a CDC (course list is authoritative). "
                         "Verify with AUGSD.")
        slots.append(slot)
        last_in_cell[cell] = slot
    for s in slots:                          # PS-II and its thesis alternatives are one requirement
        if s.kind == "cdc" or s.kind == "disputed":
            continue
        if any(o in PS_CODES or o.endswith("T") for o in s.options) and len(s.options) > 1:
            s.kind = "ps_thesis"
    return slots


def degree_slots(programme, chart, store: Store) -> tuple[list[Slot], list[str]]:
    core_codes = {o.course_code for s in programme.core for o in s.options}
    mismatch = next((m for m in store.validation.get("chart_vs_list_core_mismatches", [])
                     if m["programme"] == programme.name), None)
    chart_only = set(mismatch["chart_only"]) if mismatch else set()
    notes = []
    slots = chart_slots(chart, core_codes, chart_only, store) if chart else []
    placed = {o for s in slots for o in s.options}
    for cs in programme.core:                 # CDCs the chart doesn't place (list-only)
        codes = [o.course_code for o in cs.options]
        if placed & set(codes):
            continue
        slots.append(Slot(kind="cdc", options=codes, title=cs.options[0].title, units=cs.options[0].units,
                          note="In the course list but not in the semester chart (Bulletin conflict): "
                               "year/semester unknown. Verify with AUGSD.",
                          source=programme.source))
    for s in slots:
        if s.kind == "cdc" and len(s.options) == 1:    # the list may give OR options the chart doesn't
            listed = next((cs for cs in programme.core if s.options[0] in [o.course_code for o in cs.options]), None)
            if listed:
                s.options = [o.course_code for o in listed.options]
    if mismatch:
        notes.append(f"Bulletin conflict for this degree: chart core {mismatch['chart_core']} vs course list "
                     f"{mismatch['list_core']} (courses, units); list-only {mismatch['list_only']}, "
                     f"chart-only {mismatch['chart_only']} (pages {mismatch['chart_page']}, {mismatch['list_page']}).")
    for s in slots:
        c = store.course(s.options[0])
        s.title = s.title or (c.title if c else "")
        s.units = s.units or (c.units if c else None)
    return slots, notes


# ---------------------------------------------------------------- categories

def elective_categories(programme, chart, store: Store) -> list[CategoryStatus]:
    del_row, hel_row, opel_row = (category_row(store, n) for n in ("Elective", "Humanities", "Open"))
    t = chart.totals if chart else None
    del_units = (t.elective_units if t and t.elective_units else del_row.units.min)
    del_courses = (t.elective_courses if t and t.elective_courses else del_row.courses.min)
    slot_text = lambda word: [f"Y{e.year} S{e.semester}: {e.text} {e.units or ''}".strip()
                              for e in (chart.entries if chart else [])
                              if e.kind == "elective_slot" and word in e.text.lower()]
    src = store.degree_rules.source
    return [
        CategoryStatus(category="DEL", required_units=del_units, required_courses=del_courses,
                       slots_in_chart=slot_text("discipline"),
                       source=chart.source if chart else programme.source),
        CategoryStatus(category="HUEL", required_units=hel_row.units.min, required_courses=hel_row.courses.min,
                       slots_in_chart=slot_text("humanities"), source=src),
        CategoryStatus(category="OPEL", required_units=opel_row.units.min, required_courses=opel_row.courses.min,
                       slots_in_chart=slot_text("open"), source=src),
    ]


def own_discipline(programme) -> set[str]:
    """Courses of the student's own discipline (can't count as HUEL, Bulletin IV p.335)."""
    codes = {o.course_code for s in programme.core for o in s.options} | {e.course_code for e in programme.electives}
    return codes | {a for e in programme.electives for a in e.aliases}


def fill_categories(cats: list[CategoryStatus], pool: list[str], programme, store: Store,
                    count_huel: bool) -> None:
    by = {c.category: c for c in cats}
    del_codes = {e.course_code for e in programme.electives} | {a for e in programme.electives for a in e.aliases}
    huel_codes = {h.course_code for h in store.course_lists.humanities_pool}
    own = own_discipline(programme)
    for code in pool:
        u = units_of(code, store)
        same = store.same_course(code)
        if same & del_codes and not by["DEL"].complete:
            target = by["DEL"]
        elif count_huel and same & huel_codes and not (same & own) and not by["HUEL"].complete:
            target = by["HUEL"]
        else:
            target = by["OPEL"]            # 2.05: overflow and everything else is an open elective
        target.courses.append(code)
        target.done_units += u
        target.done_courses += 1


# ---------------------------------------------------------------- entry point

def analyse(profile: StudentProfile, store: Store | None = None) -> RequirementReport:
    store = store or get_store()
    cleared = cleared_courses(profile)
    report = RequirementReport(study_year=profile.study_year or SEMESTER_YEAR - profile.batch + 1, cleared=cleared,
                               in_progress=[normalize_code(c) for c in profile.current])
    if profile.batch > MAX_SUPPORTED_BATCH:
        report.warnings.append(f"Batch {profile.batch} is not supported: its curriculum is not in the supplied "
                               f"Bulletin 2025-26 (supported: up to {MAX_SUPPORTED_BATCH}).")
    if profile.campus.lower() != "pilani":
        report.warnings.append(f"Only the Pilani timetable was supplied; {profile.campus} offerings can't be checked.")

    used_named: set[str] = set()              # dual: GIR courses of degree 1 aren't electives of degree 2
    for n, name in enumerate(profile.programmes[:2]):
        programme = store.programme(name)
        if not programme:
            report.warnings.append(f"Programme '{name}' not found in the Bulletin course lists.")
            continue
        chart = store.chart(programme.name)
        slots, notes = degree_slots(programme, chart, store)
        used: set[str] = set()
        for s in slots:
            for opt in s.options:
                via = done_via(opt, cleared, store)
                if via and via not in used:
                    s.done_via = via
                    used.add(via)
                    break
        if n == 1:                            # second degree of a dual: GIR met once (IV-2)
            gir = [s for s in slots if s.kind == "named"]
            used_named |= {s.done_via for s in gir if s.done_via}   # its GIR courses aren't electives either
            slots = [s for s in slots if s.kind != "named"]
            notes.append("Dual degree: GIR and Humanities Electives are met once (first degree); PS-II or a thesis "
                         "is needed for each degree; this degree's Discipline Electives may fill the other's Open "
                         "Electives (Bulletin IV-2).")
        for s in slots:                       # registered this semester (not cleared yet)
            if not s.done_via:
                s.in_progress = next((c for c in report.in_progress if store.same_course(c) & set(s.options)), None)
        if n == 0:
            used_named = {s.done_via for s in slots if s.kind in ("named", "ps_thesis") and s.done_via}
        cats = elective_categories(programme, chart, store)
        taken = used | (used_named if n == 1 else set())
        pool = []
        for c in cleared:
            if c in taken:
                continue
            twin = store.same_course(c) & taken
            if twin:                          # an equivalent already counted: no double credit (E4)
                notes.append(f"{c} not counted: equivalent to {sorted(twin)[0]}, already counted.")
                continue
            pool.append(c)
        fill_categories(cats, pool, programme, store, count_huel=(n == 0))
        if n == 1:
            cats = [c for c in cats if c.category != "HUEL"]
        report.degrees.append(DegreeReport(programme=programme.name, chart=chart.title if chart else None,
                                           slots=slots, categories=cats, notes=notes))
    if len(profile.programmes) == 2 and report.degrees:
        dual = store.dual_chart(*profile.programmes)
        if dual:
            report.degrees[0].notes.append(f"Semester timing for the dual degree: '{dual.title}' "
                                           f"(Bulletin p.{dual.source.page}).")
    return report
