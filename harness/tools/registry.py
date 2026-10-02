"""Tool registry: the schemas the Planner sees, and the one entry point that runs a tool.

`call` is the only way a tool runs. It applies the guardrails first, counts the call
against the budget, and turns tool errors into results the Planner can read.
"""
from __future__ import annotations

from typing import Any, Callable

from harness.drive.store import Drive
from harness.guardrails.rules import check
from harness.state import RunState
from . import analysis, files, lab
from .errors import ToolError


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


_path = {"type": "string", "description": "Drive path, e.g. raw/readings.xlsx"}

TOOLS: dict[str, dict[str, Any]] = {
    "parse_xlsx": {
        "fn": files.parse_xlsx,
        "description": "Read a readings workbook (readings + run_metadata sheets) into tidy rows.",
        "parameters": _obj({"path": _path}, ["path"]),
    },
    "extract_protocol_fields": {
        "fn": files.extract_protocol_fields,
        "description": "Read a protocol PDF and pull out compound, readout, comparison, "
                       "treatment time, controls and hit criteria.",
        "parameters": _obj({"path": _path}, ["path"]),
    },
    "read_notebook": {
        "fn": files.read_notebook,
        "description": "Read a notebook's code, saved outputs and written conclusion.",
        "parameters": _obj({"path": _path}, ["path"]),
    },
    "run_notebook": {
        "fn": files.run_notebook,
        "description": "Re-run a notebook on a copy of its data and check it reproduces.",
        "parameters": _obj({"path": _path}, ["path"]),
    },
    "check_consistency": {
        "fn": analysis.check_consistency,
        "description": "Compare every plate's run metadata and wells with the protocol. "
                       "Returns contradictions for their owners to answer.",
        "parameters": _obj({}, []),
    },
    "filter_dataset": {
        "fn": analysis.filter_dataset,
        "description": "Apply the team's confirmed facts (e.g. excluded plates) to the readings.",
        "parameters": _obj({}, []),
    },
    "run_stats": {
        "fn": analysis.run_stats,
        "description": "Compare a compound with vehicle, each plate normalised to its own "
                       "vehicle wells. Uses the requester's threshold.",
        "parameters": _obj({"compound": {"type": "string"},
                            "min_drop": {"type": "number"},
                            "max_p": {"type": "number"}}, ["compound", "min_drop", "max_p"]),
    },
    "plan_followup_plate": {
        "fn": lab.plan_followup_plate,
        "description": "Draft a follow-up plate (re-run or dose-response) with vehicle on the "
                       "same plate. Needs a scientist's approval.",
        "parameters": _obj({"kind": {"type": "string", "enum": ["rerun", "dose_response"]},
                            "compound": {"type": "string"},
                            "treatment_hours": {"type": "integer"}}, ["kind", "compound"]),
    },
    "search_sops": {
        "fn": lab.search_sops,
        "description": "Keyword search over the lab's SOP rules.",
        "parameters": _obj({"query": {"type": "string"}}, ["query"]),
    },
    "load_skill": {
        "fn": lambda drive, name: lab.load_skill(name),
        "description": "Load a skill: a step-by-step procedure, e.g. batch_qc.",
        "parameters": _obj({"name": {"type": "string"}}, ["name"]),
    },
}

SCHEMAS = {name: t["parameters"] for name, t in TOOLS.items()}


def openai_tools() -> list[dict]:
    """The tool list in OpenAI function-calling format, for the Planner."""
    return [{"type": "function", "function": {"name": n, "description": t["description"],
                                               "parameters": t["parameters"]}}
            for n, t in TOOLS.items()]


def call(drive: Drive, state: RunState, name: str, args: dict | None = None,
         overrides: dict[str, Callable] | None = None) -> dict:
    """Run one tool through the guardrails. Always returns a dict; never raises for the
    Planner's mistakes. `overrides` swaps a tool's function (e.g. an LLM-backed extractor)."""
    args = dict(args or {})
    decision = check(name, args, drive, state, SCHEMAS)
    record = {"tool": name, "args": args}
    if not decision.allowed:
        result = {"ok": False, "blocked": decision.reason}
    else:
        state.tool_calls += 1
        fn = (overrides or {}).get(name, TOOLS[name]["fn"])
        try:
            result = {"ok": True, **fn(drive, **args)}
        except ToolError as exc:
            result = {"ok": False, "error": str(exc)}
        if decision.needs_approval and result["ok"]:
            result["approval"] = "pending: sent to the approval inbox"
    state.history.append(record | {"result": result})
    return result
