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
from zoneinfo import ZoneInfo

from . import ledger, youtube
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


def _slot_due(s: dict, now: datetime) -> bool:
    """True when the short's grid slot time has already passed."""
    try:
        return datetime.fromisoformat(str(s.get("slot"))) <= now
    except (TypeError, ValueError):
        return False


def _videos_status(token: str, ids: list) -> dict:
    """privacyStatus map for a batch of video ids (1 quota unit)."""
    req = urllib.request.Request(
        "https://www.googleapis.com/youtube/v3/videos?part=status&id="
        + ",".join(ids[:50]),
        headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        doc = json.loads(r.read())
    return {v["id"]: v["status"] for v in doc.get("items", [])}


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

    # C10 (audit 2026-11-18): ONE malformed record must never blind
    # the whole verification pass (the old whole-list try/except
    # silently zeroed `recent` and suppressed every VERIFY-ALERT)
    recent = []
    bad_records = 0
    for s in state.get("shorts", []):
        try:
            if s.get("video_id") and date.fromisoformat(
                    s.get("date") or "1970-01-01") >= cutoff:
                recent.append(s)
        except (TypeError, ValueError):
            bad_records += 1
    if bad_records:
        line = (f"VERIFY-ALERT: {bad_records} malformed shorts record(s) "
                "in state — check data/state.json")
        alerts.append(line)
        print(line)
    # D9 (audit 2026-11-18): a destroyed/lost state.json must never
    # verify green silently — stats say shorts were published but the
    # records are gone (ledger recovered from the empty default)
    stats_published = int((state.get("stats") or {}).get(
        "shorts_published", 0) or 0)
    if stats_published and not state.get("shorts"):
        line = ("VERIFY-ALERT: stats report {} published short(s) but "
                "state has NO shorts records — data/state.json may be "
                "corrupt or lost").format(stats_published)
        alerts.append(line)
        print(line)

    for s in recent:
        if s.get("verified") or s.get("owner_deleted"):
            # owner_deleted: the owner removed/private-ized this Short
            # himself — a tombstone, not a machine failure. Never alert.
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

    # ── GIVEAWAY COMMENT ensure-loop (owner rule 2026-11-22, ────────
    # PERMANENT): every recent short must carry the giveaway
    # comment. Self-healing: a fresh live video gets it within the
    # hour; a publishAt-scheduled video gets it as soon as it is
    # live; a failed post retries the next heal. The flag lives in
    # the short record so a comment is never posted twice.
    try:
        gconf = cfg().get("giveaway") or {}
        if gconf.get("enabled", True) and state.get("shorts"):
            token = youtube.get_access_token()
            for s in recent:
                if s.get("giveaway_comment") or s.get("owner_deleted"):
                    continue
                if not s.get("video_id"):
                    continue
                if youtube.post_giveaway_comment(token, s["video_id"]):
                    s["giveaway_comment"] = now_iso
                    dirty = True
    except youtube.AuthError as e:
        print(f"[giveaway] auth not ready ({e}) — next heal retries")
    except Exception as e:                       # noqa: BLE001
        print(f"[giveaway] ensure pass skipped ({e}) — next heal "
              "retries")

    # ── SLOT-DUE FLIP (owner order 2026-11-23) ──────────────────
    # STRICT GRID SCHEDULING, self-healing side: a Short whose grid
    # slot time has ALREADY passed but whose video is still PRIVATE
    # goes public HERE — at slot cadence, one at a time, never in
    # bursts (HARD CAP: at most ONE flip per pass, earliest slot
    # first; extra riders wait for the next hourly pass). Why this
    # exists: a video that has been public once can never carry a
    # publishAt again (YouTube rejects it with invalidPublishAt), so
    # the 2026-11-23 burst (three past holes published live within
    # two minutes) was re-privatized and rides THIS pass instead.
    # Normal publishAt-scheduled videos are already public once
    # their slot passes, so this never touches them; future slots
    # never flip. Best-effort like every ensure loop here — a
    # failure retries at the next heal (tools/slotcare.py runs this
    # pass at every heal fire, slot-anchored crons included).
    try:
        now_local = datetime.now(ZoneInfo(cfg()["schedule"]["timezone"]))
        due = [s for s in state.get("shorts", [])
               if s.get("video_id") and s.get("slot")
               and not s.get("owner_deleted")
               and _slot_due(s, now_local)]
        if due:
            token = youtube.get_access_token()
            yconf = cfg()["youtube"]
            doc = _videos_status(token, [s["video_id"] for s in due])
            # still-private riders only, EARLIEST slot first — and
            # at most ONE flip per pass (owner order 2026-11-23:
            # never in bursts; the rest ride the next hourly pass)
            riders = sorted(
                (s for s in due
                 if (doc.get(s["video_id"]) or {}).get("privacyStatus")
                 == "private"),
                key=lambda s: str(s.get("slot") or ""))
            for s in riders[1:]:
                print(f"[flip] short #{s.get('n')} waits for a later "
                      f"pass (slot {s.get('slot')}) — one flip per "
                      "pass, never in bursts")
            for s in riders[:1]:
                st = doc.get(s["video_id"])
                if not st or st.get("privacyStatus") != "private":
                    continue
                body = json.dumps({"id": s["video_id"], "status": {
                    "privacyStatus": "public",
                    "madeForKids": bool(yconf.get("made_for_kids", False)),
                    "selfDeclaredMadeForKids":
                        bool(yconf.get("made_for_kids", False)),
                }}).encode()
                req = urllib.request.Request(
                    "https://www.googleapis.com/youtube/v3/videos"
                    "?part=status", data=body, method="PUT",
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=60) as r:
                        r.read()
                    print(f"[flip] short #{s.get('n')} {s['video_id']} "
                          f"went public at slot {s.get('slot')} "
                          "(slot-due flip, owner order 2026-11-23)")
                except urllib.error.HTTPError as e:
                    print(f"[flip] short #{s.get('n')} failed "
                          f"({e.code}) — next heal retries")
    except youtube.AuthError as e:
        print(f"[flip] auth not ready ({e}) — next heal retries")
    except Exception as e:                       # noqa: BLE001
        print(f"[flip] pass skipped ({e}) — next heal retries")

    if dirty:
        try:
            ledger.save(state)
        except Exception as exc:
            print(f"[verify] ledger save skipped ({exc})")
    return {"confirmed": confirmed, "late": late, "alerts": alerts}
