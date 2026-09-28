"""BITS course recommender - dashboard (brief Sec 10).

Run:  streamlit run app/streamlit_app.py

Everything shown comes from data/processed (via recommender.store) and the deterministic engine;
the Advisor tab adds the Gemini agent on top. Nothing is hard-coded per student or course.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # run from anywhere

import pandas as pd
import streamlit as st

from recommender import llm
from recommender.engine.eligibility import check_all, priority
from recommender.engine.requirements import analyse
from recommender.engine.scheduler import plan
from recommender.schema import CompletedCourse, StudentProfile
from recommender.store import get_store

st.set_page_config(page_title="BITS Course Recommender", page_icon="🎓", layout="wide")
store = get_store()

DAYS = ["M", "T", "W", "Th", "F", "S"]
DAY_NAMES = dict(zip(DAYS, ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]))


def period_label(p: int) -> str:
    h = 7 + p
    return f"{p} ({h if h <= 12 else h - 12} {'AM' if h < 12 else 'PM'})"


# ---------------------------------------------------------------- profile (sidebar)
with st.sidebar:
    st.header("Student profile")
    name = st.text_input("Name", "")
    campus = st.selectbox("Campus", ["Pilani", "Goa", "Hyderabad", "Dubai"],
                          help="Only the Pilani timetable was supplied.")
    batch = st.selectbox("Admission year (batch)", list(range(2025, 2018, -1)), index=1)
    auto_year = 2026 - batch + 1
    study_year = st.number_input("Current year of study (First Semester 2026-27)", 1, 6, min(auto_year, 6),
                                 help="Defaults from the batch; change it if you are off the normal pattern.")
    progs = store.programme_names()
    first = st.selectbox("Degree", progs, index=progs.index("COMPUTER SCIENCE"))
    dual = st.checkbox("Dual degree")
    second = st.selectbox("Second degree (B.E.)", [p for p in progs if p != first]) if dual else None
    minor = st.selectbox("Minor (optional)", ["—"] + [m.name for m in store.minors.minors])
    def load_sample():                        # callback: runs before the widgets are drawn
        st.session_state.completed = "\n".join(
            f"{c} B" for c in ["BITS F103", "BIO F101", "CHEM F101", "MATH F101", "PHY F101", "BITS F101", "BITS K101",
                               "BITS F111", "BITS F112", "CS F111", "MATH F113", "MATH F102", "EEE F111", "BITS F102",
                               "MATH F211", "CS F214", "CS F222", "CS F213", "CS F215", "ECON F211", "CS F211",
                               "CS F241", "CS F212", "BITS F225", "HSS F222"])

    st.button("Load sample 3rd-year CS student", on_click=load_sample)
    st.caption("Completed courses: one per line, optional grade, e.g. `CS F211 A-`. "
               "Order = chronological (the latest attempt counts). W is ignored; NC/I/RC are not cleared.")
    completed_text = st.text_area("Completed courses", height=200, key="completed",
                                  placeholder="BIO F101 B\nCHEM F101 A\nCS F111 A-\n...")
    current_text = st.text_input("Current courses (already registered this semester)", "",
                                 placeholder="e.g. CS F342, BITS F464")
    cgpa = st.number_input("CGPA (optional)", 0.0, 10.0, 0.0, 0.01)
    interests = st.text_input("Interests (for the advisor)", "")


def parse_completed(text: str) -> tuple[list[CompletedCourse], list[str]]:
    from recommender.codes import find_codes
    done, bad = [], []
    for line in text.splitlines():
        if not line.strip():
            continue
        codes = find_codes(line)
        if not codes:
            bad.append(line.strip())
            continue
        rest = line.upper().split(codes[0].split()[1], 1)[-1].strip()
        done.append(CompletedCourse(code=codes[0], grade=rest.split()[0] if rest else None))
    return done, bad


completed, unparsed = parse_completed(completed_text)
from recommender.codes import find_codes  # noqa: E402

profile = StudentProfile(name=name, campus=campus, batch=batch, programmes=[first] + ([second] if second else []),
                         minor=None if minor == "—" else minor, completed=completed,
                         current=find_codes(current_text),
                         study_year=study_year if study_year != auto_year else None,
                         cgpa=cgpa or None, interests=interests)


@st.cache_data(show_spinner="Checking requirements and eligibility…")
def compute(profile_json: str):
    p = StudentProfile.model_validate_json(profile_json)
    report = analyse(p, store)
    return report, check_all(p, report, store)


report, options = compute(profile.model_dump_json())

# ---------------------------------------------------------------- header
st.title("🎓 BITS Pilani course recommender — First Semester 2026-27")
st.caption(f"{report.caveat} Pilani campus timetable. Year of study: {report.study_year}.")
for w in report.warnings:
    st.warning(w)
if unparsed:
    st.warning(f"Could not read these lines as course codes: {', '.join(unparsed)}")

tab_req, tab_courses, tab_tt, tab_chat = st.tabs(["📋 Requirements", "📚 Courses", "🗓️ Timetable", "💬 Advisor"])

# ---------------------------------------------------------------- requirements
with tab_req:
    for d in report.degrees:
        st.subheader(d.programme.title())
        st.caption(f"Semester chart: {d.chart or 'not found'}")
        for n in d.notes:
            (st.error if "conflict" in n.lower() else st.info)(n)
        done = [s for s in d.slots if s.done_via]
        remaining = d.remaining_slots()
        c1, c2, c3 = st.columns(3)
        c1.metric("Named/CDC slots done", f"{len(done)} / {len(done) + len(remaining)}")
        for col, cat in zip((c2, c3), d.categories[:2]):
            col.metric(f"{cat.category} units", f"{cat.done_units} / {cat.required_units}",
                       help=f"{cat.done_courses} of {cat.required_courses} courses")
        for cat in d.categories:
            st.progress(min(1.0, cat.done_units / max(cat.required_units, 1)),
                        text=f"{cat.category}: {cat.done_units}/{cat.required_units} units, "
                             f"{cat.done_courses}/{cat.required_courses} courses — {', '.join(cat.courses) or 'none yet'}")
        rows = [{"When": f"Y{s.year} S{s.semester}" if s.year else "—", "Kind": s.kind.upper(),
                 "Course(s)": " / ".join(s.options), "Title": s.title, "Units": s.units,
                 "Done": ("✅ " + s.done_via + (" (equivalent)" if s.done_via not in s.options else "")) if s.done_via else "",
                 "Note": s.note or ""} for s in d.slots]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

# ---------------------------------------------------------------- courses
with tab_courses:
    f1, f2, f3, f4 = st.columns([2, 2, 2, 3])
    status = f1.multiselect("Status", ["eligible", "warning", "ineligible", "done", "registered"],
                            ["eligible", "warning"])
    cats = f2.multiselect("Counts as", ["backlog", "CDC", "GIR", "DEL", "HUEL", "OPEL", "extra"])
    open_book = f3.selectbox("Open-book component", ["any", "yes", "no"])
    query = f4.text_input("Search title/description", "")
    g1, g2, g3, g4 = st.columns(4)
    no_midsem = g1.checkbox("No midsem (verified)")
    project = g2.checkbox("Has project/report component")
    att_stated = g3.checkbox("Attendance policy stated")
    only_minor = g4.checkbox("Only courses for my minor") if profile.minor else False

    def keep(o):
        if o.status not in status:
            return False
        if cats and not any(any(c in v for c in cats) for v in o.categories.values()):
            return False
        ob = (o.handout or {}).get("has_open_book")
        if open_book != "any" and ob is not (open_book == "yes"):
            return False
        if query and query.lower() not in (o.title + " " + o.description + " " + o.code).lower():
            return False
        hd = o.handout or {}
        if no_midsem and hd.get("has_midsem") is not False:
            return False
        if project and not (hd.get("project_weight") or 0) > 0:
            return False
        if att_stated and hd.get("attendance_policy") is None:
            return False
        return not only_minor or bool(o.minor)

    shown = sorted(filter(keep, options), key=lambda o: (priority(o), o.code))
    st.caption(f"{len(shown)} of {len(options)} offered courses")
    table = [{"Code": o.code, "Title": o.title, "Units": o.units, "Status": o.status,
              "Counts as": "; ".join(o.categories.values()), "Minor": o.minor or "",
              "Midsem %": (o.handout or {}).get("midsem_weight"), "Compre %": (o.handout or {}).get("compre_weight"),
              "Attendance": (o.handout or {}).get("attendance", "no handout"),
              "Compre": f"{o.compre.date} {o.compre.session}" if o.compre else ""} for o in shown]
    st.dataframe(pd.DataFrame(table), hide_index=True, width="stretch", height=380)

    pick = st.selectbox("Details for", [o.code for o in shown] or ["—"])
    o = next((x for x in options if x.code == pick), None)
    if o:
        st.markdown(f"#### {o.code} — {o.title} ({o.units} units)")
        for r in o.reasons:
            st.error(r)
        for w in o.warnings:
            st.warning(w)
        for u in o.unverified:
            st.info("ℹ️ " + u)
        st.write(f"**Counts as:** {'; '.join(f'{k.title()}: {v}' for k, v in o.categories.items()) or '—'}")
        st.write(f"**Prerequisites:** {o.prerequisites or '—'}")
        if o.handout and o.handout.get("instructor_in_charge"):
            st.write(f"**Instructor-in-charge:** {o.handout['instructor_in_charge']}")
        if o.description:
            st.write(o.description)
        h = o.handout
        if h and h["evaluation"]:
            src = f"handout {h['file']}, p.{h['page']}" + (" — extracted by LLM, medium confidence" if h["extracted_by"] == "llm" else "")
            st.write(f"**Evaluation** ({src})")
            st.dataframe(pd.DataFrame(h["evaluation"]), hide_index=True)
            for n in h["evaluation_notes"]:
                st.caption(n)
        if h and h["makeup_policy"]:
            with st.expander("Make-up policy (handout)"):
                st.write(h["makeup_policy"])
        if h and h["attendance_policy"]:
            with st.expander("Attendance policy (handout)"):
                st.write(h["attendance_policy"])
        if o.sections:
            st.dataframe(pd.DataFrame([s.model_dump() for s in o.sections]), hide_index=True)

# ---------------------------------------------------------------- timetable
with tab_tt:
    eligible = [o for o in sorted(options, key=lambda o: (priority(o), o.code)) if o.status in ("eligible", "warning")]
    default = [o.code for o in eligible if priority(o) <= 3][:6]
    chosen = st.multiselect("Courses to plan", [o.code for o in options], default=default)
    c1, c2, c3 = st.columns([3, 3, 2])
    avoid_days = c1.multiselect("Keep these days free (if possible)", DAYS, format_func=DAY_NAMES.get)
    avoid_hours = c2.multiselect("Avoid these periods (if possible)", list(range(1, 11)), format_func=period_label)
    compact = c3.checkbox("Compact (fewest gaps)")
    chosen = list(dict.fromkeys(chosen + profile.current))      # registered courses are always in the plan
    if chosen:
        p = plan(chosen, set(avoid_hours), set(avoid_days), store, compact=compact)
        if p.ok:
            st.success(f"No clashes — {p.units} units, {p.gap_hours} idle period(s) between classes. Sections: "
                       + "; ".join(f"{c} {'/'.join(s)}" for c, s in p.sections.items()))
            grid = pd.DataFrame({DAY_NAMES[d]: {period_label(h): p.grid.get(d, {}).get(h, "") for h in range(1, 11)}
                                 for d in DAYS})
            st.dataframe(grid, width="stretch")
            for n in p.notes:
                st.caption(n)
        else:
            for pr in p.problems:
                st.error(pr)

# ---------------------------------------------------------------- advisor chat
with tab_chat:
    if not llm.available():
        st.info("The advisor needs GEMINI_API_KEY in a .env file (see README). Everything else works without it.")
    else:
        from recommender.agent.agent import Advisor
        key = profile.model_dump_json()
        if st.session_state.get("advisor_key") != key:
            st.session_state.advisor = Advisor(profile)
            st.session_state.advisor_key = key
            st.session_state.messages = []
        for m in st.session_state.messages:
            with st.chat_message(m["role"]):
                st.markdown(m["content"])
                if m.get("tools"):
                    st.caption("Tools used: " + ", ".join(m["tools"]))
        prompt = st.chat_input("Ask e.g. “I like machine learning and want Fridays free — what should I take?”")
        if prompt:
            if profile.interests and not st.session_state.messages:
                prompt = f"(My interests: {profile.interests}) {prompt}"
            st.session_state.messages.append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.markdown(prompt)
            with st.chat_message("assistant"):
                with st.spinner("Checking the data…"):
                    try:
                        answer, tools = st.session_state.advisor.ask(prompt)
                    except Exception as e:
                        answer, tools = f"Sorry, the language model is unavailable right now ({e}).", []
                st.markdown(answer)
                if tools:
                    st.caption("Tools used: " + ", ".join(tools))
            st.session_state.messages.append({"role": "assistant", "content": answer, "tools": tools})
