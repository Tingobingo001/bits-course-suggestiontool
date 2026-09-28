"""Timetable intelligence (brief Sec 8, bonus): pick sections for a set of courses.

Hard rules (a plan breaking one is rejected, with the reason):
  - no two class meetings at the same day + hour ........................ clause 3.19
  - a lunch period (4, 5 or 6) free every day ............ timetable p.116 (rule lunch_hours)
  - no two comprehensive exams in the same date + session ..... timetable p.116 (compre_must_not_clash)
  - no two mid-semester tests in the same date + session (same reasoning; flagged, not a printed rule)
  - total units <= 25 ...................................................... clause 1.01
Soft preferences (only choose between valid plans): hours / days to keep free, and 'compact' =
fewest idle periods between a day's first and last class (brief: avoid long gaps).

Search: for each course, one section of each type it has (L, T, P). Depth-first with pruning
on clashes; among valid plans keep the one with the fewest meetings in avoided slots.
"""
from itertools import product

from pydantic import BaseModel, Field

from recommender.codes import normalize_code
from recommender.store import Store, get_store


class Plan(BaseModel):
    ok: bool
    sections: dict[str, list[str]] = Field(default_factory=dict)   # code -> ["L1", "T3", "P2"]
    grid: dict[str, dict[int, str]] = Field(default_factory=dict)   # day -> hour -> "CS F301 L1"
    units: int = 0
    problems: list[str] = Field(default_factory=list)              # why the set can't be scheduled
    notes: list[str] = Field(default_factory=list)                 # soft-preference misses, exam info
    avoided_hits: int = 0
    gap_hours: int = 0                                             # idle periods inside the day, summed


def _exam_key(slot) -> tuple[str, str] | None:
    return (slot.date, slot.session) if slot and slot.date else None


MAX_LEAVES = 50000          # safety cap on complete plans examined


def gaps(busy) -> int:
    """Idle periods between the first and last class of each day."""
    total = 0
    for d in {d for d, _ in busy}:
        hours = sorted(h for dd, h in busy if dd == d)
        total += (hours[-1] - hours[0] + 1) - len(hours)
    return total


def plan(codes: list[str], avoid_hours: set[int] | None = None, avoid_days: set[str] | None = None,
         store: Store | None = None, compact: bool = False) -> Plan:
    store = store or get_store()
    avoid_hours, avoid_days = avoid_hours or set(), avoid_days or set()
    codes = list(dict.fromkeys(normalize_code(c) for c in codes))
    result = Plan(ok=False)

    offers = {}
    for c in codes:
        rows = store.offering(c)
        if not rows:
            result.problems.append(f"{c} is not offered this semester (or is out of scope).")
        else:
            offers[c] = rows[0]
    if result.problems:
        return result

    # exams: compre clash is a registration rule; midsem clash is flagged too
    lunch = set(store.rule("lunch_hours").value)
    for exam, label in (("compre", "comprehensive exams"), ("midsem", "mid-semester tests")):
        seen: dict[tuple, str] = {}
        for c, t in offers.items():
            key = _exam_key(getattr(t, exam))
            if key and key in seen:
                result.problems.append(f"{seen[key]} and {c} have their {label} at the same time "
                                       f"({key[0]} {key[1]})" + (" - not allowed (timetable p.116)." if exam == "compre" else "."))
            elif key:
                seen[key] = c
    units = sum(t.units or 0 for t in offers.values())
    result.units = units
    max_units = store.rule("max_units_per_semester").value
    if units > max_units:
        result.problems.append(f"{units} units is over the {max_units}-unit limit per semester (clause 1.01).")
    if result.problems:
        return result

    # per course: every combination of one section per type it has
    choices = []
    for c, t in offers.items():
        by_type: dict[str, list] = {}
        for s in t.sections:
            if s.meetings:
                by_type.setdefault(s.type, []).append(s)
        combos = list(product(*by_type.values())) if by_type else [()]
        choices.append((c, combos))
    choices.sort(key=lambda x: len(x[1]))                      # most constrained course first

    best: dict | None = None
    leaves = 0

    def lunch_ok(busy: dict) -> bool:
        days = {d for d, _ in busy}
        return all(not lunch <= {h for dd, h in busy if dd == d} for d in days)

    def search(i: int, busy: dict, picked: dict, hits: int):
        nonlocal best, leaves
        if best is not None and (hits * 100 >= best["score"] or leaves >= MAX_LEAVES):
            return                             # can't beat the best plan (gaps only add to the score)
        if i == len(choices):
            leaves += 1
            g = gaps(busy)
            score = hits * 100 + (g if compact else 0)          # avoided slots matter more than gaps
            if best is None or score < best["score"]:
                best = {"busy": dict(busy), "picked": dict(picked), "hits": hits, "gaps": g, "score": score}
            return
        code, combos = choices[i]
        for combo in combos:
            slots = [(m.day, m.hour, f"{code} {s.code}") for s in combo for m in s.meetings]
            if any((d, h) in busy for d, h, _ in slots) or len({(d, h) for d, h, _ in slots}) < len(slots):
                continue
            new = {(d, h): label for d, h, label in slots}
            merged = {**busy, **new}
            if not lunch_ok(merged):
                continue
            extra = sum(1 for d, h, _ in slots if h in avoid_hours or d in avoid_days)
            picked[code] = [s.code for s in combo]
            search(i + 1, merged, picked, hits + extra)
            del picked[code]

    search(0, {}, {}, 0)
    if best is None:
        result.problems.append(_explain_no_plan(choices, lunch))
        return result
    result.ok = True
    result.sections = {c: best["picked"][c] for c in codes}
    result.avoided_hits = best["hits"]
    result.gap_hours = best["gaps"]
    for (d, h), label in sorted(best["busy"].items(), key=lambda x: ("M T W Th F S".split().index(x[0][0]), x[0][1])):
        result.grid.setdefault(d, {})[h] = label
    if best["hits"]:
        result.notes.append(f"{best['hits']} class meeting(s) fall in the hours/days you wanted free - "
                            "no valid plan avoids them all.")
    for c, t in offers.items():
        result.notes.append(f"{c}: midsem {t.midsem.date + ' ' + t.midsem.session if t.midsem else 'not given'}, "
                            f"compre {t.compre.date + ' ' + t.compre.session if t.compre else 'not given'}")
    return result


def _explain_no_plan(choices, lunch) -> str:
    """Find a pair of courses that can never be scheduled together, to say WHY there is no plan."""
    for i in range(len(choices)):
        for j in range(i + 1, len(choices)):
            (a, ca), (b, cb) = choices[i], choices[j]
            if all({(m.day, m.hour) for s in x for m in s.meetings} & {(m.day, m.hour) for s in y for m in s.meetings}
                   for x in ca for y in cb):
                return f"{a} and {b} clash in every combination of their sections."
    return ("No combination of sections avoids every clash while keeping a lunch period "
            f"({', '.join(map(str, sorted(lunch)))}) free each day.")
