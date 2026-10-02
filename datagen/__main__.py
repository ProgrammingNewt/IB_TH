"""Build the toy drives: `python -m datagen [--out drives] [--seed N]`."""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .scenarios import DEFAULT_SEED, SCENARIOS, ScenarioCheckFailed
from .writers import write_scenario


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=Path("drives"))
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = ap.parse_args(argv)

    built = []
    for name, make in SCENARIOS.items():
        try:
            scenario = make(args.seed)
        except ScenarioCheckFailed as exc:
            print(f"[{name}] seed {args.seed} doesn't support the scenario: {exc}", file=sys.stderr)
            return 1
        built.append(scenario)

    # Only replace what this script owns: each scenario's drive, incoming folder and answer key.
    args.out.mkdir(parents=True, exist_ok=True)
    for s in built:
        for p in (args.out / s.name, args.out / f"{s.name}.incoming"):
            if p.exists():
                shutil.rmtree(p)
        drive = write_scenario(s, args.out)
        print(f"[{s.name}] wrote {drive}")
        for check in s.checks:
            print(f"  ok  {check}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
