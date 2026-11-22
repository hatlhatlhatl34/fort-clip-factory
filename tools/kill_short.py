#!/usr/bin/env python3
"""OWNER RULE (2026-11-23): Short #81 — kill the duplicate.

What happened: #76 (live 2026-11-22 00:00 Cairo) and #81 (scheduled
2026-11-23 06:00 Cairo) were sibling cuts of ONE archive.org video
("Fortnite Item Shop – April 12, 2025 Adventure Time Skins, Sabrina
Carpenter & Coachella collabs"). Archive.org hands the same item out
under identifier AND identifier_YYYYMM (a derivative re-upload); the
exact-match mined() check in downloader.py saw two different strings,
so the same stale April-2025 item-shop footage was banked and
scheduled twice. The owner caught #81 on the channel before its
06:00 slot fired.

Phases (idempotent, the state marker remembers — every re-fire costs
nothing and never repeats finished work):

  delete      videos.delete(flclQkSGilw) — 50 units, spent honestly.
              A 404 (already gone) is settled, never re-called.
  clean       ledger surgery: drop the short record, unbook slot
              2026-11-23T06:00+02:00, recount stats. NO replacement
              upload: bank clip n=82 (Whoods, AFK XP map, fresh)
              already owns that slot — the machine fills it at the
              next heal/session (the slot booking stays with the
              bank clip; only the zombie short record dies).

The harvest-side fix (folding derivative suffixes in mined()) and the
publish-side guard (bank.take_next skips clips whose folded source
already backs a published Short) ride in the same push — this never
happens again.

Usage:
  python tools/kill_short.py                # both phases
  python tools/kill_short.py --dry-run      # rehearse, no writes
"""
from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import ledger, youtube  # noqa: E402

TARGET = {"n": 81, "video_id": "flclQkSGilw",
          "slot": "2026-11-23T06:00:00+02:00"}
DELETE_UNITS = 50          # videos.delete listed cost


def _marker(state: dict) -> dict:
    return state.setdefault("dup_kill_81", {})


# ── phase 1: delete the duplicate video ───────────────────────────
def phase_delete(state: dict, dry_run: bool = False) -> None:
    print("== phase 1: DELETE the duplicate Short from YouTube ==")
    mark = _marker(state)
    done = set(mark.get("deleted") or [])
    # durable zero-cost guard: an id already absent from the ledger was
    # deleted + cleaned by this fix (or never existed) — re-fires (even
    # a year out) never re-spend the delete call
    ledger_vids = {s.get("video_id") for s in state.get("shorts") or []}
    vid = TARGET["video_id"]
    if vid in done:
        print(f"  {vid}: already deleted (marker) — zero cost")
        return
    if vid not in ledger_vids:
        print(f"  {vid}: already gone from the ledger — zero cost "
              "(deletion is permanent on YouTube)")
        done.add(vid)
        mark["deleted"] = sorted(done)
        ledger.save(state)
        return
    if dry_run:
        print(f"  {vid}: [dry-run] would DELETE")
        return
    token = youtube.get_access_token()
    print(f"  deleting {vid} ...", end=" ", flush=True)
    req = urllib.request.Request(
        f"{youtube.API_BASE}/videos?id={vid}", method="DELETE",
        headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30):
            print("DELETED")
            done.add(vid)
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:160]
        if e.code == 404:
            print("already gone (404)")
            done.add(vid)
        else:
            # 403 quota etc: fail loudly — the ledger stays untouched
            # until the video is really gone (no zombie records)
            print(f"HTTP {e.code}: {body}")
            raise SystemExit(f"delete failed ({e.code}) — ledger not "
                             "cleaned; retry after the quota reset")
    ledger.spend_quota(state, DELETE_UNITS)   # honest accounting
    mark["deleted"] = sorted(done)
    ledger.save(state)
    print("  delete settled (marker remembers — re-fires cost zero)")


# ── phase 2: ledger surgery ───────────────────────────────────────
def phase_clean(state: dict, dry_run: bool = False) -> None:
    print("== phase 2: clean the ledger (drop record, free the slot) ==")
    vid, kill_slot = TARGET["video_id"], TARGET["slot"]
    before = len(state.get("shorts") or [])
    state["shorts"] = [s for s in state.get("shorts") or []
                       if s.get("video_id") != vid]
    removed = before - len(state["shorts"])

    box = (state.get("slots") or {}).get("short") or []
    kept = [x for x in box if x != kill_slot]
    freed = len(box) - len(kept)
    state.setdefault("slots", {})["short"] = kept

    st = state.setdefault("stats", {})
    st["shorts_published"] = max(0, int(st.get("shorts_published", 0))
                                  - removed)
    _marker(state)["cleaned"] = True

    if not dry_run:
        ledger.save(state)
    print(f"  short records removed: {removed}; slots unbooked: {freed}")
    print("  the 06:00 Cairo slot goes to bank clip n=82 (Whoods AFK "
          "XP map) — no replacement upload, the machine owns the slot")
    if removed:
        print("  its pending IG/FB crossposts died with the record — "
              "no dead-link reels will ever ship")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    state = ledger.load()
    phase_delete(state, args.dry_run)
    phase_clean(state, args.dry_run)
    print("== done: the duplicate is gone; the grid heals itself ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
