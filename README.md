# BITS Academic Course Recommender

An agentic course recommender for BITS Pilani students. A deterministic rules engine
decides what a student is required and eligible to take; an LLM (Claude) handles
natural-language queries, interest matching and explanations. All answers come from
structured data pre-processed from the supplied BITS documents, with source references.

> Status: work in progress — data ingestion pipeline in place; engine, agent and
> dashboard to follow. See `PROJECT_NOTES.md` for design, decisions and progress.

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
```

## Data

The supplied dataset is not committed (≈250 MB). Place `dataset.zip` in the repo root and unzip it:

```bash
mkdir -p data/raw && unzip dataset.zip -x "__MACOSX/*" -d data/raw
```

Expected layout:

```
data/raw/Academic-Regulations-2023.pdf
data/raw/bulletin.pdf
data/raw/timetable.pdf
data/raw/handouts/*.pdf
```

## Build the structured dataset

```bash
python -m scripts.build_data            # all stages (~2 min)
python -m scripts.build_data --skip-text   # reuse already-extracted text
```

Outputs in `data/processed/` (committed, so the app runs without re-building):

| File | Contents |
|---|---|
| `timetable.json` | Offered courses: sections, days/hours, rooms, instructors, midsem/compre slots |
| `courses.json` | Course catalogue from Bulletin Part VI: title, units, description, stated prerequisites |
| `equivalents.json` | Equivalent / cross-listed course codes (printed, handout-stated, inferred) |
| `validation_report.json` | Cross-document checks: unknown prerequisite codes, offered courses without descriptions, flagged records |

Every record carries a `source` (document, page) and a `needs_verification` list for
anything that could not be extracted reliably.

On Windows, if console output fails with `UnicodeEncodeError`, set `PYTHONIOENCODING=utf-8`.

## Project layout

```
recommender/
  config.py        paths and scope constants
  codes.py         course-code normalisation
  schema.py        Pydantic data models
  ingest/          one parser per source document
  engine/          deterministic requirement / eligibility / scheduling logic
  agent/           LLM tools and query handling
scripts/build_data.py   runs the ingestion pipeline
app/               Streamlit dashboard
tests/
```

## Scope

- Campus: Pilani (the supplied timetable is Pilani's).
- Semester: First Semester 2026-27.
- Batches: admitted up to 2025. Requirements are based on Bulletin 2025-26 and
  Academic Regulations 2023.
