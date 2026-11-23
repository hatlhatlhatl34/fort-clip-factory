#!/usr/bin/env python3
"""The slot caretaker — the ALWAYS-ON arm of the hourly heal.

WHY IT EXISTS (owner order 2026-11-23, strict grid scheduling): the
heal's gate only RUNs the factory job when an OPEN slot would expire
before the next 08:33 UTC session. At a BOOKED slot's publish moment
the verdict is SKIP — no factory job, no main.py, therefore no
verify pass — yet that is exactly when the machine must:

  * flip slot-due rider Shorts public (re-privatized videos that were
    public once and can never carry a publishAt again — they ride
    the slot-due flip in src/verify.py, AT MOST ONE per pass),
  * confirm fresh publishAt Shorts as they go live (oEmbed, zero
    quota),
  * ensure the giveaway comment exists on every recent live video.

This tool runs the whole verify pass regardless of any gate verdict.
It can never upload, book a slot, or pop a clip — it only ever flips
(at most one per pass) and comments. The hourly-heal workflow runs
it on EVERY fire, including the four slot-anchored crons (3 minutes
after each 6-hour grid slot), so a rider goes public within minutes
of its slot instead of waiting for the next 08:33 session.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import ledger, verify   # noqa: E402


def main() -> int:
    state = ledger.load()
    out = verify.run(state)
    print(f"[slotcare] pass done — {out.get('confirmed', 0)} confirmed "
          f"public, {out.get('late', 0)} late, "
          f"{len(out.get('alerts') or [])} alert(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
