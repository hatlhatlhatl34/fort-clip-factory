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

  PUBLISH  (daily, unchanged grid):  fetch the month's artifact,
  pop the next banked clip, upload with publishAt. No yt-dlp, no
  cookies, no bot-check surface at all on daily IPs.

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
"""
from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import ledger, youtube
from .config import ROOT, cfg

POINTER = ROOT / "data" / "bank_pointer.json"


# ── planning ────────────────────────────────────────────────────────

def plan(state: dict, days: int = 31) -> list[str]:
    """Every open grid slot in the next `days` days (ISO, channel tz),
    chronological — booked and already-banked slots skipped."""
    sched = cfg()["schedule"]
    tz = ZoneInfo(sched["timezone"])
    slots = list(sched.get("slots", []))
    weekend = list(sched.get("weekend_slots") or [])
    wdays = {int(d) for d in (sched.get("weekend_days") or [])}
    booked = set((state.get("slots") or {}).get("short", []) or [])
    banked = {c.get("slot") for c in
              (state.get("bank") or {}).get("clips", [])}
    now = datetime.now(tz)
    lead = timedelta(minutes=int(sched.get("publish_lead_minutes", 25)))
    out: list[str] = []
    for d in range(days):
        base = (now + timedelta(days=d)).date()
        for hhmm in slots + (weekend if base.weekday() in wdays else []):
            hh, mm = map(int, hhmm.split(":"))
            slot_dt = datetime(base.year, base.month, base.day,
                               hh, mm, tzinfo=tz)
            iso = slot_dt.isoformat()
            if slot_dt > now + lead and iso not in booked | banked:
                out.append(iso)
    return out


# ── state helpers ───────────────────────────────────────────────────

def reset(state: dict) -> None:
    """Start a clean month bank (rebuild semantics — a resumed harvest
    rebuilds whole rather than juggling multiple artifacts)."""
    ym = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m")
    state["bank"] = {"month": ym, "clips": [], "reserved_n": []}


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


def take_next(state: dict) -> dict | None:
    """Earliest remaining banked clip (slot order), or None."""
    clips = (state.get("bank") or {}).get("clips") or []
    if not clips:
        return None
    return sorted(clips, key=lambda c: c.get("slot", ""))[0]


def remaining(state: dict) -> int:
    return len((state.get("bank") or {}).get("clips") or [])


# ── artifact side ──────────────────────────────────────────────────

def artifact_name() -> str:
    return "bank-" + datetime.now(ZoneInfo("UTC")).strftime("%Y-%m")


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
    if dry_run:
        print(f"[dry-run] would upload banked short #{meta.get('n')} "
              f"({f.name}, {f.stat().st_size/1e6:.1f} MB) with title "
              f"{meta.get('title')!r}")
        return 0

    print(f"[bank] publishing banked short #{meta.get('n')} "
          f"(slot {clip.get('slot')})")
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
    ledger.mark_short_uploaded(state, {
        "n": meta.get("n"),
        "source_id": clip.get("source_id", ""),
        "source_title": clip.get("source_title", ""),
        "source_channel": clip.get("source_channel", ""),
        "date": today.date().isoformat(),
        "video_id": video_id,
        "clip_start": clip.get("clip_start", 0),
        "clip_dur": clip.get("clip_dur", 0),
        "title": meta.get("title", ""),
        "slot": clip.get("slot", ""),
        "banked": True,
    }, units=0)                  # units already spent inside upload

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
