"""Data shapes shared by all modules.

Every parser writes these shapes to data/processed/*.json and every consumer
(store, engine, agent) reads them back. Pydantic validates types on the way in
and out, so a malformed record fails loudly instead of corrupting a recommendation.
"""
from typing import Literal

from pydantic import BaseModel, Field

Day = Literal["M", "T", "W", "Th", "F", "S"]
SectionType = Literal["L", "T", "P"]  # lecture, tutorial, practical/lab


class SourceRef(BaseModel):
    """Where a fact came from, so any recommendation can be traced back."""
    document: str
    page: int
    section: str | None = None  # e.g. "Part VI - Computer Science"
    confidence: Literal["high", "medium", "low"] = "high"


class Meeting(BaseModel):
    day: Day
    hour: int  # timetable period: 1 = 8 AM, 2 = 9 AM, ... (lunch periods are 4-6)


class ExamSlot(BaseModel):
    date: str     # "DD/MM" as printed
    session: str  # e.g. "FN1", "AN", as printed


class Section(BaseModel):
    code: str                 # e.g. "L1", "T2", "P3"
    type: SectionType
    instructors: list[str] = Field(default_factory=list)
    room: str | None = None
    meetings: list[Meeting] = Field(default_factory=list)
    raw_schedule: str | None = None  # original "DAYS & HOURS" text, kept for verification


class Course(BaseModel):
    """Catalogue entry from Bulletin Part VI (course descriptions)."""
    course_code: str               # normalised, e.g. "CS F364"
    title: str                     # full title
    department: str | None = None  # Part VI heading the course is printed under
    lecture_hours: int | None = None
    practical_hours: int | None = None
    units: int | None = None
    variable_units: bool = False   # printed with '*': breakup announced via timetable
    description: str = ""
    prerequisites: list[str] | None = None  # None = not stated in the bulletin (NOT "no prereqs")
    prerequisite_logic: Literal["all", "any"] | None = None  # "A OR B" -> any, "A AND B" -> all
    prerequisite_text: str | None = None    # the raw sentence, for verification
    # "Those who have done MATH F471 are not allowed to take this course" (None = not stated;
    # the timetable says course restrictions are published on the website, not supplied)
    excluded_if_completed: list[str] | None = None
    restriction_text: str | None = None
    source: SourceRef
    needs_verification: list[str] = Field(default_factory=list)


class ListedCourse(BaseModel):
    """A course as it appears in a Bulletin course list (Part IV, 'List of Courses')."""
    course_code: str
    aliases: list[str] = Field(default_factory=list)  # cross-listed codes printed in the same cell ("CS G514/ SS G514")
    title: str
    lecture_hours: int | None = None
    practical_hours: int | None = None
    units: int | None = None
    variable_units: bool = False
    group: str | None = None   # elective track / pool, e.g. "Track - 1: Environment and sustainable design"
    alternative_to_previous: bool = False   # printed as "X or Y": either one counts
    page: int


class CoreSlot(BaseModel):
    """One discipline-core requirement. Usually one course; 'MATH F212 OR ME F344' is one
    slot that either course satisfies."""
    options: list[ListedCourse]


class ProgrammeCourseList(BaseModel):
    name: str                          # as printed, e.g. "COMPUTER SCIENCE"
    core: list[CoreSlot] = Field(default_factory=list)
    electives: list[ListedCourse] = Field(default_factory=list)
    source: SourceRef


class CourseLists(BaseModel):
    """Everything in Bulletin Part IV 'List of Courses for B.E. / M.Sc. / B.Pharm. Programmes'."""
    programmes: list[ProgrammeCourseList]
    humanities_pool: list[ListedCourse]   # HUEL pool
    project_courses: list[ListedCourse]   # 'XXX F266 Study Project' etc. (XXX = any discipline)
    other_courses: list[ListedCourse]
    audit_courses: list[ListedCourse]
    rules: list[dict]                     # policy sentences found in this section, with page refs


class ChartEntry(BaseModel):
    """One line in a semester-wise chart: a course, or an elective slot like
    'Discipline Electives 3(min)'."""
    year: int
    semester: Literal[1, 2, "summer"]
    kind: Literal["course", "elective_slot", "other"]
    course_code: str | None = None
    text: str                      # as printed (course code, or slot label like "Open Electives")
    units: str | None = None       # as printed: "3", "3(min)", "6to12", "18-20"
    alternative_to_previous: bool = False   # 'X or Y' in the same semester cell


class ChartTotals(BaseModel):
    """'Discipline Core - 48 Units (14 Courses)' / 'Discipline Electives - 12 Units (4 Courses)'."""
    core_units: int | None = None
    core_courses: int | None = None
    elective_units: int | None = None
    elective_courses: int | None = None
    electives_are_minimum: bool = False    # "15 Units (min)"


class SemesterChart(BaseModel):
    title: str                              # as printed, e.g. "B. E. Computer Science"
    kind: Literal["single", "dual"]
    course_list: str | None = None          # linked ProgrammeCourseList name (single degrees)
    course_list_overlap: float | None = None  # share of the chart's courses found in that list's CDCs
    years_same_as_first_degree: list[int] = Field(default_factory=list)
    entries: list[ChartEntry] = Field(default_factory=list)
    totals: ChartTotals = Field(default_factory=ChartTotals)
    source: SourceRef
    needs_verification: list[str] = Field(default_factory=list)


class Range(BaseModel):
    min: int | None = None
    max: int | None = None     # None = no stated maximum ("129 (min)")


class CategoryRequirement(BaseModel):
    """One row of the Bulletin's 'category-wise structure of each program' table (IV-1)."""
    group: str                 # "(I) General Institutional Requirement", ...
    category: str              # "Humanities Electives", "Core", "Open Electives", ...
    units: Range
    courses: Range


class PolicyText(BaseModel):
    """A rule stated in prose. The engine implements it in code and cites this text."""
    topic: str                 # e.g. "dual_degree", "general_institutional_requirement"
    text: str
    source: SourceRef


class DegreeRules(BaseModel):
    category_requirements: list[CategoryRequirement]
    policies: list[PolicyText]
    source: SourceRef


class MinorGroup(BaseModel):
    """'Core Courses', or an elective pool like 'Electives (Science Pool) 01 (min)'."""
    label: str
    is_core: bool
    min_courses: int | None = None       # "01 (min)" on the pool label
    courses: list[ListedCourse] = Field(default_factory=list)


class Minor(BaseModel):
    name: str                            # "Minor in Aeronautics"
    description: str = ""
    min_courses: int | None = None       # "06 courses (min)"
    min_units: int | None = None         # "18 units (min)"
    groups: list[MinorGroup] = Field(default_factory=list)
    source: SourceRef
    needs_verification: list[str] = Field(default_factory=list)


class MinorProgrammes(BaseModel):
    minors: list[Minor]
    general_rules: list[PolicyText]      # "Requirements for a minor" (IV-129)


class Clause(BaseModel):
    """One numbered clause of the Academic Regulations, kept verbatim for citation."""
    clause: str                          # "3.13", "1.04a", "3.25 II"
    section: str                         # "3. Registration"
    text: str
    source: SourceRef


class RegulationRule(BaseModel):
    """A machine-usable rule the engine enforces, hand-encoded from the source text.

    `quotes` are exact phrases from the cited clause/page; the build checks they are
    really there, so a mistyped rule or a changed document is caught (verified=False).
    """
    id: str                              # "max_units_per_semester"
    value: int | float | bool | str | list | dict
    description: str
    quotes: list[str]
    source: SourceRef
    clause: str | None = None            # None for rules from the timetable
    verified: bool = False


ComponentKind = Literal["midsem", "compre", "quiz", "assignment", "project", "lab", "tutorial",
                        "seminar", "viva", "participation", "report", "other"]


class EvaluationComponent(BaseModel):
    """One row of a handout's evaluation table."""
    name: str                            # as printed: "Mid-Semester Test"
    kind: ComponentKind
    weight: float | None = None          # percent of the course total
    duration: str | None = None          # as printed: "90 min"
    date: str | None = None              # as printed: "09/10 AN2", "TBA"
    nature: str | None = None            # as printed: "Closed Book", "CB/OB"
    open_book: Literal["open", "closed", "partly"] | None = None


class HandoutSection(BaseModel):
    """A titled part of a handout kept as text: make-up policy, attendance, course plan, ..."""
    topic: str                           # normalised: "makeup", "attendance", "course_plan", ...
    heading: str                         # as printed
    text: str
    page: int


class Handout(BaseModel):
    file: str                            # "002_BIO_F101.pdf"
    course_codes: list[str]              # all codes on the 'Course No.' line
    title: str = ""
    instructor_in_charge: str = ""
    sections: list[HandoutSection] = Field(default_factory=list)
    evaluation: list[EvaluationComponent] = Field(default_factory=list)
    evaluation_page: int | None = None
    evaluation_notes: list[str] = Field(default_factory=list)  # table footnotes, marks->% conversion
    weights_total: float | None = None
    # derived from `evaluation` (None = not stated / could not be read)
    midsem_weight: float | None = None
    compre_weight: float | None = None
    continuous_weight: float | None = None   # everything except midsem + compre
    has_open_book: bool | None = None
    attendance_min_percent: int | None = None
    project_type: bool = False           # study/lab/design project, thesis: no exam table expected
    extracted_by: Literal["regex", "llm", "none"] = "regex"
    source: SourceRef
    needs_verification: list[str] = Field(default_factory=list)


class Regulations(BaseModel):
    clauses: list[Clause]
    rules: list[RegulationRule]
    registration_instructions: list[PolicyText]   # timetable "VII. Instructions regarding registration"


class Equivalence(BaseModel):
    """One row of the timetable's 'List of Equivalent Courses'.
    All codes in a row are mutually equivalent; rows are NOT chained together."""
    course_code: str          # the course offered this semester
    title: str
    equivalents: list[str]    # other codes that count as the same course
    basis: Literal["printed", "handout", "inferred_crosslist"] = "printed"
    source: SourceRef


class TimetableCourse(BaseModel):
    comp_code: int            # registration number; >= 5000 = only for 2026 admissions
    course_code: str          # normalised, e.g. "CS F301"
    title: str                # abbreviated title as printed in the timetable
    lecture_hours: int | None = None
    practical_hours: int | None = None
    units: int | None = None
    sections: list[Section] = Field(default_factory=list)
    midsem: ExamSlot | None = None
    compre: ExamSlot | None = None
    cancelled: bool = False
    source: SourceRef
    needs_verification: list[str] = Field(default_factory=list)  # human-readable issues


# ---------------------------------------------------------------- student side (brief Sec 4)

class CompletedCourse(BaseModel):
    code: str
    grade: str | None = None   # letter grade (A..E) or report (NC, W, I, RC...); None = "passed, grade not given"


class StudentProfile(BaseModel):
    name: str = ""
    campus: str = "Pilani"
    batch: int                           # admission year, e.g. 2024
    programmes: list[str]                # one degree, or two for a dual degree (M.Sc. first)
    minor: str | None = None
    completed: list[CompletedCourse] = Field(default_factory=list)
    current: list[str] = Field(default_factory=list)   # already registered this semester (in progress)
    study_year: int | None = None        # year of study now; None = derived from batch (off-pattern students set it)
    cgpa: float | None = None
    interests: str = ""                  # free text, used by the agent for matching
