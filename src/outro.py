"""The outro card (owner rule 2026-11-22, PERMANENT):

    "put it for 5 seconds in the end of ANY video or clip or short
     or reel that will be published in the future FOREVER"

Architecture (same shape as transform.restamp_current):
  * apply(path) is called on EVERY file that is about to be
    published — both publish ladders call it right before
    youtube.upload_short():
       - the live ladder:  orchestrator.run()    right after render
       - the bank ladder:  bank.publish()        right after restamp
    Because BOTH ladders cache the published bytes AFTER this step
    (crosspost.cache_video), the SAME outro version ships to
    YouTube Shorts, Instagram Reels and Facebook Reels — every
    surface, forever.
  * Best-effort: on any ffmpeg problem the ORIGINAL file is
    returned unchanged — an outro failure never blocks a publish
    (the alert line makes it visible instead of silent).
  * Structurally idempotent: the output file is <stem>_og.mp4 and
    apply() refuses to re-process a file already carrying the
    suffix, so a retry cycle can never stack two outros.

THE 2026-11-23 REWRITE — why this shape: the runners moved to
ffmpeg 8.0.1 (ubuntu-latest) and the concat FILTER died on every
banked publish ("[fc#0] Error reinitializing filters!" -> rc=234
-> "OUTRO-ALERT ... publishing without the outro card", 3/3 on
shorts #82/#83/#84). Isolated live on a runner (workflow
outro-debug, rounds 1-3): the concat filter fails on ANY variant
with these clips' B-frame h264 (image loop, lavfi color+overlay,
testsrc2, a real pre-rendered card — all rc=234), while an
audio-only concat passes and the concat DEMUXER path is untouched.
The old graph never worked ONCE on the new runners; this rewrite
joins the clip and the card at the DEMUXER level instead:

  1. THE CARD: a real 5s MP4 (1080x1920 30fps yuv420p + silent
     44.1k stereo aac) rendered once per session from
     assets/outro.png (channel.yaml outro.image) into work/ —
     rendered WITHOUT the concat filter (lavfi color canvas +
     static overlay), which round 3 proved works on 8.0.1
     (rc=0). The PNG stays the single source of truth: swap the
     image, the card regenerates next session.
  2. THE JOIN: ffmpeg -f concat -safe 0 with a two-entry list
     (the clip, then the card) and a full re-encode — the demuxer
     feeds packets in order, the decoder handles the B-frames
     exactly as it does for restamp, and the concat filter is out
     of the graph entirely.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .config import ROOT, cfg


def _conf() -> dict:
    return (cfg().get("outro") or {})


def _duration(p: Path) -> float:
    """ffprobe duration, 0.0 on any problem (never raises)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration", "-of", "csv=p=0", str(p)],
            capture_output=True, text=True, timeout=120)
        return float(r.stdout.strip() or 0.0)
    except Exception:                              # noqa: BLE001
        return 0.0


def _tail(stderr: str) -> str:
    return (stderr or "no stderr")[-900:]


def _render_card(card: Path, img: Path, seconds: float,
                 w: int, h: int, fps: int) -> bool:
    """The 5s outro card as a real MP4 — concat-filter-free (safe on
    ffmpeg 8.0.1). Returns True when a healthy card exists on disk."""
    if card.exists() and card.stat().st_size > 100_000 \
            and abs(_duration(card) - seconds) <= 1.0:
        return True                      # cached this session already
    card.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i",
        f"color=c=black:s={w}x{h}:r={fps}:d={seconds}",
        "-i", str(img),
        "-f", "lavfi", "-t", f"{seconds}", "-i",
        "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-filter_complex",
        f"[1:v]scale={w}:{h}:force_original_aspect_ratio=decrease"
        f"[img];[0:v][img]overlay=(W-w)/2:(H-h)/2:format=auto,"
        f"fps={fps},format=yuv420p[v]",
        "-map", "[v]", "-map", "2:a", "-t", f"{seconds}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
        "-r", str(fps), "-movflags", "+faststart",
        str(card),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=900)
    if card.exists() and card.stat().st_size > 100_000 \
            and abs(_duration(card) - seconds) <= 1.0:
        print(f"[outro] 5s card rendered -> {card.name} "
              f"({card.stat().st_size/1e6:.1f} MB)")
        return True
    print(f"OUTRO-ALERT: card render problem "
          f"({_tail(r.stderr)}) — publishing without the outro card")
    try:
        card.unlink(missing_ok=True)
    except OSError:
        pass
    return False


def apply(src: Path) -> Path:
    """Return the file to publish: src with the 5s outro appended,
    or src unchanged when disabled/skipped/failed (never raises)."""
    try:
        conf = _conf()
        if not conf.get("enabled", True):
            return src
        if src.stem.endswith("_og"):
            return src            # already carries the outro

        img = ROOT / conf.get("image", "assets/outro.png")
        if not img.exists():
            print(f"OUTRO-ALERT: outro enabled but {img} is missing — "
                  "publishing WITHOUT the outro card (drop the PNG "
                  "at that path to restore the owner rule)")
            return src

        seconds = float(conf.get("seconds", 5.0))
        tconf = cfg().get("transform") or {}
        w = int(tconf.get("width", 1080))
        h = int(tconf.get("height", 1920))
        fps = int(tconf.get("fps", 30))

        work = ROOT / "work"
        card = work / "outro_card.mp4"
        if not _render_card(card, img, seconds, w, h, fps):
            return src

        out = src.with_name(src.stem + "_og.mp4")
        # The join: concat DEMUXER + full re-encode. The concat FILTER
        # is ffmpeg-8.0.1-broken on these clips (B-frame inputs, "Error
        # reinitializing filters!"); the demuxer path is not.
        lst = work / "concat_list.txt"
        esc = lambda p: str(p).replace("'", "'\\''")   # noqa: E731
        lst.write_text(
            f"file '{esc(src.resolve())}'\n"
            f"file '{esc(card.resolve())}'\n")
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(lst),
            "-map", "0:v", "-map", "0:a",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k",
            "-r", str(fps),
            "-movflags", "+faststart",
            str(out),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=1800)
        # Integrity is judged by ffprobe, NOT the exit code: a
        # memory-tight host can SIGKILL ffmpeg during process
        # teardown AFTER the mux completed — the file on disk is
        # complete and valid, and a duration check proves it (a kill
        # mid-encode leaves a short file, which this correctly
        # rejects).
        if out.exists() and out.stat().st_size > 1_000_000 \
                and _duration(out) >= _duration(src) + seconds - 1.0:
            print(f"[outro] 5s outro card appended -> {out.name} "
                  f"({out.stat().st_size/1e6:.1f} MB) "
                  "(owner rule 2026-11-22: every video, forever)")
            return out
        print(f"OUTRO-ALERT: ffmpeg outro problem "
              f"({_tail(r.stderr)}) — publishing without the outro card")
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass
    except Exception as e:                       # noqa: BLE001
        print(f"OUTRO-ALERT: outro skipped ({e}) — publishing "
              "without the outro card", file=sys.stderr)
    return src
