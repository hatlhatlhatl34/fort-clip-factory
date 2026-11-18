"""The Bank — the monthly food rung (owner idea, 2026-11-11).

WHY IT EXISTS: GitHub runners get a fresh datacenter IP every run.
Downloading daily from YouTube through rotating IPs is exactly what
trips the "Sign in to confirm you're not a bot" wall. The owner's
fix: do the risky work (download + clip) ONCE a month, in ONE long
session, from ONE IP, with ONE rotating cookie jar — then let the
daily runs do only the safe work (API upload with publishAt: OAuth
is identity-based, never IP-flagged).

  HARVEST  (monthly, resumable, self-retrying):  search ~93 fresh
  sources -> download a ~5-min SLICE of each (--download-sections:
  bandwidth sane, runner disk flat) -> clip the best 45-120s window
  -> render the Short -> bank/ folder -> ONE artifact per month
  (free on public repos, auto-expires). Blocked by the wall? The
  guard cron retries daily until it gets through, then the bank is
  simply rebuilt whole next month.

  OWNER RULE (2026-11-14) — STACKING: the month's harvest must
  total >= max_clips (124 = 4/day x 31 — "at least 120 per month",
  exactly as the owner put it). One session can only render ~25
  clips in its ~5h budget, so guard sessions now APPEND to the
  same-month bank (the workflow fetches the existing artifact
  first) until bank.harvested_total >= max_clips. A full month
  (~7 GB) exceeds the 5 GB single-artifact cap, so the bank ships
  as parts bank-<ym>-p1/p2/... (~4.2 GB each) and the daily fetch
  pulls them all with one pattern download.

  PUBLISH  (daily, unchanged grid):  fetch the month's artifact
  parts, pop the next banked clip, upload with publishAt. No
  yt-dlp, no cookies, no bot-check surface at all on daily IPs.

  NEVER STARVES:  bank empty / artifact expired -> the daily run
  falls back to the live ladder exactly as before. The bank is a
  cache, never a single point of failure.

State (data/state.json):
  bank.month       "2026-12"
  bank.clips       remaining clips in slot order — popped as they
                   publish: [{slot, file, n, meta, source_id,
                   source_title, source_channel, clip_start,
                   clip_dur}]
  bank.reserved_n  Short numbers allocated at harvest time so the
                   live fallback can never collide with a banked
                   title
  bank.harvested_total  clips ever banked this month (never drops
                   when clips publish) — the guard's provision meter
"""
from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import crosspost, ledger, metadata, transform, youtube
from .config import ROOT, cfg

POINTER = ROOT / "data" / "bank_pointer.json"


# ── planning ────────────────────────────────────────────────────────

def plan(state: dict, days: int = 31) -> list[str]:
    """Every open grid slot in the next `days` days (ISO, channel tz),
    chronological — booked and already-banked slots skipped."""
    sched = cfg()["schedule"]
    tz = ZoneInfo(sched["timezone"])
    slots = list(sched.get("slots", []))
    booked = set((state.get("slots") or {}).get("short", []) or [])
    banked = {c.get("slot") for c in
              (state.get("bank") or {}).get("clips", [])}
    now = datetime.now(tz)
    lead = timedelta(minutes=int(sched.get("publish_lead_minutes", 25)))
    out: list[str] = []
    for d in range(days):
        base = (now + timedelta(days=d)).date()
        for hhmm in slots:
            hh, mm = map(int, hhmm.split(":"))
            slot_dt = datetime(base.year, base.month, base.day,
                               hh, mm, tzinfo=tz)
            iso = slot_dt.isoformat()
            if slot_dt > now + lead and iso not in booked | banked:
                out.append(iso)
    return out


def open_targets(state: dict) -> list[str]:
    """Open grid slots in (now + lead, next daily fire] — the exact
    set ONE daily session fills (owner rule 2026-11-13: 4 Shorts/day,
    one upload session schedules the whole day).

    Mirrors tools/gate.py's single rule — the gate and the session
    must always agree on what counts as work. Slots beyond the next
    fire are NOT targets: tomorrow's session owns them."""
    sched = cfg()["schedule"]
    tz = ZoneInfo(sched["timezone"])
    slots = list(sched.get("slots", []))
    fire_h, fire_m = map(int,
                         (sched.get("daily_run_utc") or "08:30").split(":"))
    lead = timedelta(minutes=int(sched.get("publish_lead_minutes", 25)))
    now_utc = datetime.now(ZoneInfo("UTC"))
    fire = now_utc.replace(hour=fire_h, minute=fire_m,
                           second=0, microsecond=0)
    if fire <= now_utc + lead:
        fire += timedelta(days=1)
    fire_local = fire.astimezone(tz)
    booked = set((state.get("slots") or {}).get("short", []) or [])
    now = datetime.now(tz)
    probe = now + lead
    out: list[str] = []
    for d in range(2):                       # today + tomorrow is enough
        base = (now + timedelta(days=d)).date()
        for hhmm in slots:
            hh, mm = map(int, hhmm.split(":"))
            slot_dt = datetime(base.year, base.month, base.day,
                               hh, mm, tzinfo=tz)
            iso = slot_dt.isoformat()
            if probe < slot_dt <= fire_local and iso not in booked:
                out.append(iso)
    return sorted(out)


# ── state helpers ───────────────────────────────────────────────────

def reset(state: dict) -> None:
    """Start a clean month bank (rebuild semantics — month rollover
    or an unreachable artifact). harvested_total restarts at 0 and
    climbs with every bank.add until the month is provisioned."""
    ym = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m")
    state["bank"] = {"month": ym, "clips": [], "reserved_n": [],
                     "harvested_total": 0}


def add(state: dict, slot: str, fname: str, meta: dict, source: dict,
        clip_start: float, clip_dur: float) -> None:
    bank = state.setdefault("bank", {})
    bank.setdefault("clips", []).append({
        "slot": slot,
        "file": fname,
        "n": meta["n"],
        "meta": {k: meta[k] for k in ("title", "description", "tags", "n")},
        "source_id": source.get("id", ""),
        "source_title": source.get("title", ""),
        "source_channel": source.get("channel", ""),
        "clip_start": round(float(clip_start), 1),
        "clip_dur": round(float(clip_dur), 1),
    })
    bank.setdefault("reserved_n", []).append(meta["n"])
    # the provision meter: every banked clip counts toward the
    # month's total, even after it publishes (owner rule 2026-11-14)
    bank["harvested_total"] = int(bank.get("harvested_total") or 0) + 1


def take_next(state: dict) -> dict | None:
    """Earliest remaining banked clip (slot order), or None."""
    clips = (state.get("bank") or {}).get("clips") or []
    if not clips:
        return None
    return sorted(clips, key=lambda c: c.get("slot", ""))[0]


def remaining(state: dict) -> int:
    return len((state.get("bank") or {}).get("clips") or [])


# ── artifact side ──────────────────────────────────────────────────

PART_CAP = 4_200_000_000       # bytes; margin under the 5 GB cap


def artifact_name() -> str:
    return "bank-" + datetime.now(ZoneInfo("UTC")).strftime("%Y-%m")


def split_parts(bank_dir: Path, cap_bytes: int = PART_CAP) -> int:
    """Split bank/*.mp4 into bank/p1, bank/p2, ... (~<= cap each) —
    a full month (~7 GB) cannot ride in one 5 GB artifact. The
    manifest + artifact name land in part 1; bank/artifact_parts.txt
    carries the part count for the workflow. Returns the number of
    parts (0 = no clips banked)."""
    files = sorted(bank_dir.glob("*.mp4"))
    if not files:
        return 0
    parts: list[Path] = []
    cur_dir: Path | None = None
    cur_bytes = 0
    for f in files:
        size = f.stat().st_size
        if cur_dir is None or cur_bytes + size > cap_bytes:
            cur_dir = bank_dir / f"p{len(parts) + 1}"
            cur_dir.mkdir(parents=True, exist_ok=True)
            parts.append(cur_dir)
            cur_bytes = 0
        f.rename(cur_dir / f.name)
        cur_bytes += size
    for meta_name in ("manifest.json", "artifact_name.txt"):
        meta = bank_dir / meta_name
        if meta.exists():
            meta.rename(parts[0] / meta_name)
    (bank_dir / "artifact_parts.txt").write_text(str(len(parts)))
    return len(parts)


def write_manifest(bank_dir: Path, state: dict) -> str:
    """bank/manifest.json + bank/artifact_name.txt for the workflow."""
    bank_dir.mkdir(parents=True, exist_ok=True)
    name = artifact_name()
    manifest = {
        "artifact": name,
        "generated": datetime.now(ZoneInfo("UTC")).isoformat(),
        "clips": (state.get("bank") or {}).get("clips", []),
    }
    (bank_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    (bank_dir / "artifact_name.txt").write_text(name)
    return name


# ── the daily publish path ──────────────────────────────────────────

def publish(bank_dir: Path, clip: dict, state: dict,
            dry_run: bool = False) -> int | None:
    """Upload one banked clip into its grid slot. Returns an
    orchestrator exit code, or None when the clip file is missing
    (the caller then falls back to the live ladder)."""
    f = bank_dir / clip["file"]
    if not f.exists() or f.stat().st_size < 1_000_000:
        print(f"[bank] clip file {clip['file']} missing from the "
              "artifact — falling back to the live ladder")
        return None

    meta = dict(clip.get("meta") or {})
    # PUBLISH-TIME TRUTH (live-diagnosed 2026-11-18): the banked meta
    # was baked at harvest time, so clips banked BEFORE a code switch
    # would ship the OLD map code in their description for weeks.
    # The description is rebuilt from the CURRENT channel.yaml; the
    # burned-in bars get the same guarantee via transform.restamp.
    meta = metadata.refresh_description(meta)
    if dry_run:
        print(f"[dry-run] would upload banked short #{meta.get('n')} "
              f"({f.name}, {f.stat().st_size/1e6:.1f} MB) with title "
              f"{meta.get('title')!r}")
        return 0

    print(f"[bank] publishing banked short #{meta.get('n')} "
          f"(slot {clip.get('slot')})")
    # re-burn the code bars with the CURRENT codes (opaque cover of
    # the harvest-time bars) — best-effort, never blocks a publish
    f = transform.restamp_current(f)
    slots_before = len((state.get("slots") or {}).get("short") or [])
    try:
        video_id = youtube.upload_short(f, meta, state)
    except youtube.QuotaDefer:
        return 0                 # clean exit; the grid retries later
    except youtube.AuthError as e:
        print(f"AUTH-ALERT: {e}")
        return 1
    if not video_id:
        print("[upload] deferred (local quota gate) — exiting clean")
        return 0

    # number grows up: reserved -> published
    reserved = (state.get("bank") or {}).get("reserved_n") or []
    if meta.get("n") in reserved:
        reserved.remove(meta["n"])

    today = datetime.now(ZoneInfo(cfg()["schedule"]["timezone"]))
    # the ACTUALLY booked slot, not the clip's planned one — they can
    # drift after heal fills an older open slot, and the reels
    # cross-poster times itself off short.slot
    booked = (state.get("slots") or {}).get("short") or []
    booked_slot = booked[-1] if len(booked) > slots_before else \
        clip.get("slot", "")
    rec = {
        "n": meta.get("n"),
        "source_id": clip.get("source_id", ""),
        "source_title": clip.get("source_title", ""),
        "source_channel": clip.get("source_channel", ""),
        "date": today.date().isoformat(),
        "video_id": video_id,
        "clip_start": clip.get("clip_start", 0),
        "clip_dur": clip.get("clip_dur", 0),
        "title": meta.get("title", ""),
        "slot": booked_slot,
        "banked": True,
    }
    # cache the exact published bytes for the reels cross-poster
    # (owner rule 2026-11-15: same Short -> YT + IG + FB). Never
    # raises — a cache miss only skips this Short's reels.
    crosspost.cache_video(state, rec, f)
    ledger.mark_short_uploaded(state, rec, units=0)   # units spent in upload

    clips = (state.get("bank") or {}).get("clips") or []
    clips[:] = [c for c in clips if c is not clip]
    if not clips:
        state["bank"] = {"month": None, "clips": [], "reserved_n": []}
        POINTER.unlink(missing_ok=True)   # bank empty: pointer retired
        print("[bank] month consumed — back to the live ladder "
              "(guard will refill when due)")
    state["last_run"] = today.date().isoformat()
    state["last_run_ts"] = int(time.time())
    ledger.save(state)
    print(f"\n[done] banked short #{meta.get('n')} published as "
          f"{video_id} — no download, no cookies, no bot wall")
    return 0
