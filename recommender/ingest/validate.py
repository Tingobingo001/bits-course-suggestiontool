"""Final stage: cross-document validation (brief Sec 9: "validate extracted course codes,
prerequisites, categories and programme requirements before using them").

Each parser validates its own output; this module checks the documents AGAINST EACH
OTHER and writes data/processed/validation_report.json so gaps are visible to a reviewer.

Usage: python -m recommender.ingest.validate
"""
import json
from collections import Counter

from recommender.config import NEW_ADMISSIONS_MIN_COMP_CODE, PROCESSED
from recommender.ingest.handouts import evaluation_flagged

OUT = PROCESSED / "validation_report.json"


def load(name: str) -> list[dict]:
    return json.loads((PROCESSED / name).read_text(encoding="utf-8"))


def run():
    catalogue = load("courses.json")
    timetable = load("timetable.json")
    equivalents = load("equivalents.json")
    lists = json.loads((PROCESSED / "course_lists.json").read_text(encoding="utf-8"))
    charts = load("semester_charts.json")
    minors = json.loads((PROCESSED / "minors.json").read_text(encoding="utf-8"))["minors"]
    regulations = json.loads((PROCESSED / "regulations.json").read_text(encoding="utf-8"))
    handouts = load("handouts.json")

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

    # 4. Every course in a programme list / pool should exist in the catalogue
    #    ('XXX F266' project entries mean "any discipline" and are skipped)
    listed = [(p["name"], o) for p in lists["programmes"] for s in p["core"] for o in s["options"]]
    listed += [(p["name"], e) for p in lists["programmes"] for e in p["electives"]]
    listed += [(k, c) for k in ("humanities_pool", "other_courses", "audit_courses") for c in lists[k]]
    listed_unknown = sorted({
        (where, c["course_code"], c["page"]) for where, c in listed
        if c["course_code"] not in known and not (equiv.get(c["course_code"], set()) & known)
    })
    unnamed = [p["source"]["page"] for p in lists["programmes"] if p["source"]["confidence"] != "high"]

    # 5. Chart totals vs course lists: two independent parts of the Bulletin must agree
    by_name = {p["name"]: p for p in lists["programmes"]}
    totals_mismatch = []
    for ch in charts:
        lst = by_name.get(ch["course_list"]) if ch["kind"] == "single" else None
        if not lst or ch["totals"]["core_courses"] is None:
            continue
        n = len(lst["core"])
        units = sum((s["options"][0]["units"] or 0) for s in lst["core"])
        if (n, units) != (ch["totals"]["core_courses"], ch["totals"]["core_units"]):
            core = {o["course_code"] for s in lst["core"] for o in s["options"]}
            listed_anywhere = core | {e["course_code"] for e in lst["electives"]}
            in_chart = {e["course_code"] for e in ch["entries"] if e["course_code"]}
            # chart courses from the programme's MAIN department(s) that the core list doesn't have
            # (prefixes making up >= 25% of its core; institute-wide BITS courses excluded)
            prefix_counts = Counter(c.split()[0] for c in core)
            depts = {d for d, k in prefix_counts.items() if k >= 0.25 * len(core) and d != "BITS"}
            chart_only = sorted(c for c in in_chart - listed_anywhere if c.split()[0] in depts)
            totals_mismatch.append({
                "programme": lst["name"], "chart": ch["title"],
                "chart_core": [ch["totals"]["core_courses"], ch["totals"]["core_units"]],
                "list_core": [n, units], "list_only": sorted(core - in_chart), "chart_only": chart_only,
                "chart_page": ch["source"]["page"], "list_page": lst["source"]["page"]})

    # 6. Minor course codes should exist in the catalogue
    minor_unknown = sorted({(m["name"], c["course_code"]) for m in minors for g in m["groups"] for c in g["courses"]
                            if c["course_code"] not in known and not (equiv.get(c["course_code"], set()) & known)})

    # 7. Hand-encoded regulation rules whose quotes were not found in the source text
    unverified_rules = [r["id"] for r in regulations["rules"] if not r["verified"]]

    # 8. Handout coverage for offered in-scope courses (a handout covers every code on its Course No. line)
    with_handout = {c for h in handouts for c in h["course_codes"]} & offered
    verified_eval = {c for h in handouts if h["weights_total"] and not evaluation_flagged(h["needs_verification"])
                     for c in h["course_codes"]} & offered
    by_llm = sum(1 for h in handouts if h["extracted_by"] == "llm")

    # 9. Records each parser already flagged
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
            "listed_courses_without_catalogue_entry": len(listed_unknown),
            "programme_lists_without_printed_name": len(unnamed),
            "charts_checked_against_lists": sum(1 for c in charts if c["kind"] == "single" and c["totals"]["core_courses"]),
            "chart_vs_list_core_mismatches": len(totals_mismatch),
            "minor_courses_without_catalogue_entry": len(minor_unknown),
            "regulation_clauses": len(regulations["clauses"]),
            "regulation_rules_unverified": len(unverified_rules),
            "offered_in_scope_with_handout": len(with_handout),
            "offered_in_scope_with_verified_evaluation": len(verified_eval),
            "handouts_evaluation_by_llm": by_llm,
            "flagged_handouts": sum(1 for h in handouts if h["needs_verification"]),
            "flagged_catalogue_records": len(flagged["catalogue"]),
            "flagged_timetable_records": len(flagged["timetable"]),
        },
        "unknown_prerequisites": unknown_prereqs,
        "offered_without_description": no_description,
        "unknown_equivalence_codes": unknown_equiv,
        "listed_courses_without_catalogue_entry": [
            {"list": w, "course": c, "page": pg} for w, c, pg in listed_unknown],
        "programme_lists_without_printed_name": unnamed,
        "chart_vs_list_core_mismatches": totals_mismatch,
        "minor_courses_without_catalogue_entry": [{"minor": m, "course": c} for m, c in minor_unknown],
        "regulation_rules_unverified": unverified_rules,
        "offered_without_handout": sorted(offered - with_handout),
        "flagged_handouts": {h["file"]: h["needs_verification"] for h in handouts if h["needs_verification"]},
        "flagged": flagged,
    }
    OUT.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    for k, v in report["summary"].items():
        print(f"  {k:36} {v}")
    for mm in totals_mismatch:
        print(f"  core mismatch: {mm['chart']}: chart {mm['chart_core']} vs list {mm['list_core']}"
              f"  list-only {mm['list_only']}  chart-only {mm['chart_only']}")
    for u in unknown_prereqs[:10]:
        print(f"  unknown prereq: {u['course']} -> {u['unknown_prerequisite']}  ({u['text'][:60]})")
    print(f"-> {OUT}")


if __name__ == "__main__":
    run()
