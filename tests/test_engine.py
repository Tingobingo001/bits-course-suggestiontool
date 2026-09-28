"""Engine tests on the real processed data (data/processed/*.json).

Each test pins one rule from the Regulations / Bulletin / owner decisions, so a parser or
engine change that breaks a rule fails loudly. Run: python -m pytest -q
"""
import pytest

from recommender.codes import find_codes
from recommender.engine.eligibility import check_all, check_course
from recommender.engine.requirements import analyse, cleared_courses
from recommender.schema import CompletedCourse, StudentProfile
from recommender.store import get_store

YEAR1 = ["BITS F103", "BIO F101", "CHEM F101", "MATH F101", "PHY F101", "BITS F101", "BITS K101",
         "BITS F111", "BITS F112", "CS F111", "MATH F113", "MATH F102", "EEE F111", "BITS F102"]
CS_YEAR2 = ["MATH F211", "CS F214", "CS F222", "CS F213", "CS F215", "ECON F211", "CS F211", "CS F241",
            "CS F212", "BITS F225"]


def student(codes, programmes=("Computer Science",), batch=2024, grade="B", **kw) -> StudentProfile:
    done = [CompletedCourse(code=c, grade=grade) if isinstance(c, str) else CompletedCourse(code=c[0], grade=c[1])
            for c in codes]
    return StudentProfile(batch=batch, programmes=list(programmes), completed=done, **kw)


def option(profile, code):
    report = analyse(profile)
    return check_course(code, profile, report, get_store())


def slot_for(report, code, degree=0):
    return next(s for s in report.degrees[degree].slots if code in s.options)


# ---------------------------------------------------------------- data layer

def test_all_regulation_rules_verified_against_source():
    assert all(r.verified for r in get_store().regulations.rules)


def test_shared_number_codes_expand():
    assert find_codes("ECE/EEE/INSTR F212 OR PHY F212") == ["ECE F212", "EEE F212", "INSTR F212", "PHY F212"]


def test_duplicate_catalogue_code_prefers_pilani_version():
    assert get_store().courses["BITS F335"].title == "Discover India"          # not "Discover Dubai" (C5)


# ---------------------------------------------------------------- grades (Regulations 1.15, 1.16, 4.17)

def test_latest_performance_and_reports():
    p = student([("CS F213", "A"), ("CS F213", "NC"),          # latest is NC -> not cleared
                 ("CS F214", "E"),                             # E is a grade -> cleared
                 ("CS F215", "B"), ("CS F215", "W"),           # W ignored -> B stands
                 ("CS F222", "I")])                            # incomplete -> not cleared
    cleared = cleared_courses(p)
    assert "CS F213" not in cleared and "CS F222" not in cleared
    assert cleared["CS F214"] == "E" and cleared["CS F215"] == "B"


# ---------------------------------------------------------------- requirements

def test_cdc_satisfied_via_equivalent():
    report = analyse(student(["ECE F215"]))
    assert slot_for(report, "CS F215").done_via == "ECE F215"                 # E5


def test_no_double_credit_for_equivalents():
    report = analyse(student(YEAR1 + ["CS F215", "ECE F215"]))
    counted = [c for cat in report.degrees[0].categories for c in cat.courses]
    assert "ECE F215" not in counted


def test_alternatives_form_one_slot():
    report = analyse(student(YEAR1 + ["MGTS F211"]))
    slot = slot_for(report, "ECON F211")
    assert "MGTS F211" in slot.options and slot.done_via == "MGTS F211"


def test_elective_overflow_goes_to_open_electives():
    humanities = [h.course_code for h in get_store().course_lists.humanities_pool
                  if h.course_code.startswith("HSS")][:4]
    report = analyse(student(YEAR1 + humanities))
    cats = {c.category: c for c in report.degrees[0].categories}
    assert cats["HUEL"].complete
    assert set(humanities) - set(cats["HUEL"].courses) <= set(cats["OPEL"].courses)   # clause 2.05


def test_dual_degree_gir_met_once():
    report = analyse(student(YEAR1 + ["ECON F211"], programmes=("Economics", "Computer Science"), batch=2023))
    cs = report.degrees[1]
    assert not any(s.kind == "named" for s in cs.slots)
    assert "ECON F211" not in [c for cat in cs.categories for c in cat.courses]


def test_bulletin_conflict_is_shown_not_hidden():
    report = analyse(student([], programmes=("Electronics and Communication Engineering",)))
    d = report.degrees[0]
    assert any("Bulletin conflict" in n for n in d.notes)
    assert any(s.kind == "disputed" for s in d.slots)


def test_unsupported_batch_and_campus_warn():
    report = analyse(student([], batch=2026, campus="Goa"))
    assert len(report.warnings) == 2


# ---------------------------------------------------------------- eligibility

def test_done_course_is_not_recommended():
    assert option(student(YEAR1 + CS_YEAR2), "CS F213").status == "done"


def test_equivalent_done_blocks_course():
    assert option(student(["ECE F215"]), "CS F215").status == "done"          # E4


def test_prerequisite_missing_blocks():
    opt = option(student(YEAR1, programmes=("Civil Engineering",)), "CE F331")
    assert opt.status == "ineligible" and any("CE F243" in r for r in opt.reasons)


def test_prerequisite_any_of_met_by_one_option():
    opt = option(student(YEAR1 + ["EEE F212"], programmes=("Electronics and Communication Engineering",)),
                 "ECE F331")
    assert not any("Prerequisite not met" in r for r in opt.reasons)


def test_unstated_prerequisite_is_unverified_not_assumed():
    opt = option(student(YEAR1 + CS_YEAR2), "CS F301")
    assert opt.prerequisites == "not stated in the supplied documents"
    assert any("could not be verified" in u for u in opt.unverified)


def test_other_degree_course_needs_third_year_preparation():
    opt = option(student(YEAR1, batch=2025), "ME F341")                       # 2nd-year CS student
    assert opt.status == "ineligible" and any("3.15" in r for r in opt.reasons)


def test_higher_degree_course_warns():
    opt = option(student(YEAR1 + CS_YEAR2), "CS G525")
    assert any("2.08" in w for w in opt.warnings)


def test_current_semester_cdc_is_top_priority():
    from recommender.engine.eligibility import priority
    opts = [o for o in check_all(student(YEAR1 + CS_YEAR2)) if o.status in ("eligible", "warning")]
    top = sorted(opts, key=priority)[:4]
    assert {o.code for o in top} == {"CS F301", "CS F342", "CS F351", "CS F372"}


# ---------------------------------------------------------------- scheduler (brief Sec 8)

def test_plan_has_no_clash_and_keeps_lunch_free():
    from recommender.engine.scheduler import plan
    p = plan(["CS F301", "CS F342", "CS F351", "CS F372", "BITS F464"])
    assert p.ok
    busy = [(d, h) for d, hours in p.grid.items() for h in hours]
    assert len(busy) == len(set(busy))
    lunch = set(get_store().rule("lunch_hours").value)
    assert all(not lunch <= set(hours) for hours in p.grid.values())


def test_compre_clash_is_rejected():
    from recommender.engine.scheduler import plan
    by_slot = {}
    for code in sorted(get_store().offered_codes()):
        t = get_store().offering(code)[0]
        if t.compre and t.compre.date:
            by_slot.setdefault((t.compre.date, t.compre.session), []).append(code)
    a, b = next(v for v in by_slot.values() if len(v) >= 2)[:2]
    p = plan([a, b])
    assert not p.ok and any("comprehensive" in x for x in p.problems)


# ---------------------------------------------------------------- handout course numbers

def _handout_text(stem):
    from recommender.config import TEXT_HANDOUTS
    return (TEXT_HANDOUTS / f"{stem}.txt").read_text(encoding="utf-8")


def test_misprinted_course_number_is_ignored():
    from recommender.ingest.handouts import handout_codes
    codes, flags = handout_codes(_handout_text("162_CS_F215"), "CS F215")   # 'Digital Design' printed as F342
    assert codes == ["CS F215"] and flags


def test_wrong_document_in_file_is_not_used():
    from recommender.ingest.handouts import handout_codes
    codes, flags = handout_codes(_handout_text("017_BIO_G523"), "BIO G523")  # contains the BIO F212 handout
    assert "BIO G523" not in codes and flags


def test_renumbered_course_keeps_both_codes():
    from recommender.ingest.handouts import handout_codes
    codes, _ = handout_codes(_handout_text("025_BIO_U101"), "BIO U101")
    assert {"BIO U101", "BIO F101"} <= set(codes)


# ---------------------------------------------------------------- brief gaps closed (Sec 4, 6, 8)

def test_current_courses_are_not_recommended():
    p = student(YEAR1 + CS_YEAR2)
    p.current = ["CS F342"]
    assert option(p, "CS F342").status == "registered"


def test_no_midsem_filter_only_returns_verified_courses():
    from recommender.agent.tools import AgentTools
    t = AgentTools(student(YEAR1 + CS_YEAR2))
    res = t.find_courses(no_midsem=True, limit=50)
    assert res["matches"] and all(m["handout_verified"] and m["has_midsem"] is False for m in res["matches"])


def test_compact_plan_has_no_more_gaps():
    from recommender.engine.scheduler import plan
    codes = ["CS F301", "CS F342", "CS F351", "CS F372", "BITS F464", "HSS F222"]
    assert plan(codes, compact=True).gap_hours <= plan(codes).gap_hours
