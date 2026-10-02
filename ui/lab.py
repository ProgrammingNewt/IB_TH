"""The Lab page: what one scientist sees. Pick who you are in the sidebar (or ?as=B)."""
import datetime as dt
import json
import tempfile
import time
from pathlib import Path

import nbformat
import pandas as pd
import streamlit as st
from pypdf import PdfReader

import common as c
from harness.drive.store import Drive
from harness.tools import files as files_tools

c.ensure_default_drives()
runner = c.jobs()
qp = st.query_params

# ---- sidebar: drive, who you are, providers, job status -----------------------------

names = c.drives()
drive_name = st.sidebar.selectbox("Drive", names,
                                  index=names.index(qp["drive"]) if qp.get("drive") in names else 0)
qp["drive"] = drive_name
drive = Drive(c.WORKSPACE / drive_name)
people = drive.people
ids = list(people)
me = st.sidebar.radio("Viewing as", ids, index=ids.index(qp["as"]) if qp.get("as") in ids else 0,
                      format_func=lambda p: f"{people[p]['name']} · {people[p]['role']}")
qp["as"] = me
st.sidebar.caption("Open a tab per scientist: add `?as=A`, `?as=B` or `?as=C` to the URL.")

with st.sidebar.expander("Model providers", expanded=False):
    st.dataframe(pd.DataFrame(c.provider_status()), hide_index=True, width="stretch")
    refuse = st.toggle("Provider 1 refuses biology (demo)", key="refuse",
                       help="Swaps model_provider_1 for a fake that refuses every request, "
                            "so you can watch the gateway fall back to model_provider_2.")
refuse = st.session_state.get("refuse", False)


with st.sidebar.expander("Start fresh"):
    busy = runner.busy()
    pending = st.session_state.get("fresh")
    options = {
        "work": ("Clear the harness's work",
                 "Deletes goals, runs, questions, every inbox, findings, traces and derived files. "
                 "All team files stay, including uploads."),
        "reset": ("Reset to the generated files",
                  "Rebuilds the drive exactly as it was generated. Uploads and removals are undone too."),
        "inbox": ("Clear my inbox", "Deletes only your own messages."),
    }
    for key, (label, help_text) in options.items():
        if st.button(label, key=f"fresh-{key}", disabled=busy, help=help_text, width="stretch"):
            st.session_state["fresh"] = key
            st.rerun()
    if pending:
        label, help_text = options[pending]
        st.warning(f"**{label}** on `{drive_name}`?\n\n{help_text}")
        yes, no = st.columns(2)
        if yes.button("Yes", type="primary", key="fresh-yes", disabled=busy):
            if pending == "work":
                c.clear_harness_work(drive_name)
            elif pending == "reset":
                problem = c.reset_drive(drive_name)
                if problem:
                    st.error(problem)
            else:
                drive.write_json(f"messages/{me}.json", [])
            st.session_state.pop("fresh", None)
            st.rerun()
        if no.button("Cancel", key="fresh-no"):
            st.session_state.pop("fresh", None)
            st.rerun()
    if busy:
        st.caption("Disabled while a job is running.")


@st.fragment(run_every=2)
def job_status():
    job = runner.current
    if job:
        st.info(f"⏳ **{job.label}** ({people.get(job.by, {}).get('name', job.by)}) · "
                f"{time.time() - job.started:.0f}s")
    elif runner.history:
        last = runner.history[0]
        if last.error:
            st.error(f"Last job failed: {last.label}\n\n{last.error}")
        else:
            st.success(f"✓ {last.label} · {last.finished - last.started:.0f}s")
    seen = st.session_state.setdefault("seen_job", runner.last_finished_id())
    if runner.last_finished_id() != seen:          # a job finished: refresh the whole page
        st.session_state.seen_job = runner.last_finished_id()
        st.rerun(scope="app")


with st.sidebar:
    job_status()


def submit(label: str, fn) -> None:
    """Run `fn(harness, person)` in the background, bound to who clicked and which drive."""
    d, who, r = drive_name, me, refuse
    if runner.submit(d, label, who, lambda: fn(c.harness_for(d, r), who)):
        st.toast(f"Started: {label}")
        time.sleep(0.3)
        st.rerun()
    else:
        st.warning("The harness is busy with another job. Try again when it finishes.")


h = c.harness_for(drive_name, refuse)          # reads only; jobs build their own
person = people[me]

st.title(f"{person['name']}")
st.caption(f"{person['role']} · drive `{drive_name}`")

my_open = h.questions(person=me, status="open")
tab_ask, tab_q, tab_drive, tab_findings, tab_trace = st.tabs(
    ["💬 Ask", f"❓ My questions ({len(my_open)})", "📁 Drive", "📊 Findings", "🔍 Trace"])

# ---- Ask ------------------------------------------------------------------------------

STATUS_STATE = {"working": "running", "waiting": "running", "done": "complete", "failed": "error"}


def _age(iso: str) -> str:
    secs = int((dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(iso)).total_seconds())
    return f"{secs}s" if secs < 120 else f"{secs // 60} min"


@st.fragment(run_every=1)
def conversation():
    """Each request and its live reply, like a chat. Refreshes every second on its own."""
    threads = c.harness_for(drive_name).threads(me)[-10:]
    if not threads:
        st.caption("Ask a question about this drive's experiment, e.g. \"Is ABC-12 a hit?\"")
    for t in threads:
        by_me = t["by"] == me
        with st.chat_message("user" if by_me else "human", avatar=None if by_me else "👤"):
            who = "" if by_me else f"**{people.get(t['by'], {}).get('name', t['by'])}:** "
            st.markdown(who + ("📎 " if t["kind"] == "upload" else "") + t["text"])
        with st.chat_message("assistant", avatar="🧪"):
            last = t["steps"][-1] if t["steps"] else {"text": "…", "at": t["started"]}
            live = t["state"] in ("working", "waiting")
            label = last["text"] + (f"  ·  {_age(last['at'])}" if live else "")
            with st.status(label, state=STATUS_STATE.get(t["state"], "running"),
                           expanded=live and t is threads[-1]):
                for step in t["steps"]:
                    st.markdown(f"`{step['at'][11:19]}` {step['text']}")
            if t["state"] == "waiting":
                st.caption("Whoever was asked answers in their ❓ My questions tab. "
                           "This updates as soon as they do.")


with tab_ask:
    conversation()
    prompt = st.chat_input("Ask the harness, e.g. Is ABC-12 a hit?")
    if prompt and prompt.strip():
        if runner.busy():
            st.warning("The harness is busy with another job. Try again when it finishes.")
        else:
            pid = h.receive(me, prompt.strip())          # instant acknowledgement, no model call
            submit(f"Request: {prompt.strip()}",
                   lambda hh, who, t=prompt.strip(), p=pid: hh.message(who, t, progress_id=p))

    inbox = list(reversed(h.inbox(me)))
    with st.expander(f"Inbox ({len(inbox)})", expanded=False):
        if not inbox:
            st.caption("Nothing yet.")
        open_ids = {q["id"]: q for q in my_open}
        for m in inbox:
            with st.container(border=True):
                st.caption(f"{c.KIND_BADGE.get(m['kind'], m['kind'])} · {m['at'][11:19]} UTC"
                           + (f" · `{m['ref']}`" if m.get("ref") else ""))
                st.write(m["text"])
                q = open_ids.get(m.get("ref"))
                if m["kind"] == "question" and q:
                    st.caption("Options: " + " · ".join(ch["label"] for ch in q["choices"]) +
                               ". Answer in the ❓ My questions tab.")
    goals = h.goals()
    if goals:
        with st.expander("Goals on this drive"):
            st.dataframe(pd.DataFrame([{
                "goal": g["id"], "question": g["question"],
                "asked by": people.get(g["requester"], {}).get("name"), "status": g["status"],
                "runs": ", ".join(g["runs"]),
                "verdict": g["history"][-1]["verdict"] if g["history"] else ""} for g in goals]),
                hide_index=True, width="stretch")

# ---- My questions ------------------------------------------------------------------------

def show_question_context(q: dict) -> None:
    kind, _, ref = q["about"].partition(":")
    if kind == "contradiction":
        con = next((x for x in drive.contradictions() if x["id"] == ref), None)
        if con:
            st.dataframe(pd.DataFrame(con["issues"]).astype(str), hide_index=True)
            if con.get("notes"):
                st.caption(f"Run notes for {con['plate_id']}: {con['notes']}")
    elif kind == "approval":
        layout = drive.path(ref)
        if layout.exists():
            st.altair_chart(c.plate_map(pd.read_csv(layout)))


with tab_q:
    if not my_open:
        st.caption("No questions waiting for you.")
    for q in my_open:
        with st.container(border=True):
            st.markdown(f"**{q['message']}**")
            st.caption(f"{q['id']} · about `{q['about']}`"
                       + (f" · copied to {', '.join(people[p]['name'] for p in q['cc'])}" if q["cc"] else ""))
            show_question_context(q)
            choices = [ch for ch in q["choices"] if ch["key"] != "other"]
            cols = st.columns(len(choices) or 1)
            for col, ch in zip(cols, choices):
                if col.button(ch["label"], key=f"{q['id']}-{ch['key']}", width="stretch"):
                    submit(f"{person['name']} answers {q['id']}: {ch['label']}",
                           lambda hh, who, qid=q["id"], k=ch["key"]: hh.answer(qid, who, k))
            if any(ch["key"] == "other" for ch in q["choices"]):
                other = st.text_input("Something else", key=f"{q['id']}-other-text",
                                      placeholder="Type your answer")
                if st.button("Send", key=f"{q['id']}-other") and other.strip():
                    submit(f"{person['name']} answers {q['id']}",
                           lambda hh, who, qid=q["id"], t=other.strip(): hh.answer(qid, who, "other", t))

    copied = [q for q in h.questions(status="open") if me in q["cc"]]
    if copied:
        st.subheader("Copied to you")
        for q in copied:
            st.info(f"{people[q['to']]['name']} was asked ({q['id']}): {q['text']}")
    answered = [q for q in h.questions() if q["status"] == "answered" and (q["to"] == me or me in q["cc"])]
    if answered:
        st.subheader("Answered")
        st.dataframe(pd.DataFrame([{
            "question": q["id"], "about": q["about"], "asked": people[q["to"]]["name"],
            "answered by": people[q["answer"]["by"]]["name"],
            "answer": q["answer"].get("statement") or q["answer"].get("status") or q["answer"]["choice"]}
            for q in answered]), hide_index=True, width="stretch")

# ---- Drive ------------------------------------------------------------------------------

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def preview(rel: str) -> None:
    p = drive.path(rel)
    if not p.exists():
        st.caption("Not created yet.")
        return
    suffix = p.suffix.lower()
    try:
        if suffix == ".xlsx":
            sheets = pd.read_excel(p, sheet_name=None)
            if "readings" in sheets:
                st.altair_chart(c.readings_chart(sheets["readings"]))
            for name, df in sheets.items():
                st.caption(f"Sheet: {name}")
                st.dataframe(df, hide_index=True, width="stretch", height=min(400, 35 * (len(df) + 1)))
        elif suffix == ".pdf":
            st.text("\n".join(page.extract_text() or "" for page in PdfReader(p).pages))
        elif suffix == ".docx":
            st.text(files_tools._docx_text(p))
        elif suffix == ".ipynb":
            for cell in nbformat.read(p, as_version=4).cells:
                if cell.cell_type == "markdown":
                    st.markdown(cell.source)
                else:
                    st.code(cell.source, language="python")
                    out = "".join(o.get("text", "") for o in cell.get("outputs", []))
                    if out.strip():
                        st.text(out)
        elif suffix == ".md":
            st.markdown(p.read_text())
        elif suffix in (".csv", ".tsv"):
            st.dataframe(pd.read_csv(p, sep="\t" if suffix == ".tsv" else ","), hide_index=True, width="stretch")
        elif suffix == ".json":
            st.json(json.loads(p.read_text()), expanded=2)
        elif suffix in IMAGE_SUFFIXES:
            st.image(str(p))
            st.caption("The harness can store images but can't read them.")
        elif suffix in files_tools.TEXT_SUFFIXES:
            st.text(p.read_text(errors="replace")[:5000])
        else:
            st.caption(f"No preview for {suffix or 'this'} files ({p.stat().st_size:,} bytes). "
                       "The harness keeps it but can't read it.")
    except Exception as exc:                                        # noqa: BLE001
        st.warning(f"Couldn't preview {rel}: {exc}")
    lineage = drive.path(rel + ".lineage.json")
    if lineage.exists():
        st.caption("Lineage")
        st.json(json.loads(lineage.read_text()), expanded=True)


def remove_now(rel: str) -> None:
    if runner.busy():
        st.error("The harness is busy. Try again when the current job finishes.")
        return
    try:
        c.harness_for(drive_name, refuse).remove(rel, me)
    except Exception as exc:                                        # noqa: BLE001
        st.error(str(exc))
        return
    st.session_state.pop("confirm_remove", None)
    st.toast(f"Moved {rel} to trash")
    st.rerun()


with tab_drive:
    files = drive.files()
    st.subheader("Team files")
    st.dataframe(pd.DataFrame([{"file": f["path"], "owner": people.get(f["owner"], {}).get("name", f["owner"]),
                                "modified": f["modified"], "description": f["description"]} for f in files]),
                 hide_index=True, width="stretch")

    mine = [f for f in files if f["owner"] == me]
    if mine:
        st.caption("Your files (only a file's owner can remove it)")
        for f in mine:
            col1, col2 = st.columns([5, 1])
            col1.markdown(f"`{f['path']}` · {f['description']}")
            if col2.button("Remove", key=f"rm-{f['path']}", width="stretch"):
                st.session_state["confirm_remove"] = f["path"]
            if st.session_state.get("confirm_remove") == f["path"]:
                with st.container(border=True):
                    st.write(f"Move **{f['path']}** to the drive's trash?")
                    st.caption("Data the harness read from it is dropped, any open question about it "
                               "is withdrawn, and findings that used it are marked out of date. It can "
                               "be restored from trash/.")
                    yes, no = st.columns(2)
                    if yes.button("Yes, move it to trash", key=f"rm-yes-{f['path']}", type="primary"):
                        remove_now(f["path"])
                    if no.button("Cancel", key=f"rm-no-{f['path']}"):
                        st.session_state.pop("confirm_remove", None)
                        st.rerun()

    derived = sorted(str(p.relative_to(drive.root)) for folder in ("facts", "derived")
                     if drive.path(folder).exists() for p in drive.path(folder).glob("*")
                     if not p.name.endswith(".lineage.json"))
    choice = st.selectbox("Open a file", [f["path"] for f in files] + derived,
                          format_func=lambda r: r + ("  (made by the harness)" if r in derived else ""))
    if choice:
        preview(choice)

    st.subheader("Upload a file")
    st.caption("Any file type. Tables (.xlsx, .csv, .tsv), documents (.pdf, .docx, .txt, .md…), "
               "notebooks and code are read by the harness. Other files, such as images, are kept "
               "but not read.")
    up = st.file_uploader("File", type=None)
    desc = st.text_input("Description", placeholder="What is this file? The Planner reads this")
    rerun = st.checkbox("Re-run the analysis with this file", value=True,
                        help="Starts a new run for every goal that already has one.")
    if up:
        existing = drive.owner_of(f"raw/{up.name}")
        replace = False
        if existing and existing != me:
            st.error(f"raw/{up.name} belongs to {people[existing]['name']}. Rename your file to upload it.")
        elif existing == me:
            replace = st.checkbox(f"Replace my existing raw/{up.name} (the old one goes to trash)")
        if st.button("Upload", type="primary", disabled=bool(existing) and not replace):
            tmp = Path(tempfile.mkdtemp()) / up.name
            tmp.write_bytes(up.getvalue())
            submit(f"{person['name']} uploads {up.name}",
                   lambda hh, who, tmp=tmp, d=(desc or up.name), r=replace, ev=rerun:
                   hh.upload(tmp, who, d, replace=r, reevaluate=ev))
    for f in c.incoming_files(drive_name):
        if not drive.owner_of(f"raw/{f.name}"):
            if st.button(f"Upload the demo file {f.name} as {person['name']}", key=f"demo-{f.name}"):
                submit(f"{person['name']} uploads {f.name}",
                       lambda hh, who, f=f: hh.upload(f, who, "Re-run at 72 h with vehicle on the same plate"))

# ---- Findings ----------------------------------------------------------------------------

with tab_findings:
    findings = drive.path("findings/findings.md")
    if not findings.exists():
        st.caption("No findings yet.")
    else:
        stale = [x for g in h.goals() for x in g.get("stale", [])]
        if stale:
            st.warning("**These findings may be out of date.** Since they were written:\n\n" +
                       "\n".join(f"- {x['change']}" for x in stale))
            if st.button("Re-evaluate with the files as they are now", type="primary"):
                changes = "; ".join(x["change"] for x in stale)
                submit("Re-evaluate", lambda hh, who, ch=changes: hh.reevaluate(f"Files changed: {ch}."))
        st.markdown(findings.read_text())
        plans = sorted(p for p in drive.path("findings/plans").glob("*.json")
                       if not p.name.endswith(".lineage.json")) if drive.path("findings/plans").exists() else []
        for meta_path in plans:
            meta = json.loads(meta_path.read_text())
            with st.expander(f"Plate plan {meta['plan']} · {meta['status']}"):
                st.altair_chart(c.plate_map(pd.read_csv(drive.path(meta["plan"]))))
                st.json(meta, expanded=False)
        notes = sorted(drive.path("findings/notes").glob("*.md")) if drive.path("findings/notes").exists() else []
        for n in notes:
            with st.expander(f"Notes on {n.stem}"):
                st.markdown(n.read_text())
        history = sorted(drive.path("findings/history").glob("*.md"))
        if len(history) > 1:
            with st.expander("Earlier versions"):
                for hfile in history[:-1]:
                    st.caption(hfile.stem)
                    st.markdown(hfile.read_text())

# ---- Trace -----------------------------------------------------------------------------------

with tab_trace:
    run_ids = sorted(p.stem for p in drive.path("runs").glob("*.json")) if drive.path("runs").exists() else []
    if not run_ids:
        st.caption("No runs yet.")
    else:
        runs = [h._load("runs", r) for r in run_ids]
        st.dataframe(pd.DataFrame([{
            "run": r["id"], "goal": r["goal_id"], "status": r["status"], "tool calls": r["tool_calls"],
            "planner turns": r["turns"], "tokens": r.get("tokens_used", 0), "trigger": r["trigger"][:90]}
            for r in runs]), hide_index=True, width="stretch")
        pick = st.selectbox("Run", run_ids, index=len(run_ids) - 1)
        tpath = drive.path(f"trace/{pick}.jsonl")
        events = [json.loads(line) for line in tpath.read_text().splitlines()] if tpath.exists() else []
        rows = []
        for e in events:
            u = e.get("usage") or {}
            rows.append({
                "time": e.get("at", "")[11:19],
                "what": e.get("agent") or e.get("event"),
                "tool": e.get("tool", ""),
                "provider": f"{e.get('label', '')} {e.get('model', '')}".strip(),
                "outcome": e.get("outcome") or ("ok" if e.get("ok") else "blocked" if e.get("blocked")
                                                else "error" if e.get("error") else ""),
                "tokens": (u.get("prompt_tokens", 0) + u.get("completion_tokens", 0)) or None,
                "detail": e.get("blocked") or e.get("error") or e.get("reason") or
                          ("; ".join(e["problems"]) if e.get("problems") else "") or e.get("note", ""),
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        with st.expander("Planner conversation"):
            for m in h._load("runs", pick)["messages"][1:]:
                if m["role"] == "assistant" and m.get("tool_calls"):
                    for t in m["tool_calls"]:
                        st.markdown(f"**Planner → `{t['function']['name']}`** `{t['function']['arguments'][:300]}`")
                elif m["role"] == "tool":
                    st.code(m["content"][:1500], language="json")
                elif m["content"]:
                    st.markdown(f"**{m['role']}:** {m['content'][:1500]}")
