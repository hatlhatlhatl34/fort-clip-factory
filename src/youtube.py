"""YouTube upload — hand-rolled OAuth2 + resumable upload over urllib.

Ported from forever-factory (60+ episodes of proven uploads), adapted:
  * shorts-only (no thumbnails, no long-video machinery)
  * made_for_kids = False, synthetic-media disclosure OFF (real
    gameplay footage is not AI-generated media)
  * publish grid = 00:00/06:00/12:00/18:00 Cairo (one Short every
    6 hours), booked via publishAt
  * the full quota-defer classification (daily vs rolling vs
    unrecognized-fail-loudly) is preserved verbatim — it is the
    difference between a machine that heals and one that dies quietly.

Credentials: three GitHub secrets — YT_CLIENT_ID, YT_CLIENT_SECRET,
YT_REFRESH_TOKEN. Plus YOUTUBE_API_KEY for search (sources.py).
"""
from __future__ import annotations

import json
import os
import re as _re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from . import ledger
from .config import cfg

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
API_BASE = "https://www.googleapis.com/youtube/v3"
CHUNK = 8 * 1024 * 1024
UPLOAD_UNITS = 1600
SEARCH_LOOKUP_UNITS = 100

QUOTA_DAY_REASONS = ("quotaExceeded", "dailyLimitExceeded")
QUOTA_ROLLING_REASONS = ("uploadLimitExceeded",)


class AuthError(RuntimeError):
    pass


class QuotaDefer(RuntimeError):
    """Raised when YouTube itself refused the upload for quota-family
    reasons; the orchestrator records the defer and exits cleanly."""


def _error_reason(msg: str) -> str:
    m = _re.search(r'"reason"\s*:\s*"([^"]+)"', msg or "")
    return m.group(1).strip() if m else ""


def _hours_to_pacific_midnight() -> float:
    from zoneinfo import ZoneInfo
    pt = ZoneInfo("America/Los_Angeles")
    now = datetime.now(pt)
    tomorrow = (now + timedelta(days=1)).date()
    midnight = datetime(tomorrow.year, tomorrow.month, tomorrow.day,
                        tzinfo=pt)
    return max((midnight - now).total_seconds() / 3600.0 + 0.05, 0.05)


def _mark_server_defer(state: dict, reason: str, hours: float) -> None:
    q = state.setdefault("quota", {})
    q["defer_reason"] = reason
    q["defer_until"] = int(time.time() + hours * 3600)
    ledger.save(state)


def _clear_server_defer(state: dict) -> None:
    q = state.get("quota") or {}
    if "defer_until" in q or "defer_reason" in q:
        q.pop("defer_until", None)
        q.pop("defer_reason", None)
        ledger.save(state)


def _creds() -> dict:
    cid = os.environ.get("YT_CLIENT_ID", "")
    csec = os.environ.get("YT_CLIENT_SECRET", "")
    refresh = os.environ.get("YT_REFRESH_TOKEN", "")
    if not (cid and csec and refresh):
        raise AuthError(
            "YouTube credentials missing. Set YT_CLIENT_ID, "
            "YT_CLIENT_SECRET, YT_REFRESH_TOKEN (GitHub repo secrets). "
            "See RUNBOOK.md.")
    return {"client_id": cid, "client_secret": csec,
            "refresh_token": refresh}


def _post(url: str, data: bytes, headers: dict) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


def get_access_token() -> str:
    c = _creds()
    body = urllib.parse.urlencode({
        "client_id": c["client_id"],
        "client_secret": c["client_secret"],
        "refresh_token": c["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    status, _, raw = _post(
        TOKEN_URL, body,
        {"Content-Type": "application/x-www-form-urlencoded"})
    if status != 200:
        raise AuthError(f"token refresh failed ({status}): "
                        f"{raw.decode()[:400]}")
    return json.loads(raw)["access_token"]


def verify_credentials() -> dict:
    token = get_access_token()
    req = urllib.request.Request(
        f"{API_BASE}/channels?part=snippet&mine=true",
        headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    items = data.get("items", [])
    if not items:
        return {"ok": False, "error": "token works but no channel found"}
    ch = items[0]["snippet"]
    return {"ok": True, "channel": ch.get("title"),
            "channel_id": items[0]["id"]}


# ── scheduling ──────────────────────────────────────────────────────

def next_slot(state: dict) -> str | None:
    """Next free FUTURE grid slot (ISO, Cairo) for publishAt.

    Grid = channel.yaml schedule.slots (01:00/07:00/13:00/19:00
    Cairo, US-tuned phase). Scans the next 8 days, skipping
    slots already booked in state.slots AND every slot whose time
    already passed. A missed slot just means the next Short takes
    the next open one — the grid self-heals.

    OWNER RULE 2026-11-23 (supersedes D8 2026-11-20): past slots are
    NEVER returned — no off-schedule LIVE publishes, no backfilling.
    Every Short is uploaded private with a future publishAt and
    goes public exactly at its slot, one at a time. The 2026-11-23
    burst (three past holes published live within two minutes
    after the channel swap) must never happen again.
    """
    from zoneinfo import ZoneInfo
    sched = cfg()["schedule"]
    tz = ZoneInfo(sched["timezone"])
    slots = list(sched.get("slots", []))
    weekend_slots = list(sched.get("weekend_slots") or [])
    weekend_days = {int(d) for d in (sched.get("weekend_days") or [])}
    booked = set((state.get("slots") or {}).get("short", []) or [])
    now = datetime.now(tz)
    lead = timedelta(minutes=int(
        sched.get("publish_lead_minutes", 25)))
    # OWNER RULE 2026-11-23: past slots are written off — never
    # backfilled, never published live off-schedule.
    # 2026-11-23 widening: scan 8 days (was 3) — a fully-booked
    # 3-day window could strand a video private with NO publishAt
    # ("private, not scheduled", live-diagnosed 2026-11-23); an
    # 8-day horizon makes a slotless upload unreachable in practice.
    for day_offset in range(0, 8):
        base = (now + timedelta(days=day_offset)).date()
        day_slots = slots + (weekend_slots
                             if base.weekday() in weekend_days else [])
        for hhmm in day_slots:
            hh, mm = map(int, hhmm.split(":"))
            slot_dt = datetime(base.year, base.month, base.day,
                               hh, mm, tzinfo=tz)
            if slot_dt > now + lead and slot_dt.isoformat() not in booked:
                return slot_dt.isoformat()
    return None


def _book_slot(state: dict, slot_iso: str) -> None:
    box = state.setdefault("slots", {}).setdefault("short", [])
    if slot_iso not in box:
        box.append(slot_iso)
    ledger.save(state)


# promote_scheduled (D8 2026-11-20 "publish NOW") was REMOVED entirely
# (owner order 2026-11-23): the publish-now request was ONE-TIME — "The
# publish now was one time request and forget about it the rest will
# be according to plan and schedules." Nothing in the machine can
# publish off-grid anymore: every upload is private + publishAt on the
# 6-hour grid, and the ONLY off-schedule flip left in the codebase is
# the slot-due rider pass in src/verify.py (fires at slot time, at
# most one per pass — re-privatized burst Shorts riding their slots).


# ── resumable upload ────────────────────────────────────────────────

def _upload_video(token: str, filepath: Path, meta: dict, status: dict,
                  max_retries: int = 4) -> str:
    body = {"snippet": meta, "status": status}
    init_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Upload-Content-Length": str(filepath.stat().st_size),
    }
    qs = urllib.parse.urlencode(
        {"uploadType": "resumable", "part": "snippet,status"})
    status_code, headers, raw = _post(f"{UPLOAD_URL}?{qs}",
                                      json.dumps(body).encode(),
                                      init_headers)
    if status_code not in (200, 201):
        raise RuntimeError(f"upload init failed ({status_code}): "
                           f"{raw.decode()[:400]}")
    location = headers.get("Location") or headers.get("location")
    if not location:
        raise RuntimeError("no resumable session URL returned")

    size = filepath.stat().st_size
    offset = 0
    with open(filepath, "rb") as f:
        while offset < size:
            f.seek(offset)
            block = f.read(CHUNK)
            end = offset + len(block) - 1
            req = urllib.request.Request(
                location, data=block, method="PUT", headers={
                    "Content-Length": str(len(block)),
                    "Content-Range":
                        f"bytes {offset}-{end}/{size}",
                })
            resp_body = b""
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    resp_body = resp.read()
            except urllib.error.HTTPError as e:
                if e.code == 308:
                    rng = e.headers.get("Range", "")
                    try:
                        offset = int(rng.split("-")[-1]) + 1
                    except (ValueError, IndexError):
                        offset = end + 1
                    continue
                rawerr = e.read()
                if e.code in (500, 502, 503, 429) and max_retries:
                    time.sleep(8 * (5 - max_retries))
                    max_retries -= 1
                    continue
                raise RuntimeError(f"upload failed ({e.code}): "
                                   f"{rawerr.decode()[:400]}")
            vid = json.loads(resp_body or b"{}").get("id")
            if not vid:
                vid = _poll_video_id(token)
            return vid
    raise RuntimeError("upload ended without completion response")


def _poll_video_id(token: str) -> str:
    req = urllib.request.Request(
        f"{API_BASE}/search?part=snippet&forMine=true&type=video"
        f"&order=date&maxResults=1",
        headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        items = json.loads(resp.read()).get("items", [])
    return items[0]["id"]["videoId"] if items else ""


def upload_short(filepath: Path, meta: dict, state: dict) -> str | None:
    """Full upload flow with quota gate + publishAt booking.

    Returns the video id, or None when deferred (quota-family) — the
    caller records the deferral and exits 0 so the grid retries later.
    Raises on anything unrecognized (the alert issue must fire)."""
    remaining = ledger.quota_remaining(state)
    if remaining < UPLOAD_UNITS + 200:
        print(f"[quota] {remaining} units left — deferring to next window")
        return None

    token = get_access_token()

    yconf = cfg()["youtube"]
    slot = next_slot(state)
    # OWNER RULE 2026-11-23 (supersedes D8 2026-11-20): every upload
    # is PRIVATE with a future publishAt (next_slot only returns
    # future slots now) — public exactly at the grid slot, one Short
    # at a time. A no-slot edge (grid fully booked 8 days out —
    # unreachable in practice) keeps the video private rather than
    # publishing off-schedule, and screams below so it is never
    # invisible.
    status = {
        "privacyStatus": "private",
        "madeForKids": bool(yconf.get("made_for_kids", False)),
        "selfDeclaredMadeForKids": bool(yconf.get("made_for_kids", False)),
    }
    if slot:
        status["publishAt"] = slot
    if yconf.get("synthetic_media_disclosure", False):
        status["containsSyntheticMedia"] = True

    meta = dict(meta)
    meta.pop("chapters", None)
    meta.setdefault("categoryId", str(yconf.get("category_id", "20")))
    meta.setdefault("defaultLanguage", yconf.get("default_language", "en"))
    meta.setdefault("defaultAudioLanguage", yconf.get("default_language", "en"))

    # invalidTags fallback: a YouTube-side validation change must never
    # kill the day (hashtags already live in title/description)
    variants = [meta]
    if meta.get("tags"):
        variants.append({k: v for k, v in meta.items() if k != "tags"})

    video_id = None
    rate_waited = False
    for variant in variants:
        while True:
            try:
                video_id = _upload_video(token, filepath, variant, status)
                break
            except RuntimeError as e:
                msg = str(e)
                reason = _error_reason(msg)
                if reason == "invalidTags" and variant is not variants[-1]:
                    print("[youtube] tags rejected — retrying without tags")
                    break
                if reason == "rateLimitExceeded" and not rate_waited:
                    rate_waited = True
                    print("[quota] rateLimitExceeded — waiting 90s, retrying")
                    time.sleep(90)
                    continue
                if reason in QUOTA_DAY_REASONS:
                    hours = _hours_to_pacific_midnight()
                    print(f"[quota] {reason}: deferring {hours:.1f}h to the "
                          f"Pacific reset")
                    _mark_server_defer(state, reason, hours)
                    raise QuotaDefer(msg)
                if reason in QUOTA_ROLLING_REASONS:
                    print(f"[quota] {reason}: rolling limit — deferring 2h")
                    _mark_server_defer(state, reason, 2.0)
                    raise QuotaDefer(msg)
                raise
        if video_id is not None:
            # C3 (audit 2026-11-18): a BLIND poll (upload done, id
            # unknown) must NOT trigger a duplicate re-upload of the
            # whole file via the no-tags variant
            break

    if video_id:
        # C3 (audit 2026-11-18): book the slot + burn local quota ONLY
        # on a real id — a blind-poll "" booked the slot forever while
        # the clip re-published next session (invisible grid hole)
        if slot:
            tail = f"publishAt {slot}"
        else:
            tail = ("NO OPEN SLOT within 8 days — video kept private "
                    "(UPLOAD-ALERT: inspect state.slots booking)")
        print(f"[youtube] uploaded short {video_id} ({tail})")
        if slot:
            _book_slot(state, slot)
        ledger.spend_quota(state, UPLOAD_UNITS)
        _clear_server_defer(state)
    else:
        print("UPLOAD-ALERT: upload finished with no video id — slot "
              "NOT booked, local quota NOT spent; the grid retries")
    return video_id


# ── GIVEAWAY COMMENT (owner rule 2026-11-22, PERMANENT) ─────────────
# Every video's comment section gets the giveaway comment: use the
# creator code, screenshot the proof on Instagram & tag the channel —
# the winner gets a free Gold/Rare account or 2,000 V-Bucks.
# Posted by the verify pass (hourly heal) so it is self-healing:
# scheduled (publishAt) videos get it once they are live, and any
# failure is retried the next hour. Never raises, never blocks.

GIVEAWAY_FALLBACK = (
    "\U0001F381 GIVEAWAY \U0001F381 Use our creator code H15, "
    "screenshot your proof on Instagram & tag us — winner gets a "
    "FREE Gold/Rare account or 2,000 V-Bucks! \U0001F3C6\u2728")


def post_giveaway_comment(token: str, video_id: str) -> bool:
    """Best-effort: post the giveaway comment on one video.

    Returns True when the comment is confirmed created (id received).
    Any failure just returns False — the hourly heal retries next
    pass, and a disabled/absent config is a clean False, never an
    alert-worthy error."""
    try:
        gconf = cfg().get("giveaway") or {}
        if not gconf.get("enabled", True):
            return False
        text = str(gconf.get("comment") or GIVEAWAY_FALLBACK)
        body = json.dumps({"snippet": {
            "videoId": video_id,
            "topLevelComment": {"snippet": {"textOriginal": text}}}})
        qs = urllib.parse.urlencode({"part": "snippet"})
        status, _, raw = _post(
            f"{API_BASE}/commentThreads?{qs}", body.encode(), {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"})
        if status in (200, 201):
            cid = json.loads(raw or b"{}").get("id", "")
            print(f"[giveaway] comment posted on {video_id} "
                  f"(id {str(cid)[:18]}…)")
            return True
        if status == 409:
            # duplicate — our comment already exists: success state
            print(f"[giveaway] comment already exists on {video_id}")
            return True
        print(f"[giveaway] comment deferred on {video_id} "
              f"({status}: {raw.decode()[:180]}) — the heal retries")
    except Exception as e:                       # noqa: BLE001
        print(f"[giveaway] comment failed on {video_id} ({e}) — "
              "the heal retries next pass")
    return False
