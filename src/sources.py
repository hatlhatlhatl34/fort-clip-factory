"""Source finder — YouTube Data API v3 search.list.

OWNER RULES baked into the call itself:
  1. FRESHNESS: publishedAfter = now - max_age_days (334 days ≈ 11
     months). A video older than the window cannot even enter the
     candidate pool — the 7-year-old-footage mistake is structurally
     impossible, not filtered after the fact.
  2. VARIETY: each run uses a DIFFERENT query phrasing ("fortnite
     creative map", "fortnite map", "fortnite map gameplay", ...) and
     a rotating result order (date / viewCount / relevance), so the
     machine never drills one corner of YouTube.
  3. NO REPEATS: every source already in state.used_sources is
     skipped — a video is mined at most once, ever.

Cost: 100 quota units per search (the only read the machine does).

AUTH: prefers the YOUTUBE_API_KEY secret; when that is absent it
falls back to the OAuth bearer token minted from the upload
credentials (tools/auth.py grants the full `youtube` scope, so the
same refresh token covers search too — one fewer secret to manage).
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import ledger
from .config import cfg

API_BASE = "https://www.googleapis.com/youtube/v3"
SEARCH_UNITS = 100


class SourceError(RuntimeError):
    pass


_BEARER: str | None = None      # access-token cache (one mint per run)


def _search_auth() -> tuple[dict, dict]:
    """Auth for read calls (search.list / videos.list).

    Returns (extra_query_params, extra_headers): either an API key in
    the query string, or an OAuth Authorization: Bearer header from
    the YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN trio.
    """
    import os
    key = os.environ.get("YOUTUBE_API_KEY", "")
    if key:
        return {"key": key}, {}
    global _BEARER
    if _BEARER is None:
        from . import youtube
        try:
            _BEARER = youtube.get_access_token()
        except youtube.AuthError as e:
            raise SourceError(
                "no search credential available: set either the "
                "YOUTUBE_API_KEY secret or the OAuth trio YT_CLIENT_ID / "
                "YT_CLIENT_SECRET / YT_REFRESH_TOKEN (see RUNBOOK.md)") \
                from e
    return {}, {"Authorization": f"Bearer {_BEARER}"}


def _iso_duration_seconds(iso: str) -> float:
    import re
    m = re.fullmatch(
        r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?",
        (iso or "").strip())
    if not m:
        return 0.0
    d, h, mi, s = (float(x) if x else 0.0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def search_sources(state: dict, seed: int) -> list[dict]:
    """Search for fresh Fortnite map gameplay videos.

    Returns a list of candidates:
      [{id, title, channel, published_at, duration_s, views}]
    ordered by the API's ranking. Fresh + duration-filtered + not
    already mined. Two complementary calls are merged (date-ordered
    AND rank-ordered) so the pick is never just "whatever is newest".
    """
    conf = cfg()["sources"]
    auth_params, auth_headers = _search_auth()
    rng = random.Random(seed)

    queries = conf["queries"]
    orders = conf.get("orders") or ["relevance"]
    cursor = int(state.get("query_cursor", 0)) % len(queries)
    query = queries[cursor]
    state["query_cursor"] = (cursor + 1) % len(queries)
    order = rng.choice(orders)

    published_after = (datetime.now(timezone.utc)
                       - timedelta(days=int(conf["max_age_days"]))
                       ).strftime("%Y-%m-%dT%H:%M:%SZ")

    seen: dict[str, dict] = {}
    for order_i in ({order} | set(orders)):
        params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": str(conf.get("results_per_search", 25)),
            "order": order_i,
            "publishedAfter": published_after,
            "videoEmbeddable": "true",
            "relevanceLanguage": "en",
        }
        params.update(auth_params)
        url = f"{API_BASE}/search?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url, headers={"User-Agent": "fort-clip-factory/1.0",
                         **auth_headers})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:300]
            except Exception:
                pass
            raise SourceError(f"search failed ({e.code}): {body}") from e
        ledger.spend_quota(state, SEARCH_UNITS)
        for it in data.get("items", []):
            vid = (it.get("id") or {}).get("videoId")
            if not vid:
                continue
            sn = it.get("snippet", {})
            seen.setdefault(vid, {
                "id": vid,
                "title": sn.get("title", ""),
                "channel": (sn.get("channelTitle") or "").strip(),
                "published_at": sn.get("publishedAt", ""),
                "order": order_i,
                "query": query,     # rides along for XP-aware titling
            })

    candidates = list(seen.values())
    if not candidates:
        raise SourceError(
            f"search '{query}' (after {published_after}) returned no videos")

    # duration filter needs one videos.list call (1 unit, up to 50 ids)
    durations = _fetch_durations(list(seen.keys()))
    min_s = float(conf.get("min_duration_s", 120))
    max_s = float(conf.get("max_duration_s", 5400))
    used = ledger.used_sources(state)

    fresh = []
    for c in candidates:
        dur = durations.get(c["id"])
        if dur is None or not (min_s <= dur <= max_s):
            continue
        if conf.get("no_repeat_sources", True) and c["id"] in used:
            continue
        c["duration_s"] = dur
        fresh.append(c)

    if not fresh:
        raise SourceError(
            "all candidates filtered out (too short/too long/already "
            f"mined) — widen sources.queries or raise max_age_days")

    # prefer lively uploaders over dead ones: sort by a seeded shuffle
    # of recency (freshness first) — never the same pick order twice.
    fresh.sort(key=lambda c: c.get("published_at") or "", reverse=True)
    # B5 (audit 2026-11-18): shuffle(fresh[:k]) shuffled a COPY — a
    # silent no-op. Shuffle the real list: the fresher half randomized,
    # the older half stays date-sorted behind it.
    half = fresh[:max(1, len(fresh) // 2)]
    rng.shuffle(half)
    fresh = half + fresh[len(half):]
    return fresh


def _fetch_durations(video_ids: list[str]) -> dict[str, float]:
    auth_params, auth_headers = _search_auth()
    out: dict[str, float] = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        params = {
            "part": "contentDetails",
            "id": ",".join(batch),
        }
        params.update(auth_params)
        url = f"{API_BASE}/videos?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url, headers={"User-Agent": "fort-clip-factory/1.0",
                         **auth_headers})
        # B2 (audit 2026-11-18): an unguarded videos.list blew the
        # whole session (uncaught HTTPError/URLError) BEFORE any clip
        # was made and lost the search's quota spend. One retry, then
        # a clean SourceError the orchestrator already handles.
        data = None
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read())
                break
            except (urllib.error.HTTPError, urllib.error.URLError) as e:
                if attempt == 2:
                    raise SourceError(
                        f"videos.list failed: {e}") from e
                print(f"[sources] videos.list hiccup ({e}) — retrying")
                time.sleep(3)
        for it in (data or {}).get("items", []):
            out[it["id"]] = _iso_duration_seconds(
                (it.get("contentDetails") or {}).get("duration", ""))
    return out


def pick_source(state: dict, seed: int) -> dict:
    """Search + pick ONE random fresh source (the machine's daily act)."""
    candidates = search_sources(state, seed)
    rng = random.Random(seed ^ 0xC0FFEE)
    choice = rng.choice(candidates[:10])   # among the 10 freshest-ranked
    print(f"[sources] picked: {choice['title']!r} "
          f"({choice['channel']}, {choice.get('duration_s', 0):.0f}s, "
          f"published {choice.get('published_at', '?')[:10]})")
    return choice


# ── THE MONTHLY SWEEP (owner rule 2026-11-18) ────────────────────────
#
# "harvest ALL the clips in the beginning of the month" — the month's
# candidate pool is built by ONE full sweep over EVERY query phrasing
# (not the one-per-run rotation the daily act uses), 50 results each,
# then cached in state.bank.pool: the month's stacking sessions reuse
# the SAME pool without re-spending search quota. The pool is wiped
# with the bank at month rollover, so every month starts from a fully
# fresh "all videos" sweep — exactly the owner's algorithm-freshness
# rule (no old bank clips, no old pool).

def harvest_pool(state: dict, seed: int) -> list[dict]:
    """The month's unmined candidate pool (bank.pool), swept or cached.

    Returns candidates [{id, title, channel, published_at,
    duration_s, query}] sorted freshest-first. Raises SourceError
    only when no credential is available at all; a partially failed
    sweep returns whatever did come through."""
    conf = cfg()["sources"]
    hconf = cfg().get("harvest") or {}
    pool_min = int(hconf.get("pool_min", 40))
    sweep_results = int(hconf.get("sweep_results", 50))
    pool_cap = int(hconf.get("pool_cap", 300))

    used = ledger.used_sources(state)
    bankst = state.setdefault("bank", {})
    cached = [c for c in (bankst.get("pool") or [])
              if c.get("id") not in used]
    if len(cached) >= pool_min:
        print(f"[harvest] reusing the cached month pool: "
              f"{len(cached)} unmined candidate(s) — no quota spent")
        return cached
    if cached:
        print(f"[harvest] cached pool low ({len(cached)} < {pool_min}) "
              "— re-sweeping every query for fresh videos")

    # a full sweep costs 16 x 100 + duration calls. Margin note: the
    # sweep runs on the CURRENT Pacific quota day, and the only other
    # big spender on a day is the 08:33 upload session (6,400) — a
    # sweep next to it totals ~8,000 of 10,000, so a small margin
    # suffices; the 00:29 first-of-month run often still has most of
    # the previous Pacific day's budget left.
    n_queries = len(conf["queries"])
    need_units = n_queries * SEARCH_UNITS + 40
    if ledger.quota_remaining(state) < need_units + 400:
        if cached:
            print(f"[harvest] quota too low for a full sweep "
                  f"({ledger.quota_remaining(state)} left) — running "
                  f"on the cached pool ({len(cached)})")
            return cached
        raise SourceError(
            f"quota too low for the monthly sweep "
            f"({ledger.quota_remaining(state)} units left, need "
            f"{need_units}) — guard retries on the next quota day")

    auth_params, auth_headers = _search_auth()
    rng = random.Random(seed)
    published_after = (datetime.now(timezone.utc)
                       - timedelta(days=int(conf["max_age_days"]))
                       ).strftime("%Y-%m-%dT%H:%M:%SZ")
    orders = list(conf.get("orders") or ["relevance"])

    seen: dict[str, dict] = {}
    failures = 0
    for qi, query in enumerate(conf["queries"]):
        order = orders[(qi + rng.randrange(len(orders))) % len(orders)]
        params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": str(sweep_results),
            "order": order,
            "publishedAfter": published_after,
            "videoEmbeddable": "true",
            "relevanceLanguage": "en",
        }
        params.update(auth_params)
        url = f"{API_BASE}/search?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url, headers={"User-Agent": "fort-clip-factory/1.0",
                          **auth_headers})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            failures += 1
            print(f"[harvest] sweep query {query!r} failed "
                  f"({e.code}) — continuing with the rest")
            continue
        ledger.spend_quota(state, SEARCH_UNITS)
        for it in data.get("items", []):
            vid = (it.get("id") or {}).get("videoId")
            if not vid or vid in seen:
                continue
            sn = it.get("snippet", {})
            seen[vid] = {
                "id": vid,
                "title": sn.get("title", ""),
                "channel": (sn.get("channelTitle") or "").strip(),
                "published_at": sn.get("publishedAt", ""),
                "query": query,
            }
    if not seen:
        raise SourceError(
            "the monthly sweep returned no videos at all "
            f"({failures}/{n_queries} queries failed)")

    # duration window filter (same law as the daily act)
    durations = _fetch_durations(list(seen.keys()))
    min_s = float(conf.get("min_duration_s", 120))
    max_s = float(conf.get("max_duration_s", 5400))
    fresh = []
    for c in seen.values():
        dur = durations.get(c["id"])
        if dur is None or not (min_s <= dur <= max_s):
            continue
        if conf.get("no_repeat_sources", True) and c["id"] in used:
            continue
        c["duration_s"] = dur
        fresh.append(c)

    fresh.sort(key=lambda c: c.get("published_at") or "", reverse=True)
    # B5 (audit 2026-11-18): shuffle the REAL list, not a slice copy
    half = fresh[:max(1, len(fresh) // 2)]
    rng.shuffle(half)                    # lively, not robotic
    fresh = half + fresh[len(half):]
    pool = fresh[:pool_cap]
    bankst["pool"] = pool            # cached for the stacking sessions
    print(f"[harvest] full sweep: {n_queries} queries x "
          f"{sweep_results} results -> {len(seen)} unique, "
          f"{len(fresh)} fresh after filters, pool capped at {len(pool)}")
    return pool
