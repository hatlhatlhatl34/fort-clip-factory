"""Cross-platform publishing — Instagram Reels + Facebook Reels,
plus the TikTok mirror (src/tiktok.py).

OWNER RULE (2026-11-15, extended 2026-11-18): every Short goes to
ALL platforms. The exact MP4 that YouTube publishes as a Short
becomes an Instagram Reel, a Facebook Reel AND a TikTok video — same
content, same rhythm.

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

CROSS-LINKS (owner rule 2026-11-15): every description carries the
other platforms. YouTube descriptions carry an IG + FB block; reel
captions/descriptions carry the YouTube twin link
(youtube.com/shorts/<id>) plus the other platform's profile; once
reels are live the YouTube description also gains the per-video reel
permalinks (videos.update, quota-aware).

BACKFILL (owner rule 2026-11-15): the three feeds must look alike —
the already-public YouTube catalogue is mirrored to Instagram +
Facebook, oldest first, at crosspost.backfill.per_hour shorts per
heal run. Live-probed 2026-11-15: Instagram reports NO
content_publishing_limit (empty config) and Facebook documents no
reel cap, so the whole catalogue gets mirrored. Legacy Shorts without
a media pointer are downloaded back from their own public YouTube
URL via yt-dlp using the cookie pool (YouTube bot-gates anonymous
datacenter IPs); Shorts deleted from YouTube are settled as GONE
(no retry burn). IG's transcoders reject 4:4:4 chroma, so a file
that fails Meta's transcode is re-encoded to yuv420p and retried
once — and the factory render now emits yuv420p at birth.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
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
YT_API = "https://www.googleapis.com/youtube/v3"   # description edits
STATUS_POLL_S = 20          # container status poll cadence
STATUS_TIMEOUT_S = 300      # doc advice: poll <= 5 minutes
CAPTION_MAX = 2200          # Instagram caption limit
SCAN_WINDOW = 12            # recent shorts examined per heal run

CACHE_DIR = ROOT / "media_cache"


def _tt():
    """TikTok module access, lazily + defensively: a broken or
    unarmed TikTok integration must never break the IG/FB reels."""
    try:
        from . import tiktok
        return tiktok
    except Exception as e:                     # noqa: BLE001
        print(f"[crosspost] tiktok module unavailable: {e}")
        return None


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


# ── cross-links (owner rule 2026-11-15) ──────────────────────────────

def _links() -> dict:
    return _conf().get("links") or {}


def short_url(video_id: str) -> str:
    """Per-video YouTube link (https://youtube.com/shorts/<id>)."""
    tpl = str(_links().get("yt_short")
              or "https://www.youtube.com/shorts/{video_id}")
    return tpl.format(video_id=video_id)


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

def build_caption(title: str, video_id: str = "",
                  for_platform: str = "") -> str:
    """Reel caption/description from the YouTube title + cross-links.

    OWNER RULE (2026-11-15): every reel description carries the OTHER
    platforms' links — the Instagram reel points at its YouTube twin
    + the Facebook page; the Facebook reel points at its YouTube twin
    + the Instagram profile; the TikTok title points at its YouTube
    twin + IG + FB. for_platform ("ig" / "fb" / "tiktok") skips the
    platform's own link; "" includes all (back-compat).
    """
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
    ln = _links()
    if video_id:
        lines += ["", f"▶ WATCH ON YOUTUBE: {short_url(video_id)}"]
    if for_platform != "ig" and ln.get("ig"):
        lines.append(f"📸 INSTAGRAM: {ln['ig']}")
    if for_platform != "fb" and ln.get("fb"):
        lines.append(f"📱 FACEBOOK: {ln['fb']}")
    if for_platform != "tiktok" and ln.get("tiktok"):
        lines.append(f"🎵 TIKTOK: {ln['tiktok']}")
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

    # Live probe 2026-11-15: the container response carries Meta's own
    # rupload URL in `uri` (currently the v27.0 path — v23.0-shaped
    # URLs get 400 there). Prefer Meta's URL; the versioned and
    # unversioned shapes remain as fallbacks inside _rupload.
    primary = str(c.get("uri")
                  or f"{RUPLOAD}/ig-api-upload/{_version()}/{container}")
    _rupload(primary, token, video,
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

def _permalink(token: str, oid: str, *, ig: bool) -> str:
    """Best-effort public permalink for a published reel (recorded in
    the crosspost status; feeds the YouTube description links)."""
    try:
        if ig:
            return str(_graph(token, oid, params={"fields": "permalink"})
                       .get("permalink") or "")
        p = str(_graph(token, oid, params={"fields": "permalink_url"})
                    .get("permalink_url") or "")
        if p.startswith("/"):
            p = "https://www.facebook.com" + p
        return p
    except Exception:                          # noqa: BLE001
        return ""


def _existing_reel(platform: str, yt_vid: str, *, igt: str = "",
                    ig_id: str = "", fbt: str = "",
                    page_id: str = "") -> str:
    """Dedupe gate (owner rule 2026-11-15: NO duplicate reels, ever).

    A reel for this exact YouTube twin may already be live even when
    the ledger disagrees — the 2026-11-15 races proved it (rupload
    500s that still publish, heal runs racing on stale state, #69/#11/
    #19 each went out twice on Instagram). Before ANY publish attempt
    we scan the platform's newest reels for the shorts/<video_id>
    link every caption carries; a hit means ADOPT, never re-publish.
    Returns the existing media id ('' = no twin found)."""
    if not yt_vid:
        return ""
    needle = f"shorts/{yt_vid}"
    try:
        if platform == "ig":
            d = _graph(igt, f"{ig_id}/media", params={
                "fields": "id,caption", "limit": "50"})
            for m in d.get("data") or []:
                if needle in str(m.get("caption") or ""):
                    return str(m.get("id") or "")
        else:
            d = _graph(fbt, f"{page_id}/videos", params={
                "fields": "id,description", "limit": "50"})
            for m in d.get("data") or []:
                if needle in str(m.get("description") or ""):
                    return str(m.get("id") or "")
    except Exception as e:                      # noqa: BLE001
        print(f"[crosspost] dedupe scan skipped ({e})")
    return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None        # redirects are followed by hand


def _download(url: str, headers: dict, out_path: Path,
              timeout: int = 600) -> None:
    """GET to a file, following redirects MANUALLY. GitHub artifact
    downloads 302/303 to a signed blob URL; urllib's automatic
    redirect forwards the Authorization header, which the blob host
    rejects with 401 (live-diagnosed 2026-11-15 — it delayed #69's
    reels by an hour). Leaving api.github.com drops the auth header."""
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(5):
        req = urllib.request.Request(url, headers=headers)
        try:
            with opener.open(req, timeout=timeout) as resp:
                out_path.write_bytes(resp.read())
                return
        except urllib.error.HTTPError as e:
            loc = (e.headers or {}).get("Location")
            if e.code in (301, 302, 303, 307) and loc:
                if urllib.parse.urlsplit(loc).netloc != "api.github.com":
                    headers = {k: v for k, v in headers.items()
                               if k.lower() != "authorization"}
                url = loc
                continue
            raise CrosspostError(
                f"download {e.code}: {url}") from e
    raise CrosspostError(f"too many redirects: {url}")


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
            _download(art["archive_download_url"],
                      {"Authorization": f"Bearer {token}",
                       "Accept": "application/vnd.github+json",
                       "User-Agent": "fort-clip-factory"}, zpath)
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


# ── the YouTube twin carries the links too ──────────────────────────

def youtube_add_links(state: dict, s: dict) -> None:
    """OWNER RULE (2026-11-15): the YouTube description carries the
    other platforms' links. Once a Short's reels are live, append a
    'SEE THIS SHORT ON' block (IG + FB permalinks) via videos.update.
    Idempotent, quota-aware (list 1 + update 50 units), non-fatal —
    failures retry with the next hourly heal."""
    vid = str(s.get("video_id") or "")
    cp = s.get("crosspost") or {}
    if not vid or cp.get("yt_links"):
        return
    ig, fb = cp.get("ig") or {}, cp.get("fb") or {}
    if not (ig.get("status") == "done" or fb.get("status") == "done"):
        return
    from . import ledger as _led
    from . import youtube as _yt
    try:
        if _led.quota_remaining(state) < 60:
            print("[crosspost] yt link update deferred (quota budget) "
                  "— retried next hour")
            return
        token = _yt.get_access_token()
        req = urllib.request.Request(
            f"{YT_API}/videos?part=snippet&id={vid}",
            headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            items = json.loads(r.read()).get("items") or []
        if not items:
            print(f"[crosspost] yt link update: {vid} not visible — "
                  "flagged done to stop retrying")
            s.setdefault("crosspost", {})["yt_links"] = True
            _led.save(state)
            return
        sn = items[0].get("snippet") or {}
        desc = str(sn.get("description") or "")
        if "SEE THIS SHORT ON" in desc:
            s.setdefault("crosspost", {})["yt_links"] = True
            _led.save(state)
            return
        ln = _links()
        lines = ["SEE THIS SHORT ON:",
                 "📸 Instagram: " + (str(ig.get("permalink") or "")
                                    or str(ln.get("ig") or "")),
                 "📱 Facebook: " + (str(fb.get("permalink") or "")
                                   or str(ln.get("fb") or ""))]
        new_desc = (desc.rstrip() + "\n\n" + "\n".join(lines))[:4900]
        body = json.dumps({"id": vid, "snippet": {
            "title": sn.get("title") or "",
            "categoryId": sn.get("categoryId") or "20",
            "description": new_desc}}).encode()
        req = urllib.request.Request(
            f"{YT_API}/videos?part=snippet", data=body, method="PUT",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        _led.spend_quota(state, 51)
        s.setdefault("crosspost", {})["yt_links"] = True
        _led.save(state)
        print(f"[crosspost] #{s.get('n')} YouTube description now "
              f"carries the reel links")
    except Exception as e:                     # noqa: BLE001
        print(f"[crosspost] yt link update failed for #{s.get('n')} "
              f"({e}) — retried next hour")


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

def auto_discover(state: dict, force: bool = False) -> None:
    """The owner's Meta side may complete at any moment (page created,
    assets assigned to the system user, fresh token pasted). Each heal
    run re-probes what the tokens can see and records any page + IG
    account — zero code changes needed to go live.

    Two discovery routes per token:
      /me/accounts -> pages, each with its linked IG business account
      /me/businesses -> /{b}/owned_instagram_accounts   (needs
      business_management on the token; live-probed 2026-11-15 — the
      current IG token lacks it, a fresh wider token unlocks this)

    The ig_username hint (channel.yaml crosspost.ig_username — owner
    said "Instagram is @h15master" 2026-11-15) picks the owner's
    account when several are visible. Every candidate is verified
    queryable with the SAME token before arming, so an id the machine
    could never publish to is never recorded.
    """
    ids = state.get("crosspost_ids") or {}
    if not force and ids.get("fb") and ids.get("ig"):
        # both platforms armed — the probes below are wasted calls (and
        # a Page-scoped FB token cannot even call /me/accounts; that
        # 400 noise printed in every heal log 2026-11-15). Manual
        # --check re-arms with force=True after a token rotation.
        return
    hint = str(_conf().get("ig_username") or "").strip().lstrip("@").lower()
    pages: list[dict] = []
    ig_candidates: list[tuple[str, dict]] = []      # (token, account)
    for label, tok in (("IG", ig_token()), ("FB", fb_token())):
        if not tok:
            continue
        try:
            d = _graph(tok, "me/accounts", params={
                "fields": "id,name,instagram_business_account"
                          "{id,username}",
                "limit": "25"})
            for p in d.get("data") or []:
                pages.append(p)
                igba = p.get("instagram_business_account") or {}
                if igba.get("id"):
                    ig_candidates.append((tok, igba))
        except Exception as e:                      # noqa: BLE001
            print(f"[crosspost] {label} token /me/accounts probe: {e}")
        try:
            b = _graph(tok, "me/businesses", params={"fields": "id,name"})
            for biz in b.get("data") or []:
                bid = str(biz.get("id") or "")
                if not bid:
                    continue
                try:
                    ib = _graph(tok, bid, params={
                        "fields": "owned_instagram_accounts"
                                  "{id,username}"})
                except Exception:                  # noqa: BLE001
                    continue
                for a in ((ib.get("owned_instagram_accounts") or {})
                          .get("data") or []):
                    if a.get("id"):
                        ig_candidates.append((tok, a))
        except Exception:                          # noqa: BLE001
            pass    # most tokens lack business_management — expected

    ids = state.setdefault("crosspost_ids", {})
    changed = False
    if pages and not ids.get("fb"):
        p = pages[0]
        ids["fb"] = str(p["id"])
        print(f"[crosspost] AUTO-DISCOVERED Facebook page "
              f"{p.get('name')} ({p['id']}) — FB reels ARMED")
        changed = True

    if not ids.get("ig") and ig_candidates:
        def _rank(pair: tuple[str, dict]) -> int:
            u = str((pair[1].get("username") or "")).lower()
            return 0 if (hint and u == hint) else 1
        for tok, acct in sorted(ig_candidates, key=_rank):
            aid = str(acct["id"])
            try:
                prof = _graph(tok, aid, params={"fields": "id,username"})
                uname = str(prof.get("username") or "")
                if uname and uname.lower() != str(
                        acct.get("username") or "").lower():
                    print(f"[crosspost] IG candidate {aid} username "
                          f"mismatch ({uname}) — skipped")
                    continue
            except Exception as e:                  # noqa: BLE001
                print(f"[crosspost] IG candidate {aid} not queryable by "
                      f"this token ({e}) — skipped")
                continue
            ids["ig"] = aid
            print(f"[crosspost] AUTO-DISCOVERED Instagram account "
                  f"@{acct.get('username')} ({aid}) — IG reels ARMED")
            changed = True
            break
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
    plats = ("ig", "fb")
    _t = _tt()
    if _t and _t.armed():
        plats = plats + ("tiktok",)
    return any((cp.get(p) or {}).get("status")
               not in ("done", "gave_up", "skipped")
               for p in plats)


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
    _t = _tt()
    tt_on = bool(_t and _t.armed())

    if not ig_on and not fb_on and not tt_on:
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
        if not tt_on:
            why.append("TikTok unarmed (TIKTOK_ACCESS_TOKEN secret "
                       "missing or crosspost.tiktok disabled — "
                       "RUNBOOK section 9)")
        print("[crosspost] no platform armed yet (" + "; ".join(why) +
              ") — YouTube keeps publishing alone; RUNBOOK.md has the "
              "3-minute Meta-side checklist (section 8) and the "
              "TikTok one (section 9)")
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
    if tt_on and not _t.token_alive(_t.token()):
        alert_once(state, "tiktok-token",
                   "TikTok token invalid — mirror paused",
                   "The TIKTOK_ACCESS_TOKEN was rejected by user/info. "
                   "The daily tiktok-token-refresh workflow renews it "
                   "(24 h lifetime); if it failed twice in a row, "
                   "re-authorize once via go-sb.html and update the "
                   "secrets (RUNBOOK section 9).")
        tt_on = False
    if not (ig_on or fb_on or tt_on):
        print("[crosspost] all tokens unhealthy — nothing to do this hour")
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
        yt_vid = str(s.get("video_id") or "")
        cap_ig = build_caption(str(s.get("title") or ""), yt_vid, "ig")
        cap_fb = build_caption(str(s.get("title") or ""), yt_vid, "fb")
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
            if _attempt_reel(state, s, "ig", video, cap_ig, dur,
                             igt=igt, ig_id=ig_id,
                             max_attempts=max_attempts,
                             yt_vid=yt_vid):
                published += 1
        if fb_on:
            if _attempt_reel(state, s, "fb", video, cap_fb, dur,
                             fbt=fbt, page_id=page_id,
                             max_attempts=max_attempts,
                             yt_vid=yt_vid):
                published += 1
        if tt_on:
            cap_tt = build_caption(str(s.get("title") or ""), yt_vid,
                                   "tiktok")
            if _t.attempt(state, s, video, cap_tt,
                          max_attempts=max_attempts):
                published += 1
        youtube_add_links(state, s)

    print(f"\n[crosspost] DONE: {published} post(s) live this run")
    return 0


def _attempt_reel(state: dict, s: dict, platform: str, video: Path,
                  caption: str, dur: float, *, igt: str = "",
                  ig_id: str = "", fbt: str = "", page_id: str = "",
                  max_attempts: int = 8, yt_vid: str = "") -> bool:
    """One platform attempt for one short. Returns True iff a reel
    went live this call. Failures are recorded (attempts/error) and
    swallowed — the YouTube pipeline is never affected."""
    n = s.get("n")
    st = (s.setdefault("crosspost", {})
          .setdefault(platform, {"status": "pending"}))
    if st.get("status") in ("done", "skipped", "gave_up"):
        return False
    # dedupe gate: adopt an already-live twin instead of re-publishing
    existing = _existing_reel(platform, yt_vid, igt=igt, ig_id=ig_id,
                               fbt=fbt, page_id=page_id)
    if existing:
        st.update(status="done", media_id=existing,
                  ts=int(time.time()), adopted=True)
        st["permalink"] = _permalink(igt if platform == "ig" else fbt,
                                      existing, ig=(platform == "ig"))
        print(f"[crosspost] #{n} {platform.upper()} already live as "
              f"{existing} — ADOPTED (no duplicate posted)")
        ledger.save(state)
        return True
    if platform == "fb" and dur > FB_REEL_MAX_S:
        st.update(status="skipped",
                  reason=f"duration {dur:.0f}s > "
                         f"{FB_REEL_MAX_S:.0f}s FB cap")
        print(f"[crosspost] #{n} FB skipped: {dur:.0f}s over the "
              f"{FB_REEL_MAX_S:.0f}s reel cap (Instagram still gets it)")
        ledger.save(state)
        return False
    attempts = int(st.get("attempts") or 0)
    if attempts >= max_attempts:
        st["status"] = "gave_up"
        alert_once(state, f"{platform}-{n}",
                   f"{platform.upper()} reel gave up for short #{n}",
                   f"short #{n} failed {attempts} hourly attempts: "
                   f"{st.get('error', '?')}")
        ledger.save(state)
        return False
    try:
        if platform == "ig":
            media_id = ig_publish_reel(video, caption, ig_id, igt)
            st.update(status="done", media_id=media_id,
                      ts=int(time.time()), attempts=attempts + 1)
            st["permalink"] = _permalink(igt, media_id, ig=True)
            print(f"[crosspost] #{n} -> Instagram reel {media_id} "
                  f"{st['permalink']}")
        else:
            vid = fb_publish_reel(video, caption, page_id, fbt)
            st.update(status="done", media_id=vid,
                      ts=int(time.time()), attempts=attempts + 1)
            st["permalink"] = _permalink(fbt, vid, ig=False)
            print(f"[crosspost] #{n} -> Facebook reel {vid} "
                  f"{st['permalink']}")
        ledger.save(state)
        return True
    except Exception as first_err:              # noqa: BLE001
        # IG second chance: 4:4:4 chroma files fail Meta's transcode
        # — re-encode to yuv420p and retry once within this attempt
        # (live-diagnosed + live-verified 2026-11-15).
        if platform == "ig" and any(k in str(first_err).lower() for k
                                    in ("transcode", "processingfailed",
                                        "rupload")):
            try:
                fixed = _ig_reencode(video, n)
                if fixed:
                    media_id = ig_publish_reel(fixed, caption, ig_id, igt)
                    st.update(status="done", media_id=media_id,
                              ts=int(time.time()), attempts=attempts + 1)
                    st["permalink"] = _permalink(igt, media_id, ig=True)
                    print(f"[crosspost] #{n} -> Instagram reel "
                          f"{media_id} (re-encoded yuv420p) "
                          f"{st['permalink']}")
                    ledger.save(state)
                    return True
            except Exception as e:                # noqa: BLE001
                first_err = e
        st.update(status="failed", attempts=attempts + 1,
                  error=str(first_err)[:220], ts=int(time.time()))
        print(f"[crosspost] #{n} {platform.upper()} FAILED "
              f"(attempt {attempts + 1}/{max_attempts}): {first_err}")
        ledger.save(state)
        return False


# ── the backfill: the whole catalogue gets its reels ───────────────

def _fetch_public_short(video_id: str, workdir: Path, n) -> Path | str | None:
    """Backfill source: pull our own already-public YouTube Short back
    down with yt-dlp, riding the SAME proven ladder as the factory —
    cookie pool (COOKIES_TXT/2/3) x player-client rotation + the EJS
    challenge-solver + deno runtime the job installs. YouTube
    bot-gates anonymous datacenter IPs (live-diagnosed 2026-11-15,
    twice). Returns the Path, None on a soft failure (retry next
    run), "GONE" when the source video was deleted/made private
    (permanent — the caller settles instead of burning retries), or
    "THROTTLED" when EVERY combo hit the bot-gate / 429 /
    storyboard-only wall (live-diagnosed 2026-11-15 18:06: after ~15
    backfill downloads in 2 h YouTube served storyboard-only even
    WITH cookies) — the caller cools down instead of hammering."""
    url = f"https://www.youtube.com/shorts/{video_id}"
    out = workdir / f"short_{n}.mp4"
    try:
        from .downloader import _cookie_jars, _rotate, FMT, JAR_CLIENTS
        jars = _cookie_jars(workdir)
    except Exception:                          # noqa: BLE001
        FMT, JAR_CLIENTS = "bv*+ba/b", ["web", "web_embedded", "tv"]
        jars = []
    # cookie-less clients hit DIFFERENT throttle pools (youtubei API,
    # no cookie support by design) — the tail of the ladder
    cookieless = ("android_vr", "tv_simply", "ios")
    if jars:
        combos = [(c, j) for j in _rotate(jars)
                   for c in JAR_CLIENTS][:12]
        combos += [(c, None) for c in cookieless]
    else:
        combos = ([(c, None) for c in cookieless]
                  + [(c, None) for c in JAR_CLIENTS])
    gone_markers = ("Video unavailable", "removed by the uploader",
                    "Private video", "account associated with",
                    "has been terminated")
    throttle_markers = ("Requested format is not available",
                        "Only images are available",
                        "HTTP Error 429", "Too Many Requests",
                        "Sign in to confirm")
    deadline = time.monotonic() + 480
    last = "no yt-dlp output"
    throttled = False
    for client, jar in combos:
        if time.monotonic() > deadline - 30:
            print(f"[backfill] #{n}: ladder out of time budget")
            break
        cmd = ["yt-dlp", "-f", FMT, "--merge-output-format", "mp4",
               "--no-playlist", "--no-part", "-o", str(out),
               "--socket-timeout", "20", "--retries", "2",
               "--fragment-retries", "2", "--no-warnings"]
        if jar:
            cmd += ["--cookies", str(jar)]
        cmd += ["--extractor-args",
                f"youtube:player_client={client}", url]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=150)
        except Exception as e:                  # noqa: BLE001
            last = f"timeout: {e}"
            continue
        if out.exists() and out.stat().st_size > 100_000:
            return out
        blob = (r.stderr or "") + (r.stdout or "")
        if any(m in blob for m in throttle_markers):
            throttled = True
        errs = [l for l in blob.splitlines() if "ERROR" in l]
        last = errs[-1][:160] if errs else "no error line"
        if any(m in blob for m in gone_markers):
            print(f"[backfill] #{n}: source video is GONE from "
                  "YouTube (deleted/private) — settling permanently")
            return "GONE"
        if out.exists() and out.stat().st_size < 100_000:
            out.unlink(missing_ok=True)       # error-page stub
        time.sleep(2)
    if throttled:
        print(f"[backfill] #{n}: YouTube served storyboard-only / "
              "throttled on every combo — cooling down, attempts "
              "NOT burned")
        return "THROTTLED"
    print(f"[backfill] download failed for #{n}: {last}")
    return None


def _ig_reencode(video: Path, n) -> Path | None:
    """Instagram's transcoders reject 4:4:4 chroma (live-diagnosed
    2026-11-15: "Video Transcoding Error: both HD and SD progressive
    failed to transcode") — the factory render emitted yuv444p before
    the yuv420p fix. Normalize any file to yuv420p High and let the
    caller retry the upload once. Best-effort: returns None on any
    ffmpeg problem."""
    out = video.with_name(video.stem + "_ig420.mp4")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
           "-c:v", "libx264", "-preset", "veryfast",
           "-profile:v", "high", "-pix_fmt", "yuv420p", "-crf", "20",
           "-c:a", "copy", "-movflags", "+faststart", str(out)]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except Exception as e:                      # noqa: BLE001
        print(f"[crosspost] IG re-encode failed for #{n}: {e}")
        return None
    if out.exists() and out.stat().st_size > 100_000:
        print(f"[crosspost] #{n} re-encoded to yuv420p for Instagram "
              f"({out.stat().st_size / 1e6:.1f} MB)")
        return out
    print(f"[crosspost] IG re-encode produced nothing for #{n}")
    return None


def _ffprobe_duration(path: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show-entries",
             "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip() or 0.0)
    except Exception:                            # noqa: BLE001
        return 0.0


def _backfill_settled(s: dict) -> bool:
    cp = s.get("crosspost") or {}
    plats = ("ig", "fb")
    _t = _tt()
    if _t and _t.armed():
        plats = plats + ("tiktok",)
    return all((cp.get(p) or {}).get("status")
               in ("done", "skipped", "gave_up")
               for p in plats)


def run_backfill(state: dict) -> int:
    """OWNER RULE (2026-11-15): make the three feeds look alike — the
    already-public YouTube Shorts get their Instagram + Facebook
    reels too. Quota reality (live-probed 2026-11-15): Instagram
    reports NO content_publishing_limit for this account (empty
    config) and Facebook documents no reel cap — the backfill runs at
    crosspost.backfill.per_hour shorts per heal run, oldest first,
    until the catalogue is mirrored. Legacy shorts are downloaded
    back from their own public YouTube URL (yt-dlp); fresh ones reuse
    the media-cache artifact."""
    bf = dict(_conf().get("backfill") or {})
    if not bf.get("enabled", True):
        print("[backfill] disabled in channel.yaml — nothing to do")
        return 0
    # YouTube throttle cooldown (live-diagnosed 2026-11-15 18:06):
    # storyboard-only formats even WITH cookies after ~15 downloads
    # in 2 h. Hammering worsens it; the backfill waits it out.
    cool = float(state.get("backfill_cooldown_until") or 0)
    if cool > time.time():
        until = datetime.fromtimestamp(
            cool, ZoneInfo(cfg()["schedule"]["timezone"]))
        print(f"[backfill] cooling down until {until:%H:%M %Z} "
              "(YouTube throttle) — due-reels unaffected")
        return 0
    auto_discover(state)
    igt, fbt = ig_token(), fb_token()
    ig_id, page_id = ig_user_id(state), fb_page_id(state)
    ig_on, fb_on = bool(igt and ig_id), bool(fbt and page_id)
    _t = _tt()
    tt_on = bool(_t and _t.armed())
    if not (ig_on or fb_on or tt_on):
        print("[backfill] no platform armed yet — nothing to backfill")
        return 0

    # recover media-fetch give-ups (the 401 artifact bug, fixed
    # 2026-11-15): those were never upload failures
    plats = ("ig", "fb") + (("tiktok",) if tt_on else ())
    for s in state.get("shorts", []):
        for p in plats:
            st = (s.get("crosspost") or {}).get(p) or {}
            if st.get("status") == "gave_up" and "media" in str(
                    st.get("reason") or "").lower():
                st.update(status="pending", attempts=0)
                st.pop("reason", None)
                print(f"[backfill] recovering #{s.get('n')} {p} "
                      "(media-fetch give-up — fetch bug fixed)")

    now = datetime.now(ZoneInfo(cfg()["schedule"]["timezone"]))
    cands = [s for s in state.get("shorts", [])
             if s.get("video_id")
             and (s.get("slot") is None or _slot_passed(s, now))
             and not _backfill_settled(s)]
    cands.sort(key=lambda x: int(x.get("n") or 0))
    budget = int(bf.get("per_hour", 4))
    cands = cands[:budget]
    if not cands:
        print("[backfill] the catalogue is fully cross-posted — idle")
        return 0
    print(f"[backfill] {len(cands)} Short(s) this run "
          f"(budget {budget}/run, oldest first)")

    max_attempts = int(_conf().get("max_attempts", 8))
    workdir = ROOT / "work_backfill"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    zips: dict = {}
    published = 0
    for s in cands:
        n, yt_vid = s.get("n"), str(s.get("video_id") or "")
        cap_ig = build_caption(str(s.get("title") or ""), yt_vid, "ig")
        cap_fb = build_caption(str(s.get("title") or ""), yt_vid, "fb")
        # adoption fast-path: if BOTH reels are already live (state
        # lagged reality — a stateless one-off published them, or a
        # memory push lost the race), adopt + settle WITHOUT
        # downloading a single byte from YouTube.
        # 2026-11-18 guard: only when NO TikTok work remains — the
        # fast-path `continue` used to skip the TikTok mirror
        # entirely, so ig/fb-complete shorts spun un-settled forever
        # (backfill picked them, adopted, skipped, repeat; the TikTok
        # backlog never drained). With TikTok pending we fall
        # through: the ig/fb attempts no-op on their done marks, the
        # video comes from the cache ladder, and the mirror runs.
        tt_pending = tt_on and (s.get("crosspost") or {}) \
            .get("tiktok", {}).get("status") \
            not in ("done", "skipped", "gave_up")
        if ig_on and fb_on and not tt_pending:
            ex_ig = _existing_reel("ig", yt_vid, igt=igt, ig_id=ig_id)
            if ex_ig:
                ex_fb = _existing_reel("fb", yt_vid, fbt=fbt,
                                       page_id=page_id)
                if ex_fb:
                    for platform, mid, tok in (("ig", ex_ig, igt),
                                               ("fb", ex_fb, fbt)):
                        st = (s.setdefault("crosspost", {})
                              .setdefault(platform, {"status": "pending"}))
                        st.update(status="done", media_id=mid,
                                  ts=int(time.time()), adopted=True)
                        st["permalink"] = _permalink(
                            tok, mid, ig=(platform == "ig"))
                    ledger.save(state)
                    print(f"[backfill] #{n} both reels already live — "
                          "ADOPTED, download skipped")
                    youtube_add_links(state, s)
                    continue
        video = None
        try:
            if s.get("media"):
                video = fetch_media(s["media"], workdir, zips)
        except CrosspostError as e:
            print(f"[backfill] #{n} cache fetch: {e}")
        if video is None:
            video = _fetch_public_short(yt_vid, workdir, n)
        if video == "THROTTLED":
            # YouTube served storyboard-only on every combo: cool down
            # (3 h default) WITHOUT burning the retry budget — the
            # remaining candidates would hit the same wall this run.
            cooldown_s = int(bf.get("cooldown_s", 3 * 3600))
            state["backfill_cooldown_until"] = time.time() + cooldown_s
            ledger.save(state)
            print(f"[backfill] YouTube throttled the downloads — "
                  f"cooling down {cooldown_s // 60} min (attempts "
                  "preserved); trying again after the cooldown")
            alert_once(
                state, "yt-throttle",
                "YouTube throttled the backfill downloads",
                "yt-dlp got storyboard-only formats / 429 on every "
                "cookie x player-client combo (bot-gate). The "
                "backfill cools down and retries automatically; the "
                "daily grid Shorts and their reels are unaffected "
                "(fresh media comes from the session cache, no "
                "download needed).")
            break
        if video == "GONE":
            # the source Short no longer exists on YouTube — settle
            # permanently instead of burning 8 retries
            for p in plats:
                stx = (s.setdefault("crosspost", {})
                       .setdefault(p, {"status": "pending"}))
                stx.update(status="gave_up", attempts=max_attempts,
                           reason="source video deleted from YouTube",
                           ts=int(time.time()))
            ledger.save(state)
            continue
        if video is None:
            for p in plats:
                stx = (s.setdefault("crosspost", {})
                       .setdefault(p, {"status": "pending"}))
                if stx.get("status") not in ("done", "skipped",
                                             "gave_up"):
                    stx.update(
                        attempts=int(stx.get("attempts") or 0) + 1,
                        error="backfill download failed",
                        ts=int(time.time()))
                    if int(stx.get("attempts")) >= max_attempts:
                        stx["status"] = "gave_up"
            ledger.save(state)
            continue
        dur = float(s.get("clip_dur") or 0)
        if not dur:
            dur = _ffprobe_duration(video)
            if dur:
                s["clip_dur"] = dur
        if ig_on and _attempt_reel(state, s, "ig", video, cap_ig, dur,
                                   igt=igt, ig_id=ig_id,
                                   max_attempts=max_attempts,
                                   yt_vid=yt_vid):
            published += 1
        if fb_on and _attempt_reel(state, s, "fb", video, cap_fb, dur,
                                   fbt=fbt, page_id=page_id,
                                   max_attempts=max_attempts,
                                   yt_vid=yt_vid):
            published += 1
        if tt_on:
            cap_tt = build_caption(str(s.get("title") or ""), yt_vid,
                                   "tiktok")
            if _t.attempt(state, s, video, cap_tt,
                          max_attempts=max_attempts):
                published += 1
        youtube_add_links(state, s)
    print(f"\n[backfill] DONE: {published} post(s) live this run")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────

def report(state: dict) -> None:
    igt, fbt = ig_token(), fb_token()
    ig_id, page_id = ig_user_id(state), fb_page_id(state)
    print("cross-posting status:")
    bf = _conf().get("backfill") or {}
    print(f"  backfill    : {'ON' if bf.get('enabled', True) else 'off'} "
          f"({bf.get('per_hour', 4)}/run, oldest first)")
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
    _t = _tt()
    ttok = _t.token() if _t else ""
    print(f"  TikTok      : "
          f"{'ARMED' if ttok else 'off'} "
          f"(token={'set' if ttok else 'MISSING'}, "
          f"privacy={(_t._conf().get('privacy', 'SELF_ONLY') if _t else 'SELF_ONLY')})")
    if ttok and _t:
        print(f"    token: {'alive' if _t.token_alive(ttok) else 'INVALID'} "
              "(refreshed daily by tiktok-token-refresh)")
    recent = state.get("shorts", [])[-SCAN_WINDOW:]
    for s in recent:
        cp = s.get("crosspost") or {}
        print(f"  #{s.get('n'):>3} slot={s.get('slot')} "
              f"ig={(cp.get('ig') or {}).get('status', '-'):>7} "
              f"fb={(cp.get('fb') or {}).get('status', '-') or '-':>7} "
              f"tt={(cp.get('tiktok') or {}).get('status', '-'):>7}")


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Cross-post Shorts as Instagram + Facebook reels "
                    "+ TikTok videos")
    ap.add_argument("--due", action="store_true",
                    help="publish posts for every Short whose grid slot "
                         "has passed (the hourly heal)")
    ap.add_argument("--backfill", action="store_true",
                    help="mirror the already-public YouTube catalogue "
                         "to Instagram + Facebook + TikTok "
                         "(paced per_hour/run)")
    ap.add_argument("--check", action="store_true",
                    help="status report: tokens, ids, recent shorts")
    args = ap.parse_args()

    state = ledger.load()
    if args.check:
        auto_discover(state, force=True)   # manual re-arm path
        report(state)
        return 0
    if args.due:
        return run_due(state)
    if args.backfill:
        return run_backfill(state)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
