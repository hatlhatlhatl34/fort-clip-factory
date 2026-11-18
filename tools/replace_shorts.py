#!/usr/bin/env python3
"""OWNER RULE (2026-11-18 #4) — THE 4-OLD-SHORTS BUG: one-time fix.

What happened: the purge of the FIRST OLD BANK (owner rule #3) landed
2026-11-18 AFTER the 08:33 UTC session had already published that
day's FOUR Shorts from the old bank's random-footage clips. Four
old-bank (poison) Shorts went onto the channel one last time — that
is the "4 old shorts bug". The owner's one-time deal (verbatim intent,
2026-11-18): delete them, upload new ones, the FIRST one uploaded
PUBLIC, and if the YouTube quota hits its full limit it is fine to
skip the 4-shorts rule for today — ONLY because the quota exceeded.

Phases (idempotent — safe to re-run; already-done work is skipped):

  delete      DELETE the four old-bank videos from YouTube
              (videos.delete, 50 units each, spent honestly).
  clean       ledger surgery: drop the four short records, unbook
              their four grid slots (the freed slots refill with
              FRESH niche clips), stats recount, memory saved.
  replacement upload ONE fresh replacement Short, PUBLIC
              immediately (privacyStatus=public, no publishAt —
              "the first one to be uploaded make it public"). Live
              ladder: fresh niche source -> slice download -> best
              window -> 9:16 render with current code bars -> upload.
              Quota-gated: under the floor the upload is skipped for
              today (the owner's one-time quota-skip rule) and the
              grid retries on the next session.

The remaining freed slots are healed by the machine itself: the
hourly heal + tomorrow's 08:33 session publish from the fresh
same-month bank (monthly-bank-harvest guards stack it). When the
quota is at its limit every one of those sessions stops CLEAN at the
quota floor — exactly the owner's "skip the rest today" rule; from
the next Pacific-day reset the grid is back to 4/day.

Usage:
  python tools/replace_shorts.py                # all phases
  python tools/replace_shorts.py --phases delete,clean
  python tools/replace_shorts.py --dry-run      # rehearse, no writes

One-time: dispatch-only (replace-shorts.yml), never a cron.
"""
from __future__ import annotations

import argparse
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import (bank, clipper, crosspost, downloader, ledger, metadata,  # noqa: E402
                 music, overlay, sources, transform, youtube)
from src.config import cfg  # noqa: E402
from src.rng import seed_for  # noqa: E402

# ── the four old-bank Shorts that went live 2026-11-18 ───────────────
# (frozen video ids + their booked grid slots; from the purge report)
OLD_SHORTS = [
    {"n": 31, "video_id": "5ysmLlL7U9k",          # 12:00 Cairo — was public
     "slot": "2026-11-18T12:00:00+02:00"},
    {"n": 51, "video_id": "Wp1mvU-dtDw",          # 18:00 Cairo — premiere
     "slot": "2026-11-18T18:00:00+02:00"},
    {"n": 32, "video_id": "Jw1QpIN3Xf8",          # 00:00 Cairo tomorrow
     "slot": "2026-11-19T00:00:00+02:00"},
    {"n": 33, "video_id": "-Vwdn3bk8uA",          # 06:00 Cairo tomorrow
     "slot": "2026-11-19T06:00:00+02:00"},
]
DELETE_UNITS = 50          # videos.delete listed cost, per video
MAX_LADDER_TRIES = 3       # fresh-source attempts for the replacement


# ── phase 1: delete the old-bank videos ─────────────────────────────
def phase_delete(state: dict, dry_run: bool = False) -> int:
    print("== phase 1: DELETE the four old-bank Shorts from YouTube ==")
    token = youtube.get_access_token()
    deleted = gone = 0
    for s in OLD_SHORTS:
        vid = s["video_id"]
        print(f"  deleting n={s['n']} {vid} ...", end=" ", flush=True)
        if dry_run:
            print("[dry-run] skipped")
            continue
        req = urllib.request.Request(
            f"{youtube.API_BASE}/videos?id={vid}", method="DELETE",
            headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30):
                print("DELETED")
                deleted += 1
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:160]
            if e.code == 404:
                print("already gone (404)")
                gone += 1
            else:
                # 403 = not ours / deletions disabled: report, never crash
                print(f"HTTP {e.code}: {body}")
        ledger.spend_quota(state, DELETE_UNITS)   # honest accounting
    if not dry_run:
        ledger.save(state)
    print(f"  deletes: {deleted} removed, {gone} already gone "
          f"({len(OLD_SHORTS) * DELETE_UNITS} quota units spent)")
    return deleted + gone


# ── phase 2: ledger surgery ──────────────────────────────────────────
def phase_clean(state: dict, dry_run: bool = False) -> int:
    print("== phase 2: clean the ledger (drop records, free slots) ==")
    vids = {s["video_id"] for s in OLD_SHORTS}
    kill_slots = {s["slot"] for s in OLD_SHORTS}
    before = len(state.get("shorts") or [])
    state["shorts"] = [s for s in state.get("shorts") or []
                       if s.get("video_id") not in vids]
    removed = before - len(state["shorts"])

    box = (state.get("slots") or {}).get("short") or []
    kept = [x for x in box if x not in kill_slots]
    freed = len(box) - len(kept)
    state.setdefault("slots", {})["short"] = kept

    st = state.setdefault("stats", {})
    st["shorts_published"] = max(0, int(st.get("shorts_published", 0))
                                  - removed)

    if removed or freed:
        if dry_run:
            ledger.set_read_only(True)
        ledger.save(state)
        ledger.set_read_only(False)
    print(f"  short records removed: {removed}; slots unbooked: {freed}")
    if removed:
        print("  their pending IG/FB/TikTok crossposts died with the "
              "records — no dead-link reels will ever ship")
    return removed


# ── phase 3: ONE fresh replacement Short, PUBLIC now ────────────────
def phase_replacement(state: dict, dry_run: bool = False) -> int:
    print("== phase 3: upload ONE fresh replacement Short, PUBLIC now ==")
    remaining = ledger.quota_remaining(state)
    if remaining < youtube.UPLOAD_UNITS + 200:
        print(f"  quota at its full limit ({remaining} units left) — "
              "skipping today's replacement upload too (owner rule "
              "2026-11-18 #4: the one-time quota skip); the grid "
              "retries on the next session")
        return 0

    today = datetime.now(ZoneInfo(cfg()["schedule"]["timezone"]))
    day = today.date().isoformat()
    seed = seed_for(day, today.hour, state.get("query_cursor", 0))
    rng = random.Random(seed ^ 0xF17E)
    work = ROOT / "work"
    work.mkdir(parents=True, exist_ok=True)
    slice_len = int((cfg().get("harvest") or {}).get("slice_s", 300))

    for attempt in range(1, MAX_LADDER_TRIES + 1):
        if ledger.quota_remaining(state) < youtube.UPLOAD_UNITS + 200:
            print("  quota floor reached mid-ladder — clean skip today")
            return 0
        print(f"  [ladder] fresh-source attempt {attempt}/"
              f"{MAX_LADDER_TRIES}")
        try:
            source = sources.pick_source(state, seed + attempt * 17)
        except sources.SourceError as e:
            print(f"  SEARCH-ALERT: {e}")
            return 0
        ledger.save(state)

        dur = float(source.get("duration_s") or 0)
        max_start = int(max(0, dur - slice_len - 30))
        slice_start = rng.randint(30, max_start) if max_start > 30 else 0
        try:
            path = downloader.download_slice(source, work, slice_start,
                                              slice_len)
        except downloader.DownloadError as e:
            print(f"  {e}")
            continue
        if path is None:
            continue

        try:
            start, cdur = clipper.best_window(path, seed + attempt)
            top_p, foot_p = overlay.render_overlays(work)
            wav = work / "track.wav"
            music.make_track(wav, cdur, seed + attempt)
            meta = metadata.build(source, state, seed + attempt)
        except Exception as e:  # noqa: BLE001
            print(f"  render pipeline failed on this source ({e}) — "
                  "next source")
            continue

        n = meta["n"]
        final = work / f"short_{n}.mp4"
        try:
            transform.render(path, start, cdur, (top_p, foot_p), wav,
                             final)
        except Exception as e:  # noqa: BLE001
            print(f"  transform failed ({e}) — next source")
            continue

        if dry_run:
            print(f"  [dry-run] would upload {final.name} "
                  f"({final.stat().st_size / 1e6:.1f} MB) PUBLIC now "
                  f"with title {meta['title']!r}")
            return 0

        video_id = _upload_public_now(final, meta, state)
        if not video_id:
            print("  upload deferred (quota) — clean skip today")
            return 0

        ledger.mark_source_used(state, source["id"])
        now_iso = datetime.now(
            ZoneInfo(cfg()["schedule"]["timezone"])).isoformat()
        rec = {
            "n": n,
            "source_id": source.get("id", ""),
            "source_title": source.get("title", ""),
            "source_channel": source.get("channel", ""),
            "date": day,
            "video_id": video_id,
            "clip_start": slice_start + start,
            "clip_dur": cdur,
            "title": meta["title"],
            "slot": now_iso,          # crossposter times off this
            "public_now": True,       # the one-time owner rule marker
            "replacement": True,
        }
        ledger.mark_short_uploaded(state, rec, units=0)
        state["last_run"] = day
        state["last_run_ts"] = int(time.time())
        ledger.save(state)
        crosspost.cache_video(state, rec, final)   # reels ride the cache
        ledger.save(state)
        print(f"\n  [done] replacement short #{n} PUBLIC NOW as "
              f"{video_id} — {meta['title']!r}")
        print("  remaining freed slots refill via the machine "
              "(heals + tomorrow's session from the fresh bank); "
              "quota-full skips are clean, per the one-time owner rule")
        return 1
    print("  no fresh source survived the ladder — the heals retry; "
          "nothing else spent")
    return 0


def _upload_public_now(final: Path, meta: dict,
                       state: dict) -> str | None:
    """Upload with privacyStatus=public and NO publishAt — live the
    second YouTube finishes ingesting. Quota-defer aware."""
    if ledger.quota_remaining(state) < youtube.UPLOAD_UNITS + 200:
        return None
    token = youtube.get_access_token()
    yconf = cfg()["youtube"]
    meta = dict(meta)
    meta.pop("chapters", None)
    meta.setdefault("categoryId", str(yconf.get("category_id", "20")))
    meta.setdefault("defaultLanguage", yconf.get("default_language", "en"))
    meta.setdefault("defaultAudioLanguage",
                   yconf.get("default_language", "en"))
    status = {
        "privacyStatus": "public",          # THE owner rule: first one
        "madeForKids": bool(yconf.get("made_for_kids", False)),
        "selfDeclaredMadeForKids": bool(yconf.get("made_for_kids", False)),
    }
    variants = [meta]
    if meta.get("tags"):
        variants.append({k: v for k, v in meta.items() if k != "tags"})
    for variant in variants:
        try:
            video_id = youtube._upload_video(token, final, variant, status)
        except RuntimeError as e:
            reason = youtube._error_reason(str(e))
            if reason == "invalidTags" and variant is not variants[-1]:
                print("  [youtube] tags rejected — retrying without tags")
                continue
            if reason in (youtube.QUOTA_DAY_REASONS
                          + youtube.QUOTA_ROLLING_REASONS):
                print(f"  [quota] {reason} — quota at its full limit; "
                      "clean skip today (owner one-time rule)")
                return None
            raise
        if video_id:
            ledger.spend_quota(state, youtube.UPLOAD_UNITS)
            ledger.save(state)
            return video_id
    return None


# ─── main ────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--phases", default="delete,clean,replacement",
                    help="comma list: delete,clean,replacement")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    phases = [p.strip() for p in args.phases.split(",") if p.strip()]

    if args.dry_run:
        ledger.set_read_only(True)

    state = ledger.load()
    print(f"[replace] one-time fix for the 4-old-shorts bug — "
          f"phases: {', '.join(phases)}"
          + (" (dry-run)" if args.dry_run else ""))
    print(f"[replace] quota: {ledger.quota_remaining(state)} units "
          "left today\n")

    rc = 0
    if "delete" in phases:
        phase_delete(state, dry_run=args.dry_run)
    if "clean" in phases:
        phase_clean(state, dry_run=args.dry_run)
    if "replacement" in phases:
        if not phase_replacement(state, dry_run=args.dry_run):
            rc = 0 if not args.dry_run else 0
    if not args.dry_run:
        ledger.save(state)
    print("\n[replace] DONE — old-bank poison off the channel, "
          "fresh blood public")
    return rc


if __name__ == "__main__":
    sys.exit(main())
