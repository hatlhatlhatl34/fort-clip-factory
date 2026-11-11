"""The machine's memory — data/state.json, committed by every run.

Ported from forever-factory with clip-machine fields:
  * used_sources  — YouTube video ids already mined (never re-clip)
  * shorts        — one record per published Short
  * quota ledger  — daily YouTube API units (Pacific-day rollover)

Memory survives in git itself: the workflow commits state.json back
every run, and the daily commit keeps GitHub Actions from ever
disabling the schedule for inactivity.
"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import cfg, ROOT

LOCK = threading.Lock()
STATE_PATH = ROOT / "data" / "state.json"
BAK_PATH = ROOT / "data" / "state.json.bak"

_READ_ONLY = False   # --dry-run protection

# YouTube's quota window resets at midnight PACIFIC, not UTC.
_PT = ZoneInfo("America/Los_Angeles")


def _quota_today() -> str:
    return datetime.now(_PT).date().isoformat()


def set_read_only(v: bool) -> None:
    global _READ_ONLY
    _READ_ONLY = v


DEFAULT = {
    "version": 1,
    "shorts": [],           # [{n, source_id, source_title, date, video_id, slot}]
    "used_sources": [],     # YouTube video ids already mined — no repeats
    "query_cursor": 0,      # rotating search phrasing position
    "quota": {"date": None, "units_used": 0},
    "last_run": None,
    "last_run_ts": None,
    "stats": {"shorts_published": 0, "days_active": 0},
}


def load() -> dict:
    """state.json -> state.json.bak -> DEFAULT, never raising, with a
    loud warning on either recovery (visible in factory.log)."""
    with LOCK:
        for path in (STATE_PATH, BAK_PATH):
            if not path.exists():
                continue
            try:
                state = json.loads(path.read_text())
            except json.JSONDecodeError as e:
                print(f"[memory] WARNING: {path.name} is corrupt ({e}) "
                      f"— trying the next recovery source")
                continue
            if path is not STATE_PATH:
                print(f"[memory] recovered from {path.name}")
            for k, v in DEFAULT.items():
                state.setdefault(k, v)
            return state
        print("[memory] WARNING: no readable state — starting from the "
              "empty default (first run, or both files corrupt)")
        return json.loads(json.dumps(DEFAULT))


def save(state: dict) -> None:
    if _READ_ONLY:
        return
    with LOCK:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # last-known-good ladder: current state.json validates -> .bak
        if STATE_PATH.exists():
            try:
                current = STATE_PATH.read_text()
                json.loads(current)
                BAK_PATH.write_text(current)
            except (json.JSONDecodeError, OSError):
                pass
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(STATE_PATH)
        try:
            json.loads(BAK_PATH.read_text())
        except Exception:
            BAK_PATH.write_text(json.dumps(state, indent=1))


# ── clip-machine specifics ──────────────────────────────────────────

def next_short_number(state: dict) -> int:
    """Numbers already burned by published Shorts AND numbers
    reserved by banked-but-unpublished clips (bank.reserved_n) —
    the live fallback can never collide with a banked title."""
    nums = [s["n"] for s in state.get("shorts", [])]
    nums += list((state.get("bank") or {}).get("reserved_n", []) or [])
    return (max(nums) + 1) if nums else 1


def used_sources(state: dict) -> set[str]:
    return set(state.get("used_sources", []))


def mark_source_used(state: dict, source_id: str) -> None:
    if source_id not in state.setdefault("used_sources", []):
        state["used_sources"].append(source_id)


def mark_short_uploaded(state: dict, rec: dict, units: int) -> None:
    """Record a published Short (idempotent on video_id)."""
    for s in state.get("shorts", []):
        if s.get("video_id") == rec.get("video_id") and rec.get("video_id"):
            return
    state.setdefault("shorts", []).append(rec)
    state["stats"]["shorts_published"] = \
        state["stats"].get("shorts_published", 0) + 1
    spend_quota(state, units)


def spend_quota(state: dict, units: int) -> None:
    q = state["quota"]
    today = _quota_today()
    if q.get("date") != today:
        q["date"] = today
        q["units_used"] = 0
    q["units_used"] = q.get("units_used", 0) + units


def quota_remaining(state: dict) -> int:
    q = state["quota"]
    if q.get("date") != _quota_today():
        return int(cfg()["daily"]["quota_budget"])
    return int(cfg()["daily"]["quota_budget"]) - int(q.get("units_used", 0))
