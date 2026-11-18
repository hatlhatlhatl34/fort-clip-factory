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

Phases (idempotent, state["one_time_fix"] is the progress marker —
every re-run costs nothing and never repeats finished work):

  delete      DELETE the four old-bank videos (videos.delete, 50
              units each, spent honestly). Ids already in the marker
              are skipped at zero cost.
  clean       ledger surgery: drop the four short records, unbook
              their four grid slots (they refill with FRESH clips),
              stats recount.
  replacement upload ONE fresh replacement Short, PUBLIC immediately
              (privacyStatus=public, no publishAt — "the first one
              to be uploaded make it public"). BANK FIRST: if the
              same-month bank is ready, the earliest banked clip is
              popped (no bot-wall risk); else the live ladder tries
              fresh sources (a filtered query rotates to the next
              phrasing instead of aborting). Quota-gated: under the
              floor the upload is skipped for today — the owner's
              one-time quota-skip rule — and the scheduled 08:07 UTC
              re-run (right after the Pacific reset, before the
              08:33 session) lands the FIRST new upload PUBLIC.

The remaining freed slots refill via the machine itself (hourly
heals + the 08:33 session from the fresh same-month bank). Every
quota-blocked attempt stops CLEAN at the floor — exactly the
owner's "skip the rest today" rule.

Usage:
  python tools/replace_shorts.py                # all phases
  python tools/replace_shorts.py --phases delete,clean
  python tools/replace_shorts.py --dry-run       # rehearse, no writes

One-time: the workflow fires 2026-11-19 08:07 UTC (the quota reset
slot); later fires are guarded no-ops via the state marker.
"""
from __future__ import annotations

import argparse
import os
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
MAX_LADDER_TRIES = 3       # fresh-source attempts (query rotates each)


def _marker(state: dict) -> dict:
    return state.setdefault("one_time_fix", {})


# ── phase 1: delete the old-bank videos ─────────────────────────────
def phase_delete(state: dict, dry_run: bool = False) -> None:
    print("== phase 1: DELETE the four old-bank Shorts from YouTube ==")
    mark = _marker(state)
    done = set(mark.get("deleted") or [])
    # durable zero-cost guard: an id already absent from the ledger was
    # deleted + cleaned by this fix (or never existed) — re-fires (even
    # a year out) never re-spend the delete calls
    ledger_vids = {s.get("video_id") for s in state.get("shorts") or []}
    token = None
    for s in OLD_SHORTS:
        vid = s["video_id"]
        if vid in done:
            print(f"  n={s['n']} {vid}: already deleted (marker) — "
                  "zero cost")
            continue
        if vid not in ledger_vids:
            print(f"  n={s['n']} {vid}: already gone from the ledger — "
                  "zero cost (deletion is permanent on YouTube)")
            done.add(vid)
            continue
        if dry_run:
            print(f"  n={s['n']} {vid}: [dry-run] would DELETE")
            continue
        if token is None:
            token = youtube.get_access_token()
        print(f"  deleting n={s['n']} {vid} ...", end=" ", flush=True)
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
                done.add(vid)          # gone is gone — never re-call it
            else:
                # 403 = not ours / deletions disabled: report, keep going
                print(f"HTTP {e.code}: {body}")
        ledger.spend_quota(state, DELETE_UNITS)   # honest accounting
    mark["deleted"] = sorted(done)
    if not dry_run:
        ledger.save(state)
    print(f"  deletes settled: {len(done)}/{len(OLD_SHORTS)} "
          f"(marker remembers — future runs cost zero)")


# ── phase 2: ledger surgery ──────────────────────────────────────────
def phase_clean(state: dict, dry_run: bool = False) -> None:
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
    _marker(state)["cleaned"] = True

    if not dry_run:
        ledger.save(state)
    print(f"  short records removed: {removed}; slots unbooked: {freed}")
    if removed:
        print("  their pending IG/FB/TikTok crossposts died with the "
              "records — no dead-link reels will ever ship")


# ── phase 3: ONE fresh replacement Short, PUBLIC now ────────────────
def phase_replacement(state: dict, dry_run: bool = False) -> int:
    print("== phase 3: upload ONE fresh replacement Short, PUBLIC now ==")
    mark = _marker(state)
    # durable guard (survives state merges): a short already marked as
    # the replacement means the one-time deal is fulfilled — forever
    if mark.get("replacement") or any(s.get("replacement")
                                      for s in state.get("shorts") or []):
        r = mark.get("replacement") or {
            "n": next(s.get("n") for s in state.get("shorts") or []
                      if s.get("replacement")),
            "ts": "(ledger)"}
        print(f"  already replaced by short #{r.get('n')} on {r.get('ts')} "
              "— nothing to do, ever again")
        return 1

    remaining = ledger.quota_remaining(state)
    if remaining < youtube.UPLOAD_UNITS + 200:
        print(f"  quota at its full limit ({remaining} units left) — "
              "skipping today's replacement upload (owner rule "
              "2026-11-18 #4: the one-time quota skip); the scheduled "
              "re-run after the Pacific reset lands it PUBLIC")
        return 0

    today = datetime.now(ZoneInfo(cfg()["schedule"]["timezone"]))
    day = today.date().isoformat()
    mark["attempt_date"] = day

    # ── BANK FIRST: the fresh same-month bank is the designed source ─
    bdir = ROOT / os.environ.get("BANK_ARTIFACT_DIR", "bank_month")
    if (bdir / "manifest.json").exists() and not bank.stale(state):
        clip = bank.take_next(state)
        if clip:
            print(f"  [bank] popping the earliest banked clip "
                  f"#{(clip.get('meta') or {}).get('n')} "
                  f"(slot {clip.get('slot')}) — no bot wall, no download")
            f = bdir / clip["file"]
            if f.exists() and f.stat().st_size >= 1_000_000:
                meta = dict(clip.get("meta") or {})
                meta = metadata.refresh_description(meta)
                if dry_run:
                    print(f"  [dry-run] would upload {f.name} "
                          f"({f.stat().st_size / 1e6:.1f} MB) PUBLIC now "
                          f"with title {meta.get('title')!r}")
                    return 0
                f = transform.restamp_current(f)   # current code bars
                video_id = _upload_public_now(f, meta, state)
                if video_id:
                    _finish_banked(state, clip, f, video_id, day, mark)
                    return 1
                print("  upload deferred (quota) — clean skip; the "
                      "re-run after the Pacific reset retries")
                return 0
            print("  [bank] clip file missing — the live ladder takes "
                  "over for this one")

    # ── live ladder: a fresh niche source, sliced + rendered here ────
    seed = seed_for(day, today.hour, state.get("query_cursor", 0))
    rng = random.Random(seed ^ 0xF17E)
    work = ROOT / "work"
    work.mkdir(parents=True, exist_ok=True)
    slice_len = int((cfg().get("harvest") or {}).get("slice_s", 300))

    for attempt in range(1, MAX_LADDER_TRIES + 1):
        if ledger.quota_remaining(state) < youtube.UPLOAD_UNITS + 200:
            print("  quota floor reached mid-ladder — clean skip; the "
                  "scheduled re-run retries after the Pacific reset")
            return 0
        print(f"  [ladder] fresh-source attempt {attempt}/"
              f"{MAX_LADDER_TRIES} (query rotates on every retry)")
        try:
            source = sources.pick_source(state, seed + attempt * 17)
        except sources.SourceError as e:
            # a filtered-out query phrasing is NOT fatal — the cursor
            # already rotated, the next attempt mines the next phrasing
            print(f"  {e} — rotating to the next query phrasing")
            continue
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
            print("  upload deferred (quota) — clean skip; the "
                  "scheduled re-run retries after the Pacific reset")
            return 0

        ledger.mark_source_used(state, source["id"])
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
            "slot": datetime.now(
                ZoneInfo(cfg()["schedule"]["timezone"])).isoformat(),
            "public_now": True,       # the one-time owner rule marker
            "replacement": True,
        }
        ledger.mark_short_uploaded(state, rec, units=0)
        state["last_run"] = day
        state["last_run_ts"] = int(time.time())
        ledger.save(state)
        crosspost.cache_video(state, rec, final)   # reels ride the cache
        ledger.save(state)
        mark["replacement"] = {"n": n, "video_id": video_id,
                              "ts": datetime.now(
                                  ZoneInfo("UTC")).isoformat()}
        ledger.save(state)
        print(f"\n  [done] replacement short #{n} PUBLIC NOW as "
              f"{video_id} — {meta['title']!r}")
        print("  remaining freed slots refill via the machine "
              "(heals + the 08:33 session from the fresh bank); "
              "quota-full skips are clean, per the one-time owner rule")
        return 1
    print("  no fresh source survived the ladder — the scheduled "
          "re-run retries after the Pacific reset")
    return 0


def _finish_banked(state: dict, clip: dict, f: Path, video_id: str,
                   day: str, mark: dict) -> None:
    """Accounting after a banked clip went PUBLIC (bank.publish's
    post-upload bookkeeping, adapted for the public-now path)."""
    meta = dict(clip.get("meta") or {})
    now_iso = datetime.now(
        ZoneInfo(cfg()["schedule"]["timezone"])).isoformat()
    rec = {
        "n": meta.get("n"),
        "source_id": clip.get("source_id", ""),
        "source_title": clip.get("source_title", ""),
        "source_channel": clip.get("source_channel", ""),
        "date": day,
        "video_id": video_id,
        "clip_start": clip.get("clip_start", 0),
        "clip_dur": clip.get("clip_dur", 0),
        "title": meta.get("title", ""),
        "slot": now_iso,
        "banked": True,
        "public_now": True,           # the one-time owner rule marker
        "replacement": True,
    }
    ledger.mark_short_uploaded(state, rec, units=0)
    crosspost.cache_video(state, rec, f)

    bankst = state.setdefault("bank", {})
    bankst["clips"] = [c for c in bankst.get("clips") or []
                       if c.get("file") != clip.get("file")]
    rn = bankst.setdefault("reserved_n", [])
    if clip.get("n") in rn:
        rn.remove(clip["n"])
    if not bankst.get("clips"):
        bankst["clips"] = []
        bankst["reserved_n"] = []
        print("[bank] drained — the guard tops the month's emergency "
              "buffer back up")
    state["last_run"] = day
    state["last_run_ts"] = int(time.time())
    mark["replacement"] = {"n": meta.get("n"), "video_id": video_id,
                           "ts": datetime.now(
                               ZoneInfo("UTC")).isoformat()}
    ledger.save(state)
    print(f"\n  [done] replacement short #{meta.get('n')} PUBLIC NOW "
          f"as {video_id} — {meta.get('title')!r}")


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
          "left today")
    print(f"[replace] marker: {_marker(state)}\n")

    if "delete" in phases:
        phase_delete(state, dry_run=args.dry_run)
    if "clean" in phases:
        phase_clean(state, dry_run=args.dry_run)
    if "replacement" in phases:
        phase_replacement(state, dry_run=args.dry_run)
    if not args.dry_run:
        ledger.save(state)
    print("\n[replace] DONE — old-bank poison off the channel, "
          "fresh blood public (or cleanly skipped on a full quota day)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
