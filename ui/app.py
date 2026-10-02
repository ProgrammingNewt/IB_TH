"""Streamlit entry point: `streamlit run ui/app.py`.

Open one browser tab per scientist with ?as=A, ?as=B, ?as=C to see the collaboration.
"""
import streamlit as st

st.set_page_config(page_title="Lab Drive Harness", page_icon="🧪", layout="wide")

pages = st.navigation([
    st.Page("lab.py", title="Lab", icon="🧪", default=True),
    st.Page("playground.py", title="Toy-data playground", icon="🎲"),
])
pages.run()
