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
            "has_midsem": h.get("has_midsem"), "project_weight": h.get("project_weight"),
            "attendance": h.get("attendance"), "instructor": h.get("instructor_in_charge"),
            "handout_verified": h.get("verified", False),
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

    def find_courses(self, keywords: list[str] | None = None, category: str = "", no_midsem: bool = False,
                     project_based: bool = False, open_book: bool = False, max_compre_weight: float = 100,
                     min_continuous_weight: float = 0, include_policies: bool = False, limit: int = 10) -> dict:
        """Find ELIGIBLE offered courses by interest and/or handout properties.
        keywords: interests expanded into several specific terms and synonyms (e.g. for 'AI':
          ['machine learning', 'neural', 'deep learning', 'artificial intelligence', 'data mining']);
          matched against titles, catalogue descriptions and handout course plans. Omit for no topic filter.
        category: 'CDC', 'GIR', 'DEL', 'HUEL', 'OPEL', 'backlog' or 'minor' - what the course must count as.
        no_midsem: only courses whose verified evaluation has no mid-semester test.
        project_based: only courses with project/report/seminar components (sorted by their weight).
        open_book: only courses with an open-book component.
        max_compre_weight / min_continuous_weight: limits on the evaluation split (percent).
        include_policies: also return each course's make-up and attendance policy text (use for questions
          about attendance or make-up leniency; judge from the quoted text and cite the handout page).
        Returns 'matches' plus 'could_not_verify': courses that fit everything else but whose handout
        data is missing or unverified, so the requested property can't be confirmed."""
        self.calls.append(f"find_courses({', '.join((keywords or [])[:4])}{' ' + category if category else ''}"
                          f"{' no-midsem' if no_midsem else ''}{' project' if project_based else ''})")
        words = [k.lower().strip() for k in (keywords or []) if k.strip()]
        needs_handout = no_midsem or project_based or open_book or max_compre_weight < 100 or min_continuous_weight > 0
        matches, unverifiable = [], []
        for o in self.options.values():
            if o.status not in ("eligible", "warning"):
                continue
            if category.lower() == "minor":
                if not o.minor:
                    continue
            elif category and not any(category in c for c in o.categories.values()):
                continue
            score = 0
            if words:
                h = self.store.handout(o.code)
                plan_text = " ".join(s.text for s in (h.sections if h else [])
                                     if s.topic in ("course_plan", "description", "scope"))
                title, body = o.title.lower(), (o.description + " " + plan_text).lower()
                score = sum(3 * (w in title) + min(body.count(w), 3) for w in words)
                if not score:
                    continue
            hd = o.handout or {}
            if needs_handout and not hd.get("verified"):
                unverifiable.append(o.code)
                continue
            if no_midsem and hd.get("has_midsem"):
                continue
            if project_based and not (hd.get("project_weight") or 0) > 0:
                continue
            if open_book and not hd.get("has_open_book"):
                continue
            if needs_handout and ((hd.get("compre_weight") or 0) > max_compre_weight
                                  or (hd.get("continuous_weight") or 0) < min_continuous_weight):
                continue
            item = dict(self._brief(o), match_score=score)
            if include_policies:
                item.update({k: hd.get(k) for k in ("attendance", "attendance_policy", "attendance_page",
                                                    "makeup_policy", "makeup_page", "file")})
            matches.append((score, (hd.get("project_weight") or 0) if project_based else 0, o, item))
        matches.sort(key=lambda m: (-m[0], -m[1], priority(m[2]), m[2].code))
        return {"matches": [m[3] for m in matches[:limit]], "total_matches": len(matches),
                "could_not_verify": unverifiable[:15],
                "could_not_verify_count": len(unverifiable)}

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
                        avoid_days: list[str] | None = None, compact: bool = False) -> dict:
        """Check a set of courses can be taken together and pick sections: no class clash, a lunch
        period (4, 5 or 6) free daily, no compre/midsem clash, max 25 units. avoid_hours uses periods
        (1 = 8 AM ... 10 = 5 PM); avoid_days like ['Friday']. Returns the section choice and weekly grid,
        or the exact problem. compact=True prefers fewer idle gaps between classes. Registered (current)
        courses are always included."""
        self.calls.append(f"check_timetable({', '.join(codes)})")
        days = {DAY_NAMES.get(d.lower(), d) for d in (avoid_days or [])}
        codes = list(dict.fromkeys(list(codes) + self.report.in_progress))
        return plan(codes, set(avoid_hours or []), days, self.store, compact=compact).model_dump()

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
        return [self.get_requirements, self.recommend_courses, self.find_courses, self.course_details,
                self.check_timetable, self.search_regulations]
