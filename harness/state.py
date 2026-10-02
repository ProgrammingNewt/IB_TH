"""Per-run state the guardrails read: the goal's slots and the tool-call budget."""
from __future__ import annotations

from dataclasses import dataclass, field

# A goal can't be analysed until each of these is known.
REQUIRED_SLOTS = ("compound", "readout", "comparator", "threshold")


@dataclass
class RunState:
    goal: dict = field(default_factory=dict)   # e.g. {"compound": "ABC-12", "threshold": {...}}
    budget: int = 25
    tool_calls: int = 0
    history: list[dict] = field(default_factory=list)

    def missing_slots(self) -> list[str]:
        return [s for s in REQUIRED_SLOTS if not self.goal.get(s)]
