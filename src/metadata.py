"""Metadata — titles, descriptions, tags. The codes live everywhere.

Title: rotating template, always carries the map code (and often the
creator code), plus the forever-incrementing #N so no two Shorts
share a title (crash-adoption safety).

Description:
  * MAP CODE (huge, first line)
  * CREATOR CODE + item-shop CTA
  * honest source credit (the original video + channel) — this is
    what keeps the channel in fair-use territory: credit, transform,
    no confusion about origin
  * hashtag block
"""
from __future__ import annotations

import random

from . import ledger
from .config import cfg


def build(source: dict, state: dict, seed: int) -> dict:
    codes = cfg()["codes"]
    n = ledger.next_short_number(state)
    templates = cfg()["youtube"]["title_templates"]
    # XP-aware titling: footage found via an XP-map query gets the XP
    # template family (the approved sample-1 niche); other footage
    # keeps the generic family — titles never mislabel the content.
    if "xp" in (source.get("query") or "").lower():
        xp = cfg()["youtube"].get("xp_title_templates") or []
        if xp:
            templates = xp
    rng = random.Random(seed)

    fields = {
        "map": codes["map_code"],
        "creator": codes["creator_code"],
        "n": n,
    }
    title = rng.choice(templates).format(**fields)
    title = title[:95]                       # YouTube hard limit is 100

    desc = [
        f"🗺️ MAP CODE: {codes['map_code']}",
        f"🛒 CREATOR CODE: {codes['creator_code']} — use it in the Fortnite "
        "item shop and support the channel!",
        "",
        "How to play: Fortnite → search → enter the map code above. 🎮",
    ]
    # honest source credit — only for real YouTube sources (ingest/
    # drops have no source video to credit; archive.org open-pool
    # items get their own honest line)
    if source.get("origin") == "archive":
        desc += [
            "",
            f'clipped from "{source.get("title", "")}" '
            "(via an archive.org mirror)",
        ]
    elif source.get("id") and not str(source["id"]).startswith("manual") \
            and not str(source["id"]).startswith("ao:"):
        desc += [
            "",
            f"clipped from \"{source.get('title', '')}\" "
            f"by {source.get('channel', '')} — full video: "
            f"https://www.youtube.com/watch?v={source.get('id', '')}",
        ]
    desc += [
        "",
        "#fortnite #fortnitemap #fortnitecreative #fortniteclips "
        "#uefn #mapcode #shorts",
    ]
    description = "\n".join(desc)[:4800]     # limit is 5000

    tags = [
        "fortnite", "fortnite creative", "fortnite map", "map code",
        "fortnite clips", "creative map", "uefn", "fortnite gameplay",
        "fortnite shorts", "map gameplay",
    ]

    return {
        "title": title,
        "description": description,
        "tags": tags,
        "n": n,
    }
