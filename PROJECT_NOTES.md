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
| `recommender/ingest/regulations.py` | All clauses verbatim + hand-encoded rules verified against exact quotes | No |
| `recommender/ingest/handouts.py` | Attendance, midsem/compre, evaluation, makeup | Regex first; only unreadable handouts go to the LLM, labelled `confidence: medium` (D10) |
| `recommender/llm.py` | The only module that talks to an LLM provider (Gemini by default); everything else calls it | Yes |
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
| Academic-Regulations-2023.pdf | 70 pages | All text | Numbered clauses (number in right margin), two small 2-column tables (3.14, 3.15) | Every clause kept verbatim; engine rules hand-encoded with quotes checked by the build |
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
| §2 | Use Regulations | ✅ | `regulations.py`: 256 clauses + 38 verified rules (incl. timetable registration instructions) |
| §2 | Use Bulletin | ✅ | catalogue, CDC/DEL lists, HUEL pool, semester charts (single + dual), category units, policies, minors |
| §2 | Use Timetable | ✅ | `timetable.py`, `equivalents.py` |
| §2 | Use Handouts | ✅ | `handouts.py` (regex) + `handouts_llm.py` (Gemini fallback, validated) |
| §2 | Student profile | ✅ | `StudentProfile` (schema); entered in the dashboard sidebar (none supplied) |
| §3 | Pre-process into a clean structured dataset | ✅ | `data/processed/*.json`, one command (`scripts/build_data.py`) |
| §3 | Course: code, title, department, units | ✅ | `courses.json` |
| §3 | Course: topics | ✅ | catalogue description + handout course plan/scope; matched by the agent's `search_courses` |
| §3 | Course: prerequisites | ✅/➖ | 66 stated; the rest are not in the supplied data → "not stated" |
| §3 | Course: restrictions | ✅/➖ | 1 exclusion rule; per-course restriction lists aren't supplied (timetable §VI → website) |
| §3 | Course: category | design | computed per student (depends on programme), not stored per course |
| §3 | Programme rules (CDC/DEL/HUEL/OPEL, batch-specific) | ✅ | `course_lists`, `semester_charts`, `degree_rules`, `minors` JSON; batch = one bulletin (owner decision S4) |
| §3 | Handout data | ✅ | evaluation components/weights, open/closed book, make-up, attendance, grading, course plan, pages |
| §3 | Timetable: code, section, instructor, days, hours, room, midsem, compre | ✅ | `timetable.json` |
| §3 | Source metadata: document, page/section, confidence | ✅ | `SourceRef` on every record |
| §3 | Normalise codes and categories | ✅ | codes (`codes.py`, incl. `ECE/EEE/INSTR F212`); categories computed per student |
| §3 | Mark unreliable items for verification | ✅ | `needs_verification` + `validation_report.json` |
| §4 | Profile incl. minor | ✅ | batch, degree(s), minor, completed courses + grades, CGPA, interests |
| §5 | Requirement analysis → eligible set → matching → validation | ✅ | `requirements.py` → `eligibility.py` → agent `search_courses` → `scheduler.py` |
| §6 | NL queries | ✅ | `agent/` Gemini function calling over 6 engine tools |
| §7 | Handout-based preferences; "could not be verified" | ✅ | midsem/compre/continuous %, open book, policies; `unverified` list per course |
| §8 | Timetable intelligence (bonus) | ✅ | `scheduler.py`: sections, clashes, lunch, compre/midsem clash, 25 units, free days/hours |
| §9 | Deterministic rule checking | ✅ | engine has no LLM; 21 tests pin the rules (`tests/test_engine.py`) |
| §9 | Source references kept | ✅ | |
| §9 | Validate codes, prerequisites, categories, programme requirements | ✅ | codes, prerequisites, list/minor codes, chart totals vs lists (`validate.py`) |
| §9 | New timetable/handouts without logic changes | ✅ | column detection from header; handouts globbed; page ranges found by headings |
| §10 | Working dashboard, live, not hard-coded | ✅ | `app/streamlit_app.py` (all choices from the data) |
| §10 | Clean Git repo + pipeline + README | ✅ | README quick start, tests, one-command build; commit when the owner asks |

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

**Regulations**
| # | Assumption | Status |
|---|---|---|
| R1 | "Cleared" = any letter grade, E included (clause 1.15); NC, W, I, RC do not clear. W is ignored (4.17) | ✅ clauses 1.15, 4.17 |
| R2 | A prerequisite is met by clearing it; course-specific minimum grades ("inadequate grade", 3.25 I) are not in the supplied data → not checked, stated as a caveat | ❓ data gap |
| R3 | The CGPA threshold for taking a higher-degree course (2.08) is set by AGC and not supplied → the engine warns instead of blocking | ❓ data gap |
| R4 | Lunch rule (timetable p.116) = at least one of periods 4, 5, 6 free every day | ✅ reading of "provision for lunch hour on all days" |
| R5 | Engine rules are for first-degree students; Ph.D./off-campus clauses are kept for citation only. Section 12 (administrative checklist) and 13 are skipped | design choice |

**Design**
| # | Assumption | Status |
|---|---|---|
| D1 | A course's category (CDC/DEL/OPEL/HUEL) is computed per student, not stored per course | design choice |
| D2 | DEL lists per programme come from Bulletin IV-106–128 | ✅ in the data |
| D5 | Programme list printed without a name (IV-124) = BBA (Honours) | 👤 (confidence medium) |
| D6 | An 'or' between two **elective** entries (Maths: CS F211 or BITS F232) = don't count both; both stay listed | design choice |
| D7 | Specialisations (e.g. Mech. Engg with specialization in Aerospace) are separate programme choices | ✅ separate lists in the data |
| D8 | CDCs come from the course list; chart = year/semester only; list/chart conflicts shown to the student | 👤 (`CDC_SOURCE`) |
| D9 | Dual degree: requirements = both degrees' CDCs/DELs + dual principles (IV-2); the 72 composite charts give timing | ✅ in the data |
| D3 | Tech stack: Python + **Gemini API** for the LLM + Streamlit dashboard (changed from Claude API: the owner has Gemini keys). The provider sits behind `llm.py`, so switching is one file | 👤 |
| D10 | The LLM is used only at the edges: the agent (query understanding, interest matching, explanation) and as a fallback for handouts regex can't read (labelled `extracted_by: llm`, confidence medium). All requirement/eligibility/clash decisions are deterministic, and the app works without a key except for chat | 👤 |
| D4 | Students enter completed courses as `CODE GRADE` lines, in chronological order | design choice |
| D11 | Requirement categories: cleared courses fill named/CDC slots first, then DEL (programme list), HUEL (pool, not own discipline), then OPEL (2.05 overflow) | ✅ Regulations 2.05, Bulletin IV |
| D12 | Chart courses that the list doesn't have (Bulletin conflict) are shown as `disputed`, not required; list-only CDCs are required with "year unknown" | 👤 via D8 |
| D13 | "Could not be verified" (unstated prerequisites, unreadable handouts) is shown per course but doesn't change eligibility; "warning" is kept for real conditions (DCA approval, higher-degree limit, allotment) | design choice |
| D14 | LLM for handouts only READS "best N of M"; the arithmetic is done in code. Every LLM answer passes the same checks as regex output | design choice |
| D15 | Agent falls back to lighter Gemini models when the main model is overloaded (503), keeping chat history | design choice |

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

### Step 6 — Stage 1c: programme course lists (`recommender/ingest/course_lists.py`)
Targets brief §3 (Programme Rules); feeds §5.
- Source: Bulletin Part IV "List of Courses for B.E. / M.Sc. / B.Pharm. Programmes" (IV-106–128, PDF 314–336),
  located from the Part IV contents page. It contains per-programme CORE and DISCIPLINE ELECTIVE lists,
  then the HUEL pool, project-type courses, other courses and audit courses.
- Shared helper moved to `pdf_utils.span_rows()` (catalogue output verified identical).
- Column split x=258 (measured: 0 spans cross it on these pages).
- Row-normalisation pass for split cells (`ECOM`/`F321`, `CS G514/`/`SS G514` → alias).
- State machine over rows: programme / list type / track-pool / OR alternatives.
- **Output:** `data/processed/course_lists.json`. 28 programmes, 405 core slots, 974 elective entries,
  HUEL pool 136, project 6 (`XXX` = any discipline), other 46, audit 30, 4 policy rules with pages.
- **Cross-check:** CS core = 14 courses / 48 units, matching the CS semester chart.
- **Rules captured:** own-discipline courses can't count as HUEL (p.335); project-course limits (p.333);
  Chem Engg (EES) DEL pools 3 + 2; EEE core alternative MATH F212 OR ME F344.
- **Unnamed list** (IV-124) → BACHELOR OF BUSINESS ADMINISTRATION (HONOURS), owner-confirmed, confidence medium,
  via `UNNAMED_PROGRAMME_NAMES` in `config.py` with a guard (most common core prefix, unique to this list).
  Evidence corrected during the step: 6 of 14 core courses are BBA (not all).
- **Validation:** 24 listed courses have no catalogue entry (absent from Part VI, e.g. ENVS F221, INSTR F429).

**Run it:** `python -m recommender.ingest.course_lists`

### Step 7 — Stages 1d–1f: semester charts, degree rules, minors
Completes brief §3 "Programme Rules"; feeds §5.
- **`semester_charts.py`** → `semester_charts.json`: 28 single + 72 dual charts (PDF 211–313). Year blocks cut at
  delimiter rows (semester header, bold unit totals, Summer); year from Roman numeral (BBA year III inferred + flagged);
  semester by x (split 262); courses, elective slots, alternatives ("or"); printed core/DEL totals.
  Linked to course lists by CDC overlap, ties broken by title words → one-to-one 28↔28.
- **`degree_rules.py`** → `degree_rules.json`: IV-1 category table as numbers (HUEL 8u/3c, OPEL 15–27u/5–9c, total 144u/42c,
  PS 25u OR thesis 9–20u) + IV-2 prose rules with pages (GIR courses, HUEL heads, thesis, dual degree).
- **`minors.py`** → `minors.json`: 23 minors + 9 general rules. Group boundaries from **drawn borders crossing the label
  column** (labels are vertically centred; the table detector's merged-cell guess was wrong for 2 minors).
  Title carry-over across pages; word minimums ("Any two"); "or" alternatives (`ListedCourse.alternative_to_previous`).
- **Cross-check (validate.py):** chart core totals vs course lists → 22/26 exact; 4 genuine Bulletin inconsistencies
  (ECE, ENVS, B.Pharm, BBA) with list-only / chart-only courses recorded.
  **Decision (owner):** course list defines CDCs, chart gives timing, conflicts shown to the student (`CDC_SOURCE`).

**Run:** `python -m scripts.build_data --skip-text` (all stages)

### Step 8 — Stage 1g: Academic Regulations (`recommender/ingest/regulations.py`)
Targets brief §2 (use the Regulations) and §5 "BITS policy validation"; feeds the engine and the agent's citations.
- **Clauses:** clause numbers are printed in the right margin (x > 380, after a gap ≥ 12 pt) on each clause's first
  line; `3.25 I`–`IV` carry a Roman numeral. Rows between two markers = the clause text. Section from the contents page.
  **Output:** 256 clauses, 1.00–11.02, no duplicates. Checked against a plain-text scan: nothing missing (the scan's
  "7.00"/"9.00" are CGPA values; the parser also recovered 3.26 and 4.23, which the scan missed).
- **Two-column tables** (3.14 prior preparation, 3.15 host regions) were read row by row and mixed the columns.
  Fix: a table starts on a row with two `(i)` markers (the second gives the column split); words are split by column until
  normal full-width text resumes; items are paired `(i) PS I -> requires: …`.
- **Rules:** 38 machine-usable rules (34 from clauses, 4 from timetable p.116), hand-encoded because numbers in prose
  can't be extracted reliably. Each has exact **quotes**; the build checks every quote appears in the cited clause
  (whitespace/quote-style normalised) → `verified`. The first run caught 3 rules whose quotes were broken by the table
  interleaving; all 38 verified after the table fix.
- Key rules: 25 units/semester (1.01), summer 3 courses/10 units (1.03), 4 extra electives (2.08 + p.116), PS/16-unit
  thesis exclusive (2.10), prerequisites (3.13), prior preparation (3.14/3.15), no timetable conflict (3.19),
  backlog → current → higher-level order (3.25), grade points (4.11), CGPA 4.50 (5.02, 9.01), minors (7.37, 9.01a),
  lunch periods 4–6 and no compre clash (p.116).
- **validate.py** now also reports `regulation_clauses` and `regulation_rules_unverified` (0).
- **Decisions (owner):** LLM = Gemini behind `llm.py` (D3). LLM only at the edges: agent + fallback for unreadable handouts (D10).

**Run it:** `python -m recommender.ingest.regulations`

### Step 9 — Stage 2: handouts (`ingest/handouts.py`, `ingest/handouts_llm.py`, `llm.py`)
Targets brief §3 (handout data) and §7 (handout-based preferences, "could not be verified").
- **Survey:** 540 handouts, 2 scanned (MAC/MATH F214), loose template (numbered sections + evaluation table).
- **Data vs text:** weights, midsem/compre/continuous %, open/closed book, attendance % → fields; make-up / grading
  prose → quoted text with page (no yes/no guessing).
- **Regex pass:** PyMuPDF tables; columns found by content (weight column = numbers summing to ~100; headers were
  often shifted); `50 (25%)` → 25, `20+10` → 30, marks → % (noted), merged-column names, nameless rows skipped,
  footnotes kept, page continuations merged. Check: total 100 ± 1. **455/540** clean (prototype: 64%).
- **LLM fallback (D10, D14):** only evaluation-flagged handouts of in-scope courses; schema-forced JSON, temperature 0,
  cached by input hash (`llm_cache.json`), same validation as regex, escalation to the stronger model once; "best N of M"
  read by the LLM, computed in code; scanned PDFs sent as PDF. Result: **44 fixed**, rest stay flagged.
- **Misnumbered files:** 27 files disagree with their printed course number. The printed TITLE decides: renumbered
  courses keep both codes (BIO U101 = BIO F101); misprinted numbers are dropped (the CS/ECE/EEE/INSTR F215 "Digital
  Design" handouts print "F342"); wrong documents aren't used for the file-name course (BIO G523 file = BIO F212
  handout). Equivalences from handouts use the same rule.
- **Models:** Gemini 2.5 is closed to new keys (404 despite being listed); 3.5-flash / 3.5-flash-lite tested and used.

### Step 10 — `store.py` (retrieval layer)
- Only reader of `data/processed`; lazy load + Pydantic validation + indexes. Scope applied once (offered = not
  cancelled, comp < 5000). Equivalents symmetric; catalogue falls back to an equivalent's entry.
- Duplicate catalogue codes resolved: course-list title, else not another campus (BITS F335 → Discover India), else
  first printed; the alternative title is flagged.

### Step 11 — Rules engine (`engine/requirements.py`, `engine/eligibility.py`) + tests
- **Requirements:** chart named slots (GIR, PS-I, PS-II/thesis, OR-alternatives = one slot) + list CDCs (list-only added,
  chart-only `disputed`); cleared = latest letter grade (E counts, W ignored); equivalents satisfy slots ("via") with
  no double credit; electives DEL → HUEL (not own discipline) → OPEL (2.05); dual degree per IV-2.
- **Eligibility:** done/equivalent-done, exclusions, prerequisites (None → unverified), 3.15(b)(i)/(ii), 2.08,
  3.14(iii) (DCA may allow 2), 3.25 III, PS/project allotment; category per degree; priority backlog → CDC → electives.
- **Fix found by the engine work:** `ECE/EEE/INSTR F212` prerequisites were read as INSTR F212 only → shared-number
  expansion in `codes.py` (fixes catalogue, handouts and equivalents together).
- **Tests:** `tests/test_engine.py`, 24 tests on the real data, one rule each (`python -m pytest -q`).

### Step 12 — Scheduler (bonus, `engine/scheduler.py`)
- One section per type (L/T/P) per course; hard rules: no meeting clash (3.19), lunch period 4/5/6 free daily
  (p.116), no compre/midsem clash, ≤ 25 units (1.01); soft: free days/hours. Depth-first, most-constrained first,
  pruned by best plan; explains impossible sets.

### Step 13 — Agent + dashboard (`agent/`, `app/streamlit_app.py`)
- **Agent:** Gemini function calling over 6 tools (requirements, recommend, interest search, course details, timetable
  check, regulation search). System prompt: facts only from tools, never override eligibility, "could not be
  verified", cite pages/clauses, full-plan check. Overload (503) → fallback models with history (D15).
- **Dashboard:** sidebar profile (all options from the data), tabs Requirements / Courses / Timetable / Advisor;
  works without a key except chat. Headless-tested with `streamlit.testing`.

**Run:** `python -m scripts.build_data --skip-text` · `python -m pytest -q` · `streamlit run app/streamlit_app.py`
