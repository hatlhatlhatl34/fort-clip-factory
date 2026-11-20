#!/usr/bin/env python3
"""D8 (owner order 2026-11-20): publish NOW — manual trigger.

"With the first downloaded video clip it and publish it. I will not
wait for the bank." The publish-now workflow calls this tool to flip
the earliest still-scheduled Short to LIVE immediately (~50 quota
units, no download, no render). The mutated data/state.json is
committed back by the workflow's git step.

Never raises: a failed flip prints and exits 0 so the workflow stays
green — the video itself might already be public (idempotent).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import ledger            # noqa: E402
from src.youtube import promote_scheduled   # noqa: E402


def main() -> int:
    state = ledger.load()
    vid = promote_scheduled(state)
    if vid:
        print(f"PUBLISH-NOW OK: short {vid} is LIVE")
        return 0
    print("PUBLISH-NOW: nothing scheduled to flip (or the flip failed) "
          "— see the promote lines above")
    return 0


if __name__ == "__main__":
    sys.exit(main())
