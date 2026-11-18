"""Transform — the render that makes it OUR video.

ONE ffmpeg pass turns the source window into the published Short:

  1. CUT        -ss/-t extracts the chosen window from the real video
  2. ZOOM       1.08x push-in (not a raw crop — a reframed one)
  3. 9:16 CROP  full-height vertical slice, center-weighted
  4. SCALE      1080x1920 @30fps yuv420p
  5. OVERLAYS   map-code top bar + creator-code footer, EVERY frame
  6. AUDIO      original audio is DISCARDED (only the synth WAV is
                mapped); the numpy-synth bed becomes the soundtrack
  7. MUX        h264 + AAC, faststart, -shortest

The output is byte-for-byte the kind of file the machine publishes.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import overlay as ov
from .config import cfg


def render(src: Path, start: float, dur: float,
           overlays: tuple[Path, Path], music_wav: Path,
           out: Path) -> Path:
    conf = cfg()["transform"]
    zoom = float(conf.get("zoom", 1.08))
    fps = int(conf.get("fps", 30))
    w, h = int(conf["width"]), int(conf["height"])

    top_p, foot_p = overlays
    foot_y = ov.footer_placement_y()

    # zoom happens in the scale; crop then takes a full-height 9:16
    # slice (ih inside crop = the ALREADY-zoomed height — no second
    # zoom factor there or the crop outgrows the frame)
    vf = (
        f"scale=iw*{zoom}:ih*{zoom},"
        f"crop=ih*9/16:ih,"
        f"scale={w}:{h},"
        f"fps={fps},format=yuv420p"
    )

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
        "-i", str(top_p),
        "-i", str(foot_p),
        "-i", str(music_wav),
        "-filter_complex",
        f"[0:v]{vf}[base];"
        f"[base][1:v]overlay=(W-w)/2:0:format=auto[v1];"
        f"[v1][2:v]overlay=(W-w)/2:{foot_y}:format=auto[v]",
        "-map", "[v]",
        "-map", "3:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p",   # IG/Meta transcoders reject 4:4:4
                                  # chroma (live-diagnosed 2026-11-15)
        "-c:a", "aac", "-b:a", "160k",
        "-shortest", "-movflags", "+faststart",
        "-r", str(fps),
        str(out),
    ]

    print(f"[render] {start:.1f}s +{dur:.0f}s -> {out.name} "
          f"({w}x{h}@{fps}, zoom {zoom})")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"ffmpeg failed: {r.stderr[-600:]}")
    print(f"[render] done: {out.stat().st_size/1e6:.1f} MB")
    return out


def restamp_current(src: Path) -> Path:
    """Re-burn the code bars over a FINISHED render with the CURRENT
    channel.yaml codes — the publish-time guarantee for banked clips.

    A banked clip was rendered at HARVEST time, so a map-code switch
    after its harvest would keep selling the old code for weeks (the
    bank held 38 pre-switch clips when 1789-8528-6429 ->
    7992-0068-2838 landed 2026-11-17). The restamp bars are fully
    opaque (rendered by overlay.render_restamp) and cover whatever is
    underneath; restamping an already-current clip is a visual no-op,
    so it is applied to EVERY banked publish unconditionally.

    Best-effort: on any ffmpeg/PIL problem the ORIGINAL file is
    returned unchanged — a restamp failure never blocks a publish.
    Only ever used from the daily/heal factory job (ffmpeg present).
    """
    from .config import ROOT

    try:
        from . import overlay as ov
        top_p, foot_p = ov.render_restamp(ROOT / "work")
        foot_y = ov.footer_placement_y()
        out = src.with_name(src.stem + "_rs.mp4")
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src), "-i", str(top_p), "-i", str(foot_p),
            "-filter_complex",
            f"[0:v][1:v]overlay=(W-w)/2:0:format=auto[v1];"
            f"[v1][2:v]overlay=(W-w)/2:{foot_y}:format=auto[v]",
            "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "fast", "-crf", "19",
            "-pix_fmt", "yuv420p", "-c:a", "copy",
            "-movflags", "+faststart",
            str(out),
        ]
        print(f"[restamp] re-burning the code bars on {src.name} "
              f"(map {str(ov.cfg()['codes']['map_code'])[:14]}...)")
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=1800)
        if r.returncode == 0 and out.exists() \
                and out.stat().st_size > 1_000_000:
            print(f"[restamp] done: {out.name} "
                  f"({out.stat().st_size/1e6:.1f} MB)")
            return out
        print(f"[restamp] ffmpeg problem ({r.stderr[-300:]}) — "
              "publishing the un-restamped file")
    except Exception as e:                      # noqa: BLE001
        print(f"[restamp] skipped ({e}) — publishing un-restamped")
    return src
