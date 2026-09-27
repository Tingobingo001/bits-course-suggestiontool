"""Stage 2b: LLM fallback for handouts the regex pass could not read (decision D10).

Only handouts that are (a) flagged by handouts.py and (b) for a course offered in scope
this semester are sent. The model gets the handout's evaluation section (or the whole text,
or - for scanned handouts - the PDF itself) and must answer in a fixed JSON schema.

Its answer goes through the SAME checks as the regex output (weights total 100, readable
names). Passing answers replace the evaluation and are labelled extracted_by="llm",
confidence="medium"; failing ones keep the flag, so the agent says "could not be verified".
Answers are cached (llm.py), so re-running the build is free and repeatable.
Without an API key the stage is skipped and the flags stay.

Usage: python -m recommender.ingest.handouts_llm   (after handouts)
"""
import json

from pydantic import BaseModel, Field

from recommender import llm
from recommender.config import (LLM_MODEL_AGENT, LLM_MODEL_EXTRACT, NEW_ADMISSIONS_MIN_COMP_CODE, PROCESSED,
                                RAW_HANDOUTS, TEXT_HANDOUTS)
from recommender.ingest.handouts import (OUT, component_kind, evaluation_flagged, evaluation_problem, open_book,
                                         set_evaluation)
from recommender.schema import ComponentKind, EvaluationComponent, Handout

MAX_CHARS = 15000     # a whole handout is ~10k characters; this bounds the odd long one

SYSTEM = (
    "You extract the evaluation scheme from a university course handout. Use ONLY the given "
    "document; never invent a component, weight or date. Weights are percentages of the course "
    "total: if the handout gives marks, convert them to percent of the total marks. List every row "
    "as printed; if only the best N of several rows count (e.g. 'best two of three quizzes'), mark "
    "those rows with the same best_of_group label and best_n = N. But if ONE printed row already "
    "gives the weight for the whole group (e.g. 'Quizzes (best 5 out of 6) 25%'), return that single "
    "row with its printed weight and no best_of_group. Sub-parts printed inside a row (e.g. "
    "'Laboratory 20%' containing 'Lab record 10%' and 'Lab quiz 10%') are NOT extra components: "
    "return only the parent row. Copy duration, date and "
    "nature (open/closed book) as printed, or null if not stated. If the handout has no "
    "evaluation scheme, set evaluation_found to false and return no components."
)


class LLMComponent(BaseModel):
    name: str
    kind: ComponentKind
    weight: float = Field(description="percent of the course total for THIS row, as printed (or converted from marks)")
    duration: str | None = None
    date: str | None = None
    nature: str | None = Field(None, description="open/closed book etc., as printed")
    best_of_group: str | None = Field(None, description="if only the best N of several rows count "
                                      "(e.g. 'best two of three quizzes'), the same short label on each of those rows")
    best_n: int | None = Field(None, description="N, how many rows of that group count")


class LLMEvaluation(BaseModel):
    evaluation_found: bool
    components: list[LLMComponent]
    notes: list[str]


def in_scope_codes() -> set[str]:
    timetable = json.loads((PROCESSED / "timetable.json").read_text(encoding="utf-8"))
    return {t["course_code"] for t in timetable
            if not t["cancelled"] and t["comp_code"] < NEW_ADMISSIONS_MIN_COMP_CODE}


def prompt_for(h: Handout) -> tuple[str, bytes | None]:
    """Evaluation section text if we found one, else the whole handout; PDF bytes if scanned."""
    if h.extracted_by == "none":
        return "Extract the evaluation scheme from this scanned handout.", (RAW_HANDOUTS / h.file).read_bytes()
    section = next((s.text for s in h.sections if s.topic == "evaluation" and len(s.text) > 80), None)
    text = section or (TEXT_HANDOUTS / h.file.replace(".pdf", ".txt")).read_text(encoding="utf-8")
    notes = "\nTable footnotes: " + " ".join(h.evaluation_notes) if h.evaluation_notes else ""
    return f"Course {', '.join(h.course_codes)} - {h.title}\n\n{text[:MAX_CHARS]}{notes}", None


def merge_best_of(rows: list[LLMComponent]) -> list[EvaluationComponent]:
    """The LLM only READS 'best N of M'; the arithmetic is done here: a group of M rows where
    the best N count becomes one component worth the N largest weights."""
    out: list[EvaluationComponent] = []
    groups: dict[str, list[LLMComponent]] = {}
    for r in rows:
        if r.best_of_group and r.best_n and r.best_n > 0:
            groups.setdefault(r.best_of_group, []).append(r)
            if len(groups[r.best_of_group]) > 1:
                continue                                  # merged below, keep the first row's position
        kind = r.kind if r.kind != "other" else component_kind(r.name)   # same keyword rules as the regex pass
        out.append(EvaluationComponent(name=r.name, kind=kind, weight=r.weight, duration=r.duration,
                                       date=r.date, nature=r.nature, open_book=open_book(r.nature or "")))
    for label, members in groups.items():
        if len(members) == 1:
            continue                                      # already one aggregated row (e.g. "best 3 of 4" in a footnote)
        n = min(members[0].best_n, len(members))
        first = next(c for c in out if c.name == members[0].name)
        first.name = f"{label} (best {n} of {len(members)})"
        first.weight = sum(sorted((m.weight for m in members), reverse=True)[:n])
        first.date = "; ".join(m.date for m in members if m.date) or None
    return out


def fallback(h: Handout) -> str:
    """Try the cheap model, escalate to the stronger one if its answer fails validation.
    Returns what happened (for the summary)."""
    h.needs_verification = [f for f in h.needs_verification if not f.startswith("LLM fallback")]  # re-runs
    prompt, pdf = prompt_for(h)
    for model in (LLM_MODEL_EXTRACT, LLM_MODEL_AGENT):
        try:
            ans = llm.extract(prompt, LLMEvaluation, system=SYSTEM, pdf=pdf, model=model)
        except Exception as e:                 # API/network error after retries: keep the flag
            h.needs_verification.append(f"LLM fallback failed: {type(e).__name__}")
            return "error"
        if not ans.evaluation_found:
            h.needs_verification.append("LLM fallback: no evaluation scheme in the handout")
            return "not_found"
        comps = merge_best_of(ans.components)
        problem = evaluation_problem(comps)
        if not problem:
            break
    else:
        h.needs_verification.append(f"LLM fallback rejected: {problem}")
        return "rejected"
    set_evaluation(h, comps, h.evaluation_page)
    h.evaluation_notes = ans.notes + [f"evaluation extracted by {model}"]
    h.extracted_by = "llm"
    h.source.confidence = "medium"
    h.needs_verification = [f for f in h.needs_verification
                            if not f.startswith(("evaluation", "no evaluation", "scanned"))]
    return "fixed"


def run():
    handouts = [Handout.model_validate(h) for h in json.loads(OUT.read_text(encoding="utf-8"))]
    scope = in_scope_codes()
    todo = [h for h in handouts if evaluation_flagged(h.needs_verification) and h.extracted_by != "llm"
            and set(h.course_codes) & scope]
    print(f"evaluation flagged: {sum(1 for h in handouts if evaluation_flagged(h.needs_verification))}, "
          f"in scope: {len(todo)}")
    if not llm.available():
        print("  skipped: no GEMINI_API_KEY - flagged handouts stay 'could not be verified'")
        return
    results = {}
    for n, h in enumerate(todo, 1):
        results[h.file] = fallback(h)
        print(f"  [{n}/{len(todo)}] {h.file}: {results[h.file]}")
    OUT.write_text(json.dumps([h.model_dump() for h in handouts], indent=1, ensure_ascii=False), encoding="utf-8")
    counts = {k: list(results.values()).count(k) for k in ("fixed", "rejected", "not_found", "error")}
    print(f"LLM fallback: {counts}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    run()
