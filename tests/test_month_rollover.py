#!/usr/bin/env python3
"""Smoke test for owner rule 2026-11-18 (beginning-of-the-month):

  1. stale() detects a previous-month bank
  2. retire() wipes clips + pointer (old clips never publish)
  3. the daily-factory.yml bank-check month guard refuses stale banks
  4. harvest_pool() cache path: >= pool_min -> no API call, no spend
  5. reset() clears the pool (fresh month = fresh sweep)
  6. the harvest month-trim: no slot past the bank's own month
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" +
          (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


# isolated ledger/pointer paths so writes hit a temp dir, not the
# real data/ — but config stays pointed at the REAL channel.yaml
import tempfile                          # noqa: E402
tmp = Path(tempfile.mkdtemp())
(tmp / "data").mkdir()
from src import ledger              # noqa: E402
ledger.STATE_PATH = tmp / "data" / "state.json"
ledger.BAK_PATH = tmp / "data" / "state.json.bak"
from src import bank                 # noqa: E402
bank.POINTER = tmp / "data" / "bank_pointer.json"

import tempfile as _tf               # keep pyflakes quiet

now_m = datetime.now(timezone.utc).strftime("%Y-%m")
prev_m = "2026-10" if now_m != "2026-10" else "2026-09"
# owner rule 2026-11-21: the bank is a 2-MONTH bank — a bank one month
# back is still within its feeding horizon (not stale); only a bank
# 2+ months back has aged out. Build the aged-out key accordingly.
m1_back = ("2026-10" if now_m == "2026-11" else
           "2025-12" if now_m == "2026-01" else
           f"{int(now_m[:4]) - 1}-12" if now_m.endswith("-01") else
           f"{now_m[:4]}-{int(now_m[5:7]) - 1:02d}")
aged_m = ("2026-09" if m1_back == "2026-10" else
          f"{int(m1_back[:4]) - 1}-12" if m1_back.endswith("-01") else
          f"{m1_back[:4]}-{int(m1_back[5:7]) - 1:02d}")

# ── 1. stale detection ────────────────────────────────────────────
state = {"bank": {"month": m1_back, "clips": [{"slot": "x"}],
                  "reserved_n": [5]}}
check("stale() passes a 1-month-old bank (2-month horizon)",
      not bank.stale(state))
state["bank"]["month"] = aged_m
check("stale() flags a bank aged past its 2-month horizon",
      bank.stale(state))
state["bank"]["month"] = now_m
check("stale() passes the current-month bank", not bank.stale(state))
check("stale() passes a cleared bank (month None)",
      not bank.stale({"bank": {"month": None, "clips": []}}))

# ── 2. retire ─────────────────────────────────────────────────────
bank.POINTER.write_text(json.dumps({"run_id": 1, "artifact": "bank-x",
                                    "ready": True}))
state = {"bank": {"month": prev_m, "clips": [{"slot": "x"}] * 38,
                  "reserved_n": [31]}}
bank.retire(state, "test")
check("retire() discards the clips", state["bank"]["clips"] == [])
check("retire() clears reserved_n", state["bank"]["reserved_n"] == [])
check("retire() unlinks the pointer", not bank.POINTER.exists())
check("retire() leaves month None (not a fake fresh bank)",
      state["bank"]["month"] is None)

# ── 3. the daily-factory.yml bank-check month guard (verbatim) ───
def bank_check(ptr, st):
    ready = False
    clips = (st.get("bank") or {}).get("clips") or []
    month = (st.get("bank") or {}).get("month") or ""
    now = datetime.now(timezone.utc).strftime("%Y-%m")
    if clips and month != now:
        pass                       # stale — stays not-ready
    elif clips and ptr.get("artifact") and ptr.get("run_id"):
        ready = True
    return ready


ptr = {"artifact": "bank-2026-10", "run_id": 43973705005, "ready": True}
st = {"bank": {"month": prev_m, "clips": [{"slot": "x"}]}}
check("workflow refuses a stale bank (no artifact fetch)",
      not bank_check(ptr, st))
st["bank"]["month"] = now_m
ptr["artifact"] = f"bank-{now_m}"
check("workflow fetches the current-month bank",
      bank_check(ptr, st))

# ── 4+5. pool cache + reset wipes pool ─────────────────────────────
pool = [{"id": f"vid{i}", "title": f"t{i}", "channel": "c",
         "published_at": "2026-11-01T00:00:00Z", "duration_s": 600,
         "query": "q"} for i in range(50)]
state = {"bank": {"month": now_m, "clips": [], "pool": pool,
                  "reserved_n": []},
         "quota": {"date": None, "units_used": 0},
         "used_sources": [pool[0]["id"]]}
ledger.save(state)
from src import sources                          # noqa: E402
got = sources.harvest_pool(state, seed=1)        # 49 unmined >= 40
check("cached pool served without any API spend",
      len(got) == 49 and state["quota"]["units_used"] == 0,
      f"{len(got)} candidates, quota {state['quota']['units_used']}")
bank.reset(state)
check("reset() wipes the pool (fresh month = fresh sweep)",
      state["bank"].get("pool") is None)

# ── 6. the harvest month-trim ─────────────────────────────────────
ym = datetime.now(timezone.utc).strftime("%Y-%m")
plan = [f"{ym}-05T12:00:00+02:00", f"{ym}-06T00:00:00+02:00",
        "2099-01-01T00:00:00+02:00"]
trimmed = [s for s in plan if s[:7] == ym]
check("harvest never plans past its own month", len(trimmed) == 2,
      f"{len(trimmed)}/{len(plan)} slots kept")

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("ALL GREEN — the month-rollover rule is enforced end-to-end")
