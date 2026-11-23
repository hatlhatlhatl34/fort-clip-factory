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

The image itself is assets/outro.png (channel.yaml: outro.image) —
5 seconds (outro.seconds), scaled/padded to the render geometry
from channel.yaml's transform block so the concat is seamless.
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

        out = src.with_name(src.stem + "_og.mp4")
        # One ffmpeg pass: scale/pad the outro PNG to the render
        # geometry, generate matching silent audio, concat onto the
        # clip, full re-encode (concat-filter — safe against codec
        # profile mismatches a demuxer concat would choke on).
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src),
            "-loop", "1", "-t", f"{seconds}", "-i", str(img),
            "-f", "lavfi", "-t", f"{seconds}", "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_complex",
            f"[1:v]scale={w}:{h}:force_original_aspect_ratio="
            f"decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:"
            f"black,fps={fps},format=yuv420p[ov];"
            f"[0:v][0:a][ov][2:a]concat=n=2:v=1:a=1[v][a]",
            "-map", "[v]", "-map", "[a]",
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
              f"({(r.stderr or 'no stderr')[-300:]}) — publishing "
              f"without the outro card")
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass
    except Exception as e:                       # noqa: BLE001
        print(f"OUTRO-ALERT: outro skipped ({e}) — publishing "
              "without the outro card", file=sys.stderr)
    return src
