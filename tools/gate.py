#!/usr/bin/env python3
"""The run gate — should the factory run RIGHT NOW?

Stdlib only (runs before pip install, costs ~15 seconds). Prints RUN
or SKIP and exits 0 either way — SKIP is a healthy, cheap outcome.

Window logic: the base cron fires every ~8h for the 3-slot grid
(00:00/08:00/16:00 Cairo) plus a weekend cron for the 4th slot
(20:00 Cairo, Fri/Sat/Sun only — 3/day weekdays, 4/day weekends). A manual workflow_dispatch inside the
window must NOT double-produce, so SKIP when the last successful run
is younger than WINDOW_HOURS (7h). A grid hole (an unbooked slot that
expires before the next cron) always RUNS — heal mode. A human
dispatch always RUNS (force). A gate crash defaults to RUN (the only
unforgivable verdict is a silent one).
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

STATE = Path(__file__).resolve().parent.parent / "data" / "state.json"
STATE_BAK = Path(__file__).resolve().parent.parent / "data" / "state.json.bak"
WINDOW_HOURS = 7.0

# keep in sync with channel.yaml schedule (gate runs before pip
# install, so no YAML — values frozen here)
GRID_TZ = ZoneInfo("Africa/Cairo")
GRID_SHORT = ("00:00", "08:00", "16:00")
GRID_WEEKEND = ("20:00",)   # 4th slot — weekend days only
WEEKEND_DAYS = (4, 5, 6)    # date.weekday(): 4=Fri 5=Sat 6=Sun
CYCLE_SPACING_H = 8          # crons fire every ~8h
BOOKABLE_MARGIN = 40         # minutes of render time before publish


def _load_state() -> dict:
    for path in (STATE, STATE_BAK):
        if not path.exists():
            continue
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            print(f"# gate: {path.name} unreadable — trying next",
                  file=sys.stderr)
    return {}


def _grid_holes(state: dict, now: datetime | None = None) -> list[str]:
    """Unbooked publish slots that expire before the next cron could
    ever claim them — the strongest reason to run right now."""
    now = now or datetime.now(tz=GRID_TZ)
    holes: list[str] = []
    booked: set[datetime] = set()
    for s in (state.get("slots") or {}).get("short", []) or []:
        try:
            when = datetime.fromisoformat(s)
        except (ValueError, TypeError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=GRID_TZ)
        booked.add(when)

    # next cron point = now rounded up to the next 8h boundary
    point = now + timedelta(hours=CYCLE_SPACING_H)
    probe = now + timedelta(minutes=BOOKABLE_MARGIN)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(3):
        grid = GRID_SHORT + (GRID_WEEKEND if day.weekday() in WEEKEND_DAYS
                             else ())
        for hhmm in grid:
            hh, mm = map(int, hhmm.split(":"))
            slot_dt = day.replace(hour=hh, minute=mm)
            if probe < slot_dt <= point and slot_dt not in booked:
                holes.append(slot_dt.isoformat())
        day += timedelta(days=1)
    return holes


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", default="schedule")
    ap.add_argument("--actor", default="")
    args = ap.parse_args()

    human = args.event == "workflow_dispatch" and \
        not args.actor.startswith("github-actions")
    if human:
        print("RUN")
        return 0

    try:
        state = _load_state()

        q = state.get("quota") or {}
        defer_until = q.get("defer_until") or 0
        if defer_until > time.time():
            print(f"SKIP  # server-side {q.get('defer_reason', 'quota')} "
                  f"defer until "
                  f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(defer_until))}")
            return 0

        holes = _grid_holes(state)
        if holes:
            print(f"RUN   # {len(holes)} grid hole(s) to heal: "
                  f"{', '.join(holes[:3])}")
            return 0

        last_ts = state.get("last_run_ts")
        age_h = (time.time() - last_ts) / 3600.0 if last_ts else None
        if last_ts and age_h < WINDOW_HOURS:
            print(f"SKIP  # last cycle {age_h:.1f}h ago "
                  f"(< {WINDOW_HOURS:.0f}h window)")
        else:
            why = ("no successful cycle yet" if not last_ts
                   else f"last cycle {age_h:.1f}h ago")
            print(f"RUN   # {why}")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"RUN   # gate internal error ({type(e).__name__}: {e}) "
              f"— attempting production; a real failure will alert")
        return 0


if __name__ == "__main__":
    sys.exit(main())
