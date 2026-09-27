"""Central paths and constants. Every module imports locations from here."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATA = ROOT / "data"
RAW = DATA / "raw"              # original supplied PDFs, never modified
TEXT = DATA / "text"            # Stage 0 output: one .txt per PDF with page markers
PROCESSED = DATA / "processed"  # clean structured JSON consumed by the app

RAW_HANDOUTS = RAW / "handouts"
TEXT_HANDOUTS = TEXT / "handouts"

PAGE_MARKER = "===== PAGE {n} ====="

# Scope decision (confirmed with the project owner): the supplied Bulletin (2025-26) and
# Regulations (2023) describe batches up to 2025. The 2026 batch follows a new curriculum
# (U-level codes) that the bulletin doesn't cover, so it is out of scope.
MAX_SUPPORTED_BATCH = 2025
# Timetable note: "Courses with com cod >= 5000 are meant only for 2026 admissions".
NEW_ADMISSIONS_MIN_COMP_CODE = 5000

# Bulletin IV-124 prints a CDC/DEL list without a programme name. BBA is the most common
# prefix of its core (6 of 14; the rest are management courses) and appears in no other
# programme's core; the Part IV contents lists a BBA (Honours) programme. Naming it this way was
# confirmed with the project owner; the record is marked confidence="medium".
#   most common core-course prefix (used by no other programme) -> programme name
UNNAMED_PROGRAMME_NAMES = {"BBA": "BACHELOR OF BUSINESS ADMINISTRATION (HONOURS)"}

# Where a degree's CDCs come from (decision confirmed with the project owner). For 4 degrees
# the Bulletin's "List of Discipline Core Courses" (IV-106+) and its semester chart disagree;
# the list is authoritative, the chart only places courses in a year/semester, and the
# conflict (validation_report.json -> chart_vs_list_core_mismatches) is shown to the student.
CDC_SOURCE = "course_list"

# LLM (decision D3/D10): Gemini, used only at the edges - the agent, and a fallback for
# handouts the regex parser can't read. All provider-specific code lives in recommender/llm.py.
ENV_FILE = ROOT / ".env"                     # holds GEMINI_API_KEY; git-ignored
LLM_MODEL_AGENT = "gemini-3.5-flash"         # stable (non-preview); 2.5 models are closed to new API keys
LLM_MODEL_EXTRACT = "gemini-3.5-flash-lite"  # cheaper; enough for pulling fields from a page
LLM_AGENT_FALLBACKS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]  # used in order if the agent model is overloaded (503)
LLM_CACHE = PROCESSED / "llm_cache.json"     # answers keyed by input hash: re-runs are free and repeatable
