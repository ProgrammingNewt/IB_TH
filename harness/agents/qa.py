"""Q&A agent. One job: every message a scientist receives.

It writes questions, copies to other owners, notes on their files, reports, and answers
about findings, and turns a free-text reply into a value. System notices (a run stopped, an
answer it couldn't read) are fixed templates here too, with no model call, so they still
work when every provider is down. No other part of the harness writes text to a person.

It never decides anything: the Planner decides what to ask, and the orchestrator decides
who receives it.
"""
from __future__ import annotations

from typing import Any, Callable

from harness.gateway.gateway import Gateway, GatewayError
from . import prompts
from .base import AgentReplyError, as_user, fix_request, parse_json


def _message(out: dict) -> str | None:
    m = out.get("message")
    return None if isinstance(m, str) and m.strip() else "Missing a non-empty \"message\"."


def _parsed_value(out: dict) -> str | None:
    return None if "value" in out else "Missing \"value\" (use null if the reply doesn't say)."


NOTICES = {
    "not_a_request": "I can analyse an experiment's data on this drive, or answer questions "
                     "about its findings. Try something like \"Is ABC-12 a hit?\"",
    "unreadable_answer": "I couldn't turn that into {what}. Could you give it again, e.g. {example}?",
    "run_stopped": "The analysis stopped before it finished: {reason}. Nothing was concluded.",
}

def _labels_for(values: list[dict]) -> Callable[[dict], str | None]:
    """A label must exist for each value and state each of its numbers (as 30% or 0.3)."""
    def check(out: dict) -> str | None:
        labels = out.get("labels")
        if not (isinstance(labels, list) and len(labels) == len(values)
                and all(isinstance(x, str) and x.strip() for x in labels)):
            return f"\"labels\" must be a list of {len(values)} non-empty strings."
        for label, value in zip(labels, values):
            for n in (value or {}).values():
                if isinstance(n, (int, float)) and f"{n * 100:g}" not in label and f"{n:g}" not in label:
                    return f"The label {label!r} doesn't state the number {n:g}."
        return None
    return check


# Progress updates, shown live while a request runs. Fixed and instant: no model call.
# Milestones are what the reply keeps showing; the rest only appear as the current status.
MILESTONES = {"understood", "slot_read_as", "not_analysis", "uploaded", "reevaluating", "waiting",
              "answered_still_waiting", "all_answered", "revising", "done", "failed"}
PROGRESS = {
    "received": "Got it. Working on: {text}",
    "understood": "Understood: a new analysis{about}.",
    "slot_read_as": "Reading \"{asked}\" as {name}, the {slot} in the files.",
    "not_analysis": "That isn't a request for an analysis.",
    "answering": "Looking that up in the findings.",
    "uploaded": "{name} uploaded {file}. Re-evaluating: {question}",
    "reevaluating": "Re-evaluating: {question}",
    "extract_protocol_fields": "Reading the protocol ({path})",
    "parse_xlsx": "Reading the plate readings ({path})",
    "read_notebook": "Reading the analysis notebook ({path})",
    "run_notebook": "Re-running the analysis notebook ({path})",
    "read_table": "Reading the table {path}",
    "read_document": "Reading {path}",
    "check_consistency": "Checking every plate against the protocol",
    "filter_dataset": "Applying the team's decisions to the data",
    "run_stats": "Running the statistics",
    "plan_followup_plate": "Drafting a follow-up plate",
    "search_sops": "Looking up the lab's SOPs",
    "load_skill": "Loading the {name} procedure",
    "asking": "Found something to check with the team. Writing the questions",
    "waiting": "Waiting for answers: {who}",
    "answered_still_waiting": "{name} answered. Still waiting for {who}.",
    "all_answered": "{name} answered. All answers are in, continuing.",
    "reviewing": "Reviewing the conclusion before anyone sees it",
    "revising": "The review sent it back. Revising.",
    "writing": "Writing up the findings",
    "done": "Done: {verdict}. The findings are in the Findings tab.",
    "failed": "Stopped: {reason}",
}

TASKS: dict[str, tuple[str, Callable[[dict], str | None]]] = {
    "ask": (prompts.QA_ASK, _message),
    "report": (prompts.QA_REPORT, _message),
    "copy": (prompts.QA_COPY, _message),
    "note": (prompts.QA_NOTE, _message),
    "answer": (prompts.QA_ANSWER, _message),
    "parse_other": (prompts.QA_PARSE_OTHER, _parsed_value),
}


class QA:
    def __init__(self, gateway: Gateway, max_attempts: int = 2):
        self.gw, self.max_attempts = gateway, max_attempts

    def run(self, task: str, payload: dict, fallback: dict,
            check: Callable[[dict], str | None] | None = None) -> dict:
        """One task, with its own check. Returns `fallback` if the model can't do it."""
        system, default_check = TASKS.get(task, (None, None))
        if task == "labels":
            system = prompts.QA_LABELS
        check = check or default_check
        messages = [{"role": "system", "content": system}, as_user(payload)]
        for _ in range(self.max_attempts):
            try:
                reply = self.gw.complete("qa", messages, json_mode=True).content
            except GatewayError:
                return fallback
            try:
                out = parse_json(reply)
            except AgentReplyError as exc:
                messages += fix_request(reply, str(exc))
                continue
            problem = check(out)
            if problem is None:
                return out
            messages += fix_request(reply, problem)
        return fallback

    # Named entry points, so callers read clearly.

    def ask(self, person: dict, question: str, context: dict, options: list[str] = (),
            must_mention: list[str] = ()) -> str:
        """A question that ends by naming the decision and its options.

        `must_mention` are words the message has to contain (e.g. "exclude", "keep"); a
        message without them, or without a question mark, is sent back to be rewritten."""
        def check(out: dict) -> str | None:
            problem = _message(out)
            if problem:
                return problem
            text = out["message"].lower()
            if "?" not in text:
                return "End the message with the question itself."
            missing = [w for w in must_mention if w.lower() not in text]
            if missing:
                return f"The question must name the options: {', '.join(missing)}."
            return None
        fallback = question + (f" Options: {'; '.join(options)}." if options else "")
        return self.run("ask", {"to": person, "question": question, "options": list(options),
                                "context": context},
                        {"message": fallback}, check=check)["message"].strip()

    def report(self, person: dict, findings_md: str) -> str:
        return self.run("report", {"to": person, "findings": findings_md}, {"message":
                        "The analysis is finished. The findings are in findings/findings.md."})["message"].strip()

    def copy(self, person: dict, asked: dict, question: str) -> str:
        return self.run("copy", {"to": person, "question_sent_to": asked, "question": question},
                        {"message": f"{asked['name']} was asked: {question}"})["message"].strip()

    def note(self, person: dict, file: str, note: str) -> str:
        return self.run("note", {"to": person, "file": file, "note": note},
                        {"message": f"There's a note on {file}: {note}"})["message"].strip()

    def labels(self, person: dict, question: str, values: list[dict],
               fallback: list[str]) -> list[str]:
        """Button labels for answer choices, written from their values."""
        out = self.run("labels", {"to": person, "question": question, "values": values},
                       {"labels": fallback}, check=_labels_for(values))
        return [x.strip() for x in out["labels"]]

    @staticmethod
    def progress(kind: str, **details) -> str:
        """A live progress line. No model call."""
        class _Blank(dict):
            def __missing__(self, key):
                return ""
        return PROGRESS[kind].format_map(_Blank(details))

    @staticmethod
    def notice(kind: str, **details) -> str:
        """A fixed system notice. No model call."""
        return NOTICES[kind].format(**details)

    def answer(self, person: dict, question: str, findings_md: str) -> str:
        if not findings_md:
            return "There are no findings yet for this drive."
        return self.run("answer", {"from": person, "question": question, "findings": findings_md},
                        {"message": "I couldn't answer that just now."})["message"].strip()

    def parse_other(self, question: dict, reply: str) -> dict[str, Any]:
        example = next((c["value"] for c in question["choices"] if c.get("value")), None)
        out = self.run("parse_other", {"question": question["text"], "value_shape_example": example,
                                       "reply": reply}, {"value": None})
        return {"value": out.get("value"), "understood_as": out.get("understood_as", "")}
