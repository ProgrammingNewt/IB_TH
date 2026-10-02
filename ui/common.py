"""Shared pieces for the Streamlit pages: the workspace, background jobs, the gateway,
and the plots."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import altair as alt
import pandas as pd
import streamlit as st

from datagen.scenarios import SCENARIOS, ScenarioCheckFailed
from datagen.writers import write_scenario
from harness.drive.store import Drive
from harness.gateway.fakes import RefusingClient
from harness.gateway.gateway import Gateway, _openai_client, load_config
from harness.harness import Harness

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = Path(os.environ.get("HARNESS_WORKSPACE", ROOT / "workspace"))

CONDITION_COLORS = alt.Scale(domain=["vehicle", "ABC-12"], range=["#7a8791", "#5848c9"])


# ---- workspace --------------------------------------------------------------------

def drives() -> list[str]:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    return sorted(p.name for p in WORKSPACE.iterdir()
                  if p.is_dir() and (p / "manifest.json").exists())


def generate(scenario: str, seed: int, name: str | None = None) -> tuple[Path | None, list[str], str]:
    """Build a drive. Returns (folder, checks passed, error)."""
    try:
        s = SCENARIOS[scenario](seed)
    except ScenarioCheckFailed as exc:
        return None, [], str(exc)
    name = name or f"{scenario}-seed{seed}"
    for p in (WORKSPACE / name, WORKSPACE / f"{name}.incoming"):
        if p.exists():
            shutil.rmtree(p)
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    folder = write_scenario(s, WORKSPACE, drive_name=name)
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["generated"] = {"scenario": scenario, "seed": seed}      # so it can be reset later
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return folder, s.checks, ""


# Folders the harness writes. Team files live in raw/ and sops/, and trash/ keeps removed ones.
HARNESS_FOLDERS = ("goals", "runs", "questions", "messages", "findings", "facts", "derived", "trace")


def clear_harness_work(drive_name: str) -> list[str]:
    """Delete everything the harness produced on a drive. Team files stay, uploads included."""
    root = WORKSPACE / drive_name
    cleared = [f for f in HARNESS_FOLDERS if (root / f).exists()]
    for f in cleared:
        shutil.rmtree(root / f)
    return cleared


def generated_from(drive_name: str) -> tuple[str, int] | None:
    """Which scenario and seed built a drive, if it was generated."""
    manifest = json.loads((WORKSPACE / drive_name / "manifest.json").read_text())
    if "generated" in manifest:
        return manifest["generated"]["scenario"], int(manifest["generated"]["seed"])
    # Drives made before the manifest recorded this: the default ones are named after their scenario.
    if drive_name in SCENARIOS:
        return drive_name, 20261002
    return None


def reset_drive(drive_name: str) -> str:
    """Rebuild a generated drive exactly as it was first made. Uploads are removed too."""
    origin = generated_from(drive_name)
    if origin is None:
        return f"{drive_name} wasn't generated, so there's nothing to reset it to"
    _, _, error = generate(origin[0], origin[1], name=drive_name)
    return error


def last_drive() -> str | None:
    """The drive someone used last, so every new tab opens on the same one."""
    p = WORKSPACE / ".last_drive"
    return p.read_text().strip() if p.exists() else None


def remember_drive(name: str) -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    (WORKSPACE / ".last_drive").write_text(name)


def ensure_default_drives() -> None:
    if not drives():
        for scenario in SCENARIOS:
            generate(scenario, 20261002, name=scenario)


def expected(drive_name: str) -> dict | None:
    p = WORKSPACE / f"{drive_name}.expected.json"
    return json.loads(p.read_text()) if p.exists() else None


def incoming_files(drive_name: str) -> list[Path]:
    d = WORKSPACE / f"{drive_name}.incoming"
    return sorted(d.glob("*")) if d.exists() else []


# ---- gateway ------------------------------------------------------------------------

def provider_status() -> list[dict]:
    rows = []
    for p in load_config()["providers"]:
        if p.get("enabled_env"):
            on = os.environ.get(p["enabled_env"]) == "1"
            state = "on" if on else f"off ({p['enabled_env']}=0)"
        else:
            state = "key set" if os.environ.get(p["key_env"]) else "no key"
        rows.append({"provider": p["name"], "label": p["label"], "status": state})
    return rows


def make_gateway(refuse_provider_1: bool) -> Gateway:
    """The real gateway, optionally with provider 1 swapped for one that refuses biology."""
    def factory(p):
        if refuse_provider_1 and p.name == "model_provider_1":
            return RefusingClient()
        return _openai_client(p)
    return Gateway(load_config(), client_factory=factory)


def harness_for(drive_name: str, refuse_provider_1: bool = False) -> Harness:
    return Harness(Drive(WORKSPACE / drive_name), make_gateway(refuse_provider_1))


# ---- background jobs ------------------------------------------------------------------

@dataclass
class Job:
    id: int
    drive: str
    label: str
    by: str
    started: float = field(default_factory=time.time)
    finished: float | None = None
    result: object = None
    error: str | None = None


class JobRunner:
    """One harness job at a time, in a background thread, shared by every browser tab.

    A Planner run can take a minute or two. Running it here keeps the page responsive and
    lets the other scientists' tabs see it happening."""

    def __init__(self):
        self.lock = threading.Lock()
        self.current: Job | None = None
        self.history: list[Job] = []
        self._next = 1

    def busy(self) -> bool:
        return self.current is not None

    def submit(self, drive: str, label: str, by: str, fn: Callable[[], object]) -> bool:
        with self.lock:
            if self.current is not None:
                return False
            job = Job(self._next, drive, label, by)
            self._next += 1
            self.current = job

        def work():
            try:
                job.result = fn()
            except Exception as exc:                                  # noqa: BLE001
                job.error = f"{type(exc).__name__}: {exc}"
                job.result = traceback.format_exc()
            finally:
                job.finished = time.time()
                with self.lock:
                    self.history.insert(0, job)
                    self.current = None

        threading.Thread(target=work, daemon=True).start()
        return True

    def last_finished_id(self) -> int:
        return self.history[0].id if self.history else 0


@st.cache_resource
def jobs() -> JobRunner:
    return JobRunner()


# ---- plots ----------------------------------------------------------------------------

def readings_chart(readings: pd.DataFrame, title: str = "") -> alt.Chart:
    """Every well's signal, one column per plate, coloured by condition."""
    df = readings.copy()
    df["plate"] = df["plate_id"] + " · batch " + df["batch"].astype(str)
    points = alt.Chart(df).mark_circle(size=55, opacity=0.8).encode(
        x=alt.X("condition:N", title=None, sort=["vehicle", "ABC-12"]),
        xOffset=alt.XOffset("jitter:Q"),
        y=alt.Y("signal:Q", title="SA-beta-gal signal", scale=alt.Scale(zero=True)),
        color=alt.Color("condition:N", scale=CONDITION_COLORS, legend=alt.Legend(orient="top")),
        tooltip=["plate_id", "batch", "well", "condition", "signal"],
    ).transform_calculate(jitter="random()")
    means = alt.Chart(df).mark_tick(color="black", thickness=2, size=30).encode(
        x=alt.X("condition:N", sort=["vehicle", "ABC-12"]), y="mean(signal):Q")
    return alt.layer(points, means).properties(width=170, height=260).facet(
        column=alt.Column("plate:N", title=title or None)).resolve_scale(x="independent")


def plate_map(layout: pd.DataFrame) -> alt.Chart:
    """A 96-well plate drawing of a plan: rows A-H, columns 1-12, outer wells empty."""
    wells = pd.DataFrame([{"row": r, "col": c, "well": f"{r}{c}"}
                          for r in "ABCDEFGH" for c in range(1, 13)])
    df = wells.merge(layout, on="well", how="left")
    df["condition"] = df["condition"].fillna("empty")
    df["label"] = df.apply(lambda x: "" if x.condition == "empty" else
                           ("V" if x.condition == "vehicle" else f"{x.conc_uM:g}"), axis=1)
    scale = alt.Scale(domain=["vehicle", "ABC-12", "empty"], range=["#7a8791", "#5848c9", "#eef1f3"])
    base = alt.Chart(df).encode(
        x=alt.X("col:O", title=None, axis=alt.Axis(orient="top", labelAngle=0)),
        y=alt.Y("row:O", title=None))
    cells = base.mark_circle(size=520, stroke="#c9d1d6").encode(
        color=alt.Color("condition:N", scale=scale, legend=alt.Legend(orient="bottom", title=None)),
        tooltip=["well", "condition", "conc_uM"])
    text = base.mark_text(fontSize=9, color="white").encode(text="label:N")
    return (cells + text).properties(width=430, height=260)


KIND_BADGE = {"question": "❓ question", "fyi": "📎 fyi", "note": "📝 note", "report": "📊 report",
              "answer": "💬 answer", "notice": "⚙️ notice"}
