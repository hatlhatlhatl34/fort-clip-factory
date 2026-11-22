"""Architecture tests for the owner's redesign:

  1. ONE daily session at 08:33 UTC fills ALL open grid slots
  2. 4 Shorts/day 6-hour grid (01:00/07:00/13:00/19:00 Cairo —
     US-tuned phase, owner rule 2026-11-23)
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
    check("harvest target 250", int(cfg()["harvest"]["max_clips"]) == 250)
    check("session produces 4",
          int(cfg()["clip"]["max_clips_per_run"]) == 4)
    check("codes banned from titles", cfg()["codes"]["show_in_title"] is False)

    # ── 1b. the TITLE ENGINE (owner rule 2026-11-22: never duplicate)
    eng = (cfg()["youtube"].get("title_engine") or {})
    parts = [p for k in ("subjects", "predicates", "xp_subjects",
                         "xp_predicates") for p in (eng.get(k) or [])]
    check("title_engine parts configured", len(parts) >= 40,
          f"{len(parts)} parts")
    ok = all("7992-0068-2838" not in p and "H15" not in p for p in parts)
    check("no codes in any engine part", ok)
    # OWNER RULE (2026-11-14): the word "Fortnite" in EVERY SUBJECT
    subs = (eng.get("subjects") or []) + (eng.get("xp_subjects") or [])
    ok = all("fortnite" in s.lower() for s in subs)
    check("every subject contains Fortnite", ok,
          str([s for s in subs if "fortnite" not in s.lower()]))
    for fam in (("subjects", "predicates"), ("xp_subjects", "xp_predicates")):
        cap = len(eng.get(fam[0]) or []) * len(eng.get(fam[1]) or [])
        check(f"family {fam[0]} x {fam[1]} capacity >= 150", cap >= 150,
              f"{cap} combos")
    # longest possible composition (lead + longest subject + longest
    # predicate + "#999") still fits YouTube's hard 100-char limit
    lead = max(eng.get("leads") or [""], key=len)
    worst = max(len(f"{lead} {s} {p} #999")
                for s, p in [(
                    max((eng.get("subjects") or []) +
                        (eng.get("xp_subjects") or []), key=len),
                    max((eng.get("predicates") or []) +
                        (eng.get("xp_predicates") or []), key=len))])
    check("worst-case title <= 95 chars", worst <= 95, f"{worst} chars")

    brand = cfg().get("branding") or {}
    check("branding block present", bool(brand.get("description")))
    check("branding keywords present", len(brand.get("keywords") or []) >= 8)

    # ── 2. metadata: hooking titles, no code, unique skeletons ─────
    # (owner rule 2026-11-22: no two titles may ever share a skeleton —
    # the sim mirrors the real flow: every built title is recorded in
    # state BEFORE the next build, exactly as bank.add does in-flight)
    state = {"shorts": [{"n": 12}], "used_sources": [],
             "bank": {"reserved_n": [13], "clips": []}}
    skeletons: list[str] = []
    clean = True
    fortnite_ok = True
    for seed in range(60):
        src = {"query": "fortnite xp map gameplay"} if seed % 2 else {}
        meta = metadata.build(src, state, seed)
        skeletons.append(metadata._skeleton(meta["title"]))
        if "7992" in meta["title"] or "H15" in meta["title"]:
            clean = False
        if "fortnite" not in meta["title"].lower():
            fortnite_ok = False
        state["shorts"].append({"n": meta["n"], "title": meta["title"]})
        state["bank"]["reserved_n"].append(meta["n"])
    check("no code in 60 generated titles", clean)
    check("every generated title contains Fortnite", fortnite_ok)
    check("60 generated titles -> 60 distinct skeletons",
          len(set(skeletons)) == 60, f"{len(set(skeletons))} distinct")
    # the 250-clip bank era (owner rule 2026-11-21) needs 250+ unique
    # skeletons without any (part k) fallback fires
    big_state = {"shorts": [], "used_sources": [],
                 "bank": {"reserved_n": [], "clips": []}}
    n_fallback = 0
    for seed in range(300):
        src = {"query": "fortnite xp map gameplay"} if seed % 5 else {}
        meta = metadata.build(src, big_state, seed)
        if "(part " in meta["title"]:
            n_fallback += 1
        big_state["shorts"].append({"n": meta["n"],
                                     "title": meta["title"]})
        big_state["bank"]["reserved_n"].append(meta["n"])
    sk = [metadata._skeleton(s["title"]) for s in big_state["shorts"]]
    check("300-clip sim -> 300 unique skeletons",
          len(set(sk)) == 300, f"{len(set(sk))} distinct")
    check("no fallback part-suffix needed in 300 clips", n_fallback == 0,
          f"{n_fallback} fallbacks")
    # the runtime repair: a Fortnite-less title can never ship even
    # if a future config edit breaks the engine rule
    broken = {"query": "", "title": ""}
    saved = dict(cfg()["youtube"]["title_engine"])
    cfg()["youtube"]["title_engine"] = dict(saved, subjects=["Secret map"])
    m = metadata.build(broken, state, 3)
    cfg()["youtube"]["title_engine"] = saved
    check("runtime guard prefixes Fortnite",
          m["title"].startswith("Fortnite"), m["title"])
    m = metadata.build({"query": "fortnite xp map gameplay"}, state, 1)
    check("description has MAP CODE", "7992-0068-2838" in m["description"])
    check("description has CREATOR CODE", "H15" in m["description"])
    # numbering skips reserved — checked on a PRISTINE state so the
    # 60-title sims above haven't bumped the counters yet
    fresh_state = {"shorts": [{"n": 12}], "used_sources": [],
                   "bank": {"reserved_n": [13], "clips": []}}
    m = metadata.build({"query": "fortnite xp map gameplay"}, fresh_state, 1)
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
          first.startswith(("RUN", "SKIP", "HOLD")),
          first.split(" # ")[0])
    n_holes = len(targets)
    if first.startswith("HOLD"):
        # owner rule 2026-11-21: open slots DURING the 2-day hold are
        # expected — the gate is right to hold, nothing to fill yet
        check("gate holds during the publishing hold", True)
    else:
        check("gate agrees with open_targets count",
              (f"{n_holes} open slot" in first) if n_holes
              else first.startswith("SKIP"), first)

    # ── 4. jar pool: rotation + multi-secret stitch ──────────────────
    jar_names = ["j0", "j1", "j2", "j3", "j4"]
    leaders = []
    for _ in range(10):
        leaders.append(downloader._rotate(jar_names)[0])
    # audit 2026-11-18 (B6): the cursor is seeded by a 15-minute time
    # bucket so every runner session starts on a DIFFERENT jar — the
    # invariant is full cyclic coverage, not a fixed start at jar #1
    # (the old always-starts-at-#1 behavior put account 1 in front of
    # the most wall-exposed download of every session)
    start = jar_names.index(leaders[0])
    check("rotation cycles through all jars",
          leaders[:5] == jar_names[start:] + jar_names[:start],
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
