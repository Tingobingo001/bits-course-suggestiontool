# BITS Course Recommender — Project Notes

A running record of what we built, why, and what we learned about the data.
Read top to bottom to understand the project; the log at the bottom is chronological.

---

## 1. The core idea (the one-sentence pitch)

**A deterministic rules engine wrapped by an LLM.**

- *Code* decides what a student is **allowed** to take: remaining requirements,
  prerequisites, eligibility, timetable clashes. It is testable and cannot hallucinate.
- The *LLM* handles **language**: extracting fields from messy handouts (offline, once),
  turning a query like "AI DEL with no midsem" into filters, matching interests,
  and writing explanations.

The LLM never decides eligibility and never names a course the tools didn't return.

## 2. Pipeline

```
OFFLINE (once per semester)                    ONLINE (per query)
PDFs ─► Stage 0: PDF → text                    Profile + query
     ─► Stage 1: per-document parsers              ▼
     ─► validate + normalise codes             requirements.py  (remaining CDC/DEL/HUEL/OPEL)
     ─► data/processed/*.json (+ source refs)      ▼
                                               eligibility.py   (eligible course set)
                                                   ▼
                                               agent: query → filters, interest ranking
                                                   ▼
                                               scheduler.py     (clash check, section choice)
                                                   ▼
                                               agent: explained answer with citations
```

## 3. Module map (why it's split this way)

Each module does exactly one job. Modules communicate only through files in
`data/` or the data shapes in `schema.py`, so any one can be rewritten or re-run alone.
A new semester = drop in new timetable/handout PDFs and re-run ingest; engine and
agent code do not change.

| Module | Job | Uses LLM? |
|---|---|---|
| `recommender/config.py` | All paths/constants in one place | — |
| `recommender/schema.py` | Data shapes (Course, Section, Handout, Rules, Student, SourceRef) | — |
| `recommender/ingest/pdf_text.py` | Stage 0: PDF → text with page markers | No |
| `recommender/ingest/timetable.py` | Offered courses, sections, slots, exam dates | No |
| `recommender/ingest/bulletin_courses.py` | Course catalogue (code, title, units, description) | No |
| `recommender/ingest/bulletin_programmes.py` | Per-degree CDC list + unit requirements | No |
| `recommender/ingest/regulations.py` | Academic rules (loads, prereq rules, etc.) | Assisted + manual check |
| `recommender/ingest/handouts.py` | Attendance, midsem/compre, evaluation, makeup | Yes (structured extraction) |
| `recommender/store.py` | Retrieval layer: load processed data, lookups | No |
| `recommender/engine/requirements.py` | Remaining requirements for a student | No |
| `recommender/engine/eligibility.py` | Prereqs, restrictions, offered-this-sem | No |
| `recommender/engine/scheduler.py` | Clash detection, section selection | No |
| `recommender/agent/tools.py` | Exposes engine/store functions as LLM tools | — |
| `recommender/agent/agent.py` | Query understanding + explanation | Yes |
| `scripts/build_data.py` | Runs all ingest stages in order | — |
| `app/streamlit_app.py` | Dashboard: profile + chat | — |

## 4. Principles we follow

1. **Never guess.** Missing info is stored as `"not_stated"`, not `false`. The answer then
   says "could not be verified" — this is an explicit requirement in the brief.
2. **Every fact carries a source** (`document`, `page`, `confidence`).
3. **Normalise course codes** everywhere (`CSF301`, `CS  F301` → `CS F301`). Most join bugs come from this.
4. **Course category is relative to the student.** CS F301 is a CDC for a CS student,
   possibly a DEL elsewhere, and an OPEL for others. So category is *computed*, not stored.
5. **Validate after extraction** (every prereq code exists, every timetable code exists, units numeric)
   and write failures to a `needs_verification` report.

## 5. What the data looks like (findings)

| Source | Size | Text or scanned? | Structure | Planned parsing |
|---|---|---|---|---|
| Academic-Regulations-2023.pdf | 70 pages | All text | Prose | Locate rule sections; extract numbers with page refs; verify by hand |
| bulletin.pdf (2025-26) | 951 pages | 12 near-empty pages | Mixed prose, charts, 2-column descriptions | See below |
| timetable.pdf (Sem I 2026-27, Pilani) | 153 pages | All text | Fixed-column table | Column-position parsing |
| handouts/ | 540 PDFs | Mostly text; a few ~empty (scanned) | Semi-structured | Regex for header, LLM for the rest |

**Bulletin — useful parts only:**
- p.209 (IV-1): category-wise unit requirements for all first degrees
  (HUEL 8 units/3 courses, Discipline Core 33–48, DEL 12–27 units/4–9 courses,
  OPEL 15–27 units/5–9 courses, 144 units / 42 courses total).
- p.~217 onwards (IV-9…): **semester-wise chart per degree** (e.g. B.E. CS on p.217).
  Lists every CDC with year/semester and units, plus the DEL total ("Discipline Electives-12 Units (4 Courses)").
- p.608+ (Part VI): **course descriptions**, format `CS F364 Design and Analysis of Algorithms  3 0 3`
  (= lecture hrs, practical hrs, units) then description text.
- Everything else (governance, fees, WILP programmes, faculty lists) is irrelevant → ignored.

**Gotcha — two-column layout.** Plain extraction interleaves the left and right columns
(e.g. the CS F364 description is mixed into CS F406's). Fix: crop each page into
left/right halves before extracting text. The semester charts need word x/y coordinates
to know which semester column a course sits in.

**Timetable row format:**
```
COMP  COURSE     TITLE                 L P T S U  SEC  INSTRUCTOR        ROOM  DAYS&HOURS  MIDSEM      COMPRE
1336  BITS F364  HUMAN COMP INTERACTION 3 - - - 3  L1   SIDDHARTH MEHROTRA 6101 M W F 9     09/10 FN1   13/12 FN
```
Extra sections/tutorials/labs continue on following lines under "Tutorial"/"Practical"
headers. Some rows say `CANCLED`. Hours are period numbers (1 = 8 AM).

**Handout header** is regular (`Course No. :`, `Course Title :`, `Instructor-in-charge :`) → regex.
Evaluation tables, attendance and makeup policy vary in wording → LLM extraction into a fixed schema.

**Open questions:**
- Where are DEL *lists* per discipline? Not found yet; the bulletin may treat any
  discipline course that isn't a CDC as a DEL. Needs confirmation.
- Scanned handouts: OCR them, or mark them `needs_verification`.
- The data is Pilani-only (timetable). The profile will still ask for campus, but only Pilani has an offering.

**Decisions made (with the project owner):**
- Cross-listed courses inferred only when title + lecture slot + room + compre all match (medium confidence).
- Supported batches: ≤ 2025. Courses with comp code ≥ 5000 (2026 admissions only) are excluded.
- If a handout says courses are the same (several codes on its `Course No.` line), record them as equivalent.

## 5a. Brief compliance checklist

Re-checked at the end of every step. ✅ done · 🟡 partial · ⬜ not started · ➖ not in supplied data.

| Brief § | Requirement | Status | Where / note |
|---|---|---|---|
| §2 | Use Regulations | ⬜ | policy step |
| §2 | Use Bulletin | 🟡 | catalogue ✅; programme structures, HUEL pool, minors ⬜ |
| §2 | Use Timetable | ✅ | `timetable.py`, `equivalents.py` |
| §2 | Use Handouts | 🟡 | equivalences only; content extraction ⬜ |
| §2 | Student profile | ⬜ | dashboard step (none supplied: created in the dashboard) |
| §3 | Pre-process into a clean structured dataset | 🟡 | `data/processed/*.json` |
| §3 | Course: code, title, department, units | ✅ | `courses.json` |
| §3 | Course: topics | 🟡 | description text; topic extraction with handouts |
| §3 | Course: prerequisites | ✅/➖ | 66 stated; the rest are not in the supplied data → "not stated" |
| §3 | Course: restrictions | ✅/➖ | 1 exclusion rule; per-course restriction lists aren't supplied (timetable §VI → website) |
| §3 | Course: category | design | computed per student (depends on programme), not stored per course |
| §3 | Programme rules (CDC/DEL/HUEL/OPEL, batch-specific) | ⬜ | next step |
| §3 | Handout data | ⬜ | handout step |
| §3 | Timetable: code, section, instructor, days, hours, room, midsem, compre | ✅ | `timetable.json` |
| §3 | Source metadata: document, page/section, confidence | ✅ | `SourceRef` on every record |
| §3 | Normalise codes and categories | 🟡 | codes ✅ (`codes.py`); categories with programme step |
| §3 | Mark unreliable items for verification | ✅ | `needs_verification` + `validation_report.json` |
| §4 | Profile incl. minor | ⬜ | dashboard; minor rules in Bulletin IV-129–141 |
| §5 | Requirement analysis → eligible set → matching → validation | ⬜ | engine + agent |
| §6 | NL queries | ⬜ | agent |
| §7 | Handout-based preferences; "could not be verified" | ⬜ | handouts + agent |
| §8 | Timetable intelligence (bonus) | ⬜ | scheduler (data ready) |
| §9 | Deterministic rule checking | ✅ | all parsers deterministic so far |
| §9 | Source references kept | ✅ | |
| §9 | Validate codes, prerequisites, categories, programme requirements | 🟡 | codes + prerequisites ✅ (`validate.py`); the rest with programme step |
| §9 | New timetable/handouts without logic changes | ✅ | column detection from header; handouts globbed; page ranges found by headings |
| §10 | Working dashboard, live, not hard-coded | ⬜ | |
| §10 | Clean Git repo + pipeline + README | 🟡 | git init, `.gitignore`, `requirements.txt`, README, `scripts/build_data.py`; not yet committed |

## 5b. Assumptions register

Status: ✅ verified in the data · 👤 confirmed by the project owner · ❓ unconfirmed.

**Scope**
| # | Assumption | Status |
|---|---|---|
| S1 | Recommendations are for First Semester 2026-27 (the supplied timetable) | ✅ timetable title |
| S2 | Only the Pilani campus is supported; other campuses get "no timetable supplied" | 👤 |
| S3 | Batches ≤ 2025 only; comp code ≥ 5000 excluded | 👤 |
| S4 | The 2025-26 bulletin's programme structures apply to all supported batches, shown with a caveat ("based on Bulletin 2025-26") | 👤 |
| S5 | Regulations 2023 apply to all supported batches (same caveat) | 👤 |

**Timetable**
| # | Assumption | Status |
|---|---|---|
| T1 | Column x-boundaries measured on one page hold on all 96 table pages | ✅ checked |
| T2 | Period n = (7+n):00–(7+n):50, e.g. 1 = 8 AM, 10 = 5 PM | ✅ legend p.6 |
| T3 | Periods 11–12 (seen in a few rows) continue hourly (6 PM, 7 PM); the legend stops at 10 | ❓ minor |
| T4 | Section type = first letter of the section code (L/T/P) | ✅ matches the Tutorial/Practical headers |
| T5 | A row with only an instructor name = co-instructor of the section above | ✅ spot-checked |
| T6 | Midsem FN1/FN2/AN1/AN2 and compre FN/AN are distinct slots; clash = same date + session | ✅ legend p.6 |
| T7 | "CANCLED" cancels that section only; a course is cancelled when no section survives | ✅ spot-checked |
| T8 | Rooms don't affect recommendations, so per-day rooms are flagged, not reconstructed | design choice |

**Catalogue**
| # | Assumption | Status |
|---|---|---|
| C1 | Only Part VI (on-campus) descriptions matter; Part VII (off-campus/WILP) is ignored | ❓ low risk |
| C2 | Prerequisites exist only where a labelled "Pre-requisite:" sentence says so; otherwise "not stated" | ✅ timetable: prereqs are on the website |
| C3 | "/" and "OR" in a prerequisite mean any-of; mixed AND/OR is flagged | ✅ read from examples |
| C4 | Units printed with "*" mean the breakup is announced via the timetable | ✅ Part VI legend |
| C5 | BITS F335 has two catalogue entries; Pilani → "Discover India" applies | 👤 via S2 |

**Equivalents**
| # | Assumption | Status |
|---|---|---|
| E1 | All codes in one printed row are mutually equivalent; rows are not chained | design choice |
| E2 | Cross-lists are inferred when title + lecture slot + room + compre match | 👤 |
| E3 | Several codes on a handout's "Course No." line = the same course | 👤 |
| E4 | Passing a course blocks recommending its equivalents | design choice |
| E5 | Passing an equivalent also **satisfies the requirement** (ECE F215 done → CS F215 CDC done, shown as "via ECE F215") | 👤 |
| E6 | Units come from the code actually registered, never from an equivalent | design choice |

**Design**
| # | Assumption | Status |
|---|---|---|
| D1 | A course's category (CDC/DEL/OPEL/HUEL) is computed per student, not stored per course | design choice |
| D2 | DEL rule for each discipline | ❓ pending: programme parser |
| D3 | Tech stack: Python + Claude API for the LLM + Streamlit dashboard | 👤 |
| D4 | Students enter completed courses by course code in the profile | ❓ |

---

## 6. Log

### Step 1 — Setup & Stage 0 (PDF → text)
- Unzipped `dataset.zip` into `data/raw/`, deleted `__MACOSX/` (macOS metadata junk).
- Installed `pymupdf` (fast PDF text extraction; `pdfplumber` is also available for tables).
- Wrote `recommender/ingest/pdf_text.py`: converts every PDF to `data/text/*.txt`
  with `===== PAGE n =====` markers. ~90 s for all 1,100+ PDFs, ~14 MB of text.
  **Why:** PDFs are slow to open and awkward to search. Text files can be grepped, and
  the page markers give us source references for free.
- Profiled documents (text vs scanned, where the useful sections are) → findings in §5.
- Created the modular package skeleton (§3) and `config.py`.

**Run it:** `python -m recommender.ingest.pdf_text`

### Step 2 — Stage 1a: timetable parser (`recommender/ingest/timetable.py`)
Targets brief §3 (pre-processing); its output also feeds §8 (timetable intelligence).
- Added shared `recommender/codes.py` (course-code normalisation) and `recommender/schema.py`
  (Pydantic models: `TimetableCourse`, `Section`, `Meeting`, `ExamSlot`, `SourceRef`).
- Parser reads words with x/y coordinates, assigns columns by x, groups rows by y (4 pt tolerance),
  classifies rows (new course / extra section / co-instructor / noise), decodes `M W F 9` schedules.
- Course table = pages 10–114; stops at "SUGGESTIONS FOR CHOOSING ELECTIVES".
- **Output:** `data/processed/timetable.json` — 728 rows → 615 active courses, 113 cancelled,
  1,568 sections, 0 skipped rows, 18 sections flagged.
- **Fixes after spot-checking:**
  - Header text leaking in as a co-instructor (88 sections) → skip everything above the `U/C` header label.
  - Per-day stacked rooms (`6107(M W) 1223(Th)`) → not reconstructed (rooms don't affect recommendations);
    fragments skipped, sections flagged `needs_verification`.
  - Cancellation made per-section: 16 courses were wrongly marked cancelled when only one section was.
- Same code can appear twice (comp ≥ 5000 = 2026 admissions only) → `comp_code` is the unique key.
- **Other finds in the timetable PDF:** list of equivalent courses (p.118–124, needed for "already done"
  checks — to parse later); HUEL pool lives in Bulletin Part IV; lunch periods are 4–6.

**Run it:** `python -m recommender.ingest.timetable`

### Step 3 — Stage 1b: course catalogue (`recommender/ingest/bulletin_courses.py`)
Targets brief §3; output feeds §5 (units, prerequisites) and §6 (interest matching on descriptions).
- Added `Course` model to `schema.py` (incl. `prerequisites: list | None`, `prerequisite_logic: all|any`).
- Part VI page range found from its own headings (not hard-coded). Each page cropped into
  left/right columns at x=297 → clean reading-order line stream.
- Header = `DEPT F123 Title` + units (`L P U`, `U` or `U*`) on the same or next 2 lines.
- **Output:** `data/processed/courses.json` — 2,007 courses; 469/539 offered courses covered;
  67 with stated prerequisites; 29 flagged.
- **Fixes after spot-checking:**
  - Prerequisite lines (printed at the end of a description, starting with a code) were read as
    headers and stole the next course's units → lookahead aborts when it meets another header.
  - Grouped headers sharing one description (`CS F366`/`CS F367 Lab Project`) → share it.
  - "is a prerequisite **for** X" produced a reversed prereq → require `Pre-requisite:` with colon.
  - OR-lists stored as `prerequisite_logic = "any"`; mixed AND/OR flagged.
- **Findings:**
  - Prerequisites are largely **absent** from the supplied data (timetable points to the website).
    `None` = not stated → the answer must say "could not be verified".
  - Missing descriptions are data gaps: 31 new U-level codes (2026 curriculum, not in the 2025-26 bulletin);
    cross-listed codes (ECON F315 is described as FIN F315) → resolve via the equivalence table;
    new courses (CS G569 Agentic AI) → handouts.
**Run it:** `python -m recommender.ingest.bulletin_courses`

### Step 4 — Stage 1a+: equivalent courses (`recommender/ingest/equivalents.py`)
Targets brief §3; feeds §5 (completed-course checks) and catalogue fallback.
- New shared helper `recommender/ingest/pdf_utils.py::group_rows`; timetable parser refactored to use it
  (verified `timetable.json` byte-identical before/after).
- `Equivalence` model with `basis: printed | inferred_crosslist`.
- **Printed:** 167 rows from the timetable's "List of Equivalent Courses" (pp.118–124). All codes in a row
  are mutually equivalent; rows are not chained.
- **Inferred cross-lists:** 45 records. Cross-listed courses (ECON F315 / FIN F315) are printed as
  separate rows and missing from the printed list. Rule: title + lecture schedule + room + compre slot
  all equal → `confidence: medium`.
- **Scope decisions (confirmed with owner):** batches ≤ 2025 only; comp code ≥ 5000 excluded
  (`MAX_SUPPORTED_BATCH`, `NEW_ADMISSIONS_MIN_COMP_CODE` in `config.py`). All 31 U-level codes have comp ≥ 5000.
- **Handout equivalences** (owner's rule): handouts whose `Course No.` line lists several codes
  → `basis: handout`, 134 records from 57 handouts.
- **Unit-conflict warning:** 12 equivalence groups have differing units (e.g. `BITS F494`/`CE F434`,
  thesis variants). Rule: equivalence blocks re-recommendation; units always come from the registered code.
- **Totals:** 346 records (167 printed, 134 handout, 45 inferred).
- **Coverage:** 479 in-scope offered courses, 453 (95%) have a description via catalogue + equivalents.
  26 remain (theses, seminars, reading/new courses).

**Run it:** `python -m recommender.ingest.equivalents` (after timetable and bulletin_courses)

### Step 5 — Brief-compliance audit & cleanup
Triggered by re-reading the brief line by line (checklist in §5a).
- **Repo:** `git init`, `.gitignore` (raw data, zip, extracted text, personal `notes.txt`), `requirements.txt`,
  README, `scripts/build_data.py` (one command runs every stage in order). `data/processed/` is kept in the repo.
- **Catalogue parser redesigned around font weight** (`bulletin_courses.py`). While adding departments,
  two prefixes mapped to two departments each, which exposed real bugs:
  - some headers are stored one word per PDF text line (`ECON`/`F412`/…) and were silently lost;
  - rebuilding lines from word positions fixed that but put units on the first title line.
  - Final approach: rows rebuilt from **span positions with font info**. Headers are bold, descriptions
    regular. A header = bold row starting with a code + up to 2 short bold continuation rows or a
    units row. Units are read from their own span or trailing the title (`3 1 4`, `4*`, `(5*)`).
  - Guards: bold prerequisite lines (`EEE F311: …` after "Pre-requisite:"), sentences starting with a code
    ("BITS F412 is a required course…"), the one course with a bold description, regular-font headers
    (flagged), unit ranges (`17-20`) and footnote `*`.
  - **Result vs original:** 0 lost, **27 courses recovered**, ~40 cut-off titles completed, departments
    consistent for every prefix, duplicates 7 → 2 (both genuine).
- **Department + section reference** on every course (from the Part VI heading it's printed under).
  Timetable/equivalence records carry the PDF's own section names.
- **Restrictions:** `excluded_if_completed` extracted (MAC F243 ✗ if MATH F471 done). Per-course restriction
  lists are not in the supplied data (timetable §VI points to the website).
- **Minors found:** Bulletin IV-129–141 ("Minor Programmes for First Degree Students"), Regulations 7.37 →
  programme step.
- **Registration policies found** (timetable p.116): max 4 electives beyond requirement; compre must not clash;
  lunch periods → policy step.
- **Timetable columns detected from header labels** (`HEADER_ANCHORS`) with fallback. Verified identical parse.
- **New `ingest/validate.py`** (runs last): all 66 prerequisite codes exist; 456/479 in-scope offered courses have
  a description; 128 unknown equivalence codes are all pre-2011 C-level codes (expected).
  Output: `data/processed/validation_report.json`.
