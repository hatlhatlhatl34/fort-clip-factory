#!/usr/bin/env python3
"""RETIRED (owner order 2026-11-23) — publish-now was a ONE-TIME
request, not a standing rule.

The owner's words: "When I said before (publish now immediately) I
meant it for once only because the channel was empty for 2 days
straight but didn't mean to forget the schedules rule. The publish
now was one time request and forget about it — the rest will be
according to plan and schedules."

The publish-now workflow (.github/workflows/publish-now.yml) was
deleted with it, and promote_scheduled was removed from
src/youtube.py. This stub stays so any stray manual run explains
itself and does NOTHING. If a Short must ever go live early, do it
by hand on YouTube — the machine itself never publishes off-grid
again: every upload is private + publishAt on the 6-hour grid
(01:00/07:00/13:00/19:00 Cairo), and the only off-schedule flip
left in the codebase is the slot-due rider pass in src/verify.py
(at slot time, at most one per pass).
"""
import sys


def main() -> int:
    print("PUBLISH-NOW RETIRED (owner order 2026-11-23): the one-time "
          "'publish now' request is consumed. Everything follows the "
          "6-hour grid (01/07/13/19 Cairo) — no off-schedule LIVE "
          "publishes, ever.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
