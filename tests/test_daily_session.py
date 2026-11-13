"""Architecture tests for the owner's 2026-11-13 redesign:

  1. ONE daily session at 08:30 UTC fills ALL open grid slots
  2. 4 Shorts/day grid (00:00/08:00/16:00/20:00 Cairo, every day)
  3. hooking titles: NO map code in titles, {n} only, <= 95 chars
  4. cookie-jar pool: true round-robin rotation + multi-secret stitch
  5. bucketing: 5 jars -> 3 secrets under GitHub's 48 KB cap

Run:  python3 tests/test_daily_session.py
(no pytest dependency; plain asserts + prints)
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import bank, downloader, metadata          # noqa: E402
from src.config import cfg                          # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def main() -> int:
    # ── 1. config: the new grid + templates ──────────────────────────
    sched = cfg()["schedule"]
    check("grid is 4 slots/day", len(sched.get("slots", [])) == 4,
          str(sched.get("slots")))
    check("no weekend slots left", not sched.get("weekend_slots"))
    check("daily session time set",
          sched.get("daily_run_utc") == "08:30")
    check("harvest target 124", int(cfg()["harvest"]["max_clips"]) == 124)
    check("session produces 4",
          int(cfg()["clip"]["max_clips_per_run"]) == 4)
    check("codes banned from titles", cfg()["codes"]["show_in_title"] is False)

    templates = (cfg()["youtube"]["title_templates"]
                 + cfg()["youtube"].get("xp_title_templates", []))
    check("20 hooking title templates", len(templates) == 20)
    ok = all("{map" not in t and "{creator" not in t for t in templates)
    check("no {map}/{creator} in any template", ok)
    ok = all("1789-8528-6429" not in t for t in templates)
    check("no map code in any template", ok)
    ok = all(len(t.format(n=999)) <= 95 for t in templates)
    check("all titles <= 95 chars", ok)

    brand = cfg().get("branding") or {}
    check("branding block present", bool(brand.get("description")))
    check("branding keywords present", len(brand.get("keywords") or []) >= 8)

    # ── 2. metadata: hooking titles, no code, unique #N ─────────────
    state = {"shorts": [{"n": 12}], "used_sources": [],
             "bank": {"reserved_n": [13]}}
    titles = set()
    clean = True
    for seed in range(40):
        src = {"query": "fortnite xp map gameplay"} if seed % 2 else {}
        meta = metadata.build(src, state, seed)
        titles.add(meta["title"])
        if "1789" in meta["title"] or "H15" in meta["title"]:
            clean = False
    check("no code in 40 generated titles", clean)
    check("40 seeds -> >= 10 distinct titles", len(titles) >= 10,
          f"{len(titles)} distinct")
    m = metadata.build({"query": "fortnite xp map gameplay"}, state, 1)
    check("description has MAP CODE", "1789-8528-6429" in m["description"])
    check("description has CREATOR CODE", "H15" in m["description"])
    check("numbering skips reserved", m["n"] == 14,
          f"n={m['n']} (12 published, 13 reserved)")

    # ── 3. gate agrees with the session (same rule) ──────────────────
    real_state = json.loads((ROOT / "data" / "state.json").read_text())
    targets = bank.open_targets(real_state)
    check("open_targets finds <= 4 slots", len(targets) <= 4,
          f"{len(targets)}: {targets}")
    tz = ZoneInfo(sched["timezone"])
    now = datetime.now(tz)
    ok = all(datetime.fromisoformat(t) > now for t in targets)
    check("all targets are in the future", ok)
    check("targets sorted", targets == sorted(targets))
    import subprocess
    gate = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "gate.py"),
         "--event", "schedule"], capture_output=True, text=True)
    first = gate.stdout.splitlines()[0] if gate.stdout else ""
    check("gate verdict well-formed",
          first.startswith("RUN") or first.startswith("SKIP"),
          first.split(" # ")[0])
    n_holes = len(targets)
    check("gate agrees with open_targets count",
          (f"{n_holes} open slot" in first) if n_holes
          else first.startswith("SKIP"), first)

    # ── 4. jar pool: rotation + multi-secret stitch ──────────────────
    jar_names = ["j0", "j1", "j2", "j3", "j4"]
    leaders = []
    for _ in range(10):
        leaders.append(downloader._rotate(jar_names)[0])
    check("rotation cycles 0..4", leaders[:5] == ["j0", "j1", "j2",
                                                  "j3", "j4"],
          str(leaders[:6]))

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        j1 = "\n".join(["# Netscape HTTP Cookie File",
                        ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc"])
        j2 = "\n".join(["# Netscape HTTP Cookie File",
                        ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tdef"])
        import os
        os.environ["COOKIES_TXT"] = j1
        os.environ["COOKIES_TXT2"] = j2
        os.environ["COOKIES_TXT3"] = ""
        jars = downloader._cookie_jars(work)
        check("two secrets stitch into 2 jars", len(jars) == 2,
              f"{len(jars)} jars")
        for k in ("COOKIES_TXT", "COOKIES_TXT2", "COOKIES_TXT3"):
            os.environ.pop(k, None)

    # ── 5. bucketing: 5 x 18 KB jars -> 3 secrets ────────────────────
    sys.path.insert(0, str(ROOT / "tools"))
    sc = __import__("set_cookies")
    blocks = [("jar %d\n" % i) + "x" * 18_000 for i in range(5)]
    buckets = sc._bucket(blocks)
    check("5 jars -> 3 buckets", len(buckets) == 3,
          str([len(b) for b in buckets]))
    ok = all(sum(len(x) for x in b) <= 40_000 for b in buckets)
    check("every bucket under 40 KB", ok)
    check("bucket separators parseable",
          all(sc.JAR_SEP.match(f"===== JAR {n} =====")
              for n in (2, 4)))

    # ── 6. import smoke: nothing broke ──────────────────────────────
    ok = True
    for mod in ("orchestrator", "youtube", "verify", "ledger",
                "sources", "clipper", "transform", "overlay", "music",
                "config"):
        try:
            __import__(f"src.{mod}")
        except Exception as e:      # noqa: BLE001
            ok = False
            print(f"      import src.{mod} failed: {e}")
    check("all src modules import cleanly", ok)

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("ALL GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
