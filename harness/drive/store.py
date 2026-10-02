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

    def upload(self, src: str | Path, owner: str, description: str, replace: bool = False) -> str:
        """Add a scientist's file to raw/ and the manifest. Any file type. Returns its path.

        A file with the same name can only be replaced by its owner, with `replace=True`;
        the old version goes to trash/."""
        src = Path(src)
        rel = f"raw/{src.name}"
        current = self.owner_of(rel)
        if current is not None:
            if not replace:
                raise DriveError(f"{rel} already exists. Replace it, or rename the file")
            self.remove(rel, by=owner)               # checks ownership
        shutil.copy2(src, self.path(rel))
        self.manifest["files"].append({"path": rel, "owner": owner, "description": description,
                                       "modified": dt.date.today().isoformat()})
        self._save_manifest()
        return rel

    def remove(self, rel: str, by: str) -> dict:
        """Move a file to trash/. Only its owner can. Data derived from it is cleaned up:
        its rows leave the parsed readings, facts read from it are dropped, and open
        contradictions about it are withdrawn. Returns what was changed."""
        owner = self.owner_of(rel)
        if owner is None:
            raise DriveError(f"{rel} is not in the drive")
        if by != owner:
            raise DriveError(f"{rel} is owned by {owner}, so only they can remove it")
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
        trash = f"trash/{stamp}-{Path(rel).name}"
        self.path("trash").mkdir(exist_ok=True)
        shutil.move(self.path(rel), self.path(trash))
        self.manifest["files"] = [f for f in self.manifest["files"] if f["path"] != rel]
        self.manifest.setdefault("removed", []).append(
            {"path": rel, "owner": owner, "by": by, "at": _now(), "trash": trash})
        self._save_manifest()
        return {"removed": rel, "trash": trash, "cleaned": self._forget(rel)}

    def _forget(self, rel: str) -> list[str]:
        """Drop everything the harness derived from `rel`."""
        import pandas as pd

        cleaned = []
        readings = self.path("derived/clean_readings.csv")
        if readings.exists():
            df = pd.read_csv(readings)
            if (df.source_file == rel).any():
                df = df[df.source_file != rel]
                self.write_artifact("derived/clean_readings.csv", lambda p: df.to_csv(p, index=False),
                                    tool="remove_file", inputs=sorted(df.source_file.unique()))
                cleaned.append("derived/clean_readings.csv")
        meta = self.read_json("facts/run_metadata.json", None)
        if meta and any(m["source_file"] == rel for m in meta):
            self.write_json("facts/run_metadata.json", [m for m in meta if m["source_file"] != rel])
            cleaned.append("facts/run_metadata.json")
        for name in ("facts/protocol.json", "facts/notebook_claims.json"):
            data = self.read_json(name, None)
            if isinstance(data, dict) and data.get("source_file") == rel:
                self.path(name).unlink()
                cleaned.append(name)
        items = self.contradictions()
        changed = False
        for c in items:
            if c["file"] == rel and c["status"] == "open":
                c.update(status="withdrawn", note=f"{rel} was removed")
                changed = True
        if changed:
            self.write_json(CONTRADICTIONS, items)
            cleaned.append(CONTRADICTIONS)
        return cleaned

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
