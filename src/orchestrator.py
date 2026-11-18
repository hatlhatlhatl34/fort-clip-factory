"""Orchestrator — the master loop. ONE daily session = the whole day.

OWNER RULE (2026-11-13): 4 Shorts/day, ONE upload session. The daily
fire (08:30 UTC) wakes the machine ONCE; it uploads all FOUR Shorts
in that single session, each with publishAt = its grid slot
(00:00/08:00/16:00/20:00 Cairo). No more 4 runs/day — one wake-up
schedules the whole day, exactly as the owner asked.

  1. memory        ledger.load() (state.json.bak ladder)
  2. gate          quota defer active? exit clean (the grid retries)
  3. targets       bank.open_targets() — every open grid slot before
                   the next daily fire (mirrors tools/gate.py's rule)
  4. per target    bank first (banked clip from the monthly harvest:
                   no yt-dlp, no cookies, no bot wall) else the live
                   ladder: find -> download (4-rung) -> clip the best
                   45-120s window -> transform (9:16 + overlays +
                   synth track) -> metadata -> upload with publishAt
  5. remember      used_sources + slot booking + quota ledger, saved
                   after EVERY short (crash-safe)
  6. verify        oEmbed pass at the end (zero quota)

Exit codes: 0 = published/deferred/skipped cleanly (the grid
self-heals); 1 = real failure with nothing produced (the workflow
files an Issue).
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
from . import crosspost, overlay, sources, transform, verify, youtube
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
    base_seed = seed_for(day, slot_hour, state.get("query_cursor", 0))
    work = ROOT / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    # today's reel-media cache starts clean — yesterday's bytes already
    # rode their own artifact, and stale files would double every upload
    shutil.rmtree(ROOT / "media_cache", ignore_errors=True)

    # ── the session: every open slot before the next daily fire ────
    targets = bank.open_targets(state)
    if not targets:
        print("[grid] fully booked until the next session — "
              "nothing to do")
        verify.run(state)
        return 0
    print(f"[session] {len(targets)} open slot(s) to fill: "
          f"{', '.join(targets)}")

    # ── OWNER RULE (2026-11-18): old bank clips are POISON ──────────
    # "harvest all the clips in the beginning of the month so the
    # machine don't use the old bank clips" — a leftover bank from
    # a previous month must never feed this month's slots. Retire it
    # BEFORE any manifest check: even if the daily workflow fetched
    # the stale artifact (bank_month/manifest.json present), nothing
    # is popped from it.
    if bank.stale(state):
        bank.retire(state,
                    f"bank.month={(state.get('bank') or {}).get('month')} "
                    f"!= current month {bank.current_month()}")
        ledger.save(state)

    # C12 (audit 2026-11-18): ROOT-anchored like every other path —
    # running main.py from another directory must never silently miss
    # the bank and fall to the bot-wall ladder
    bdir = ROOT / os.environ.get("BANK_ARTIFACT_DIR", "bank_month")
    published = 0
    hard_failures = 0

    for i, _slot in enumerate(targets):
        if i >= int(cfg()["clip"].get("max_clips_per_run", 4)):
            break

        # session-level quota guards — stop CLEAN, the grid retries
        q = state.get("quota") or {}
        if (q.get("defer_until") or 0) > time.time():
            print("[session] server quota defer stamped — "
                  "stopping the session clean")
            break
        if ledger.quota_remaining(state) < 1800:
            print("[session] local quota floor reached — stopping "
                  "the session clean (the grid retries at the next "
                  "fire)")
            break

        seed = base_seed + i * 31

        # C6 (audit 2026-11-18): the poison check runs per TARGET — a
        # session crossing the UTC month boundary must never publish a
        # just-turned-stale clip (owner rule: old bank clips are poison)
        if bank.stale(state):
            bank.retire(state, f"month rollover mid-session "
                        f"(bank.month={(state.get('bank') or {}).get('month')}")
            ledger.save(state)

        # ── 0. THE BANK: publish a banked clip when one exists ────
        # (monthly harvest cache; the daily IP never touches yt-dlp)
        if (bdir / "manifest.json").exists():
            clip = bank.take_next(state)
            if clip:
                rc = bank.publish(bank_dir=bdir, clip=clip, state=state,
                                  dry_run=dry_run)
                if rc is None:
                    print("[bank] clip unusable — live ladder for "
                          "this slot")
                elif rc == 0:
                    published += 1
                    continue
                elif rc == 3:
                    # C9 (audit 2026-11-18): a clean defer (quota) is NOT
                    # a publish — stop the session honestly, uncounted
                    print("[bank] clean defer — stopping the session")
                    break
                else:
                    return rc          # real failure -> alert

        # ── live ladder: produce ONE Short for this slot ───────────
        # 1. find a fresh source (or go offline: ingest/ only)
        if offline:
            source = {"id": "manual-ingest", "title": "manual ingest drop",
                      "channel": "owner", "duration_s": 0,
                      "published_at": day}
            print("[sources] offline mode — using ingest/ folder only")
        else:
            try:
                source = sources.pick_source(state, seed)
            except sources.SourceError as e:
                print(f"SEARCH-ALERT: {e}")
                hard_failures += 1
                continue
            ledger.save(state)

        # 2. download it
        try:
            src_path = downloader.download(source, work, state)
        except downloader.DownloadError as e:
            print(e)
            hard_failures += 1
            continue

        # 3. pick the best window in the REAL footage
        try:
            start, dur = clipper.best_window(src_path, seed)
        except Exception as e:  # noqa: BLE001
            print(f"[clip] window analysis failed ({e}) — next source")
            hard_failures += 1
            continue

        # 4. overlays + soundtrack + render
        # B1 (audit 2026-11-18): the daily path gets the SAME
        # skip-to-next-source guard the harvest path always had — one
        # ffmpeg/font/synth failure must never kill the day's
        # remaining slots
        final = None
        try:
            top_p, foot_p = overlay.render_overlays(work)
            wav = work / "track.wav"
            music.make_track(wav, dur, seed)

            final_name = sample_out.name if sample_out else \
                f"short_{day.replace('-', '')}_{slot_hour:02d}_{i}.mp4"
            final = work / final_name
            transform.render(src_path, start, dur, (top_p, foot_p),
                             wav, final)

            # 5. metadata
            meta = metadata.build(source, state, seed)
        except Exception as e:  # noqa: BLE001
            print(f"[render] pipeline failed on this source ({e}) — "
                  "skipping to the next source")
            hard_failures += 1
            try:
                if final and final.exists():
                    final.unlink()      # partial render never uploads
            except OSError:
                pass
            continue

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
            published += 1
            continue

        # 6. upload with publishAt booking
        slots_before = len((state.get("slots") or {})
                            .get("short") or [])
        try:
            video_id = youtube.upload_short(final, meta, state)
        except youtube.QuotaDefer:
            break            # clean exit; defer is already stamped
        except youtube.AuthError as e:
            print(f"AUTH-ALERT: {e}")
            return 1
        if not video_id:
            print("[upload] deferred (local quota gate) — stopping "
                  "the session clean")
            break

        # 7. remember everything
        ledger.mark_source_used(state, source["id"])
        # the slot upload_short just booked (banked clips carry theirs
        # in the clip record; the live ladder must read it back here —
        # the reels cross-poster times itself off short.slot)
        booked = (state.get("slots") or {}).get("short") or []
        booked_slot = booked[-1] if len(booked) > slots_before else None
        rec = {
            "n": meta["n"],
            "source_id": source["id"],
            "source_title": source.get("title", ""),
            "source_channel": source.get("channel", ""),
            "date": day,
            "video_id": video_id,
            "clip_start": start,
            "clip_dur": dur,
            "title": meta["title"],
            "slot": booked_slot,
        }
        # C4 (audit 2026-11-18): the short record lands FIRST (atomic);
        # the wide crash window of the multi-MB media cache copy comes
        # after — a kill mid-cache can no longer orphan a live video
        ledger.mark_short_uploaded(state, rec, units=0)
        state["last_run"] = day
        state["last_run_ts"] = int(time.time())
        ledger.save(state)
        # cache the exact published bytes for the reels cross-poster
        # (owner rule 2026-11-15 — same Short -> YT + IG + FB)
        crosspost.cache_video(state, rec, final)
        ledger.save(state)
        src_path.unlink(missing_ok=True)   # B9: multi-GB ladder files
                                            # never pile up mid-session
        published += 1
        print(f"\n[done] short #{meta['n']} published as {video_id} "
              f"(source {source['id']}, clip {start:.0f}s+{dur:.0f}s)")

    state["last_run"] = day
    state["last_run_ts"] = int(time.time())
    ledger.save(state)
    print(f"\n[session] DONE: {published}/{len(targets)} published "
          "this session — the grid self-heals anything left open")

    # ── 8. opportunistic verify pass (zero quota) ──────────────────
    verify.run(state)

    if published == 0 and hard_failures:
        print(f"[session] nothing produced ({hard_failures} hard "
              "failures) — alerting")
        return 1
    return 0


# ── THE MONTHLY HARVEST (owner idea 2026-11-11) ─────────────────────

def harvest(count: int | None = None,
            budget_minutes: float | None = None) -> int:
    """One IP, one long session: search fresh sources, download a
    ~5-min slice of each, clip + render Shorts, and bank them into
    GitHub artifact parts. The daily machine then publishes the whole
    day from the bank in ONE session (pure API upload — never
    IP-flagged). Blocked by the bot wall? Exit clean and let the
    guard cron retry; daily publishing continues via the live
    ladder meanwhile.

    OWNER RULE (2026-11-14) — STACKING: the month must be provisioned
    with >= max_clips total (at least 120). One session renders only
    ~25, so a same-month bank with reachable files is APPENDED to
    (the workflow fetched the existing artifact into bank/ first);
    guard sessions stack until bank.harvested_total >= max_clips.
    A rebuild (reset) happens only on a month rollover or when the
    previous artifact is unreachable."""
    state = ledger.load()
    hconf = cfg().get("harvest") or {}
    days_ahead = int(hconf.get("days_ahead", 31))
    budget = float(budget_minutes or hconf.get("budget_minutes", 305))
    deadline = time.monotonic() + budget * 60
    max_clips = int(hconf.get("max_clips", 124))

    work = ROOT / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    bank_dir = ROOT / "bank"

    # ── APPEND vs REBUILD (owner rule 2026-11-14) ────────────────
    # same-month bank + fetched files -> keep the clips and STACK
    # toward the month provision; anything else -> fresh month.
    ym = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m")
    bankst = state.get("bank") or {}
    existing = len(bankst.get("clips") or [])
    harvested_total = int(bankst.get("harvested_total") or 0)
    fetched = list(bank_dir.glob("*.mp4")) if bank_dir.exists() else []
    # C2 (audit 2026-11-18): a DRAINED bank keeps month/harvested_total
    # (the provision meter) — the topup path (harvested_total already
    # >= max_clips -> target = topup_clips) must stay reachable instead
    # of re-provisioning the whole 124 mid-month.
    if bankst.get("month") == ym and (existing or harvested_total) \
            and fetched:
        print(f"[harvest] APPEND mode: keeping {existing} banked clip(s) "
              f"({len(fetched)} files fetched) — provision "
              f"{harvested_total}/{max_clips}, stacking to the month")
        provision_left = max(0, max_clips - harvested_total)
    else:
        bank.reset(state)
        shutil.rmtree(bank_dir, ignore_errors=True)
        bank_dir.mkdir(parents=True, exist_ok=True)
        provision_left = max_clips

    plan_slots = bank.plan(state, days=days_ahead)
    # OWNER RULE (2026-11-18): the bank NEVER plans past its own
    # month — clips banked near month-end for NEXT-month slots would
    # be retired at rollover, so they are never banked in the first
    # place. The month's bank feeds the month, exactly.
    plan_slots = [s for s in plan_slots if s[:7] == ym]
    if count is not None:
        target = int(count)
    elif provision_left > 0:
        target = provision_left
    else:
        # month provisioned — emergency top-up for a drained bank
        target = int(hconf.get("topup_clips", 32))
    target = min(target, len(plan_slots))

    if target <= 0:
        print("[harvest] the bank already covers the window — nothing "
              "to do")
        return 0
    print(f"[harvest] banking {target} Shorts for the next {days_ahead} "
          f"days: ONE IP, ONE session, sliced downloads, human pacing")

    # OWNER RULE (2026-11-18) — "harvest ALL videos": the candidate
    # pool is a FULL sweep over every query phrasing (16 queries x
    # sweep_results), cached in bank.pool so the month's stacking
    # sessions reuse it without re-spending search quota. Re-sweeps
    # only when the unmined pool drops below harvest.pool_min.
    today = datetime.now(ZoneInfo(cfg()["schedule"]["timezone"]))
    seed = seed_for(today.date().isoformat(), 0,
                    state.get("query_cursor", 0))
    rng = random.Random(seed ^ 0xBEEF)
    try:
        candidates = sources.harvest_pool(state, seed)
    except sources.SourceError as e:
        print(f"HARVEST-ALERT: pool sweep failed — {e}. The guard "
              "retries tomorrow; daily publishing continues live")
        ledger.save(state)     # the sweep's quota spend survives the exit
        return 0
    if not candidates:
        print("HARVEST-ALERT: no candidates in the pool — guard retries "
              "tomorrow 12:43 UTC; daily publishing continues live")
        ledger.save(state)
        return 0
    # the sweep (and its 1,600-unit spend) is banked NOW — even a
    # fully-walled session that banks 0 clips keeps the pool for the
    # next stacking guard (no re-sweep, no re-spend)
    ledger.save(state)
    print(f"[harvest] {len(candidates)} unmined candidate(s) in the "
          f"month pool — {target} clip(s) to bank this session")

    # the long session
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
        except Exception as e:  # noqa: BLE001
            print(f"[harvest] candidate skipped: {e}")
            path.unlink(missing_ok=True)
            try:
                final.unlink(missing_ok=True)   # B7: a partial render
            except (NameError, OSError):         # never rides the parts
                pass
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

    # OWNER RULE (2026-11-18 #3) — THE OLD BANK IS DEAD: any file in
    # bank/ that state.bank.clips does NOT reference is poison dead
    # weight (purged clips, published clips' leftovers, abandoned
    # partial renders). Sweep before the manifest/parts are built so
    # the re-uploaded artifact carries ONLY the clips the state still
    # points at — the purge of the first old bank can never resurrect.
    referenced = {c.get("file") for c in
                  (state.get("bank") or {}).get("clips") or []}
    for f in bank_dir.glob("*.mp4"):
        if f.name not in referenced:
            print(f"[harvest] sweeping unreferenced {f.name} "
                  "(owner rule 2026-11-18 #3 — dead weight never rides)")
            f.unlink(missing_ok=True)

    if banked == 0:
        print("HARVEST-ALERT: 0 clips banked — the bot wall held today. "
              "The guard retries tomorrow 13:00 UTC; daily publishing "
              "continues from the live ladder meanwhile.")
        return 0
    bank.write_manifest(bank_dir, state)
    n_parts = bank.split_parts(bank_dir)
    b = state.get("bank") or {}
    print(f"\n[harvest] DONE: +{banked} this session — the bank holds "
          f"{bank.remaining(state)} clip(s) across {n_parts} artifact "
          f"part(s); month provision {b.get('harvested_total', 0)}/"
          f"{max_clips}. The upload follows; daily runs publish them "
          "one slot at a time (no yt-dlp on daily IPs ever again)")
    return 0
