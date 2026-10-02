"""Guardrails: checks in code that run before every tool call.

They don't advise the model, they decide. A blocked call never runs, and the reason goes
back to the Planner so it can replan (usually by asking a scientist).
"""
from __future__ import annotations

from dataclasses import dataclass

from harness.drive.store import Drive
from harness.state import RunState

ANALYSIS_TOOLS = {"filter_dataset", "run_stats"}
APPROVAL_TOOLS = {"plan_followup_plate"}


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    needs_approval: bool = False


def validate_args(schema: dict, args: dict) -> str | None:
    """Minimal JSON-schema check: required keys, no unknown keys, basic types."""
    props = schema.get("properties", {})
    unknown = set(args) - set(props)
    if unknown:
        return f"unknown arguments {sorted(unknown)}"
    missing = [k for k in schema.get("required", []) if k not in args]
    if missing:
        return f"missing required arguments {missing}"
    types = {"string": str, "integer": int, "number": (int, float), "boolean": bool}
    for k, v in args.items():
        want = props[k].get("type")
        if want in types and not isinstance(v, types[want]):
            return f"argument {k} should be {want}"
        if "enum" in props[k] and v not in props[k]["enum"]:
            return f"argument {k} must be one of {props[k]['enum']}"
    return None


def check(name: str, args: dict, drive: Drive, state: RunState,
          schemas: dict[str, dict]) -> Decision:
    if name not in schemas:
        return Decision(False, f"no tool named {name!r}")
    problem = validate_args(schemas[name], args)
    if problem:
        return Decision(False, f"{name}: {problem}")
    if state.tool_calls >= state.budget:
        return Decision(False, f"tool-call budget of {state.budget} is used up; report what you have")

    if name in ANALYSIS_TOOLS:
        open_items = drive.open_contradictions()
        if open_items:
            ids = ", ".join(c["id"] for c in open_items)
            return Decision(False, f"{name} is blocked until these are answered by their owners: {ids}")
    if name == "run_stats":
        missing = state.missing_slots()
        if missing:
            return Decision(False, f"run_stats is blocked: the goal is missing {missing}. "
                                   f"Ask the requester")
        th = state.goal["threshold"]
        if args.get("min_drop") != th["min_drop"] or args.get("max_p") != th["max_p"]:
            return Decision(False, "run_stats must use the threshold the requester gave: "
                                   f"min_drop={th['min_drop']}, max_p={th['max_p']}")

    return Decision(True, needs_approval=name in APPROVAL_TOOLS)
