"""TikTok mirror — the fourth platform (owner rule 2026-11-18).

OWNER RULE: every Short goes to ALL platforms. The exact MP4 YouTube
publishes as a Short becomes an Instagram Reel, a Facebook Reel AND a
TikTok video — same file, same title (cross-links per
crosspost.build_caption, platform-aware).

UNAUDITED APP REALITY (live-diagnosed 2026-11-18): the developer app
is sandboxed + unaudited, so (a) authorization only works through the
SANDBOX client key (sb...), and (b) every post is forced SELF_ONLY
(private) while the connected account @h15master stays private. Flip
crosspost.tiktok.privacy to PUBLIC_TO_EVERYONE in channel.yaml only
AFTER the Content Posting API audit passes — TikTok rejects public
privacy for unaudited apps outright.

TOKEN MODEL: access_token 24 h, refresh_token 365 days (rolling).
.github/workflows/tiktok-token-refresh.yml refreshes daily and swaps
the secrets in place (tools/set_secret.py). This module NEVER
refreshes on its own — a mid-session refresh would fork the token
between runners (the running job keeps its env snapshot, which is
exactly why that is safe).

FLOW (Content Posting API, Direct Post, FILE_UPLOAD):
  1. POST /v2/post/publish/video/init/ {post_info, source_info}
         -> publish_id + upload_url
  2. PUT  upload_url   Content-Range: bytes a-b/total (binary chunk)
  3. POST /v2/post/publish/status/fetch/ {publish_id}
         -> PUBLISH_COMPLETE

RATE LIMIT: 6 publish-API requests per minute per access_token —
posts are paced and the status polls run on a 20 s cadence.

NO ADOPTION: the app does not hold the video.list scope, so an
already-live TikTok twin cannot be discovered from TikTok's side.
State marks (crosspost.tiktok.status) are the ONLY dedupe gate —
never run the stateless crosspost-now twice for TikTok without a
heal committing state in between (RUNBOOK section 9).

FAILURE SEMANTICS: identical to the reels — a TikTok failure NEVER
touches the YouTube pipeline; it is recorded per short in
state.shorts[].crosspost.tiktok, retried by the next hourly heal up
to crosspost.max_attempts, then alerts once and gives up quietly.
Unarmed (missing TIKTOK_ACCESS_TOKEN) is cleanly OFF.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import cfg

TT_API = "https://open.tiktokapis.com"
UPLOAD_MAX = 64 * 1024 * 1024     # FILE_UPLOAD ceiling, live-probed
                                  # 2026-11-18 (see _fit_upload)
STATUS_POLL_S = 20                # publish API rate limit: 6 req/min
STATUS_TIMEOUT_S = 360             # 18 polls — a poll timeout is NOT a
                                  # definite failure; the retry path
                                  # re-checks the stored publish_id first
TITLE_MAX = 2200                   # TikTok caption limit (UTF-16 runes)


def _conf() -> dict:
    return dict((cfg().get("crosspost") or {}).get("tiktok") or {})


def token() -> str:
    """The runner-snapshot access token (GitHub secret)."""
    return os.environ.get("TIKTOK_ACCESS_TOKEN", "").strip()


def armed() -> bool:
    """Cheap arm check: enabled in channel.yaml + token secret set."""
    return bool(_conf().get("enabled", True) and token())


def token_alive(tok: str) -> bool:
    """Health check against the user-info endpoint (user.info.basic)."""
    try:
        _api("GET", "/v2/user/info/",
             params={"fields": "open_id,display_name"}, token=tok)
        return True
    except Exception as e:                      # noqa: BLE001
        print(f"[tiktok] token health check FAILED: {e}")
        return False


def settled(short: dict) -> bool:
    cp = short.get("crosspost") or {}
    return (cp.get("tiktok") or {}).get("status") in (
        "done", "skipped", "gave_up")


# ── HTTP plumbing (stdlib only, like the rest of the factory) ─────────

def _api(method: str, path: str, *, token: str, payload: dict | None = None,
         params: dict | None = None) -> dict:
    url = TT_API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json; charset=UTF-8"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode() or "{}")
        except Exception:                        # noqa: BLE001
            err = {}
        raise RuntimeError(
            f"{path} -> HTTP {e.code}: "
            f"{err.get('error', {}).get('code', '?')} "
            f"{err.get('error', {}).get('message', '')}"
        ) from None
    if not body:
        return {}
    d = json.loads(body)
    e = d.get("error") or {}
    if e.get("code") and e.get("code") != "ok":
        raise RuntimeError(f"{path} -> {e.get('code')}: "
                           f"{e.get('message', '')}")
    return d.get("data") or {}


# ── the publish flow ──────────────────────────────────────────────────

def _fit_upload(video: Path) -> tuple[Path, int]:
    """TikTok FILE_UPLOAD reality, live-probed 2026-11-18 with a
    controlled chunk-matrix against /v2/post/publish/video/init/:
      * total_chunk_count > 1  -> 400 "The total chunk count is
        invalid" (multi-chunk is rejected outright, matching the
        community reports "not working with more than 1 chunk")
      * one chunk > 64 MB     -> 400 "The chunk size is invalid"
      * one chunk <= 64 MB    -> 200 ok
    So every upload is EXACTLY ONE chunk of the whole file; a file
    above the ceiling gets re-encoded down (rare: 720p Shorts run
    8-40 MB, only 120 s clips at high bitrate can cross 64 MB)."""
    import subprocess

    size = video.stat().st_size
    if size <= UPLOAD_MAX:
        return video, size
    print(f"[tiktok] {video.name} is {size // (1024 * 1024)} MB — "
          "re-encoding to fit the 64 MB single-chunk ceiling")
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(video)],
            capture_output=True, text=True, timeout=60)
        dur = max(float(r.stdout.strip() or 0.0), 30.0)
    except Exception:                              # noqa: BLE001
        dur = 90.0
    # target ~60 MB (margin), split A/V, sane bounds
    bv = min(max(int(60 * 1024 * 1024 * 8 / dur) - 128_000, 800_000),
             5_000_000)
    out = video.with_name(video.stem + "_ttfit.mp4")
    for scale, rate in (("1280:720", bv), ("960:540", max(bv // 2, 600_000))):
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(video),
               "-vf", f"scale={scale}:force_original_aspect_ratio=decrease",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
               "-b:v", str(rate), "-maxrate", str(rate),
               "-bufsize", str(2 * rate),
               "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
               str(out)]
        subprocess.run(cmd, check=True, capture_output=True, timeout=900)
        if out.exists() and out.stat().st_size <= UPLOAD_MAX:
            return out, out.stat().st_size
    raise RuntimeError(f"could not fit {video.name} under the 64 MB "
                       "single-chunk ceiling after two re-encodes")


def publish(video: Path, title: str, tok: str,
            on_init=None) -> str:
    """One TikTok post: init -> single-chunk PUT -> status poll.
    Returns the publish_id. Raises on any hard failure.

    `on_init(publish_id)` is called the moment init returns — the id
    is persisted BEFORE the upload/poll, so a runner kill mid-flight
    can never cause a duplicate post next hour (audit 2026-11-18 A4).
    """
    privacy = str(_conf().get("privacy", "SELF_ONLY"))
    video, size = _fit_upload(video)
    # ALWAYS one chunk: chunk == video_size, total_chunk_count == 1
    # (the API rejects both multi-chunk counts and oversized single
    # chunks — live matrix 2026-11-18)
    chunk = size

    init = _api("POST", "/v2/post/publish/video/init/", token=tok, payload={
        "post_info": {
            "title": title[:TITLE_MAX],
            "privacy_level": privacy,
            "disable_duet": False,
            "disable_comment": False,
            "disable_stitch": False,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": chunk,
            # live-diagnosed 2026-11-18: without this field the API
            # answers 400 invalid_params "The video info is empty"
            # even though the docs mark it optional — a controlled
            # init-matrix proved it (adding ONLY this field flips the
            # call to 200 ok)
            "total_chunk_count": 1,
        },
    })
    publish_id = str(init.get("publish_id") or "")
    upload_url = str(init.get("upload_url") or "")
    if not publish_id or not upload_url:
        raise RuntimeError(f"init returned no publish_id/upload_url "
                           f"({sorted(init)})")
    if on_init:
        try:
            on_init(publish_id)
        except Exception:                       # noqa: BLE001
            pass        # best-effort persistence, never fatal

    with open(video, "rb") as fh:
        offset = 0
        while offset < size:
            n = min(chunk, size - offset)
            body = fh.read(n)
            if len(body) != n:
                raise RuntimeError(f"short read on {video.name}")
            req = urllib.request.Request(
                upload_url, data=body, method="PUT",
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(n),
                    "Content-Range": f"bytes {offset}-{offset + n - 1}/{size}",
                })
            with urllib.request.urlopen(req, timeout=600) as r:
                r.read()
            offset += n
            # keep the publish API inside its 6 req/min budget
            time.sleep(1)

    deadline = time.monotonic() + STATUS_TIMEOUT_S
    last = ""
    while time.monotonic() < deadline:
        time.sleep(STATUS_POLL_S)
        st = _api("POST", "/v2/post/publish/status/fetch/", token=tok,
                  payload={"publish_id": publish_id})
        last = str(st.get("status") or "")
        if last == "PUBLISH_COMPLETE":
            return publish_id
        if "FAILED" in last:
            raise RuntimeError(f"TikTok processing FAILED: {st}")
    # NOT a definite failure — async processing may still complete.
    # attempt() stores the id so the next hourly pass re-checks before
    # ever re-posting (no duplicates by construction).
    raise _PollTimeout(publish_id, last or "unknown")


class _PollTimeout(RuntimeError):
    def __init__(self, publish_id: str, status: str):
        super().__init__(f"status poll timeout after 360 s "
                         f"(last={status}) — may still complete")
        self.publish_id = publish_id


# ── crosspost integration (mirrors _attempt_reel semantics) ───────────

def attempt(state: dict, s: dict, video: Path, title: str,
            max_attempts: int = 8) -> bool:
    """One TikTok attempt for one short. True iff it went live (or was
    re-checked into doneness) this call. Failures recorded + swallowed."""
    from . import ledger
    from .crosspost import alert_once

    n = s.get("n")
    tok = token()
    st = (s.setdefault("crosspost", {})
          .setdefault("tiktok", {"status": "pending"}))
    if st.get("status") in ("done", "skipped", "gave_up"):
        return False

    # poll-timeout adoption: a previous run's post may have completed
    # after we stopped watching — verify BEFORE posting anything.
    # (audit 2026-11-18 A4: 'pending' counts too — a persisted
    # pre-upload id from a runner kill re-checks before re-posting)
    prior = str(st.get("publish_id") or "")
    if prior and st.get("timeout") and st.get("status") in \
            ("failed", "pending"):
        try:
            cur = _api("POST", "/v2/post/publish/status/fetch/",
                       token=tok, payload={"publish_id": prior})
            if str(cur.get("status") or "") == "PUBLISH_COMPLETE":
                st.update(status="done", media_id=prior,
                          ts=int(time.time()), adopted=True)
                print(f"[tiktok] #{n} earlier post {prior} completed "
                      "— ADOPTED (no duplicate posted)")
                ledger.save(state)
                return True
        except Exception as e:                   # noqa: BLE001
            # A13 (audit 2026-11-18): a transient probe error must
            # NEVER re-post — the earlier post may have completed; stay
            # pending and re-check next hour
            st.update(error=f"[recheck] probe failed: {str(e)[:120]}",
                      ts=int(time.time()))
            ledger.save(state)
            print(f"[tiktok] #{n} adoption re-check failed ({e}) — "
                  "staying pending, NO re-post")
            return False

    attempts = int(st.get("attempts") or 0)
    if attempts >= max_attempts:
        st["status"] = "gave_up"
        alert_once(state, f"tiktok-{n}",
                   f"TikTok video gave up for short #{n}",
                   f"short #{n} failed {attempts} hourly attempts: "
                   f"{st.get('error', '?')}")
        ledger.save(state)
        return False
    try:
        def _persist(pid):
            # A4 (audit 2026-11-18): the publish_id is in state BEFORE
            # the bytes leave the runner — a kill mid-upload/mid-poll
            # re-checks next hour instead of re-posting a duplicate
            st.update(status="pending", publish_id=pid, timeout=True,
                      error="upload in flight — re-check before re-post",
                      ts=int(time.time()))
            ledger.save(state)
            print(f"[tiktok] #{n} publish_id {pid} persisted pre-upload")
        pid = publish(video, title, tok, on_init=_persist)
        st.update(status="done", media_id=pid, ts=int(time.time()),
                  attempts=attempts + 1)
        st.pop("timeout", None)
        st.pop("tt_paused", None)
        print(f"[tiktok] #{n} -> posted ({pid}) "
              f"privacy={_conf().get('privacy', 'SELF_ONLY')}")
        ledger.save(state)
        return True
    except _PollTimeout as e:
        st.update(status="failed", attempts=attempts + 1,
                  error=str(e)[:220], ts=int(time.time()),
                  publish_id=e.publish_id, timeout=True)
        st.pop("tt_paused", None)
        print(f"[tiktok] #{n} poll timeout (kept for re-check): {e}")
        ledger.save(state)
        return False
    except Exception as e:                       # noqa: BLE001
        err = str(e)
        if "unaudited_client" in err:
            # LIVE-DIAGNOSED 2026-11-18 (shorts #7-#10): the TikTok
            # account was switched PUBLIC mid-flight and every init
            # flipped to HTTP 403
            # unaudited_client_can_only_post_to_private_accounts —
            # an unaudited app may only post while the account is
            # PRIVATE (or after the audit passes). That is a WAIT
            # state, not a failure: no attempt burned, no alert, no
            # give_up — the short stays pending and each hourly pass
            # re-probes (one init per short per hour). The moment the
            # account is private again — or the audit passes —
            # posting resumes with zero intervention.
            st.update(status="pending", tt_paused=True,
                      error="[paused] unaudited client can only post "
                            "to a PRIVATE account — flip "
                            f"@{_conf().get('username', 'h15master')} "
                            "back to private in the app (or pass the "
                            "audit, then set privacy "
                            "PUBLIC_TO_EVERYONE)",
                      ts=int(time.time()))
            st.pop("timeout", None)
            print(f"[tiktok] #{n} PAUSED: unaudited client cannot post "
                  "to a public account — attempts NOT burned; the "
                  "mirror auto-resumes once the account is private "
                  "again (RUNBOOK section 9)")
            ledger.save(state)
            return False
        if "rate_limit" in err.lower() or "HTTP 429" in err:
            # LIVE-DIAGNOSED 2026-11-18 00:29: a burst of 403-probes
            # tripped the publish API's 6-req/min budget -> HTTP 429
            # rate_limit_exceeded "Please try again later". Also a
            # WAIT state, never a real failure: burning 8 hourly
            # attempts on 429s could give_up healthy shorts while
            # the account is merely public+busy. No burn, no alert —
            # the next hourly pass retries on its own cadence.
            st.update(status="pending",
                      error="[wait] TikTok API rate limit (6 req/min) "
                            "— auto-retried next hour", ts=int(time.time()))
            st.pop("timeout", None)
            print(f"[tiktok] #{n} rate-limited (6 req/min) — attempt "
                  "NOT burned; retried next hour")
            ledger.save(state)
            return False
        if "spam_risk" in err.lower():
            # LIVE-DIAGNOSED 2026-11-18 14:25 (backfill #19-#21,
            # crosspost run 44323514723): HTTP 403
            # spam_risk_too_many_posts "This TikTok creator has made
            # too many posts" — a TRANSIENT account-level post
            # throttle, not a content failure. Burning 8 hourly
            # attempts on it would give_up healthy shorts while the
            # throttle window simply hasn't rolled yet. No burn, no
            # alert — the next hourly pass retries on its own.
            st.update(status="pending",
                      error="[wait] TikTok spam-risk throttle (too "
                            "many posts) — auto-retried next hour",
                      ts=int(time.time()))
            st.pop("timeout", None)
            print(f"[tiktok] #{n} spam-risk throttled — attempt NOT "
                  "burned; retried next hour")
            ledger.save(state)
            return False
        if "access_token" in err.lower() or "unauthorized" in err.lower():
            # A14 (audit 2026-11-18): mid-run 24 h token expiry is a
            # WAIT state (the 03:00 UTC refresh re-arms it) — never a
            # burned attempt on an otherwise healthy short
            st.update(status="pending",
                      error="[wait] access token expired mid-run — "
                            "re-armed by the next refresh, retried next "
                            "hour", ts=int(time.time()))
            st.pop("timeout", None)
            print(f"[tiktok] #{n} token expired mid-run — attempt NOT "
                  "burned; retried next hour")
            ledger.save(state)
            return False
        st.update(status="failed", attempts=attempts + 1,
                  error=err[:220], ts=int(time.time()))
        st.pop("timeout", None)
        st.pop("tt_paused", None)
        print(f"[tiktok] #{n} FAILED "
              f"(attempt {attempts + 1}/{max_attempts}): {e}")
        ledger.save(state)
        return False
