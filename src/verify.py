"""Post-publish verification — ported from forever-factory.

The Iron Law: no completion claims without fresh verification
evidence. oEmbed is anonymous, free, ZERO quota; a video the probe
sees public is stamped in the ledger (live evidence only). A Short
older than grace_days that is STILL not public raises a VERIFY-ALERT
marker the watchdog turns into a GitHub Issue.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

from . import ledger
from .config import cfg

_OEMBED = "https://www.youtube.com/oembed"


def oembed_public(video_id: str) -> bool | None:
    """True = public. False = 4xx (private/missing). None = probe
    blind (network) — never an alert."""
    url = (f"{_OEMBED}?url=https://www.youtube.com/watch?v={video_id}"
           f"&format=json")
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (factory verify)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403, 404):
            return False
        return None
    except Exception:
        return None


def run(state: dict, *, today: date | None = None,
        probe=oembed_public) -> dict:
    conf = (cfg().get("verify") or {})
    if not conf.get("enabled", True):
        print("[verify] disabled in channel.yaml — skipping")
        return {"confirmed": 0, "late": 0, "alerts": []}
    grace_days = int(conf.get("grace_days", 2))
    scan_days = int(conf.get("scan_days", 10))
    today = today or date.today()
    cutoff = today - timedelta(days=scan_days)

    q = state.get("quota") or {}
    defer_active = (q.get("defer_until") or 0) > time.time()

    confirmed = late = 0
    alerts: list[str] = []
    dirty = False
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    try:
        recent = [s for s in state.get("shorts", [])
                  if s.get("video_id")
                  and date.fromisoformat(s.get("date") or "1970-01-01")
                  >= cutoff]
    except (TypeError, ValueError):
        recent = []

    for s in recent:
        if s.get("verified"):
            confirmed += 1
            continue
        verdict = probe(s["video_id"])
        if verdict is True:
            s["verified"] = now_iso
            confirmed += 1
            dirty = True
        elif verdict is False:
            try:
                age = (today - date.fromisoformat(s.get("date"))).days
            except ValueError:
                age = 0
            if age > grace_days and not defer_active:
                late += 1
                line = (f"VERIFY-ALERT: short #{s.get('n')} "
                        f"'{(s.get('source_title') or '')[:40]}' — "
                        f"{s['video_id']} still not public {age}d "
                        f"after production")
                alerts.append(line)
                print(line)

    if defer_active:
        print("[verify] quota defer active — alerts suppressed")
    print(f"[verify] {confirmed} confirmed public, {late} late")
    if dirty:
        try:
            ledger.save(state)
        except Exception as exc:
            print(f"[verify] ledger save skipped ({exc})")
    return {"confirmed": confirmed, "late": late, "alerts": alerts}
