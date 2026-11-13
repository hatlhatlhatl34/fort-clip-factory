"""Metadata — titles, descriptions, tags.

OWNER RULE (2026-11-13): titles are pure HOOKS about XP + free
skins — the map code NEVER appears in a title (it lives in the
description + the burned-in top bar). Templates come from
channel.yaml; the only filled field is {n}, the forever-incrementing
number so no two Shorts share a title (crash-adoption safety).

Description:
  * MAP CODE (huge, first line)
  * CREATOR CODE + item-shop CTA
  * hashtag block

OWNER RULE (2026-11-10): NO source-credit sentence — the
"clipped from <video> by <channel>" line is permanently banned
from every description, on every rung (YouTube, archive.org,
ingest drops). Nothing about the source video ever appears.
"""
from __future__ import annotations

import random

from . import ledger
from .config import cfg


def build(source: dict, state: dict, seed: int) -> dict:
    codes = cfg()["codes"]          # description + bars only, NEVER titles
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

    title = rng.choice(templates).format(n=n)
    title = title[:95]                       # YouTube hard limit is 100

    desc = [
        f"🗺️ MAP CODE: {codes['map_code']}",
        f"🛒 CREATOR CODE: {codes['creator_code']} — use it in the Fortnite "
        "item shop and support the channel!",
        "",
        "How to play: Fortnite → search → enter the map code above. 🎮",
    ]
    # OWNER RULE (2026-11-10): no source credit of any kind — the
    # "clipped from ..." sentence is banned from descriptions.
    desc += [
        "",
        "#fortnite #fortnitemap #fortnitecreative #fortniteclips "
        "#uefn #mapcode #shorts",
    ]
    description = "\n".join(desc)[:4800]     # limit is 5000

    tags = [
        "fortnite", "fortnite creative", "fortnite map", "map code",
        "fortnite clips", "creative map", "uefn", "fortnite gameplay",
        "fortnite shorts", "map gameplay", "fortnite xp map",
        "free skins", "level up fast", "fortnite battle pass",
    ]

    return {
        "title": title,
        "description": description,
        "tags": tags,
        "n": n,
    }
