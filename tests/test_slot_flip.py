"""Slot-due flip rules (owner order 2026-11-23, strict grid scheduling).

The flip pass must:
  * flip AT MOST ONE still-private past-slot rider per pass (never in
    bursts — the 2026-11-23 burst must never repeat), earliest slot
    first;
  * never touch future-slot Shorts (their publishAt handles them);
  * never touch already-public past-slot Shorts.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import verify   # noqa: E402

TZ = ZoneInfo("Africa/Cairo")


class FakeResp:
    def __init__(self, status=200):
        self.status = status

    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mk(n, slot, vid=None):
    return {"n": n, "video_id": vid or f"vid{n}", "slot": slot,
            "date": "2026-11-23", "title": f"#{n}"}


@pytest.fixture
def flips(monkeypatch):
    """Stub every network surface the flip pass touches; record flips."""
    done = []

    monkeypatch.setattr(verify.youtube, "get_access_token",
                        lambda: "fake-token")

    def fake_status(token, ids):
        # shorts #82/#84 are still private riders, #83 already public
        return {f"vid82": {"privacyStatus": "private"},
                f"vid83": {"privacyStatus": "public"},
                f"vid84": {"privacyStatus": "private"},
                f"vid90": {"privacyStatus": "private"}}

    monkeypatch.setattr(verify, "_videos_status", fake_status)

    def fake_urlopen(req, timeout=60):
        body = req.data.decode() if req.data else ""
        if '"privacyStatus": "public"' in body:
            import json as _json
            done.append(_json.loads(body)["id"])
        return FakeResp()

    monkeypatch.setattr(verify.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(verify.youtube, "post_giveaway_comment",
                        lambda token, vid: True)
    monkeypatch.setattr(verify, "oembed_public",
                        lambda vid: True)   # everything else 'public'
    monkeypatch.setattr(verify.ledger, "save", lambda state: None)
    return done


def _now(days=0, hours=0):
    return datetime.now(TZ) + timedelta(days=days, hours=hours)


def test_one_flip_per_pass_cap(flips, monkeypatch):
    """Three past-due private riders -> exactly ONE flip, earliest
    slot first; the others log and wait for the next pass."""
    monkeypatch.setattr(verify, "datetime", _FixedNow(_now()))
    state = {"shorts": [
        _mk(82, _now(hours=-1).isoformat(), "vid82"),
        _mk(83, _now(hours=-2).isoformat(), "vid83"),
        _mk(84, _now(hours=-3).isoformat(), "vid84"),
    ]}
    verify.run(state)
    assert flips == ["vid84"]        # earliest past slot, ONE only


def test_future_slots_never_flip(flips, monkeypatch):
    monkeypatch.setattr(verify, "datetime", _FixedNow(_now()))
    state = {"shorts": [
        _mk(82, _now(hours=-1).isoformat(), "vid82"),
        _mk(90, _now(hours=+5).isoformat(), "vid90"),
    ]}
    verify.run(state)
    assert flips == ["vid82"]        # only the past-due rider flips


class _FixedNow:
    """Patch verify.datetime so datetime.now(tz) returns a fixed
    moment (the flip pass and _slot_due both read 'now')."""

    def __init__(self, moment):
        self._moment = moment

    def now(self, tz=None):
        return self._moment

    def fromisoformat(self, s):
        return datetime.fromisoformat(s)
