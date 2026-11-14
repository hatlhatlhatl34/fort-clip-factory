"""Restore the bank from a harvest artifact manifest.

WHY THIS EXISTS (2026-11-14 incident): the harvest run banked 25
clips and uploaded the 1.4 GB artifact, but its "commit memory"
push lost a git race with the hourly heal run (`git push || true`
swallowed the non-fast-forward). GitHub ended up with a full
artifact but state.json saying bank.clips = 0 — the machine fell
back to the live ladder and the bank sat orphaned.

This tool rebuilds state.json + data/bank_pointer.json from the
artifact's manifest.json:

  * renumbers every clip after the CURRENT max published/reserved
    n (the live ladder may have consumed numbers meanwhile —
    shorts #20-23 collided with the banked range in the incident)
  * re-titles every clip with today's channel.yaml templates
    (owner rule 2026-11-14: the word "Fortnite" in EVERY title —
    the incident clips predated the rule)
  * re-marks the harvested source_ids as used (their marks were
    lost in the same race — without them the live ladder could
    re-clip the same videos)
  * points data/bank_pointer.json at the run that owns the artifact

Usage (from the repo root):
  python tools/restore_bank.py --manifest /tmp/manifest.json \
      --run-id 43465380523 [--apply]

Default is a dry run; --apply writes the files.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import cfg  # noqa: E402

XP_HINTS = re.compile(
    r"\bxp\b|level\s?(?:up|100)|afk|v[\s-]?bucks|battle\s?pass|free skin",
    re.IGNORECASE)


def retitle(source_title: str, n: int, gi: int, xi: int) -> str:
    """New title for a banked clip — Fortnite in every one."""
    y = cfg()["youtube"]
    if XP_HINTS.search(source_title or ""):
        fam = y.get("xp_title_templates") or y["title_templates"]
        idx = xi
    else:
        fam = y["title_templates"]
        idx = gi
    title = fam[idx % len(fam)].format(n=n)
    if "fortnite" not in title.lower():        # owner rule 2026-11-14
        title = f"Fortnite: {title}"
    codes = cfg()["codes"]
    if (codes["map_code"].lower() in title.lower()
            or codes["creator_code"].lower() in title.lower()):
        title = f"How to get FREE SKINS in Fortnite 🤑 #{n}"
    return title[:95]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True,
                    help="path to the artifact's manifest.json")
    ap.add_argument("--run-id", required=True, type=int,
                    help="Actions run id that owns the artifact")
    ap.add_argument("--apply", action="store_true",
                    help="write the files (default: dry run)")
    args = ap.parse_args()

    manifest = json.loads(Path(args.manifest).read_text())
    clips = sorted(manifest.get("clips") or [],
                   key=lambda c: c.get("slot", ""))
    if not clips:
        print("manifest has no clips — nothing to restore")
        return 1

    state = json.loads((ROOT / "data" / "state.json").read_text())

    # next free number: after every published AND reserved n
    used_ns = [s.get("n") for s in (state.get("shorts") or [])
               if isinstance(s.get("n"), int)]
    used_ns += [r for r in ((state.get("bank") or {}).get("reserved_n")
                            or []) if isinstance(r, int)]
    n = (max(used_ns) + 1) if used_ns else 1

    gi = xi = 0
    out, reserved = [], []
    for clip in clips:
        src_title = clip.get("source_title") or ""
        title = retitle(src_title, n, gi, xi)
        if XP_HINTS.search(src_title):
            xi += 1
        else:
            gi += 1
        meta = dict(clip.get("meta") or {})
        meta["title"] = title
        meta["n"] = n
        meta.setdefault("description", "")
        meta.setdefault("tags", [])
        out.append({
            "slot": clip.get("slot", ""),
            "file": clip.get("file", ""),
            "n": n,
            "meta": meta,
            "source_id": clip.get("source_id", ""),
            "source_title": src_title,
            "source_channel": clip.get("source_channel", ""),
            "clip_start": clip.get("clip_start", 0),
            "clip_dur": clip.get("clip_dur", 0),
        })
        reserved.append(n)
        n += 1

    # re-mark the harvested sources (lost in the same push race)
    used = state.setdefault("used_sources", [])
    before = len(used)
    for clip in clips:
        sid = clip.get("source_id")
        if sid and sid not in used:
            used.append(sid)

    state["bank"] = {
        "month": manifest.get("artifact", "").replace("bank-", "")
                  or clips and out[0]["slot"][:7],
        "clips": out,
        "reserved_n": reserved,
    }
    pointer = {
        "run_id": args.run_id,
        "artifact": manifest.get("artifact"),
        "ready": True,
    }

    xp_n = xi
    print(f"clips restored:   {len(out)}  (n {out[0]['n']}..{out[-1]['n']})")
    print(f"xp-family titles: {xp_n}, generic: {gi}")
    print(f"used_sources:     {before} -> {len(used)} "
          f"(+{len(used) - before} re-marked)")
    print(f"pointer:          run {args.run_id} / "
          f"{manifest.get('artifact')}")
    fn = all("fortnite" in c["meta"]["title"].lower() for c in out)
    cd = all("1789-8528-6429" not in c["meta"]["title"]
             and "H15" not in c["meta"]["title"] for c in out)
    print(f"every title contains Fortnite: {fn}")
    print(f"no codes in any title:         {cd}")
    print()
    for c in out[:6] + ["..."] + out[-2:]:
        if c == "...":
            print("  ...")
            continue
        print(f"  #{c['n']:>3} {c['meta']['title']}")
        print(f"        slot {c['slot']} | {c['file']}")

    if not args.apply:
        print("\nDRY RUN — pass --apply to write state.json + "
              "bank_pointer.json")
        return 0

    shutil.copy2(ROOT / "data" / "state.json",
                 ROOT / "data" / "state.json.bak")
    (ROOT / "data" / "state.json").write_text(
        json.dumps(state, indent=1))
    (ROOT / "data" / "bank_pointer.json").write_text(
        json.dumps(pointer, indent=1))
    print("\nAPPLIED: state.json + bank_pointer.json written "
          "(backup: data/state.json.bak)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
