"""Architecture tests for the owner's redesign:

  1. ONE daily session at 08:33 UTC fills ALL open grid slots
  2. 4 Shorts/day 6-hour grid (00:00/06:00/12:00/18:00 Cairo)
  3. hooking titles: NO map code, {n} only, <= 95 chars, "Fortnite"
     in EVERY title (owner rules 2026-11-13/14)
  4. cookie-jar pool: true round-robin rotation + multi-secret stitch
  5. bucketing: 5 jars -> 3 secrets under GitHub's 48 KB cap
  6. month-provision guard + artifact parts (owner rule 2026-11-14)

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
    # owner rule 2026-11-14: even 6-hour spacing (6+6+6+6 = 24h)
    hh = sorted(int(s.split(":")[0]) for s in sched.get("slots", []))
    wrap = 24 + hh[0] - hh[-1]
    gaps = [b - a for a, b in zip(hh, hh[1:])] + [wrap]
    check("grid spacing is every 6 hours", gaps == [6, 6, 6, 6],
          f"gaps={gaps}")
    check("no weekend slots left", not sched.get("weekend_slots"))
    check("daily session time set",
          sched.get("daily_run_utc") == "08:33")
    check("harvest target 124", int(cfg()["harvest"]["max_clips"]) == 124)
    check("session produces 4",
          int(cfg()["clip"]["max_clips_per_run"]) == 4)
    check("codes banned from titles", cfg()["codes"]["show_in_title"] is False)

    templates = (cfg()["youtube"]["title_templates"]
                 + cfg()["youtube"].get("xp_title_templates", []))
    check("20 hooking title templates", len(templates) == 20)
    ok = all("{map" not in t and "{creator" not in t for t in templates)
    check("no {map}/{creator} in any template", ok)
    ok = all("7992-0068-2838" not in t for t in templates)
    check("no map code in any template", ok)
    ok = all(len(t.format(n=999)) <= 95 for t in templates)
    check("all titles <= 95 chars", ok)
    # OWNER RULE (2026-11-14): the word "Fortnite" in EVERY template
    ok = all("fortnite" in t.format(n=999).lower() for t in templates)
    check("every template contains Fortnite", ok,
          str([t for t in templates
               if "fortnite" not in t.format(n=999).lower()]))

    brand = cfg().get("branding") or {}
    check("branding block present", bool(brand.get("description")))
    check("branding keywords present", len(brand.get("keywords") or []) >= 8)

    # ── 2. metadata: hooking titles, no code, unique #N ─────────────
    state = {"shorts": [{"n": 12}], "used_sources": [],
             "bank": {"reserved_n": [13]}}
    titles = set()
    clean = True
    fortnite_ok = True
    for seed in range(40):
        src = {"query": "fortnite xp map gameplay"} if seed % 2 else {}
        meta = metadata.build(src, state, seed)
        titles.add(meta["title"])
        if "1789" in meta["title"] or "H15" in meta["title"]:
            clean = False
        if "fortnite" not in meta["title"].lower():
            fortnite_ok = False
    check("no code in 40 generated titles", clean)
    check("every generated title contains Fortnite", fortnite_ok)
    # the runtime repair: a Fortnite-less title can never ship even
    # if a future template edit breaks the config rule
    broken = {"query": "", "title": ""}
    saved = cfg()["youtube"]["title_templates"]
    cfg()["youtube"]["title_templates"] = ["Free skins, no codes #{n}"]
    m = metadata.build(broken, state, 3)
    cfg()["youtube"]["title_templates"] = saved
    check("runtime guard prefixes Fortnite",
          m["title"].startswith("Fortnite"), m["title"])
    check("40 seeds -> >= 10 distinct titles", len(titles) >= 10,
          f"{len(titles)} distinct")
    m = metadata.build({"query": "fortnite xp map gameplay"}, state, 1)
    check("description has MAP CODE", "7992-0068-2838" in m["description"])
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

    # ── 7. month-provision guard + artifact parts (2026-11-14) ────
    from datetime import timezone

    def guard_rule(bankst, thr=8, target=124, ptr=True):
        # replica of the guard verdict in monthly-harvest.yml
        clips = len(bankst.get("clips") or [])
        harvested = bankst.get("harvested_total") or 0
        month = bankst.get("month") or ""
        now_m = datetime.now(timezone.utc).strftime("%Y-%m")
        provisioned = month == now_m and harvested >= target
        return (not provisioned) or clips <= thr or not ptr

    now_m = datetime.now(timezone.utc).strftime("%Y-%m")
    check("guard RUNs a partially provisioned month",
          guard_rule({"month": now_m, "clips": [1] * 25,
                      "harvested_total": 25}))
    check("guard SKIPs a provisioned month",
          not guard_rule({"month": now_m, "clips": [1] * 40,
                          "harvested_total": 124}))
    check("guard RUNs a drained bank anyway",
          guard_rule({"month": now_m, "clips": [],
                      "harvested_total": 124}))
    check("guard RUNs a stale month (rollover)",
          guard_rule({"month": "2000-01", "clips": [1] * 40,
                      "harvested_total": 124}))

    st = {"bank": {"month": "2000-01", "clips": [], "reserved_n": [],
                   "harvested_total": 3}}
    bank.add(st, "2000-01-01T00:00:00+00:00", "f.mp4",
             {"title": "t", "description": "d", "tags": [], "n": 9},
             {"id": "x", "title": "s", "channel": "c"}, 0, 60)
    check("bank.add bumps the provision meter",
          st["bank"]["harvested_total"] == 4)
    bank.reset(st)
    check("bank.reset zeroes the provision meter",
          st["bank"]["harvested_total"] == 0
          and st["bank"]["clips"] == [])

    # ── PUBLISH-TIME TRUTH (live-diagnosed 2026-11-18) ──────────────
    # A banked clip was rendered + described at HARVEST time; a code
    # switch after the harvest must not ship the old code for weeks.
    stale = {"title": "This Fortnite map levels you up FAST 🔥 #31",
             "description": "🗺️ MAP CODE: 1789-8528-6429\n"
                            "🛒 CREATOR CODE: H15 — old baked copy",
             "tags": ["fortnite"], "n": 31}
    fresh = metadata.refresh_description(stale)
    check("refresh_description swaps in the CURRENT map code",
          cfg()["codes"]["map_code"] in fresh["description"]
          and "1789-8528-6429" not in fresh["description"])
    check("refresh_description keeps the banked title",
          fresh["title"] == stale["title"])
    check("refresh_description keeps tags/n",
          fresh["tags"] == stale["tags"] and fresh["n"] == 31)
    check("refresh_description carries the creator code + TikTok link",
          cfg()["codes"]["creator_code"] in fresh["description"]
          and "tiktok.com/@h15master" in fresh["description"])
    from src import overlay as ov, transform
    with tempfile.TemporaryDirectory() as rtd:
        top, foot = ov.render_restamp(Path(rtd))
        check("restamp bars render on demand (no cache)",
              top.exists() and foot.exists())
        from PIL import Image
        with Image.open(top) as im:
            alpha = im.getextrema()[3][1] if im.mode == "RGBA" else 255
        check("restamp bars are FULLY opaque (old code cannot bleed)",
              alpha == 255)
    missing = transform.restamp_current(Path("/nonexistent.mp4"))
    check("restamp failure degrades to the original file",
          str(missing) == "/nonexistent.mp4")

    with tempfile.TemporaryDirectory() as td:
        bdir = Path(td)
        for i in range(2):
            (bdir / f"short_{i}.mp4").write_bytes(b"x" * 1000)
        (bdir / "manifest.json").write_text("{}")
        (bdir / "artifact_name.txt").write_text("bank-2000-01")
        n_parts = bank.split_parts(bdir, cap_bytes=1500)
        check("parts split under the cap", n_parts == 2, f"{n_parts}")
        ok = all(p.stat().st_size <= 1500
                 for d in bdir.glob("p*") for p in d.iterdir()
                 if p.suffix == ".mp4")
        check("every part stays under the cap", ok)
        check("manifest rides in part 1",
              (bdir / "p1" / "manifest.json").exists())
        check("parts file written for the workflow",
              (bdir / "artifact_parts.txt").read_text() == "2")
        # fresh flat layout -> default cap keeps both files in one part
        for d in bdir.glob("p*"):
            for p in list(d.iterdir()):
                p.rename(bdir / p.name)
            d.rmdir()
        (bdir / "artifact_parts.txt").unlink()
        n_parts = bank.split_parts(bdir)   # default ~4.2 GB cap
        check("small banks stay in one part", n_parts == 1, f"{n_parts}")
        check("manifest back in part 1",
              (bdir / "p1" / "manifest.json").exists())

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("ALL GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
