"""Anti-race tests for tools/merge_state.py (the 2026-11-15 17:42
post-mortem: a git stash-pop conflict silently ate the crosspost
job's memory and the next run re-posted two reels as duplicates).

Run:  python3 tests/test_merge_state.py
(no pytest dependency; plain asserts + prints)
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "merge_state.py"

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail and not cond else ""))
    PASS, FAIL = PASS + (1 if cond else 0), FAIL + (0 if cond else 1)


def run_tool(ours: dict, base: dict, prefer: str = "crosspost") -> dict:
    with tempfile.TemporaryDirectory() as td:
        po, pb = Path(td) / "ours.json", Path(td) / "base.json"
        po.write_text(json.dumps(ours))
        pb.write_text(json.dumps(base))
        r = subprocess.run(
            [sys.executable, str(TOOL), str(po), str(pb),
             "--prefer", prefer],
            capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)


def short(n, **kw):
    d = {"n": n}
    d.update(kw)
    return d


# ── 1. the exact 17:42 incident, crosspost direction ──────────────
# base (origin, factory-fresh) has the heal's bank churn but NO
# crosspost memory (it was lost); ours (crosspost job) has the reels.
ours = {"shorts": [short(11, video_id="V11",
                         crosspost={"ig": {"status": "done",
                                           "media_id": "IG11",
                                           "ts": 100},
                                    "fb": {"status": "done",
                                           "media_id": "FB11",
                                           "ts": 101},
                                    "yt_links": True})],
        "stats": {"shorts_published": 21},
        "crosspost_ids": {"ig": "178", "fb": "131"},
        "bank": {"clips": []}}
base = {"shorts": [short(11, video_id="V11", title="t11",
                         slot="2026-11-15T12:00:00+02:00"),
                   short(24, video_id="V24")],
        "stats": {"shorts_published": 21, "days_active": 0},
        "bank": {"clips": ["c1", "c2"], "reserved_n": 2}}
m = run_tool(ours, base)
cp = (m["shorts"][0].get("crosspost") or {})
check("incident: the crossposted reels survive the merge",
      cp.get("ig", {}).get("status") == "done"
      and cp.get("fb", {}).get("media_id") == "FB11")
check("incident: yt_links survives",
      cp.get("yt_links") is True)
check("incident: the factory's bank wins (crosspost job owns it not)",
      m["bank"] == base["bank"])
check("incident: slot/title from base ride along",
      m["shorts"][0].get("slot", "").startswith("2026-11-15")
      and m["shorts"][0].get("title") == "t11")
check("incident: base-only short #24 kept",
      any(s.get("n") == 24 for s in m["shorts"]))
check("incident: crosspost_ids armed",
      m["crosspost_ids"] == {"ig": "178", "fb": "131"})

# ── 2. factory direction: stale pending must NOT clobber done ────
ours = {"shorts": [short(5, video_id="V5",
                         crosspost={"ig": {"status": "pending",
                                           "attempts": 1}})],
        "bank": {"clips": ["NEW"]}}
base = {"shorts": [short(5, video_id="V5",
                         crosspost={"ig": {"status": "done",
                                           "media_id": "IG5",
                                           "ts": 99},
                                    "yt_links": True})],
        "bank": {"clips": ["OLD"]}}
m = run_tool(ours, base, prefer="core")
check("factory: done survives a stale pending",
      m["shorts"][0]["crosspost"]["ig"]["status"] == "done")
check("factory: yt_links survives",
      m["shorts"][0]["crosspost"].get("yt_links") is True)
check("factory: the factory's fresh bank wins",
      m["bank"] == {"clips": ["NEW"]})

# ── 3. status rank + ts tie-break, both directions ─────────────────
ours = {"shorts": [short(7, crosspost={"ig": {"status": "failed",
                                               "ts": 50}})]}
base = {"shorts": [short(7, crosspost={"ig": {"status": "pending",
                                               "ts": 10}})]}
m = run_tool(ours, base, prefer="core")
check("rank: failed beats pending even in base",
      m["shorts"][0]["crosspost"]["ig"]["status"] == "failed")
ours = {"shorts": [short(7, crosspost={"ig": {"status": "pending",
                                               "ts": 50}})]}
base = {"shorts": [short(7, crosspost={"ig": {"status": "pending",
                                               "ts": 10}})]}
m = run_tool(ours, base, prefer="crosspost")
check("tie+ts: newer ts wins", m["shorts"][0]["crosspost"]["ig"]["ts"] == 50)

# ── 4. stats are a max-union ───────────────────────────────────────
m = run_tool({"stats": {"shorts_published": 30, "days_active": 0}},
             {"stats": {"shorts_published": 21, "bank_total": 9}})
check("stats: max per key + keys unioned",
      m["stats"] == {"shorts_published": 30, "days_active": 0,
                     "bank_total": 9})

# ── 5. gave_up/skipped settle correctly across writers ────────────
ours = {"shorts": [short(1, crosspost={
    "ig": {"status": "gave_up", "reason": "source video deleted",
           "ts": 200},
    "fb": {"status": "gave_up", "reason": "source video deleted",
           "ts": 200}})]}
base = {"shorts": [short(1, crosspost={
    "ig": {"status": "pending", "attempts": 0, "ts": 5},
    "fb": {"status": "pending", "attempts": 0, "ts": 5}})]}
m = run_tool(ours, base)
check("settle: gave_up beats pending from either side",
      m["shorts"][0]["crosspost"]["ig"]["status"] == "gave_up"
      and m["shorts"][0]["crosspost"]["fb"]["status"] == "gave_up")

# ── 6. media/clip_dur fill from the other side when missing ───────
ours = {"shorts": [short(9, video_id="V9", media={"file": "f9"})]}
base = {"shorts": [short(9, video_id="V9", clip_dur=77.5)]}
m = run_tool(ours, base, prefer="core")
s9 = m["shorts"][0]
check("media+clip_dur both survive across writers",
      s9.get("media", {}).get("file") == "f9" and s9.get("clip_dur") == 77.5)

# ── 7. --inplace write + CLI exit codes ────────────────────────────
with tempfile.TemporaryDirectory() as td:
    po, pb, out = (Path(td) / "o.json", Path(td) / "b.json",
                   Path(td) / "out.json")
    po.write_text(json.dumps({"shorts": [short(3, crosspost={
        "ig": {"status": "done", "ts": 1}})]}))
    pb.write_text(json.dumps({"shorts": [short(3)]}))
    r = subprocess.run([sys.executable, str(TOOL), str(po), str(pb),
                        "--inplace", str(out)], capture_output=True,
                       text=True, timeout=30)
    check("inplace: exit 0 + file written",
          r.returncode == 0 and out.exists())
    r2 = subprocess.run([sys.executable, str(TOOL)], capture_output=True,
                        text=True, timeout=30)
    check("usage: exit 2 with no args", r2.returncode == 2)

print()
print("ALL GREEN" if FAIL == 0 else f"{FAIL} FAILURE(S)")
sys.exit(1 if FAIL else 0)
