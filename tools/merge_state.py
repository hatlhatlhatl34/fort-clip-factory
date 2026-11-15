#!/usr/bin/env python3
"""Merge two state.json versions field-by-field (the anti-race tool).

WHY (live-diagnosed 2026-11-15 17:42): the heal's factory job and the
crosspost job run on SEPARATE runners of the same workflow. The
factory pushed its memory seconds before the crosspost job's checkout
fetched; at commit time `git pull --rebase --autostash` popped the
stash into a state.json conflict, `git commit` died, and `|| true`
swallowed the whole loss — that run's cross-posted reels vanished
from state and the next run re-posted them (duplicates went live on
both platforms).

THE RULE: git must never line-merge state.json. The commit step now
does `git checkout origin/main -- data/state.json` (freshest pushed
memory = BASE), re-applies THIS job's just-written state (OURS)
through this tool, and pushes the union — no conflict possible.

DIRECTION (who owns the non-crosspost keys):
  --prefer crosspost  (default) — the crosspost job's commit: BASE
      wins bank/slots/used_sources (the factory owns those); OURS
      wins the crosspost fields it just wrote.
  --prefer core       — the factory job's commit: OURS wins
      bank/slots/shorts-core (it just did that work); BASE wins
      crosspost fields unless OURS ranks higher.

CROSSPOST FIELDS are merged by STATUS RANK so a stale 'pending' can
never clobber a fresh 'done', no matter the direction:
  done(4) > gave_up/skipped(3) > failed(2) > pending(1); ties break
  on newer ts, then OURS.

Usage:
  python tools/merge_state.py OURS BASE [--prefer core|crosspost]
         [--inplace OUT]      (> merged.json without --inplace)
Exit 0 ok; 2 usage; 3 unreadable input.
"""
from __future__ import annotations

import json
import sys

STATUS_RANK = {"done": 4, "gave_up": 3, "skipped": 3, "failed": 2,
               "pending": 1}
PLATFORMS = ("ig", "fb")


def _nonempty(v) -> bool:
    return v is not None and not (isinstance(v, (dict, list, str)) and not v)


def _plat_merge(ours: dict, base: dict) -> dict:
    """One platform's record — higher status rank wins, then newer
    ts, then ours (this job just wrote it)."""
    if not ours:
        return dict(base)
    if not base:
        return dict(ours)
    ro = STATUS_RANK.get(str(ours.get("status")), 0)
    rb = STATUS_RANK.get(str(base.get("status")), 0)
    if ro != rb:
        return dict(ours if ro > rb else base)
    if int(ours.get("ts") or 0) >= int(base.get("ts") or 0):
        return dict(ours)
    return dict(base)


def _crosspost_merge(ours: dict | None, base: dict | None) -> dict:
    ours, base = ours or {}, base or {}
    out: dict = {}
    for p in PLATFORMS:
        m = _plat_merge(ours.get(p) or {}, base.get(p) or {})
        if m:
            out[p] = m
    if ours.get("yt_links") or base.get("yt_links"):
        out["yt_links"] = True
    for src in (base, ours):           # any extra bookkeeping keys
        for k, v in src.items():
            if k not in out and k not in PLATFORMS and k != "yt_links":
                out.setdefault(k, v)
    return out


def merge_shorts(ours: list, base: list, prefer: str) -> list:
    def index(recs):
        out: dict = {}
        for r in recs or []:
            if isinstance(r, dict) and _nonempty(r.get("n")):
                out[r["n"]] = dict(r)
        return out
    o, b = index(ours), index(base)
    keys = sorted(set(o) | set(b), key=lambda x: (str(type(x)), str(x)))
    merged: list = []
    for k in keys:
        if k not in o:
            merged.append(b[k])
            continue
        if k not in b:
            merged.append(o[k])
            continue
        core, other = (o[k], b[k]) if prefer == "core" else (b[k], o[k])
        rec = dict(other)                       # filler keys from the
        rec.update(core)                        # non-owner, owner wins
        cp = _crosspost_merge(o[k].get("crosspost"), b[k].get("crosspost"))
        if cp:
            rec["crosspost"] = cp
        else:
            rec.pop("crosspost", None)
        for f in ("media", "clip_dur"):
            if not _nonempty(rec.get(f)):
                rec[f] = other.get(f) or core.get(f)
        merged.append(rec)
    return merged


def merge(ours: dict, base: dict, prefer: str = "crosspost") -> dict:
    prefer = "core" if prefer == "core" else "crosspost"
    out = dict(ours if prefer == "core" else base)   # non-owned keys
    out["shorts"] = merge_shorts(ours.get("shorts") or [],
                                 base.get("shorts") or [], prefer)
    stats: dict = dict(base.get("stats") or {})
    for k, v in (ours.get("stats") or {}).items():
        try:
            stats[k] = max(stats.get(k, v), v)
        except TypeError:
            stats[k] = v
    out["stats"] = stats
    for key in ("crosspost_ids", "alerts_sent"):
        m = dict(base.get(key) or {})
        for k, v in (ours.get(key) or {}).items():
            if _nonempty(v) or k not in m:
                m[k] = v
        out[key] = m
    return out


def main() -> int:
    args = sys.argv[1:]
    prefer, inplace, positional = "crosspost", None, []
    i = 0
    while i < len(args):
        if args[i] == "--prefer" and i + 1 < len(args):
            prefer = args[i + 1]
            i += 2
        elif args[i] == "--inplace" and i + 1 < len(args):
            inplace = args[i + 1]
            i += 2
        else:
            positional.append(args[i])
            i += 1
    if len(positional) != 2 or prefer not in ("core", "crosspost"):
        print(__doc__)
        return 2
    try:
        ours = json.loads(open(positional[0], encoding="utf-8").read())
        base = json.loads(open(positional[1], encoding="utf-8").read())
    except (OSError, json.JSONDecodeError) as e:
        print(f"merge_state: cannot read inputs: {e}", file=sys.stderr)
        return 3
    merged = merge(ours, base, prefer)
    blob = json.dumps(merged, indent=1, ensure_ascii=False) + "\n"
    if inplace:
        open(inplace, "w", encoding="utf-8").write(blob)
        print(f"merge_state: wrote {inplace} (prefer={prefer}, "
              f"{len(merged.get('shorts') or [])} shorts, "
              f"stats={merged.get('stats')})")
    else:
        sys.stdout.write(blob)
    return 0


if __name__ == "__main__":
    sys.exit(main())
