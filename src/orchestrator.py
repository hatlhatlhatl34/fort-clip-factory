"""Orchestrator — the master loop. One run = one Short.

  1. memory        ledger.load() (state.json.bak ladder)
  2. gate          quota defer active? exit clean (the grid retries)
  3. bank          THE BANK (owner idea 2026-11-11): when the monthly
                   harvest has banked clips, the daily run pops one
                   and uploads it — no yt-dlp, no cookies, no bot
                   wall on daily IPs. Bank empty? live ladder below.
  4. find          YouTube Data API search — rotating query, 11-month
                   freshness hard-baked into publishedAfter
  5. download      4-rung ladder (ingest drops -> yt-dlp+COOKIES_TXT
                   -> archive.org mirror -> archive.org open pool ->
                   loud DOWNLOAD-ALERT issue)
  6. clip          scene-change + motion analysis -> best 45-120s
                   window (owner rule 2026-11-10)
  7. transform     9:16 vertical zoom-crop, overlays burned in,
                   original audio discarded, numpy-synth soundtrack
  8. metadata      codes in title + description (NO source credit —
                   owner rule 2026-11-10)
  9. upload        resumable upload, publishAt = next grid slot
 10. remember      used_sources + slot booking + quota ledger, saved

Exit codes: 0 = published OR cleanly deferred/skipped (the grid
self-heals); 1 = real failure (the workflow files an Issue).
"""
from __future__ import annotations

import os
import random
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import bank, clipper, downloader, ledger, metadata, music
from . import overlay, sources, transform, verify, youtube
from .config import ROOT, cfg
from .rng import seed_for


def run(*, dry_run: bool = False, offline: bool = False,
        sample_out: Path | None = None) -> int:
    state = ledger.load()
    if dry_run:
        ledger.set_read_only(True)

    # ── gate: server-side quota defer ───────────────────────────────
    q = state.get("quota") or {}
    if (q.get("defer_until") or 0) > time.time():
        print(f"[gate] server-side {q.get('defer_reason', 'quota')} defer "
              f"active — exiting clean; grid retries later")
        return 0

    today = datetime.now(ZoneInfo(cfg()["schedule"]["timezone"]))
    day = today.date().isoformat()
    slot_hour = today.hour
    seed = seed_for(day, slot_hour, state.get("query_cursor", 0))
    work = ROOT / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    # ── 0. THE BANK: publish a banked clip when one exists ────────
    # (monthly harvest cache; the daily IP never touches yt-dlp)
    bdir = Path(os.environ.get("BANK_ARTIFACT_DIR", "bank_month"))
    if (bdir / "manifest.json").exists():
        clip = bank.take_next(state)
        if clip:
            rc = bank.publish(bank_dir=bdir, clip=clip, state=state,
                              dry_run=dry_run)
            if rc is not None:
                return rc          # published / deferred cleanly

    # ── 1. find a fresh source (or go offline: ingest/ only) ───────
    if offline:
        source = {"id": "manual-ingest", "title": "manual ingest drop",
                  "channel": "owner", "duration_s": 0, "published_at": day}
        print("[sources] offline mode — using ingest/ folder only")
    else:
        try:
            source = sources.pick_source(state, seed)
        except sources.SourceError as e:
            print(f"SEARCH-ALERT: {e}")
            return 1
        ledger.save(state)

    # ── 2. download it ──────────────────────────────────────────────
    try:
        src_path = downloader.download(source, work, state)
    except downloader.DownloadError as e:
        print(e)
        return 1

    # ── 3. pick the best window in the REAL footage ────────────────
    start, dur = clipper.best_window(src_path, seed)

    # ── 4. overlays + soundtrack + render ───────────────────────────
    top_p, foot_p = overlay.render_overlays(work)
    wav = work / "track.wav"
    music.make_track(wav, dur, seed)

    final_name = sample_out.name if sample_out else \
        f"short_{day.replace('-', '')}_{slot_hour:02d}.mp4"
    final = work / final_name
    transform.render(src_path, start, dur, (top_p, foot_p), wav, final)

    # ── 5. metadata ─────────────────────────────────────────────────
    meta = metadata.build(source, state, seed)

    if sample_out is not None:
        # sample mode: copy to the requested path, no upload
        shutil.copy(final, sample_out)
        print(f"\n[sample] rendered sample Short -> {sample_out}")
        print(f"[sample] title would be: {meta['title']!r}")
        print("[sample] description would be:\n" +
              "\n".join("    " + ln for ln in
                        meta["description"].splitlines()[:6]))
        return 0

    if dry_run:
        print(f"[dry-run] would upload {final.name} "
              f"({final.stat().st_size/1e6:.1f} MB) with title "
              f"{meta['title']!r}")
        return 0

    # ── 6. upload with publishAt booking ────────────────────────────
    try:
        video_id = youtube.upload_short(final, meta, state)
    except youtube.QuotaDefer:
        return 0            # clean exit; defer is already stamped
    except youtube.AuthError as e:
        print(f"AUTH-ALERT: {e}")
        return 1
    if not video_id:
        print("[upload] deferred (local quota gate) — exiting clean")
        return 0

    # ── 7. remember everything ──────────────────────────────────────
    ledger.mark_source_used(state, source["id"])
    ledger.mark_short_uploaded(state, {
        "n": meta["n"],
        "source_id": source["id"],
        "source_title": source.get("title", ""),
        "source_channel": source.get("channel", ""),
        "date": day,
        "video_id": video_id,
        "clip_start": start,
        "clip_dur": dur,
        "title": meta["title"],
    }, units=0)             # units already spent inside upload_short
    state["last_run"] = day
    state["last_run_ts"] = int(time.time())
    ledger.save(state)
    print(f"\n[done] short #{meta['n']} published as {video_id} "
          f"(source {source['id']}, clip {start:.0f}s+{dur:.0f}s)")

    # ── 8. opportunistic verify pass (zero quota) ──────────────────
    verify.run(state)
    return 0


# ── THE MONTHLY HARVEST (owner idea 2026-11-11) ─────────────────────

def harvest(count: int | None = None,
            budget_minutes: float | None = None) -> int:
    """One IP, one session, once a month: search fresh sources,
    download a ~5-min slice of each, clip + render ~93 Shorts, and
    bank them into a single GitHub artifact. The daily machine then
    publishes one banked clip per grid slot (pure API upload —
    never IP-flagged). Blocked by the bot wall? Exit clean and let
    the guard cron retry tomorrow; daily publishing continues via
    the live ladder meanwhile. A retry rebuilds the whole month bank
    from scratch (rebuild semantics — see bank.py docstring)."""
    state = ledger.load()
    hconf = cfg().get("harvest") or {}
    days_ahead = int(hconf.get("days_ahead", 31))
    plan_slots = bank.plan(state, days=days_ahead)
    target = int(count or hconf.get("max_clips", 93))
    target = min(target, len(plan_slots))
    budget = float(budget_minutes or hconf.get("budget_minutes", 280))
    deadline = time.monotonic() + budget * 60

    if target <= 0:
        print("[harvest] the bank already covers the window — nothing "
              "to do")
        return 0
    print(f"[harvest] banking {target} Shorts for the next {days_ahead} "
          "days: ONE IP, ONE session, sliced downloads, human pacing")

    # fresh month bank (rebuild semantics — reset, not append)
    bank.reset(state)

    # candidates: oversample 2.5x (some will fail the wall)
    today = datetime.now(ZoneInfo(cfg()["schedule"]["timezone"]))
    seed = seed_for(today.date().isoformat(), 0,
                    state.get("query_cursor", 0))
    rng = random.Random(seed ^ 0xBEEF)
    need = target * 2 + 4
    candidates: list[dict] = []
    for attempt in range(8):
        if len(candidates) >= need:
            break
        try:
            batch = sources.search_sources(state, seed + attempt)
        except sources.SourceError as e:
            print(f"[harvest] search attempt {attempt+1}: {e}")
            if "credential" in str(e).lower():
                break          # env cannot change mid-run — stop hammering
            continue
        for c in batch:
            if c.get("id") not in {x["id"] for x in candidates}:
                candidates.append(c)
    if not candidates:
        print("HARVEST-ALERT: no candidates found — guard retries "
              "tomorrow 07:00 UTC; daily publishing continues live")
        return 0
    print(f"[harvest] {len(candidates)} fresh candidates pooled")

    # the long session
    work = ROOT / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    bank_dir = ROOT / "bank"
    shutil.rmtree(bank_dir, ignore_errors=True)
    bank_dir.mkdir(parents=True, exist_ok=True)
    top_p, foot_p = overlay.render_overlays(work)

    slice_len = int(hconf.get("slice_s", 300))
    pace_lo, pace_hi = [float(x) for x in hconf.get("pacing_s", (10, 35))]
    banked = 0
    for slot, source in zip(plan_slots, candidates * 5):
        if banked >= target:
            break
        if time.monotonic() + 240 > deadline:
            print("[harvest] out of session budget — the guard resumes "
                  "tomorrow (the partial bank still serves the grid)")
            break
        if source.get("id") in ledger.used_sources(state):
            continue
        dur = float(source.get("duration_s") or 0)
        max_start = int(max(0, dur - slice_len - 30))
        slice_start = rng.randint(30, max_start) if max_start > 30 else 0

        path = downloader.download_slice(source, work, slice_start,
                                         slice_len)
        if path is None:
            continue
        try:
            start, cdur = clipper.best_window(path, seed + banked)
            wav = work / "track.wav"
            music.make_track(wav, cdur, seed + banked)
            fname = (f"short_{slot[:16].replace('-', '')
                     .replace('T', '_').replace(':', '')}.mp4")
            final = bank_dir / fname
            transform.render(path, start, cdur, (top_p, foot_p), wav,
                             final)
            meta = metadata.build(source, state, seed + banked)
        except Exception as e:
            print(f"[harvest] candidate skipped: {e}")
            path.unlink(missing_ok=True)
            continue

        ledger.mark_source_used(state, source["id"])
        bank.add(state, slot, fname, meta, source,
                 clip_start=slice_start + start, clip_dur=cdur)
        ledger.save(state)        # crash-safe: every banked clip is saved
        path.unlink(missing_ok=True)
        banked += 1
        print(f"[harvest] banked {banked}/{target} for slot {slot} — "
              f"{str(source.get('title', ''))[:60]!r}")
        time.sleep(rng.uniform(pace_lo, pace_hi))   # human-ish pacing

    bank.write_manifest(bank_dir, state)
    if banked == 0:
        print("HARVEST-ALERT: 0 clips banked — the bot wall held today. "
              "The guard retries tomorrow 07:00 UTC; daily publishing "
              "continues from the live ladder meanwhile.")
        return 0
    print(f"\n[harvest] DONE: {banked}/{target} Shorts banked — the "
          "artifact upload follows; daily runs publish them one slot "
          "at a time (no yt-dlp on daily IPs ever again)")
    return 0


if __name__ == "__main__":
    sys.exit(run())
