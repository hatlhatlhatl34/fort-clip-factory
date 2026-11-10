"""Clip engine — finds the best 20-35s window in a REAL gameplay video.

Two signals, both computed from the actual footage:
  1. MOTION: the video is sampled at 5 fps, 160x90 grayscale, and
     consecutive-frame differences are bucketed per second. Gameplay
     spikes (fights, builds, deaths, escapes) light up as motion peaks.
  2. SCENE CUTS: ffmpeg's scene-change detector marks hard cuts and
     big visual shifts (map transitions, respawns, camera pans).

The best window maximizes (mean motion + cut bonus), avoids the
first 8% (intros/channel cards) and last 5% (outros/CTAs), and never
touches a previously used window of the same source (not needed —
sources are never re-mined — but the code enforces it anyway for
future multi-clip-per-source use).

Long sources (a 73-min stream) are analyzed on a seeded-random
16-minute stretch: bounded compute on a free runner, and different
runs would see different stretches of the same video anyway.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import numpy as np

from .config import cfg

SAMPLE_FPS = 5
SAMPLE_W, SAMPLE_H = 160, 90
ANALYZE_MAX_S = 16 * 60            # bounded analysis window
SKIP_HEAD_FRAC = 0.08              # skip intros
SKIP_TAIL_FRAC = 0.05              # skip outros


def _duration_of(src: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(src)],
        capture_output=True, text=True, timeout=60)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _motion_curve(src: Path, start: float, dur: float) -> np.ndarray:
    """Per-second mean absolute frame diff, normalized to 0..100."""
    n_frames = int(dur * SAMPLE_FPS)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
           "-vf", f"fps={SAMPLE_FPS},scale={SAMPLE_W}:{SAMPLE_H},format=gray",
           "-f", "rawvideo", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=max(60, int(dur * 4)))
    raw = r.stdout
    frame_size = SAMPLE_W * SAMPLE_H
    # the fps filter can emit slightly more/fewer frames than the
    # nominal count (timestamp rounding) — size the buffer from what
    # actually arrived, capped at the nominal + slack
    got = len(raw) // frame_size
    if got < 2:
        return np.zeros(max(1, int(dur)))
    buf = np.frombuffer(raw[:got * frame_size], dtype=np.uint8) \
        .reshape(got, SAMPLE_H * SAMPLE_W)
    diffs = np.abs(np.diff(buf.astype(np.int16), axis=0)).mean(axis=1)
    # bucket per second
    n_sec = max(1, int(dur))
    per = np.zeros(n_sec)
    counts = np.zeros(n_sec)
    for i, d in enumerate(diffs):
        s = min(i // SAMPLE_FPS, n_sec - 1)
        per[s] += d
        counts[s] += 1
    counts[counts == 0] = 1
    per = per / counts
    if per.max() > 0:
        per = per / per.max() * 100.0
    return per


def _scene_cuts(src: Path, start: float, dur: float,
                threshold: float) -> list[float]:
    """Timestamps (relative to `start`) of hard scene changes."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "info",
           "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
           "-vf", f"select='gt(scene,{threshold})',showinfo",
           "-an", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    cuts = []
    for m in re.finditer(r"pts_time:([\d.]+)", r.stderr or ""):
        cuts.append(float(m.group(1)))
    return cuts


def best_window(src: Path, seed: int,
                used_windows: list[tuple[float, float]] | None = None
                ) -> tuple[float, float]:
    """Return (start_seconds, duration) of the best 20-35s clip inside
    the analyzed stretch of `src`."""
    conf = cfg()["clip"]
    lo, hi = [float(x) for x in conf["target_len_s"]]
    min_motion = float(conf.get("min_motion_score", 10.0))
    threshold = float(conf.get("scene_threshold", 0.28))

    total = _duration_of(src)
    if total < lo + 10:
        raise RuntimeError(f"source too short to clip ({total:.0f}s)")

    # choose the analyzed stretch
    rng = np.random.default_rng(seed)
    skip_head = total * SKIP_HEAD_FRAC
    skip_tail = total * SKIP_TAIL_FRAC
    usable = total - skip_head - skip_tail
    if usable <= ANALYZE_MAX_S:
        a_start, a_dur = skip_head, usable
    else:
        a_start = skip_head + float(rng.uniform(0, usable - ANALYZE_MAX_S))
        a_dur = ANALYZE_MAX_S
    print(f"[clip] analyzing {a_dur/60:.1f} min stretch at "
          f"{a_start/60:.1f} min of {total/60:.1f} min source")

    motion = _motion_curve(src, a_start, a_dur)
    cuts = _scene_cuts(src, a_start, a_dur, threshold)
    print(f"[clip] motion mean={motion.mean():.1f} peak={motion.max():.1f}; "
          f"{len(cuts)} scene cuts")

    used_windows = used_windows or []
    sec = 1.0
    best: tuple[float, float, float] | None = None   # (score, start, dur)
    for dur in range(int(lo), int(hi) + 1):
        win = int(dur / sec)
        if win > len(motion):
            continue
        # sliding mean of motion
        cs = np.concatenate([[0.0], np.cumsum(motion)])
        means = (cs[win:] - cs[:-win]) / win
        for i in range(len(means)):
            s0 = i * sec
            score = float(means[i])
            # cut bonus: scene changes INSIDE the window (not at edges)
            inside = [c for c in cuts if s0 + 0.8 < c < s0 + dur - 0.8]
            score += 2.5 * min(len(inside), 6)
            # avoid overlapping already-used windows
            overlaps = any(not (s0 + dur < u0 or s0 > u0 + ud)
                           for u0, ud in used_windows)
            if overlaps:
                continue
            if score < min_motion and best is not None:
                continue
            if best is None or score > best[0]:
                best = (score, s0, float(dur))

    if best is None:
        # nothing scored above the floor: take the most-motion window anyway
        win = int(lo)
        cs = np.concatenate([[0.0], np.cumsum(motion)])
        means = (cs[win:] - cs[:-win]) / win
        i = int(np.argmax(means))
        best = (float(means[i]), i * sec, float(lo))

    score, rel_start, dur = best
    abs_start = a_start + rel_start
    print(f"[clip] best window: {abs_start/60:.2f} min +{dur:.0f}s "
          f"(score {score:.1f})")
    return round(abs_start, 2), round(float(dur), 2)
