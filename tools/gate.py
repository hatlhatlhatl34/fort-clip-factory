#!/usr/bin/env python3
"""The run gate — should the factory run RIGHT NOW?

Stdlib only (runs before pip install, costs ~15 seconds). Prints RUN
or SKIP and exits 0 either way — SKIP is a healthy, cheap outcome.

OWNER RULE (2026-11-13): ONE upload session per day at 08:30 UTC
(channel.yaml schedule.daily_run_utc — frozen copy below, keep in
sync). That one session books ALL FOUR grid slots of the day+1 window
(00:00/08:00/16:00/20:00 Cairo, 4/day every day).

The single rule (used by BOTH the daily cron and the hourly heal):

    RUN  iff an OPEN grid slot exists in (now + margin,
    next daily fire] — i.e. a slot would go public without a Short
    unless the machine wakes up before the next 08:30 UTC session.

  * At 08:30 UTC that window holds the next 4 open slots -> RUN
    (the session itself).
  * Between sessions the booked slots hide inside -> SKIP (the heal
    stays a seconds-long no-op ~99% of the time).
  * A missed daily session leaves the same 4 slots open -> the very
    next hourly heal RUNs and heals them all in one go.
  * Slots beyond the next fire are NOT holes — tomorrow's session
    owns them.

A human dispatch always RUNS (force). A gate crash defaults to RUN
(the only unforgivable verdict is a silent one).
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

# keep in sync with channel.yaml schedule (gate runs before pip
# install, so no YAML — values frozen here)
GRID_TZ = ZoneInfo("Africa/Cairo")
GRID = ("00:00", "08:00", "16:00", "20:00")   # 4 slots, EVERY day
DAILY_FIRE_UTC = (8, 30)                       # the one daily session
BOOKABLE_MARGIN = 40                           # minutes of render time
                                              # before publish


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


def _next_fire(now_utc: datetime) -> datetime:
    """The next 08:30 UTC strictly beyond the booking margin — a fire
    too close to 'now' belongs to the imminent session, not to us."""
    cand = now_utc.replace(hour=DAILY_FIRE_UTC[0],
                           minute=DAILY_FIRE_UTC[1],
                           second=0, microsecond=0)
    if cand <= now_utc + timedelta(minutes=BOOKABLE_MARGIN):
        cand += timedelta(days=1)
    return cand


def _open_slots(state: dict, now: datetime | None = None) -> list[str]:
    """Open grid slots in (now + margin, next daily fire] — the exact
    set the session would fill."""
    now_utc = datetime.now(ZoneInfo("UTC"))
    fire = _next_fire(now_utc)
    now = now or datetime.now(tz=GRID_TZ)
    booked = set()
    for s in (state.get("slots") or {}).get("short", []) or []:
        try:
            when = datetime.fromisoformat(s)
        except (ValueError, TypeError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=GRID_TZ)
        booked.add(when.astimezone(GRID_TZ))

    probe = now + timedelta(minutes=BOOKABLE_MARGIN)
    holes: list[str] = []
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(2):                       # today + tomorrow is plenty
        for hhmm in GRID:
            hh, mm = map(int, hhmm.split(":"))
            slot_dt = day.replace(hour=hh, minute=mm)
            fire_local = fire.astimezone(GRID_TZ)
            if probe < slot_dt <= fire_local and slot_dt not in booked:
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

        holes = _open_slots(state)
        if holes:
            print(f"RUN   # {len(holes)} open slot(s) before the next "
                  f"session: {', '.join(holes[:4])}")
        else:
            print("SKIP  # grid covered until the next session")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"RUN   # gate internal error ({type(e).__name__}: {e}) "
              f"— attempting production; a real failure will alert")
        return 0


if __name__ == "__main__":
    sys.exit(main())
