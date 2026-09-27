"""Course-code normalisation shared by every parser.

Different documents write the same course as "CS F301", "CSF301", "CS  F301" or
"cs f301". If each parser kept its own spelling, joins between the timetable,
bulletin and handouts would silently fail. Everything goes through normalize_code.
"""
import re

# Department (2-5 letters) + level letter (F/G/C/U/K...) + 3 digits + optional suffix (e.g. T for thesis)
CODE_RE = re.compile(r"\b([A-Z]{2,5})\s*-?\s*([A-Z])\s*(\d{3})([A-Z]?)\b")


def normalize_code(raw: str) -> str | None:
    """Return the canonical 'DEPT F123' form, or None if raw isn't a course code."""
    m = CODE_RE.search(raw.upper())
    if not m:
        return None
    dept, level, num, suffix = m.groups()
    return f"{dept} {level}{num}{suffix}"


SHARED_NUMBER_RE = re.compile(r"\b((?:[A-Z]{2,5}\s*/\s*)+[A-Z]{2,5})\s*-?\s*([A-Z]\s*\d{3}[A-Z]?)\b")


def expand_shared_numbers(text: str) -> str:
    """'ECE/EEE/INSTR F212' -> 'ECE F212 / EEE F212 / INSTR F212' (departments sharing one number)."""
    def expand(m):
        number = m.group(2).replace(" ", "")
        return " / ".join(f"{d.strip()} {number}" for d in m.group(1).split("/"))
    return SHARED_NUMBER_RE.sub(expand, text)


def find_codes(text: str) -> list[str]:
    """All course codes mentioned in a piece of text, normalised, in order."""
    return [f"{d} {l}{n}{s}" for d, l, n, s in CODE_RE.findall(expand_shared_numbers(text.upper()))]
