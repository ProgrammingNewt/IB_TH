"""Check which providers answer: `python -m harness.gateway`.

Sends one tiny prompt to each configured provider on its own and reports ok, skipped
(no key), refused or error. Useful right after filling in .env.
"""
from __future__ import annotations

import sys

from .gateway import Gateway, GatewayError, load_config


def main() -> int:
    cfg = load_config()
    any_ok = False
    for p in cfg["providers"]:
        single = dict(cfg, providers=[p])
        try:
            c = Gateway(single).complete("router", [{"role": "user", "content": "Reply with: ok"}])
            print(f"ok       {p['name']} ({p['label']}) model={c.model} reply={c.content.strip()[:40]!r}")
            any_ok = True
        except GatewayError as exc:
            a = exc.attempts[0]
            print(f"{a['outcome']:<8} {p['name']} ({p['label']}): {a['reason']}")
    return 0 if any_ok else 1


if __name__ == "__main__":
    sys.exit(main())
