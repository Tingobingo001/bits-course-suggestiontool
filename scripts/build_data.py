"""Run the whole ingestion pipeline in dependency order.

Usage (from the repo root):  python -m scripts.build_data [--skip-text]
"""
import sys

from recommender.ingest import bulletin_courses, equivalents, pdf_text, timetable, validate

STAGES = [
    ("Stage 0  PDF -> text", pdf_text.run),
    ("Stage 1a timetable", timetable.run),
    ("Stage 1b course catalogue", bulletin_courses.run),
    ("Stage 1a+ equivalents (needs 1a, 1b)", equivalents.run),
    ("Final    cross-document validation", validate.run),
]


def main():
    skip_text = "--skip-text" in sys.argv
    for name, run in STAGES:
        if skip_text and run is pdf_text.run:
            continue
        print(f"\n=== {name} ===")
        run()


if __name__ == "__main__":
    main()
