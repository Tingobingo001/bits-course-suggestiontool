"""Final stage: cross-document validation (brief Sec 9: "validate extracted course codes,
prerequisites, categories and programme requirements before using them").

Each parser validates its own output; this module checks the documents AGAINST EACH
OTHER and writes data/processed/validation_report.json so gaps are visible to a reviewer.

Usage: python -m recommender.ingest.validate
"""
import json

from recommender.config import NEW_ADMISSIONS_MIN_COMP_CODE, PROCESSED

OUT = PROCESSED / "validation_report.json"


def load(name: str) -> list[dict]:
    return json.loads((PROCESSED / name).read_text(encoding="utf-8"))


def run():
    catalogue = load("courses.json")
    timetable = load("timetable.json")
    equivalents = load("equivalents.json")

    known = {c["course_code"] for c in catalogue}
    offered = {t["course_code"] for t in timetable
               if not t["cancelled"] and t["comp_code"] < NEW_ADMISSIONS_MIN_COMP_CODE}
    equiv: dict[str, set[str]] = {}
    for e in equivalents:
        equiv.setdefault(e["course_code"], set()).update(e["equivalents"])
    all_codes = known | {t["course_code"] for t in timetable} | set(equiv)

    # 1. Prerequisite codes must refer to real courses
    unknown_prereqs = [
        {"course": c["course_code"], "unknown_prerequisite": p, "text": c["prerequisite_text"],
         "page": c["source"]["page"]}
        for c in catalogue for p in (c["prerequisites"] or [])
        if p not in all_codes
    ]

    # 2. Offered courses should have a catalogue entry (directly or via an equivalent)
    no_description = sorted(
        code for code in offered
        if code not in known and not (equiv.get(code, set()) & known)
    )

    # 3. Equivalence codes should be known somewhere (old codes like 'IS C313' legitimately aren't)
    unknown_equiv = sorted({x for codes in equiv.values() for x in codes} - all_codes)

    # 4. Records each parser already flagged
    flagged = {
        "catalogue": {c["course_code"]: c["needs_verification"] for c in catalogue if c["needs_verification"]},
        "timetable": {t["course_code"]: t["needs_verification"] for t in timetable if t["needs_verification"]},
    }

    report = {
        "summary": {
            "catalogue_courses": len(catalogue),
            "offered_in_scope": len(offered),
            "offered_with_description": len(offered) - len(no_description),
            "courses_with_stated_prerequisites": sum(1 for c in catalogue if c["prerequisites"]),
            "unknown_prerequisite_codes": len(unknown_prereqs),
            "unknown_equivalence_codes": len(unknown_equiv),
            "flagged_catalogue_records": len(flagged["catalogue"]),
            "flagged_timetable_records": len(flagged["timetable"]),
        },
        "unknown_prerequisites": unknown_prereqs,
        "offered_without_description": no_description,
        "unknown_equivalence_codes": unknown_equiv,
        "flagged": flagged,
    }
    OUT.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    for k, v in report["summary"].items():
        print(f"  {k:36} {v}")
    for u in unknown_prereqs[:10]:
        print(f"  unknown prereq: {u['course']} -> {u['unknown_prerequisite']}  ({u['text'][:60]})")
    print(f"-> {OUT}")


if __name__ == "__main__":
    run()
