"""The shared lab drive: a folder, its manifest, and the team's confirmed facts.

Everything the harness knows lives here as files, so a run can pause and resume, and a
later run starts from what the team has already confirmed. Two rules are enforced here
rather than left to an agent:

* every derived file gets a `.lineage.json` naming its inputs, the tool and the facts used;
* only the owner of a file can confirm a fact about it. Anyone else's answer is kept as a
  comment, not a fact.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path
from typing import Any, Callable

FACTS = "facts/answers.json"
CONTRADICTIONS = "facts/contradictions.json"
COMMENTS = "facts/comments.json"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class DriveError(Exception):
    pass


class Drive:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.manifest = json.loads((self.root / "manifest.json").read_text())

    # ---- paths and files ------------------------------------------------------

    def path(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root):
            raise DriveError(f"{rel} is outside the drive")
        return p

    @property
    def people(self) -> dict[str, dict]:
        return self.manifest["people"]

    def files(self) -> list[dict]:
        return list(self.manifest["files"])

    def owner_of(self, rel: str) -> str | None:
        return next((f["owner"] for f in self.manifest["files"] if f["path"] == rel), None)

    def upload(self, src: str | Path, owner: str, description: str) -> str:
        """Add a scientist's file to raw/ and the manifest. Returns its drive path."""
        src = Path(src)
        rel = f"raw/{src.name}"
        if self.owner_of(rel):
            raise DriveError(f"{rel} already exists")
        shutil.copy2(src, self.path(rel))
        self.manifest["files"].append({"path": rel, "owner": owner, "description": description,
                                       "modified": dt.date.today().isoformat()})
        self._save_manifest()
        return rel

    def _save_manifest(self) -> None:
        (self.root / "manifest.json").write_text(json.dumps(self.manifest, indent=2))

    # ---- json helpers and lineage ---------------------------------------------

    def read_json(self, rel: str, default: Any = None) -> Any:
        p = self.path(rel)
        return json.loads(p.read_text()) if p.exists() else default

    def write_json(self, rel: str, obj: Any) -> None:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, indent=2, default=str))

    def write_artifact(self, rel: str, write: Callable[[Path], None], *, tool: str,
                       inputs: list[str], facts_used: list[str] | None = None) -> str:
        """Write a derived file and its lineage record next to it."""
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        write(p)
        self.write_json(rel + ".lineage.json", {
            "file": rel, "tool": tool, "inputs": inputs,
            "facts_used": facts_used or [], "created": _now()})
        return rel

    # ---- facts and contradictions --------------------------------------------

    def facts(self) -> list[dict]:
        return self.read_json(FACTS, [])

    def add_fact(self, fact: dict) -> dict:
        facts = self.facts()
        fact = {"id": f"f-{len(facts) + 1:03d}", "at": _now(), **fact}
        facts.append(fact)
        self.write_json(FACTS, facts)
        return fact

    def contradictions(self) -> list[dict]:
        return self.read_json(CONTRADICTIONS, [])

    def open_contradictions(self) -> list[dict]:
        return [c for c in self.contradictions() if c["status"] == "open"]

    def resolve_contradiction(self, cid: str, by: str, choice: str) -> dict:
        """Record a scientist's answer. Only the deciding owner's answer becomes a fact.

        `choice` is one of the contradiction's `choices` keys: "exclude", "keep" or free
        text for "other"."""
        items = self.contradictions()
        c = next((c for c in items if c["id"] == cid), None)
        if c is None:
            raise DriveError(f"no contradiction {cid}")
        if by != c["decides"]:
            comments = self.read_json(COMMENTS, [])
            comment = {"on": cid, "by": by, "text": choice, "at": _now()}
            comments.append(comment)
            self.write_json(COMMENTS, comments)
            return {"recorded_as": "comment", "comment": comment,
                    "reason": f"{c['file']} is owned by {c['decides']}, so only they can confirm"}

        scope = {"file": c["file"], "plate_id": c["plate_id"], "batch": c["batch"]}
        if choice == "exclude":
            action = {"exclude_plate": c["plate_id"]}
        elif choice == "keep":
            action = {"keep_plate": c["plate_id"]}
        else:
            action = {"note": choice}
        fact = self.add_fact({"resolves": cid, "scope": scope, "action": action,
                              "statement": c["choices"].get(choice, choice),
                              "confirmed_by": by})
        c.update(status="resolved", resolved_by=by, fact=fact["id"])
        self.write_json(CONTRADICTIONS, items)
        return {"recorded_as": "fact", "fact": fact}
