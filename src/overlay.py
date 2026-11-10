"""Overlay engine — the codes, first frame to last.

Two transparent PNGs rendered once per machine lifetime (the codes
never change) and composited over EVERY frame by ffmpeg:

  TOP BAR    "MAP CODE" label + 1789-8528-6429 (Anton, white on a
             near-black bar with a gold rule under it)
  FOOTER     "CREATOR CODE" label + H15 (Anton, white, gold rule)

OWNER RULE (2026-11-10): the footer sits FLUSH with the true bottom
edge of the 1080x1920 frame — zero gap. It used to float 130 px
above the edge; the owner wants the bar pinned to the end of the
screen. The top bar starts at the true top.

Design law: static, high-contrast, always on. A pulsing overlay
gains nothing and risks unreadable frames.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import cfg, ROOT

W, H = 1080, 1920
FONT_DIR = ROOT / "assets" / "fonts"


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / name), size)


def render_overlays(out_dir: Path) -> tuple[Path, Path]:
    """Render top.png + footer.png (idempotent — reused if present)."""
    conf = cfg()["overlay"]
    accent = conf.get("accent", "#FFD400")
    bar_alpha = int(conf.get("bar_opacity", 205))

    top_h = int(conf.get("top_bar_height", 300))
    footer_h = int(conf.get("footer_bar_height", 240))
    map_code = cfg()["codes"]["map_code"]
    creator_code = cfg()["codes"]["creator_code"]

    out_dir.mkdir(parents=True, exist_ok=True)
    top_p = out_dir / "overlay_top.png"
    foot_p = out_dir / "overlay_footer.png"
    if top_p.exists() and foot_p.exists():
        return top_p, foot_p

    # ── TOP BAR ────────────────────────────────────────────────────
    top = Image.new("RGBA", (W, top_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(top)
    d.rectangle([0, 0, W, top_h], fill=(5, 5, 12, bar_alpha))
    # gold rule at the bar's bottom edge
    d.rectangle([0, top_h - 8, W, top_h], fill=accent)
    # label: "MAP CODE" — gold, wide tracking
    lab_f = _font("ArchivoBlack-Regular.ttf", 44)
    label = conf.get("top_label", "MAP CODE")
    _draw_tracked(d, label, lab_f, W, y=int(top_h * 0.10), fill=accent,
                  tracking=18)
    # the code itself — Anton, huge, white
    code_f = _font("Anton-Regular.ttf", 96)
    code = map_code
    tw = d.textlength(code, font=code_f)
    d.text(((W - tw) / 2, int(top_h * 0.34)), code, font=code_f,
           fill=(255, 255, 255, 255))
    top.save(top_p)

    # ── FOOTER BAR ─────────────────────────────────────────────────
    foot = Image.new("RGBA", (W, footer_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(foot)
    d.rectangle([0, 0, W, footer_h], fill=(5, 5, 12, bar_alpha))
    d.rectangle([0, 0, W, 8], fill=accent)
    lab_f2 = _font("ArchivoBlack-Regular.ttf", 38)
    label2 = conf.get("footer_label", "CREATOR CODE")
    _draw_tracked(d, label2, lab_f2, W, y=int(footer_h * 0.12),
                  fill=accent, tracking=14)
    code_f2 = _font("Anton-Regular.ttf", 118)
    code2 = f"USE CODE {creator_code}"
    tw2 = d.textlength(code2, font=code_f2)
    # if the full CTA does not fit, fall back to the code alone
    if tw2 > W - 80:
        code2 = creator_code
        tw2 = d.textlength(code2, font=code_f2)
        code_f2 = _font("Anton-Regular.ttf", 150)
        tw2 = d.textlength(code2, font=code_f2)
    d.text(((W - tw2) / 2, int(footer_h * 0.38)), code2, font=code_f2,
           fill=(255, 255, 255, 255))
    foot.save(foot_p)

    # the footer canvas we composite is (W, footer_h) and it is
    # PLACED flush with the true bottom edge of the 1920 canvas —
    # that placement lives in transform.py via footer_placement_y()
    return top_p, foot_p


def footer_placement_y() -> int:
    """Footer top-edge Y on the 1920 canvas — FLUSH with the bottom
    edge (owner rule 2026-11-10): the bar's last pixel row is the
    video's last pixel row."""
    footer_h = int(cfg()["overlay"].get("footer_bar_height", 240))
    return H - footer_h


def _draw_tracked(d: ImageDraw.ImageDraw, text: str,
                  font: ImageFont.FreeTypeFont, canvas_w: int, y: int,
                  fill, tracking: int) -> None:
    """Centered text with letter-spacing (PIL has no tracking)."""
    widths = [d.textlength(ch, font=font) for ch in text]
    total = sum(widths) + tracking * (len(text) - 1)
    x = (canvas_w - total) / 2
    for ch, w in zip(text, widths):
        d.text((x, y), ch, font=font, fill=fill)
        x += w + tracking
