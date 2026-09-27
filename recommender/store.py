"""Retrieval layer: the ONLY place that reads data/processed/*.json.

Every file is loaded once, validated against schema.py, and indexed for the lookups the
engine and the agent need. Nothing here decides anything (that's the engine); it only
answers "what do the documents say about X?".

Scope rules applied here, once, so no caller can forget them:
  - "offered" = in this semester's timetable, not cancelled, comp code < 5000 (config);
  - an equivalent code counts as the same course (equivalents.json, all bases).
"""
import json
import re
from functools import cached_property

from recommender.codes import normalize_code
from recommender.ingest.handouts import evaluation_flagged
from recommender.config import NEW_ADMISSIONS_MIN_COMP_CODE, PROCESSED
from recommender.schema import (Course, CourseLists, DegreeRules, Equivalence, Handout, MinorProgrammes,
                                ProgrammeCourseList, RegulationRule, Regulations, SemesterChart,
                                TimetableCourse)


OTHER_CAMPUS = re.compile(r"Dubai|Goa|Hyderabad", re.IGNORECASE)


def _load(name: str):
    return json.loads((PROCESSED / name).read_text(encoding="utf-8"))


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if w not in {"b", "e", "m", "sc", "with", "and", "in", "of"}}


class Store:
    """Lazy: each file is parsed the first time something needs it."""

    # ------------------------------------------------------------------ raw collections
    @cached_property
    def courses(self) -> dict[str, Course]:
        """One entry per code. The catalogue prints 2 codes twice; pick (1) the title the programme
        course list uses, (2) else not another campus's version (BITS F335 Discover India, not
        Dubai - Pilani only, assumption C5), (3) else the first printed. The other title is flagged."""
        listed = {lc.course_code: lc.title.lower() for p in self.course_lists.programmes
                  for lc in [o for s in p.core for o in s.options] + p.electives}
        by_code: dict[str, list[Course]] = {}
        for c in map(Course.model_validate, _load("courses.json")):
            by_code.setdefault(c.course_code, []).append(c)
        out = {}
        for code, versions in by_code.items():
            chosen = min(versions, key=lambda c: (
                not (code in listed and listed[code].startswith(c.title.lower()[:20])),
                bool(OTHER_CAMPUS.search(c.title)), versions.index(c)))
            if len(versions) > 1:
                others = [v.title for v in versions if v is not chosen]
                chosen.needs_verification.append(f"catalogue prints this code twice; other title: {others}")
            out[code] = chosen
        return out

    @cached_property
    def timetable(self) -> list[TimetableCourse]:
        return [TimetableCourse.model_validate(t) for t in _load("timetable.json")]

    @cached_property
    def equivalences(self) -> list[Equivalence]:
        return [Equivalence.model_validate(e) for e in _load("equivalents.json")]

    @cached_property
    def handouts(self) -> list[Handout]:
        return [Handout.model_validate(h) for h in _load("handouts.json")]

    @cached_property
    def course_lists(self) -> CourseLists:
        return CourseLists.model_validate(_load("course_lists.json"))

    @cached_property
    def charts(self) -> list[SemesterChart]:
        return [SemesterChart.model_validate(c) for c in _load("semester_charts.json")]

    @cached_property
    def degree_rules(self) -> DegreeRules:
        return DegreeRules.model_validate(_load("degree_rules.json"))

    @cached_property
    def minors(self) -> MinorProgrammes:
        return MinorProgrammes.model_validate(_load("minors.json"))

    @cached_property
    def regulations(self) -> Regulations:
        return Regulations.model_validate(_load("regulations.json"))

    @cached_property
    def validation(self) -> dict:
        return _load("validation_report.json")

    # ------------------------------------------------------------------ indexes
    @cached_property
    def _equiv(self) -> dict[str, set[str]]:
        idx: dict[str, set[str]] = {}
        for e in self.equivalences:
            idx.setdefault(e.course_code, set()).update(e.equivalents)
            for other in e.equivalents:                       # equivalence is symmetric
                idx.setdefault(other, set()).add(e.course_code)
        return idx

    @cached_property
    def _offered(self) -> dict[str, list[TimetableCourse]]:
        idx: dict[str, list[TimetableCourse]] = {}
        for t in self.timetable:
            if not t.cancelled and t.comp_code < NEW_ADMISSIONS_MIN_COMP_CODE:
                idx.setdefault(t.course_code, []).append(t)
        return idx

    @cached_property
    def _handouts(self) -> dict[str, list[Handout]]:
        idx: dict[str, list[Handout]] = {}
        for h in self.handouts:
            for code in h.course_codes:
                idx.setdefault(code, []).append(h)
        return idx

    @cached_property
    def _rules(self) -> dict[str, RegulationRule]:
        return {r.id: r for r in self.regulations.rules}

    # ------------------------------------------------------------------ lookups
    def equivalents(self, code: str) -> set[str]:
        """Other codes that count as the same course (not including `code`)."""
        return self._equiv.get(normalize_code(code), set()) - {normalize_code(code)}

    def same_course(self, code: str) -> set[str]:
        """`code` plus its equivalents."""
        return {normalize_code(code)} | self.equivalents(code)

    def course(self, code: str) -> Course | None:
        """Catalogue entry; falls back to an equivalent's entry (ECON F315 is described as FIN F315)."""
        for c in [normalize_code(code), *sorted(self.equivalents(code))]:
            if c in self.courses:
                return self.courses[c]
        return None

    def offering(self, code: str) -> list[TimetableCourse]:
        """This semester's in-scope timetable rows for exactly this code."""
        return self._offered.get(normalize_code(code), [])

    def offered_codes(self) -> set[str]:
        return set(self._offered)

    def is_offered(self, code: str) -> bool:
        return normalize_code(code) in self._offered

    def handout(self, code: str) -> Handout | None:
        """Best handout for a course (or an equivalent): a verified evaluation beats a flagged one,
        regex beats LLM."""
        found = [h for c in self.same_course(code) for h in self._handouts.get(c, [])]
        if not found:
            return None
        return min(found, key=lambda h: (evaluation_flagged(h.needs_verification), h.extracted_by != "regex", h.file))

    def rule(self, rule_id: str) -> RegulationRule:
        return self._rules[rule_id]

    def clause(self, number: str):
        return next((c for c in self.regulations.clauses if c.clause == number), None)

    # ------------------------------------------------------------------ programmes
    def programme_names(self) -> list[str]:
        return [p.name for p in self.course_lists.programmes]

    def programme(self, name: str) -> ProgrammeCourseList | None:
        """By exact name, else by best word overlap ('Computer Science' -> 'COMPUTER SCIENCE')."""
        progs = self.course_lists.programmes
        exact = next((p for p in progs if p.name.lower() == name.lower()), None)
        if exact:
            return exact
        want = _words(name)
        scored = sorted(((len(want & _words(p.name)) / len(want | _words(p.name)), p) for p in progs),
                        key=lambda t: -t[0])
        return scored[0][1] if scored and scored[0][0] >= 0.5 else None

    def chart(self, programme: str) -> SemesterChart | None:
        """The single-degree semester chart linked to a programme's course list."""
        p = self.programme(programme)
        return next((c for c in self.charts if c.kind == "single" and p and c.course_list == p.name), None)

    def dual_chart(self, first: str, second: str) -> SemesterChart | None:
        """Composite chart for a dual degree (M.Sc. first, B.E. second), matched by title words."""
        a, b = _words(first), _words(second)
        best, best_score = None, 0.0
        for c in self.charts:
            if c.kind != "dual" or " with " not in c.title:
                continue
            left, right = (_words(x) for x in c.title.split(" with ", 1))
            score = (len(a & left) / max(len(a), 1)) + (len(b & right) / max(len(b), 1))
            if score > best_score:
                best, best_score = c, score
        return best if best_score >= 1.5 else None


_store: Store | None = None


def get_store() -> Store:
    """Process-wide shared store (Streamlit and the agent reuse the loaded data)."""
    global _store
    if _store is None:
        _store = Store()
    return _store
