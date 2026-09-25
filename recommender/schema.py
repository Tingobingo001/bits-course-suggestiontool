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
