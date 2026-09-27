# BITS Academic Course Recommender

An agentic course recommender for BITS Pilani students. A deterministic rules engine
decides what a student is required and eligible to take; an LLM (Gemini, behind a swappable `llm.py`) handles
natural-language queries, interest matching and explanations. All answers come from
structured data pre-processed from the supplied BITS documents, with source references.

See `PROJECT_NOTES.md` for design, decisions and progress.

## Quick start

```bash
pip install -r requirements.txt
streamlit run app/streamlit_app.py      # dashboard at http://localhost:8501
python -m pytest -q                     # engine tests (run on the committed processed data)
```

The processed data is committed, so the app runs without the raw PDFs. The dashboard has four tabs:
**Requirements** (degree progress, remaining CDC/GIR slots, DEL/HUEL/OPEL units, Bulletin conflicts),
**Courses** (every offered course checked for the student: status, reasons, what it counts as,
handout evaluation, sections), **Timetable** (clash-free section planner with free-day/hour
preferences) and **Advisor** (Gemini chat that answers through the engine's tools).

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
```

### Gemini API key (optional)

The LLM (Gemini) is only used for the chat agent and as a fallback for handouts the parser
can't read. Create a file named `.env` in the repo root (it is git-ignored) containing:

```
GEMINI_API_KEY=your-key
```

Get a key at https://aistudio.google.com. Without a key the build still runs (the LLM step is
skipped and those handouts stay flagged) and everything except chat works.

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
python -m scripts.build_data            # all stages (~10 min; handout tables are the slow part)
python -m scripts.build_data --skip-text   # reuse already-extracted text
```

Outputs in `data/processed/` (committed, so the app runs without re-building):

| File | Contents |
|---|---|
| `timetable.json` | Offered courses: sections, days/hours, rooms, instructors, midsem/compre slots |
| `courses.json` | Course catalogue from Bulletin Part VI: title, units, description, stated prerequisites |
| `course_lists.json` | Per-programme CDC and DEL lists (Bulletin Part IV), HUEL pool, project/other/audit courses, list rules |
| `semester_charts.json` | Year/semester placement of courses and elective slots for 28 degrees and 72 dual-degree pairs |
| `degree_rules.json` | Category-wise unit/course requirements (HUEL, OPEL, core, total…) and prose policies with page refs |
| `minors.json` | 23 minor programmes (core, elective pools with minimums) and general minor rules |
| `regulations.json` | Academic Regulations 2023 clause by clause, plus engine rules (unit limits, extra electives, clash rules, grade points…) each verified against a quote from its clause |
| `handouts.json` | Per handout: evaluation components (weight, duration, date, open/closed book), midsem/compre/continuous shares, sections (make-up, attendance, grading, course plan…) with pages; `extracted_by` regex or llm |
| `llm_cache.json` | Cached LLM answers keyed by input hash, so re-building makes no new API calls |
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
  llm.py           the only module that calls the LLM provider (Gemini); swappable
  ingest/          one parser per source document
  store.py         loads and indexes data/processed (the only reader of those files)
  engine/          requirements.py, eligibility.py, scheduler.py - deterministic, no LLM
  agent/           tools.py (engine functions exposed to the LLM), agent.py (system prompt, chat)
scripts/build_data.py   runs the ingestion pipeline
app/               Streamlit dashboard
tests/
```

## Scope

- Campus: Pilani (the supplied timetable is Pilani's).
- Semester: First Semester 2026-27.
- Batches: admitted up to 2025. Requirements are based on Bulletin 2025-26 and
  Academic Regulations 2023.
