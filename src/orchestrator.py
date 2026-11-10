"""Orchestrator — the master loop. One run = one Short.

  1. memory        ledger.load() (state.json.bak ladder)
  2. gate          quota defer active? exit clean (the grid retries)
  3. find          YouTube Data API search — rotating query, 11-month
                   freshness hard-baked into publishedAfter
  4. download      3-rung ladder (yt-dlp+COOKIES_TXT -> ingest/ ->
                   loud DOWNLOAD-ALERT issue)
  5. clip          scene-change + motion analysis -> best 20-35s
  6. transform     9:16 vertical zoom-crop, overlays burned in,
                   original audio discarded, numpy-synth soundtrack
  7. metadata      codes in title + description + source credit
  8. upload        resumable upload, publishAt = next grid slot
  9. remember      used_sources + slot booking + quota ledger, saved

Exit codes: 0 = published OR cleanly deferred/skipped (the grid
self-heals); 1 = real failure (the workflow files an Issue).
"""
from __future__ import annotations

import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import clipper, downloader, ledger, metadata, music, overlay, sources
from . import transform, verify, youtube
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
        src_path = downloader.download(source, work)
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


if __name__ == "__main__":
    sys.exit(run())
