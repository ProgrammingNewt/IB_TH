"""Toy-data playground: generate a drive from a scenario and a seed, and look at it."""
import json

import pandas as pd
import streamlit as st

import common as c
from datagen.scenarios import DEFAULT_SEED, SCENARIOS

st.title("Toy-data playground")
st.caption("Generate a drive, check that its planted story holds, and look at the data. "
           "The harness never sees the answer key.")

with st.form("generate"):
    col1, col2, col3 = st.columns([2, 1, 2])
    scenario = col1.selectbox("Scenario", list(SCENARIOS), help=
                              "planted_batch_issue: batch 2 ran 48 h with no vehicle, which fakes a hit. "
                              "clean: everything follows the protocol and the effect is real.")
    seed = col2.number_input("Seed", value=DEFAULT_SEED, step=1)
    name = col3.text_input("Drive name", placeholder=f"{scenario}-seed{int(seed)}")
    go = st.form_submit_button("Generate", type="primary")

if go:
    with st.spinner("Generating (this runs the scientist's notebook)…"):
        folder, checks, error = c.generate(scenario, int(seed), name.strip() or None)
    if error:
        st.error(f"Seed {int(seed)} doesn't support the {scenario} story: {error}. Try another seed.")
    else:
        st.success(f"Wrote drive `{folder.name}`. Open it on the Lab page.")
        for check in checks:
            st.markdown(f"- ✅ {check}")
        st.session_state["playground_drive"] = folder.name

names = c.drives()
if not names:
    st.stop()
default = st.session_state.get("playground_drive")
pick = st.selectbox("Look at a drive", names, index=names.index(default) if default in names else 0)
root = c.WORKSPACE / pick

readings_path = root / "raw" / "readings.xlsx"
sheets = pd.read_excel(readings_path, sheet_name=None)
readings, meta = sheets["readings"], sheets["run_metadata"]
for f in c.incoming_files(pick):
    if f.suffix == ".xlsx":
        extra = pd.read_excel(f, sheet_name=None)
        readings = pd.concat([readings, extra["readings"]], ignore_index=True)
        meta = pd.concat([meta, extra["run_metadata"].assign(notes=lambda d: d.notes.fillna("") +
                                                             " (arrives later, in run 2)")])

st.subheader("Readings per plate")
st.altair_chart(c.readings_chart(readings))
st.caption("Each dot is a well; the black tick is the mean.")

summary = (readings.groupby(["plate_id", "batch", "condition"]).signal
           .agg(wells="count", mean="mean", sd="std").round(1).reset_index())
col1, col2 = st.columns(2)
col1.subheader("Summary")
col1.dataframe(summary, hide_index=True, width="stretch")
col2.subheader("Run metadata")
col2.dataframe(meta, hide_index=True, width="stretch")

exp = c.expected(pick)
if exp:
    with st.expander("Answer key (hidden from the harness)"):
        st.json(exp)

with st.expander("All readings"):
    st.dataframe(readings, hide_index=True, width="stretch")
