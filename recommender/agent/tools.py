"""The agent's tools: thin wrappers that expose the deterministic engine to the LLM.

The LLM never sees raw files and never decides eligibility. It calls these functions and
explains their results. Each returns compact JSON-able data with sources, so answers can cite
pages/clauses and say "could not be verified" where the data is silent.

Gemini's automatic function calling reads each method's signature and docstring as the tool
schema, so docstrings here are written for the model.
"""
import re

from recommender.engine.eligibility import CourseOption, check_all, priority
from recommender.engine.requirements import RequirementReport, analyse
from recommender.engine.scheduler import plan
from recommender.schema import StudentProfile
from recommender.store import get_store

DAY_NAMES = {"monday": "M", "tuesday": "T", "wednesday": "W", "thursday": "Th", "friday": "F", "saturday": "S"}


class AgentTools:
    def __init__(self, profile: StudentProfile):
        self.profile = profile
        self.store = get_store()
        self.report: RequirementReport = analyse(profile, self.store)
        self.options: dict[str, CourseOption] = {o.code: o for o in check_all(profile, self.report, self.store)}
        self.calls: list[str] = []             # tool-call log shown in the UI

    # -------------------------------------------------------------- helpers
    def _brief(self, o: CourseOption) -> dict:
        h = o.handout or {}
        return {
            "code": o.code, "title": o.title, "units": o.units, "status": o.status,
            "counts_as": o.categories, "minor": o.minor,
            "warnings": o.warnings, "not_eligible_because": o.reasons, "could_not_verify": o.unverified,
            "midsem_weight": h.get("midsem_weight"), "compre_weight": h.get("compre_weight"),
            "continuous_weight": h.get("continuous_weight"), "has_open_book": h.get("has_open_book"),
            "compre": f"{o.compre.date} {o.compre.session}" if o.compre else None,
            "lecture_sections": [f"{s.section}: {s.schedule}" for s in o.sections if s.kind == "L"][:4],
        }

    # -------------------------------------------------------------- tools
    def get_requirements(self) -> dict:
        """The student's degree progress: completed and remaining CDC/GIR/PS slots (with the
        year/semester they belong to), elective categories (DEL, HUEL, OPEL) with units done and
        still required, Bulletin conflicts and warnings. Call this first for any 'what should I take'
        question."""
        self.calls.append("get_requirements")
        r = self.report
        return {
            "study_year": r.study_year, "semester": "First Semester 2026-27", "warnings": r.warnings,
            "caveat": r.caveat,
            "degrees": [{
                "programme": d.programme, "chart": d.chart, "notes": d.notes,
                "remaining_slots": [{"kind": s.kind, "options": s.options, "title": s.title,
                                     "when": f"Y{s.year} S{s.semester}" if s.year else "not in chart",
                                     "note": s.note} for s in d.remaining_slots()],
                "completed_slots": len([s for s in d.slots if s.done_via]),
                "via_equivalent": {s.options[0]: s.done_via for s in d.slots
                                   if s.done_via and s.done_via not in s.options},
                "electives": [{"category": c.category, "done_units": c.done_units,
                               "required_units": c.required_units, "done_courses": c.done_courses,
                               "required_courses": c.required_courses, "courses": c.courses,
                               "chart_slots": c.slots_in_chart} for c in d.categories],
                "source": f"Bulletin 2025-26, chart p.{d.slots[0].source.page}" if d.slots else "Bulletin 2025-26",
            } for d in r.degrees],
        }

    def recommend_courses(self, category: str = "", limit: int = 12) -> list[dict]:
        """Eligible courses offered this semester, most urgent first (backlog, then this semester's
        CDCs/GIR, then DEL, HUEL, OPEL). category optionally filters: one of 'CDC', 'GIR', 'DEL',
        'HUEL', 'OPEL', 'backlog', 'minor'. Courses with warnings are included (read the warnings)."""
        self.calls.append(f"recommend_courses({category})")
        opts = [o for o in self.options.values() if o.status in ("eligible", "warning")]
        if category.lower() == "minor":
            opts = [o for o in opts if o.minor]
        elif category:
            opts = [o for o in opts if any(c.startswith(category) or category in c for c in o.categories.values())]
        return [self._brief(o) for o in sorted(opts, key=lambda o: (priority(o), o.code))[:limit]]

    def search_courses(self, keywords: list[str], category: str = "", eligible_only: bool = True,
                       limit: int = 10) -> list[dict]:
        """Find offered courses matching a student's interests. Pass several specific keywords and
        synonyms (e.g. for 'AI': ['machine learning', 'neural', 'deep learning', 'artificial
        intelligence', 'data mining']). Matches course titles, catalogue descriptions and handout
        course plans. category optionally filters as in recommend_courses."""
        self.calls.append(f"search_courses({', '.join(keywords[:5])})")
        words = [k.lower().strip() for k in keywords if k.strip()]
        scored = []
        for o in self.options.values():
            if eligible_only and o.status not in ("eligible", "warning"):
                continue
            if category and not any(category in c for c in o.categories.values()):
                continue
            h = self.store.handout(o.code)
            plan_text = " ".join(s.text for s in (h.sections if h else []) if s.topic in ("course_plan", "description", "scope"))
            title, body = o.title.lower(), (o.description + " " + plan_text).lower()
            score = sum(3 * (w in title) + min(body.count(w), 3) for w in words)
            if score:
                scored.append((score, o))
        scored.sort(key=lambda x: (-x[0], priority(x[1])))
        return [dict(self._brief(o), match_score=s) for s, o in scored[:limit]]

    def course_details(self, code: str) -> dict:
        """Everything known about one course for this student: eligibility with reasons, what it counts
        as, prerequisites, all sections with times and instructors, midsem/compre slots, and the
        handout's evaluation scheme, make-up and attendance policy (with page)."""
        self.calls.append(f"course_details({code})")
        o = self.options.get(re.sub(r"\s+", " ", code.upper().strip()))
        if not o:
            c = self.store.course(code)
            return {"code": code, "offered_this_semester": False,
                    "catalogue": {"title": c.title, "units": c.units, "description": c.description[:600],
                                  "page": c.source.page} if c else "not in the Bulletin catalogue"}
        return {**o.model_dump(exclude={"sections"}), "offered_this_semester": True,
                "sections": [s.model_dump() for s in o.sections]}

    def check_timetable(self, codes: list[str], avoid_hours: list[int] | None = None,
                        avoid_days: list[str] | None = None) -> dict:
        """Check a set of courses can be taken together and pick sections: no class clash, a lunch
        period (4, 5 or 6) free daily, no compre/midsem clash, max 25 units. avoid_hours uses periods
        (1 = 8 AM ... 10 = 5 PM); avoid_days like ['Friday']. Returns the section choice and weekly grid,
        or the exact problem."""
        self.calls.append(f"check_timetable({', '.join(codes)})")
        days = {DAY_NAMES.get(d.lower(), d) for d in (avoid_days or [])}
        return plan(codes, set(avoid_hours or []), days, self.store).model_dump()

    def search_regulations(self, keywords: list[str], limit: int = 4) -> list[dict]:
        """Search the Academic Regulations 2023 clauses and the timetable's registration instructions
        for a policy question (e.g. ['withdraw', 'withdrawal'] or ['summer term']). Returns clause
        number, page and text to quote."""
        self.calls.append(f"search_regulations({', '.join(keywords[:4])})")
        words = [k.lower() for k in keywords]
        regs = self.store.regulations
        items = [(c.clause, c.source.page, c.text, "Academic Regulations 2023") for c in regs.clauses]
        items += [("timetable VII", p.source.page, p.text, "Timetable") for p in regs.registration_instructions]
        scored = sorted(((sum(t.lower().count(w) for w in words), cl, pg, t, doc) for cl, pg, t, doc in items),
                        key=lambda x: -x[0])
        return [{"clause": cl, "document": doc, "page": pg, "text": t[:1200]}
                for s, cl, pg, t, doc in scored[:limit] if s]

    def as_list(self):
        return [self.get_requirements, self.recommend_courses, self.search_courses, self.course_details,
                self.check_timetable, self.search_regulations]
