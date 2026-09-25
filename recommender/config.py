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
