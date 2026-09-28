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
pip install -r requirements-dev.txt && python -m pytest -q   # 27 engine tests on the committed data
```

The processed data is committed, so the app runs without the raw PDFs. In the sidebar, create a
profile (campus, admission year, degree / dual degree, year of study, completed courses with grades,
current courses, minor, interests) or click **Load sample 3rd-year CS student**. Then:

- **Requirements** - degree progress: named/CDC slots done and remaining (with year/semester), DEL /
  HUEL / OPEL units, courses satisfied via an equivalent, and Bulletin conflicts shown openly.
- **Courses** - every course offered this semester checked for this student: status (eligible /
  warning / ineligible / done / registered) with the clause behind each reason, what it counts as,
  handout evaluation split, attendance and make-up policy, sections. Filters for no midsem, project
  components, attendance, minor.
- **Timetable** - picks clash-free sections (class, tutorial, lab, lunch period, midsem, compre, 25-unit
  limit), with free-day, avoided-period and compact-timetable preferences.
- **Advisor** - chat, e.g. *"Suggest an AI-related DEL with no midsem"*, *"Find an OPEL with no attendance
  requirement"*. The answer lists the requirement each course satisfies, its eligibility, the properties
  asked about (with source) and why it matches; the tools it called are shown under each answer.

## How it works

```
PDFs --(ingest/, once per semester)--> data/processed/*.json  (every record: source page + verification flags)
                                              |
Profile + question --> store.py --> engine/requirements.py   remaining CDC / DEL / HUEL / OPEL
                                    engine/eligibility.py    eligible set + category + policy checks
                                    agent/ (Gemini)          intent -> tools -> interest/handout matching
                                    engine/scheduler.py      feasible sections
                                    agent/                   concise answer with citations
```

- **Deterministic where it matters.** Requirements, eligibility, categories and clashes are plain Python
  with no LLM, each rule citing its Regulations clause or Bulletin page (38 regulation rules are checked
  against quotes from their clauses on every build). 27 tests pin the rules on the real data.
- **LLM at the edges.** Gemini understands the question, expands interests into search terms, calls the
  engine's tools and writes the explanation. It may not state a fact that no tool returned; unverifiable
  properties are reported as "could not be verified". It is also a validated, cached fallback for the
  handouts the regex parser can't read.
- **Never guessed.** Missing data is stored as "not stated", not as "none": for example, most prerequisites
  are not in the supplied documents (the timetable points to a website), so they are shown as unverified.
- **New semester = new files.** Drop in a new timetable / handouts and re-run `scripts/build_data`;
  columns and page ranges are found from headings, not hard-coded.

## Known limitations

- Only the documents supplied: prerequisites for most courses, per-course restriction lists and the CGPA
  threshold for higher-degree courses are not in them, so those checks are shown as unverified.
- The Bulletin 2025-26 and Regulations 2023 are applied to every supported batch (with a caveat); four
  programmes have genuine list-vs-chart conflicts in the Bulletin, shown to the student.
- 350 of 479 in-scope offered courses have a verified evaluation scheme; the rest have no handout or an
  unreadable one and are labelled accordingly.
- The chat depends on Gemini availability; on overload it falls back to lighter models.

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

## Deploy (Streamlit Community Cloud)

The app only reads `data/processed/` (committed), so no PDFs are needed in the cloud.

1. Push this repository to GitHub (a private repository works).
2. At https://share.streamlit.io choose **Create app**, select the repository and branch, and set
   **Main file path** to `app/streamlit_app.py`. Under *Advanced settings* choose Python 3.12.
3. In the app's **Settings → Secrets** add:
   ```toml
   GEMINI_API_KEY = "your-key"
   ```
   (`llm.py` reads `.env` locally and Streamlit secrets when hosted; without it only the chat is disabled.)

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
- Batches: admitted up to 2025 (2026 admissions have a new curriculum not in the supplied Bulletin). Requirements are based on Bulletin 2025-26 and
  Academic Regulations 2023.
