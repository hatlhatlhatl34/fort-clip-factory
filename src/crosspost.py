"""Cross-platform publishing — Instagram Reels + Facebook Reels.

OWNER RULE (2026-11-15): every Short goes to ALL platforms. The exact
MP4 that YouTube publishes as a Short becomes an Instagram Reel and a
Facebook Reel — same content, same rhythm.

WHY SLOT-TIME, NOT SESSION-TIME: the 08:30 UTC session uploads all 4
Shorts with publishAt hours ahead; the reels drop when their YouTube
twin actually goes live. The hourly heal wakes at each 6-hour grid
slot (00:00/06:00/12:00/18:00 Cairo) and within minutes the same file
is live on all three platforms — no burst-posting, no platform left
behind.

HOW THE FILE SURVIVES UNTIL SLOT TIME: the session copies every
published final into media_cache/ and the workflow ships it as a
media-cache-<date> artifact; state.shorts[].media points at it
{artifact, run_id, file}. The heal fetches the zip back through the
GitHub API and pushes the bytes straight into Meta's resumable-upload
endpoints — no public hosting is ever needed:

  Instagram (Content Publishing API, resumable session):
    1. POST /{ig_id}/media         media_type=REELS,
                                   upload_type=resumable, caption
    2. POST rupload.facebook.com/ig-api-upload/{v}/{container_id}
                                   offset: 0, file_size: N (binary)
    3. poll  GET /{container}?fields=status_code   -> FINISHED
    4. POST /{ig_id}/media_publish creation_id=...

  Facebook (Reels Publishing API):
    1. POST /{page_id}/video_reels upload_phase=start
    2. POST upload_url (rupload)   binary, offset/file_size
    3. POST /{page_id}/video_reels upload_phase=finish,
                                   video_state=PUBLISHED, description

FAILURE SEMANTICS (the no-slight-error rule): a reel failure NEVER
touches the YouTube pipeline. It is recorded per platform in
state.shorts[].crosspost and retried by the next hourly heal, up to
crosspost.max_attempts; then it alerts once (GitHub issue) and gives
up quietly. A platform without credentials or account id is cleanly
OFF — never an error, YouTube keeps publishing alone.

DURATION: Facebook's Reels API caps reels at 90 s, so
clip.target_len_s is capped at [45, 90] going forward; banked clips
over 90 s skip FB (logged, alerted once) but still ship to Instagram,
which accepts minutes-long reels.

ARMING: crosspost.ig_user_id / fb_page_id in channel.yaml (or
state.crosspost_ids, filled by auto-discovery). Tokens are the GitHub
secrets IG_ACCESS_TOKEN / FB_ACCESS_TOKEN. The Instagram token is a
Business system-user token (never expires); the Facebook token must be
a Page token — see RUNBOOK.md "Cross-posting".
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import ledger
from .config import ROOT, cfg

GRAPH = "https://graph.facebook.com"
RUPLOAD = "https://rupload.facebook.com"
GH_API = "https://api.github.com"

FB_REEL_MAX_S = 90.0        # Facebook Reels API hard cap (3-90 s)
STATUS_POLL_S = 20          # container status poll cadence
STATUS_TIMEOUT_S = 300      # doc advice: poll <= 5 minutes
CAPTION_MAX = 2200          # Instagram caption limit
SCAN_WINDOW = 12            # recent shorts examined per heal run

CACHE_DIR = ROOT / "media_cache"


class CrosspostError(RuntimeError):
    """A cross-posting failure — always isolated from the YouTube flow."""


# ── config / credentials ────────────────────────────────────────────

def _conf() -> dict:
    return cfg().get("crosspost") or {}


def _version() -> str:
    return str(_conf().get("api_version", "v23.0"))


def ig_token() -> str:
    return os.environ.get("IG_ACCESS_TOKEN", "").strip()


def fb_token() -> str:
    return os.environ.get("FB_ACCESS_TOKEN", "").strip()


def ig_user_id(state: dict) -> str:
    ids = state.get("crosspost_ids") or {}
    return str(ids.get("ig") or _conf().get("ig_user_id") or "").strip()


def fb_page_id(state: dict) -> str:
    ids = state.get("crosspost_ids") or {}
    return str(ids.get("fb") or _conf().get("fb_page_id") or "").strip()


# ── HTTP plumbing (stdlib only, mirrors src/youtube.py style) ────────

def _request(url: str, *, method: str = "GET", data: bytes | None = None,
             headers: dict | None = None,
             timeout: int = 120) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=data,
                                 headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


def _graph(token: str, path: str, *, method: str = "GET",
           params: dict | None = None, timeout: int = 60) -> dict:
    params = dict(params or {})
    params["access_token"] = token
    url = f"{GRAPH}/{_version()}/{path.lstrip('/')}"
    if method == "GET":
        url += "?" + urllib.parse.urlencode(params)
        data = None
    else:
        data = urllib.parse.urlencode(params).encode()
    status, _, raw = _request(url, method=method, data=data, timeout=timeout)
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        body = {"raw": raw.decode(errors="replace")[:200]}
    if status >= 400 or "error" in body:
        err = body.get("error") or {}
        raise CrosspostError(
            f"graph {method} /{path} -> {status} "
            f"{err.get('code', '')} {err.get('message', str(body))[:220]}")
    return body


def _rupload(url: str, token: str, video: Path,
             versioned_fallback: str | None = None) -> dict:
    """Push the whole file to a rupload endpoint in one shot.

    versioned_fallback: for Instagram, the same path WITHOUT the api
    version segment — the doc shows both shapes, so we try each."""
    size = video.stat().st_size
    headers = {"Authorization": f"OAuth {token}",
               "offset": "0", "file_size": str(size)}
    for attempt_url in (url, versioned_fallback):
        if not attempt_url:
            continue
        with open(video, "rb") as f:
            status, _, raw = _request(attempt_url, method="POST",
                                      data=f.read(), headers=headers,
                                      timeout=900)
        if status < 400:
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                body = {"success": True, "raw": raw[:80].decode(errors="replace")}
            if body.get("success") is False:
                raise CrosspostError(
                    f"rupload rejected {video.name}: "
                    f"{str(body.get('debug_info') or body)[:220]}")
            return body
        if versioned_fallback and attempt_url is url:
            print(f"[crosspost] rupload {status} on {attempt_url} — "
                  "retrying the unversioned path")
            continue
        raise CrosspostError(f"rupload -> {status}: "
                             f"{raw.decode(errors='replace')[:220]}")
    raise CrosspostError(f"rupload failed for {video.name}")


# ── caption ─────────────────────────────────────────────────────────

def build_caption(title: str) -> str:
    """Reel caption from the YouTube title: drop the #{n} bookkeeping
    tag, enforce the Fortnite rule, carry the codes + hashtags."""
    t = re.sub(r"\s*#\d+$", "", (title or "").strip())
    if "fortnite" not in t.lower():
        t = f"Fortnite {t}".strip()
    codes = cfg()["codes"]
    tags = " ".join(str(h) for h in (_conf().get("hashtags") or []))
    lines = [t,
             "",
             f"CREATOR CODE: {codes['creator_code']} in the Fortnite "
             f"item shop",
             f"MAP CODE: {codes['map_code']}"]
    if tags:
        lines += ["", tags]
    return "\n".join(lines)[:CAPTION_MAX]


# ── Instagram: container -> rupload -> poll -> publish ───────────────

def ig_publish_reel(video: Path, caption: str, ig_id: str,
                    token: str) -> str:
    c = _graph(token, f"{ig_id}/media", method="POST", params={
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": caption,
    })
    container = str(c.get("id") or "")
    if not container:
        raise CrosspostError("container creation returned no id")

    _rupload(f"{RUPLOAD}/ig-api-upload/{_version()}/{container}",
             token, video,
             versioned_fallback=f"{RUPLOAD}/ig-api-upload/{container}")

    deadline = time.monotonic() + STATUS_TIMEOUT_S
    finished = False
    while time.monotonic() < deadline:
        st = _graph(token, container, params={"fields": "status_code"})
        code = st.get("status_code")
        if code == "FINISHED":
            finished = True
            break
        if code in ("ERROR", "EXPIRED"):
            raise CrosspostError(f"container {container} -> {code}")
        time.sleep(STATUS_POLL_S)
    if not finished:
        raise CrosspostError(
            f"container {container} never reached FINISHED "
            f"({STATUS_TIMEOUT_S}s)")

    m = _graph(token, f"{ig_id}/media_publish", method="POST",
               params={"creation_id": container})
    media_id = str(m.get("id") or "")
    if not media_id:
        raise CrosspostError("media_publish returned no id")
    return media_id


# ── Facebook: start -> rupload -> finish ─────────────────────────────

def fb_publish_reel(video: Path, description: str, page_id: str,
                    token: str) -> str:
    r = _graph(token, f"{page_id}/video_reels", method="POST",
               params={"upload_phase": "start"})
    vid = str(r.get("video_id") or "")
    upload_url = str(r.get("upload_url") or "")
    if not (vid and upload_url):
        raise CrosspostError("start phase returned no video_id/upload_url")

    _rupload(upload_url, token, video)

    f = _graph(token, f"{page_id}/video_reels", method="POST", params={
        "upload_phase": "finish",
        "video_id": vid,
        "video_state": "PUBLISHED",
        "description": description[:CAPTION_MAX],
    })
    if f.get("success") is False:
        raise CrosspostError(f"finish phase failed: {str(f)[:220]}")
    return vid


# ── media cache: session side ────────────────────────────────────────

def cache_video(state: dict, rec: dict, src: Path) -> dict | None:
    """Right after a successful YouTube upload: copy the final MP4
    into media_cache/ and stamp rec["media"] + rec["crosspost"] so the
    hourly heal can fetch the bytes back at slot time. NEVER raises —
    a cache failure only means that Short skips reels."""
    n = rec.get("n") or 0
    try:
        if not _conf().get("enabled", True):
            return None
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"short_{n}.mp4"
        shutil.copy2(src, CACHE_DIR / fname)
        media = {
            "file": fname,
            "artifact": "media-cache-"
                        + datetime.now(ZoneInfo("UTC"))
                        .strftime("%Y-%m-%d"),
            "run_id": os.environ.get("GITHUB_RUN_ID", ""),
            "size": (CACHE_DIR / fname).stat().st_size,
        }
        rec["media"] = media
        rec.setdefault("crosspost", {})
        rec["crosspost"].setdefault("ig", {"status": "pending"})
        rec["crosspost"].setdefault("fb", {"status": "pending"})
        print(f"[crosspost] cached {fname} "
              f"({media['size'] / 1e6:.1f} MB) for the reels")
        return media
    except Exception as e:                     # noqa: BLE001
        print(f"[crosspost] media cache failed for #{n} ({e}) — "
              "reels skipped for this Short, YouTube unaffected")
        return None


# ── media cache: heal side (GitHub artifact fetch) ───────────────────

def _gh_json(path: str, token: str) -> dict:
    req = urllib.request.Request(
        f"{GH_API}{path}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "fort-clip-factory"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def fetch_media(media: dict, workdir: Path,
                zips: dict | None = None) -> Path | None:
    """Download the cached MP4 back from the session's artifact (zip
    downloaded once per artifact thanks to `zips`). Returns the file
    path or None when the location is incomplete (local session)."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = str(media.get("run_id") or "")
    name = str(media.get("artifact") or "")
    fname = str(media.get("file") or "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not (repo and run_id and name and fname and token):
        print("[crosspost] media location incomplete (local session) — "
              "cannot fetch the video")
        return None
    workdir.mkdir(parents=True, exist_ok=True)
    zips = zips if zips is not None else {}
    try:
        if name not in zips:
            arts = _gh_json(
                f"/repos/{repo}/actions/runs/{run_id}/artifacts"
                f"?per_page=100", token).get("artifacts") or []
            art = next((a for a in arts if a.get("name") == name), None)
            if not art:
                raise CrosspostError(
                    f"artifact {name} not found in run {run_id} "
                    "(expired after 30 days?)")
            zpath = workdir / f"{name}.zip"
            req = urllib.request.Request(
                art["archive_download_url"],
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json",
                         "User-Agent": "fort-clip-factory"})
            with urllib.request.urlopen(req, timeout=600) as resp:
                zpath.write_bytes(resp.read())
            zips[name] = zpath
        with zipfile.ZipFile(zips[name]) as z:
            if fname not in z.namelist():
                raise CrosspostError(f"{fname} not inside artifact {name}")
            z.extract(fname, workdir)
        out = workdir / fname
        return out if out.exists() and out.stat().st_size > 1_000 else None
    except urllib.error.HTTPError as e:
        raise CrosspostError(
            f"media fetch failed ({e.code}): {fname}") from e


# ── alerts (deduped, best-effort) ───────────────────────────────────

def alert_once(state: dict, key: str, title: str, body: str) -> None:
    """One GitHub issue per key per day; degrades to a loud log line
    when no GITHUB_TOKEN is around (local runs)."""
    today = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d")
    sent = state.setdefault("alerts_sent", {})
    if sent.get(key) == today:
        return
    sent[key] = today
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    print(f"CROSSPOST-ALERT: {title}")
    if repo and token:
        try:
            req = urllib.request.Request(
                f"{GH_API}/repos/{repo}/issues",
                data=json.dumps({"title": f"[crosspost] {title}",
                                 "body": body[:2000] + "\n",
                                 "labels": ["machine-failure"]}).encode(),
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json",
                         "User-Agent": "fort-clip-factory"},
                method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                json.loads(resp.read())
            print(f"[crosspost] issue filed: {title}")
        except Exception as e:                 # noqa: BLE001
            print(f"[crosspost] issue filing failed ({e}) — logged here")
    ledger.save(state)


# ── auto-discovery: arms the machine the hour setup completes ───────

def auto_discover(state: dict) -> None:
    """The owner's Meta side may complete at any moment (page created,
    assets assigned to the system user, fresh token pasted). Each heal
    run re-probes /me/accounts with whatever tokens exist and records
    any page + linked Instagram account it can see — zero code changes
    needed to go live."""
    found_page: dict | None = None
    found_ig: dict | None = None
    for label, tok in (("IG", ig_token()), ("FB", fb_token())):
        if not tok:
            continue
        try:
            d = _graph(tok, "me/accounts", params={
                "fields": "id,name,instagram_business_account"
                          "{id,username}",
                "limit": "25"})
            for p in d.get("data") or []:
                if found_page is None:
                    found_page = p
                igba = p.get("instagram_business_account") or {}
                if igba.get("id") and found_ig is None:
                    found_ig = igba
        except CrosspostError as e:
            print(f"[crosspost] {label} token /me/accounts probe: {e}")
    ids = state.setdefault("crosspost_ids", {})
    changed = False
    if found_page and not ids.get("fb"):
        ids["fb"] = str(found_page["id"])
        print(f"[crosspost] AUTO-DISCOVERED Facebook page "
              f"{found_page.get('name')} ({found_page['id']}) — "
              "FB reels ARMED")
        changed = True
    if found_ig and not ids.get("ig"):
        ids["ig"] = str(found_ig["id"])
        print(f"[crosspost] AUTO-DISCOVERED Instagram account "
              f"@{found_ig.get('username')} ({found_ig['id']}) — "
              "IG reels ARMED")
        changed = True
    if changed:
        ledger.save(state)


def _token_alive(token: str) -> bool:
    try:
        _graph(token, "me", params={"fields": "id,name"})
        return True
    except CrosspostError as e:
        print(f"[crosspost] token health check FAILED: {e}")
        return False


# ── the hourly pass ─────────────────────────────────────────────────

def _slot_passed(short: dict, now: datetime) -> bool:
    try:
        when = datetime.fromisoformat(str(short.get("slot")))
    except (ValueError, TypeError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=ZoneInfo(cfg()["schedule"]["timezone"]))
    return when <= now


def _incomplete(short: dict) -> bool:
    cp = short.get("crosspost") or {}
    return any((cp.get(p) or {}).get("status")
               not in ("done", "gave_up", "skipped")
               for p in ("ig", "fb"))


def run_due(state: dict) -> int:
    cp = _conf()
    if not cp.get("enabled", True):
        print("[crosspost] disabled in channel.yaml — nothing to do")
        return 0

    auto_discover(state)

    igt, fbt = ig_token(), fb_token()
    ig_id, page_id = ig_user_id(state), fb_page_id(state)
    ig_on = bool(igt and ig_id)
    fb_on = bool(fbt and page_id)

    if not ig_on and not fb_on:
        why = []
        if not igt:
            why.append("IG_ACCESS_TOKEN secret missing")
        elif not ig_id:
            why.append("Instagram account id not known yet "
                       "(channel.yaml crosspost.ig_user_id / "
                       "auto-discovery)")
        if not fbt:
            why.append("FB_ACCESS_TOKEN secret missing")
        elif not page_id:
            why.append("Facebook page id not known yet (no page "
                       "visible — create one, see RUNBOOK)")
        print("[crosspost] no platform armed yet (" + "; ".join(why) +
              ") — YouTube keeps publishing alone; RUNBOOK.md has the "
              "3-minute Meta-side checklist")
        return 0

    # token health — a dead token must not burn the retry budget
    if ig_on and not _token_alive(igt):
        alert_once(state, "ig-token",
                   "Instagram token invalid — reels paused",
                   "The IG_ACCESS_TOKEN was rejected by /me. Renew it "
                   "(RUNBOOK.md, cross-posting section) and update the "
                   "GitHub secret.")
        ig_on = False
    if fb_on and not _token_alive(fbt):
        alert_once(state, "fb-token",
                   "Facebook token invalid — reels paused",
                   "The FB_ACCESS_TOKEN was rejected by /me. Renew it "
                   "(RUNBOOK.md, cross-posting section) and update the "
                   "GitHub secret.")
        fb_on = False
    if not (ig_on or fb_on):
        print("[crosspost] both tokens unhealthy — nothing to do this hour")
        return 0

    now = datetime.now(ZoneInfo(cfg()["schedule"]["timezone"]))
    recent = state.get("shorts", [])[-SCAN_WINDOW:]
    due = [s for s in recent
           if s.get("video_id") and s.get("media")
           and _slot_passed(s, now) and _incomplete(s)]
    if not due:
        print("[crosspost] nothing due — every recent Short is settled")
        return 0
    print(f"[crosspost] {len(due)} Short(s) due for reels")

    # one-time info: the IG publishing quota (100 / 24 h official)
    if ig_on:
        try:
            q = _graph(igt, f"{ig_id}/content_publishing_limit",
                       params={"fields": "quota_total,quota_duration"})
            print(f"[crosspost] IG publishing quota config: {q}")
        except CrosspostError as e:
            print(f"[crosspost] quota probe skipped: {e}")

    max_attempts = int(cp.get("max_attempts", 8))
    workdir = ROOT / "work_crosspost"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    zips: dict = {}
    published = 0

    for s in sorted(due, key=lambda x: str(x.get("slot"))):
        n = s.get("n")
        caption = build_caption(str(s.get("title") or ""))
        dur = float(s.get("clip_dur") or 0)

        try:
            video = fetch_media(s.get("media") or {}, workdir, zips)
        except CrosspostError as e:
            print(f"[crosspost] #{n} media fetch failed: {e}")
            alert_once(state, f"media-{n}",
                       f"media cache unreachable for short #{n}",
                       f"Could not fetch the cached video for short #{n} "
                       f"({e}). The reels for this Short cannot be "
                       "posted; YouTube is unaffected.")
            for p in ("ig", "fb"):
                stx = (s.setdefault("crosspost", {})
                       .setdefault(p, {"status": "pending"}))
                if stx.get("status") not in ("done", "gave_up", "skipped"):
                    stx.update(status="gave_up",
                               reason=f"media unreachable: {e}")
            ledger.save(state)
            continue
        if not video:
            alert_once(state, f"media-{n}",
                       f"media cache missing for short #{n}",
                       f"No cached video for short #{n} (local session "
                       "or incomplete upload). Reels skipped; YouTube "
                       "unaffected.")
            continue

        if ig_on:
            st = (s.setdefault("crosspost", {})
                  .setdefault("ig", {"status": "pending"}))
            if st.get("status") not in ("done", "gave_up", "skipped"):
                attempts = int(st.get("attempts") or 0)
                if attempts >= max_attempts:
                    st["status"] = "gave_up"
                    alert_once(state, f"ig-{n}",
                               f"Instagram reel gave up for short #{n}",
                               f"short #{n} failed {attempts} hourly "
                               f"attempts: {st.get('error', '?')}")
                    ledger.save(state)
                else:
                    try:
                        media_id = ig_publish_reel(video, caption, ig_id,
                                                   igt)
                        st.update(status="done", media_id=media_id,
                                  ts=int(time.time()), attempts=attempts + 1)
                        published += 1
                        print(f"[crosspost] #{n} -> Instagram reel "
                              f"{media_id}")
                    except Exception as e:             # noqa: BLE001
                        st.update(status="failed",
                                  attempts=attempts + 1,
                                  error=str(e)[:220], ts=int(time.time()))
                        print(f"[crosspost] #{n} Instagram FAILED "
                              f"(attempt {attempts + 1}/{max_attempts}): "
                              f"{e}")
                    ledger.save(state)

        if fb_on:
            st = (s.setdefault("crosspost", {})
                  .setdefault("fb", {"status": "pending"}))
            if st.get("status") not in ("done", "gave_up", "skipped"):
                if dur > FB_REEL_MAX_S:
                    st.update(status="skipped",
                              reason=f"duration {dur:.0f}s > "
                                     f"{FB_REEL_MAX_S:.0f}s FB cap")
                    print(f"[crosspost] #{n} FB skipped: "
                          f"{dur:.0f}s over the 90 s reel cap "
                          "(Instagram still gets it)")
                    ledger.save(state)
                else:
                    attempts = int(st.get("attempts") or 0)
                    if attempts >= max_attempts:
                        st["status"] = "gave_up"
                        alert_once(state, f"fb-{n}",
                                   f"Facebook reel gave up for short #{n}",
                                   f"short #{n} failed {attempts} hourly "
                                   f"attempts: {st.get('error', '?')}")
                        ledger.save(state)
                    else:
                        try:
                            vid = fb_publish_reel(video, caption, page_id,
                                                  fbt)
                            st.update(status="done", media_id=vid,
                                      ts=int(time.time()),
                                      attempts=attempts + 1)
                            published += 1
                            print(f"[crosspost] #{n} -> Facebook reel "
                                  f"{vid}")
                        except Exception as e:         # noqa: BLE001
                            st.update(status="failed",
                                      attempts=attempts + 1,
                                      error=str(e)[:220],
                                      ts=int(time.time()))
                            print(f"[crosspost] #{n} Facebook FAILED "
                                  f"(attempt {attempts + 1}/"
                                  f"{max_attempts}): {e}")
                        ledger.save(state)

    print(f"\n[crosspost] DONE: {published} reel(s) live this run")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────

def report(state: dict) -> None:
    igt, fbt = ig_token(), fb_token()
    ig_id, page_id = ig_user_id(state), fb_page_id(state)
    print("cross-posting status:")
    print(f"  enabled     : {_conf().get('enabled', True)}")
    print(f"  Instagram   : "
          f"{'ARMED' if igt and ig_id else 'off'} "
          f"(token={'set' if igt else 'MISSING'}, "
          f"ig_user_id={ig_id or '?'})")
    if igt:
        print(f"    token: {'alive' if _token_alive(igt) else 'INVALID'}")
    print(f"  Facebook    : "
          f"{'ARMED' if fbt and page_id else 'off'} "
          f"(token={'set' if fbt else 'MISSING'}, "
          f"fb_page_id={page_id or '?'})")
    if fbt:
        print(f"    token: {'alive' if _token_alive(fbt) else 'INVALID'}")
    recent = state.get("shorts", [])[-SCAN_WINDOW:]
    for s in recent:
        cp = s.get("crosspost") or {}
        print(f"  #{s.get('n'):>3} slot={s.get('slot')} "
              f"ig={(cp.get('ig') or {}).get('status', '-'):>7} "
              f"fb={(cp.get('fb') or {}).get('status', '-') or '-':>7}")


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Cross-post Shorts as Instagram + Facebook reels")
    ap.add_argument("--due", action="store_true",
                    help="publish reels for every Short whose grid slot "
                         "has passed (the hourly heal)")
    ap.add_argument("--check", action="store_true",
                    help="status report: tokens, ids, recent shorts")
    args = ap.parse_args()

    state = ledger.load()
    if args.check:
        auto_discover(state)
        report(state)
        return 0
    if args.due:
        return run_due(state)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
