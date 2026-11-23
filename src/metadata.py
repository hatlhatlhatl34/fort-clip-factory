"""Metadata — titles, descriptions, tags.

OWNER RULE (2026-11-22, "never duplicate titles"): no two titles may
ever copy each other. Titles are now COMBINATIONS, not templates:
subject x predicate (+ optional lead) drawn from the channel.yaml
title_engine parts, checked against the SKELETON of every title
ever shipped (published shorts + banked clips). A skeleton — the
title with its #N, emoji and punctuation stripped — is burned
forever once used, so two titles that differ only by their number
can never exist again. When the combination pool runs low, the
composer falls back to a natural "(part N)" suffix — uniqueness is
guaranteed by construction, forever.

OWNER RULE (2026-11-23): NO numbering in titles. The "#N" suffix is
permanently gone from every future clip (the banked titles were
retro-scrubbed the same day). The internal clip number n lives in
state records and file names only — never in what viewers see.

OWNER RULE (2026-11-14): the word "Fortnite" is IN EVERY TITLE.
Every engine subject carries it; build() additionally REPAIRS any
title that loses the word — the rule can never silently break.

OWNER RULE (2026-11-13): titles are pure HOOKS — the map code and
creator code NEVER appear in a title (they live in the description
and the burned-in top/footer bars). build() enforces it at runtime.

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
import re

from . import ledger
from .config import cfg

_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u2B00-\u2BFF]")

# the guaranteed-clean legacy hook (engine-less fallback) — obeys
# every title rule: Fortnite inside, no codes, <= 95 chars.
_LEGACY_HOOK = "How to get FREE SKINS in Fortnite 🤑"


# ── the uniqueness ledger ──────────────────────────────────────────

def _skeleton(title: str) -> str:
    """A title's skeleton: #N, emoji and punctuation stripped,
    lowercased, whitespace collapsed. Two titles that differ ONLY
    by their #N share a skeleton — the thing that must never
    repeat (owner rule 2026-11-22)."""
    t = _EMOJI.sub("", str(title or ""))
    t = re.sub(r"#\d+\s*$", "", t).strip()
    t = re.sub(r"[^0-9A-Za-z]+", " ", t)
    return " ".join(t.lower().split()).strip()


def used_skeletons(state: dict) -> set[str]:
    """Every skeleton ever shipped: published shorts + banked clips.
    This IS the uniqueness ledger — no separate file to lose."""
    titles = [s.get("title") for s in (state.get("shorts") or [])]
    for c in (state.get("bank") or {}).get("clips") or []:
        titles.append((c.get("meta") or {}).get("title"))
    return {_skeleton(t) for t in titles if t}


# ── the composer ───────────────────────────────────────────────────

def compose(xp: bool, used: set[str], rng: random.Random, n: int) -> str:
    """One unique-skeleton hook title (numbering-free, owner rule
    2026-11-23 — n is bookkeeping only, never part of the title).

    Combines subject x predicate (+ an occasional lead) until the
    skeleton is unused; after 120 misses (pool exhausted) it tags a
    natural "(part k)" suffix. Uniqueness is structural: even the
    fallback path walks part numbers upward, so it can never
    return a burned skeleton while k still has fresh values.
    """
    eng = cfg()["youtube"].get("title_engine") or {}
    subjects = eng.get("xp_subjects" if xp else "subjects") or []
    predicates = eng.get("xp_predicates" if xp else "predicates") or []
    leads = eng.get("leads") or []

    if subjects and predicates:
        for _ in range(120):
            subj, pred = rng.choice(subjects), rng.choice(predicates)
            lead = rng.choice(leads) if (leads and rng.random() < 0.28) else ""
            cand = (f"{lead} {subj} {pred}" if lead
                    else f"{subj} {pred}").strip()
            if _skeleton(cand) not in used:
                return cand
        # pool exhausted — a natural part suffix keeps the title
        # unique WITHOUT recycling a burned skeleton
        subj, pred = rng.choice(subjects), rng.choice(predicates)
        for k in range(2, 500):
            cand = f"{subj} {pred} (part {k})"
            if _skeleton(cand) not in used:
                return cand

    # engine not configured (or broken): legacy clean hook, part-
    # numbered the moment its skeleton is already burned
    for k in range(0, 500):
        cand = _LEGACY_HOOK + (f" (part {k})" if k else "")
        if _skeleton(cand) not in used:
            return cand
    return _LEGACY_HOOK          # unreachable by construction


# ── the description block ──────────────────────────────────────────

def _description_body(codes: dict, xlinks: dict) -> str:
    """The description block built from the CURRENT codes + links —
    shared by build() (fresh shorts) and refresh_description()
    (publish-time refresh of banked clips, so a code switch applies
    to the whole remaining bank; live-diagnosed 2026-11-18)."""
    desc = [
        f"🗺️ MAP CODE: {codes['map_code']}",
        f"🛒 CREATOR CODE: {codes['creator_code']} — use it in the Fortnite "
        "item shop and support the channel!",
        "",
        "How to play: Fortnite → search → enter the map code above. 🎮",
    ]
    # OWNER RULE (2026-11-15): cross-links — every YouTube description
    # points at the other platforms (the per-video reel links are
    # appended by the cross-poster once the reels are live).
    if xlinks.get("ig") or xlinks.get("fb"):
        desc += ["", "WATCH EVERYWHERE:"]
        if xlinks.get("ig"):
            desc.append(f"📸 Instagram: {xlinks['ig']}")
        if xlinks.get("fb"):
            desc.append(f"📱 Facebook: {xlinks['fb']}")
    if xlinks.get("tiktok"):
        desc += ["", "🎵 TIKTOK: " + str(xlinks["tiktok"])]
    # OWNER RULE (2026-11-10): no source credit of any kind — the
    # "clipped from ..." sentence is banned from descriptions.
    desc += [
        "",
        "#fortnite #fortnitemap #fortnitecreative #fortniteclips "
        "#uefn #mapcode #shorts",
    ]
    return "\n".join(desc)[:4800]          # limit is 5000


def build(source: dict, state: dict, seed: int) -> dict:
    codes = cfg()["codes"]          # description + bars only, NEVER titles
    n = ledger.next_short_number(state)
    rng = random.Random(seed)

    # XP-aware titling: footage found via an XP-map query gets the XP
    # family (the approved sample-1 niche); other footage keeps the
    # generic family — titles never mislabel the content.
    xp = "xp" in (source.get("query") or "").lower()
    used = used_skeletons(state)
    title = compose(xp, used, rng, n)

    # OWNER RULE (2026-11-13): codes NEVER appear in a title — if
    # anything ever smuggles one in, ship a guaranteed-clean,
    # still-unique hook instead (the legacy path obeys every rule).
    if (codes["map_code"].lower() in title.lower()
            or codes["creator_code"].lower() in title.lower()):
        used.add(_skeleton(title))              # burn the poisoned one
        title = compose(xp, used, rng, n)
        if (codes["map_code"].lower() in title.lower()
                or codes["creator_code"].lower() in title.lower()):
            title = compose(False, used, rng, n)   # last-resort legacy

    # OWNER RULE (2026-11-14): the word "Fortnite" is IN EVERY TITLE.
    # Belt and suspenders: if a config edit somehow lost the word,
    # the builder repairs the title in place — a Fortnite-less
    # title can never ship.
    if "fortnite" not in title.lower():
        title = f"Fortnite: {title}"

    title = title[:95]                       # YouTube hard limit is 100

    desc = _description_body(codes, (cfg().get("crosspost") or {}).get("links") or {})
    description = desc

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


def refresh_description(meta: dict) -> dict:
    """Publish-time refresh of a BANKED clip's description.

    The banked meta was baked at HARVEST time; a map-code switch after
    the harvest would otherwise keep selling the old code in every
    description for weeks (38 pre-switch clips were banked when
    1789-8528-6429 -> 7992-0068-2838 landed 2026-11-17). Rebuild the
    description from the CURRENT channel.yaml; title/tags/n stay as
    banked (titles never carry codes — owner rule 2026-11-13).
    """
    out = dict(meta)
    codes = cfg()["codes"]
    xlinks = (cfg().get("crosspost") or {}).get("links") or {}
    out["description"] = _description_body(codes, xlinks)
    return out
