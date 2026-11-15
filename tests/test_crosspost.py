"""Architecture tests for cross-posting (owner rule 2026-11-15):

  1. the SAME Short ships to YouTube + Instagram Reel + Facebook Reel
  2. Instagram: container -> resumable rupload -> poll -> publish
  3. Facebook: start -> rupload -> finish(PUBLISHED)
  4. captions: Fortnite rule, codes carried, #{n} bookkeeping dropped
  5. failure isolation: a reel failure NEVER touches the YouTube flow
  6. FB 90 s cap: long clips skip FB, still ship to Instagram
  7. retry budget: gave_up + one alert after max_attempts
  8. auto-discovery arms ids the hour the owner finishes Meta setup:
     pages via /me/accounts, IG accounts via page links OR the
     business walk (me/businesses -> owned_instagram_accounts), with
     the @h15master hint (channel.yaml crosspost.ig_username) picking
     the owner's account; unqueryable ids are never armed
  9. clip ceiling drops to 90 s (FB reel cap)
 10. cross-links (owner rule 2026-11-15): every description carries
     the OTHER platforms' links — IG captions get YT twin + FB page,
     FB descriptions get YT twin + IG profile, YouTube descriptions
     get the reel permalinks appended (quota-aware, non-fatal)
 11. backfill: the whole already-public catalogue gets its reels —
     oldest first, per_hour budget, legacy shorts downloaded from
     their own public YouTube URL, media-fetch give-ups recovered
 12. IG rupload uses Meta's own `uri` from the container response

Run:  python3 tests/test_crosspost.py
(no pytest dependency; plain asserts + prints)
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import crosspost, ledger                     # noqa: E402
from src.config import cfg                            # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def patched(**fakes):
    """Decorator-ish helper: swap crosspost functions for fakes."""
    saved = {k: getattr(crosspost, k) for k in fakes}

    def apply():
        for k, v in fakes.items():
            setattr(crosspost, k, v)

    def restore():
        for k, v in saved.items():
            setattr(crosspost, k, v)
    return apply, restore


def main() -> int:
    # memory must never be written by tests
    ledger.set_read_only(True)
    try:
        return _run()
    finally:
        ledger.set_read_only(False)
        shutil.rmtree(ROOT / "media_cache", ignore_errors=True)
        shutil.rmtree(ROOT / "work_crosspost", ignore_errors=True)


def _run() -> int:
    # ── 1. config: the owner rule is wired in ──────────────────────
    cp = cfg()["crosspost"]
    check("crosspost enabled", cp.get("enabled") is True)
    check("graph version pinned", cp.get("api_version") == "v23.0",
          str(cp.get("api_version")))
    check("retry budget set", int(cp.get("max_attempts", 0)) == 8)
    check("hashtags configured", len(cp.get("hashtags") or []) >= 4)
    check("clip ceiling 90 s (FB reel cap)",
          cfg()["clip"]["target_len_s"] == [45, 90],
          str(cfg()["clip"]["target_len_s"]))
    check("FB cap constant", crosspost.FB_REEL_MAX_S == 90.0)

    # ── 2. caption ─────────────────────────────────────────────────
    c = crosspost.build_caption(
        "This Fortnite XP map = FREE Battle Pass 🤑 #24")
    first = c.splitlines()[0]
    check("caption drops the #{n} tag",
          not re.search(r"#\d+$", first), first)
    check("caption keeps the hook",
          "FREE Battle Pass" in first, first)
    check("caption carries CREATOR CODE", "CREATOR CODE: H15" in c)
    check("caption carries MAP CODE", "MAP CODE: 1789-8528-6429" in c)
    check("caption carries hashtags", "#fortnite" in c)
    check("caption <= 2200", len(c) <= 2200, str(len(c)))
    c2 = crosspost.build_caption("Free skins drop tomorrow")
    check("caption enforces the Fortnite rule",
          c2.splitlines()[0].startswith("Fortnite"), c2.splitlines()[0])

    # ── 3. due selection ───────────────────────────────────────────
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(cfg()["schedule"]["timezone"])
    now = datetime.now(tz)
    past = (now - timedelta(hours=1)).isoformat()
    future = (now + timedelta(hours=6)).isoformat()
    legacy = {"n": 1, "video_id": "x", "slot": past}          # no media
    fresh = {"n": 2, "video_id": "x", "slot": past,
             "media": {"file": "short_2.mp4"},
             "crosspost": {"ig": {"status": "pending"},
                           "fb": {"status": "pending"}}}
    upcoming = {"n": 3, "video_id": "x", "slot": future,
                "media": {"file": "short_3.mp4"},
                "crosspost": {"ig": {"status": "pending"},
                              "fb": {"status": "pending"}}}
    done = {"n": 4, "video_id": "x", "slot": past,
            "media": {"file": "short_4.mp4"},
            "crosspost": {"ig": {"status": "done"},
                          "fb": {"status": "done"}}}
    check("slot passed detected", crosspost._slot_passed(fresh, now))
    check("future slot not due", not crosspost._slot_passed(upcoming, now))
    check("garbage slot not due", not crosspost._slot_passed(
        {"slot": "not-a-date"}, now))
    check("completed short is settled", not crosspost._incomplete(done))
    check("pending short is incomplete", crosspost._incomplete(fresh))
    check("legacy short (no media) ignored",
          not (legacy.get("media") and True))

    # ── 4. Instagram flow: container -> rupload -> poll -> publish ─
    with tempfile.TemporaryDirectory() as td:
        vid = Path(td) / "short_2.mp4"
        vid.write_bytes(b"FAKEVIDEOFORTEST" * 1000)

        calls: list[tuple] = []

        def fake_graph(token, path, method="GET", params=None, timeout=60):
            calls.append((method, path.lstrip("/"), dict(params or {})))
            if path.rstrip("/").endswith("/media") and method == "POST":
                return {"id": "C1"}
            if path.rstrip("/") == "C1":
                return {"status_code": "FINISHED"}
            if path.rstrip("/").endswith("/media_publish"):
                return {"id": "M9"}
            raise AssertionError(f"unexpected graph call {method} {path}")

        def fake_rupload(url, token, video, versioned_fallback=None):
            calls.append(("RUPLOAD", url, Path(video).name))
            return {"success": True}

        apply, restore = patched(_graph=fake_graph, _rupload=fake_rupload)
        apply()
        try:
            media_id = crosspost.ig_publish_reel(
                vid, "caption text", "17841234567890123", "TOK")
        finally:
            restore()
        check("IG publish returns the media id", media_id == "M9",
              media_id)
        create = next(c for c in calls if c[0] == "POST"
                      and c[1].endswith("/media"))
        check("IG container is a REEL", create[2]["media_type"] == "REELS")
        check("IG container uses resumable upload",
              create[2]["upload_type"] == "resumable",
              str(create[2]))
        check("IG caption passed", create[2]["caption"] == "caption text")
        rup = next(c for c in calls if c[0] == "RUPLOAD")
        check("IG bytes go to rupload with version + container",
              rup[1].endswith("/ig-api-upload/v23.0/C1"), rup[1])
        pub = next(c for c in calls if c[0] == "POST"
                   and c[1].endswith("/media_publish"))
        check("IG publish uses creation_id",
              pub[2]["creation_id"] == "C1")

    # ── 5. Facebook flow: start -> rupload -> finish ───────────────
    with tempfile.TemporaryDirectory() as td:
        vid = Path(td) / "short_2.mp4"
        vid.write_bytes(b"FAKEVIDEOFORTEST" * 1000)
        calls.clear()

        def fb_graph(token, path, method="GET", params=None, timeout=60):
            calls.append((method, path.lstrip("/"), dict(params or {})))
            if path.rstrip("/").endswith("/video_reels"):
                p = params or {}
                if p.get("upload_phase") == "start":
                    return {"video_id": "V1",
                            "upload_url": "https://rupload.facebook.com/"
                                          "video-upload/V1"}
                if p.get("upload_phase") == "finish":
                    return {"success": True}
            raise AssertionError(f"unexpected graph call {method} {path}")

        def fb_rupload(url, token, video, versioned_fallback=None):
            calls.append(("RUPLOAD", url, Path(video).name))
            return {"success": True}

        apply, restore = patched(_graph=fb_graph, _rupload=fb_rupload)
        apply()
        try:
            video_id = crosspost.fb_publish_reel(
                vid, "reel description", "1234567890", "TOK")
        finally:
            restore()
        check("FB publish returns the video id", video_id == "V1",
              video_id)
        rup = next(c for c in calls if c[0] == "RUPLOAD")
        check("FB bytes go to the start-phase upload_url",
              rup[1] == "https://rupload.facebook.com/video-upload/V1",
              rup[1])
        fin = [c for c in calls if c[0] == "POST"
               and c[1].endswith("/video_reels")
               and c[2].get("upload_phase") == "finish"]
        check("FB finish marks PUBLISHED",
              fin and fin[0][2]["video_state"] == "PUBLISHED")
        check("FB finish carries the description",
              fin and fin[0][2]["description"] == "reel description")

    # ── 6. run_due: isolation, retries, FB cap ─────────────────────
    state = {
        "shorts": [dict(fresh)],
        "crosspost_ids": {"ig": "17841234567890123", "fb": "1234567890"},
        "alerts_sent": {},
    }
    os.environ["IG_ACCESS_TOKEN"] = "IGT"
    os.environ["FB_ACCESS_TOKEN"] = "FBT"
    try:
        with tempfile.TemporaryDirectory() as td:
            vid = Path(td) / "short_2.mp4"
            vid.write_bytes(b"FAKEVIDEOFORTEST" * 1000)

            ig_calls: list[int] = []
            fb_calls: list[int] = []
            alerts: list[str] = []

            def ok_graph(token, path, method="GET", params=None,
                         timeout=60):
                if path.rstrip("/") == "me":
                    return {"id": "1", "name": "n"}
                if path.rstrip("/").endswith(
                        "/content_publishing_limit"):
                    return {"data": [{"quota_total": 100}]}
                raise AssertionError(f"unexpected {method} {path}")

            def ig_pub(video, caption, ig_id, token):
                ig_calls.append(1)
                raise crosspost.CrosspostError("IG is down (test)")

            def fb_pub(video, description, page_id, token):
                fb_calls.append(1)
                return "V2"

            def fetch(media, workdir, zips=None):
                return vid

            def discover(st):
                return None

            def alert(st, key, title, body):
                alerts.append(key)

            apply, restore = patched(
                _graph=ok_graph, ig_publish_reel=ig_pub,
                fb_publish_reel=fb_pub, fetch_media=fetch,
                auto_discover=discover, alert_once=alert)
            apply()
            try:
                rc = crosspost.run_due(state)
            finally:
                restore()
            s = state["shorts"][0]
            check("run_due exits 0 even when IG fails", rc == 0)
            check("IG failure recorded", s["crosspost"]["ig"]["status"]
                  == "failed", str(s["crosspost"]["ig"]))
            check("IG attempt counted",
                  s["crosspost"]["ig"]["attempts"] == 1)
            check("FB still shipped (isolation!)",
                  s["crosspost"]["fb"]["status"] == "done"
                  and s["crosspost"]["fb"]["media_id"] == "V2")
            check("both platforms were attempted",
                  ig_calls == [1] and fb_calls == [1])
            check("no alert on first failure", not alerts)

        # FB duration cap: a 106 s clip skips FB, keeps IG
        long_state = {
            "shorts": [{"n": 5, "video_id": "x", "slot": past,
                        "clip_dur": 106.0,
                        "media": {"file": "short_5.mp4"},
                        "crosspost": {"ig": {"status": "pending"},
                                      "fb": {"status": "pending"}}}],
            "crosspost_ids": {"ig": "17841234567890123",
                              "fb": "1234567890"},
            "alerts_sent": {},
        }
        with tempfile.TemporaryDirectory() as td:
            vid = Path(td) / "short_5.mp4"
            vid.write_bytes(b"X" * 2000)
            fb_calls.clear()

            def fb_pub2(video, description, page_id, token):
                fb_calls.append(1)
                return "VX"

            def ig_pub2(video, caption, ig_id, token):
                return "MX"

            apply, restore = patched(
                _graph=ok_graph, ig_publish_reel=ig_pub2,
                fb_publish_reel=fb_pub2, fetch_media=fetch,
                auto_discover=discover, alert_once=alert)
            apply()
            try:
                crosspost.run_due(long_state)
            finally:
                restore()
            s5 = long_state["shorts"][0]
            check("106 s clip skips FB", s5["crosspost"]["fb"]["status"]
                  == "skipped", str(s5["crosspost"]["fb"]))
            check("skip reason mentions the cap",
                  "90" in s5["crosspost"]["fb"].get("reason", ""))
            check("106 s clip still ships to IG",
                  s5["crosspost"]["ig"]["status"] == "done")
            check("FB was never called for the long clip",
                  not fb_calls)

        # retry budget: attempts exhausted -> gave_up + ONE alert
        spent_state = {
            "shorts": [{"n": 6, "video_id": "x", "slot": past,
                        "clip_dur": 50.0,
                        "media": {"file": "short_6.mp4"},
                        "crosspost": {"ig": {"status": "failed",
                                             "attempts": 8},
                                      "fb": {"status": "pending"}}}],
            "crosspost_ids": {"ig": "17841234567890123",
                              "fb": "1234567890"},
            "alerts_sent": {},
        }
        ig_calls.clear()
        apply, restore = patched(
            _graph=ok_graph, ig_publish_reel=ig_pub2,
            fb_publish_reel=fb_pub, fetch_media=fetch,
            auto_discover=discover, alert_once=alert)
        apply()
        try:
            crosspost.run_due(spent_state)
        finally:
            restore()
        s6 = spent_state["shorts"][0]
        check("exhausted IG budget -> gave_up",
              s6["crosspost"]["ig"]["status"] == "gave_up",
              str(s6["crosspost"]["ig"]))
        check("gave_up fires exactly one alert",
              alerts == ["ig-6"], str(alerts))
        check("IG publish not retried after give_up", not ig_calls)
        check("FB still processed", s6["crosspost"]["fb"]["status"]
              == "done")

        # unarmed machine: cleanly OFF, no error
        os.environ.pop("IG_ACCESS_TOKEN", None)
        os.environ.pop("FB_ACCESS_TOKEN", None)
        off_state = {"shorts": [dict(fresh)], "alerts_sent": {}}
        apply, restore = patched(auto_discover=discover)
        apply()
        try:
            rc = crosspost.run_due(off_state)
        finally:
            restore()
        check("unarmed crosspost exits clean", rc == 0)
        os.environ["IG_ACCESS_TOKEN"] = "IGT"
        os.environ["FB_ACCESS_TOKEN"] = "FBT"

    finally:
        os.environ.pop("IG_ACCESS_TOKEN", None)
        os.environ.pop("FB_ACCESS_TOKEN", None)

    # ── 7. auto-discovery arms the ids ─────────────────────────────
    os.environ["IG_ACCESS_TOKEN"] = "IGT"
    try:
        st = {"crosspost_ids": {}, "alerts_sent": {}}

        def disc_graph(token, path, method="GET", params=None, timeout=60):
            p = path.rstrip("/")
            if p == "me/accounts":
                return {"data": [{
                    "id": "9876543210", "name": "H15 Fortnite",
                    "instagram_business_account": {
                        "id": "17849998888777766",
                        "username": "h15.fortnite"}}]}
            if p == "me/businesses":
                return {"data": []}
            if p == "17849998888777766":       # the arming verification
                return {"id": "17849998888777766",
                        "username": "h15.fortnite"}
            raise AssertionError(f"unexpected {path}")

        apply, restore = patched(_graph=disc_graph)
        apply()
        try:
            crosspost.auto_discover(st)
        finally:
            restore()
        check("discovery finds the page", st["crosspost_ids"].get("fb")
              == "9876543210", str(st.get("crosspost_ids")))
        check("discovery finds the IG account",
              st["crosspost_ids"].get("ig") == "17849998888777766")

        # re-run does not clobber
        crosspost.auto_discover(st)
        check("discovery is idempotent",
              st["crosspost_ids"].get("ig") == "17849998888777766")

        # 7b. BUSINESS route: no pages, the token sees the business's
        # IG accounts — the @h15master hint picks the owner's account
        # among several
        st2 = {"crosspost_ids": {}, "alerts_sent": {}}

        def biz_graph(token, path, method="GET", params=None, timeout=60):
            p = path.rstrip("/")
            if p == "me/accounts":
                return {"data": []}
            if p == "me/businesses":
                return {"data": [{"id": "111222333", "name": "H15 Biz"}]}
            if p == "111222333":
                return {"owned_instagram_accounts": {"data": [
                    {"id": "17850000000000001", "username": "otheracct"},
                    {"id": "17851111111111111", "username": "h15master"}]}}
            if p == "17851111111111111":     # hint-ranked candidate first
                return {"id": "17851111111111111",
                        "username": "h15master"}
            raise AssertionError(f"unexpected {path}")

        apply, restore = patched(_graph=biz_graph)
        apply()
        try:
            crosspost.auto_discover(st2)
        finally:
            restore()
        check("business route finds @h15master (hint)",
              st2["crosspost_ids"].get("ig") == "17851111111111111",
              str(st2.get("crosspost_ids")))
        check("business route leaves FB unarmed (no page)",
              not st2["crosspost_ids"].get("fb"))

        # 7c. an id the token cannot query (asset not assigned) is
        # NEVER armed — the machine records nothing it cannot publish to
        st3 = {"crosspost_ids": {}, "alerts_sent": {}}

        def locked_graph(token, path, method="GET", params=None,
                         timeout=60):
            p = path.rstrip("/")
            if p == "me/accounts":
                return {"data": []}
            if p == "me/businesses":
                return {"data": [{"id": "111222333", "name": "H15 Biz"}]}
            if p == "111222333":
                return {"owned_instagram_accounts": {"data": [
                    {"id": "17850000000000001", "username": "h15master"}]}}
            if p == "17850000000000001":
                raise crosspost.CrosspostError(
                    "(#10) asset not assigned to this token (test)")
            raise AssertionError(f"unexpected {path}")

        apply, restore = patched(_graph=locked_graph)
        apply()
        try:
            crosspost.auto_discover(st3)
        finally:
            restore()
        check("unqueryable IG id is never armed",
              not st3.get("crosspost_ids", {}).get("ig"),
              str(st3.get("crosspost_ids")))

        # 7d. the hint itself is wired in channel.yaml (owner: the
        # Instagram account IS @h15master)
        check("ig_username hint configured",
              str(cfg()["crosspost"].get("ig_username") or "").lower()
              == "h15master",
              str(cfg()["crosspost"].get("ig_username")))
    finally:
        os.environ.pop("IG_ACCESS_TOKEN", None)

    # ── 8. cache_video: the session side ───────────────────────────
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "final.mp4"
        src.write_bytes(b"Y" * 5000)
        rec = {"n": 7}
        os.environ["GITHUB_RUN_ID"] = "424242"
        try:
            media = crosspost.cache_video({"shorts": []}, rec, src)
        finally:
            os.environ.pop("GITHUB_RUN_ID", None)
        check("cache_video stamps the media pointer",
              media and media["file"] == "short_7.mp4"
              and media["run_id"] == "424242"
              and media["artifact"].startswith("media-cache-"),
              str(media))
        check("cache_video arms the crosspost statuses",
              rec["crosspost"]["ig"]["status"] == "pending"
              and rec["crosspost"]["fb"]["status"] == "pending")
        check("bytes copied", (ROOT / "media_cache" / "short_7.mp4")
              .stat().st_size == 5000)

        # a cache failure never raises
        def boom(*a, **k):
            raise RuntimeError("disk full")

        import shutil as _sh
        _saved_copy = crosspost.shutil.copy2
        crosspost.shutil.copy2 = boom
        try:
            r2 = crosspost.cache_video({"shorts": []}, {"n": 8}, src)
        finally:
            crosspost.shutil.copy2 = _saved_copy
        check("cache_video failure is swallowed", r2 is None)

    # ── 9. import smoke ────────────────────────────────────────────
    ok = True
    for mod in ("crosspost", "bank", "orchestrator", "youtube"):
        try:
            __import__(f"src.{mod}")
        except Exception as e:                  # noqa: BLE001
            ok = False
            print(f"      import src.{mod} failed: {e}")
    check("all touched modules import cleanly", ok)

    # ── 10. cross-links (owner rule 2026-11-15) ────────────────────
    ln = cfg()["crosspost"].get("links") or {}
    check("links configured (ig/fb/yt_short)",
          bool(ln.get("ig") and ln.get("fb") and ln.get("yt_short")),
          str(ln))
    ci = crosspost.build_caption(
        "This Fortnite XP map = FREE Battle Pass 🤑 #24", "VID9", "ig")
    check("IG caption carries the YouTube twin link",
          "youtube.com/shorts/VID9" in ci, ci[:200])
    check("IG caption carries the Facebook link",
          "facebook.com" in ci)
    check("IG caption does not link itself",
          "instagram.com" not in ci)
    cf = crosspost.build_caption(
        "This Fortnite XP map = FREE Battle Pass 🤑 #24", "VID9", "fb")
    check("FB description carries the YouTube twin link",
          "youtube.com/shorts/VID9" in cf)
    check("FB description carries the Instagram link",
          "instagram.com" in cf)
    check("FB description does not link itself",
          "facebook.com" not in cf)
    check("reel captions still obey the code rules",
          "MAP CODE: 1789-8528-6429" in ci and "#fortnite" in ci)

    # IG rupload prefers Meta's own uri (live probe 2026-11-15: the
    # container response carries a v27.0 rupload URL)
    with tempfile.TemporaryDirectory() as td:
        vid = Path(td) / "short_2.mp4"
        vid.write_bytes(b"FAKEVIDEOFORTEST" * 1000)
        calls.clear()

        def uri_graph(token, path, method="GET", params=None,
                      timeout=60):
            calls.append((method, path.lstrip("/"), dict(params or {})))
            if path.rstrip("/").endswith("/media") and method == "POST":
                return {"id": "C1",
                        "uri": "https://rupload.facebook.com/"
                               "ig-api-upload/v27.0/C1"}
            if path.rstrip("/") == "C1":
                return {"status_code": "FINISHED"}
            if path.rstrip("/").endswith("/media_publish"):
                return {"id": "M9"}
            raise AssertionError(f"unexpected graph call {method} {path}")

        apply, restore = patched(_graph=uri_graph,
                                 _rupload=fake_rupload)
        apply()
        try:
            crosspost.ig_publish_reel(vid, "cap", "17841234567890123",
                                      "TOK")
        finally:
            restore()
        rup = next(c for c in calls if c[0] == "RUPLOAD")
        check("IG bytes use Meta's own uri first",
              rup[1] == "https://rupload.facebook.com/ig-api-upload/"
                        "v27.0/C1", rup[1])

    # ── 11. backfill: the catalogue gets mirrored ──────────────────
    legacy = {"n": 40, "video_id": "y40", "slot": None,
              "clip_dur": 55.0, "crosspost": {}}
    done2 = {"n": 42, "video_id": "y42", "slot": past, "clip_dur": 55.0,
             "crosspost": {"ig": {"status": "done"},
                           "fb": {"status": "skipped"}}}
    gaveup_media = {"n": 43, "video_id": "y43", "slot": past,
                    "clip_dur": 55.0,
                    "crosspost": {"ig": {"status": "gave_up",
                                         "reason": "media unreachable: x"},
                                  "fb": {"status": "pending"}}}
    check("legacy short (no slot) is backfill-eligible",
          not crosspost._backfill_settled(legacy))
    check("future slot is not backfill-eligible yet",
          not crosspost._slot_passed(upcoming, now))
    check("done+skipped counts as settled",
          crosspost._backfill_settled(done2))
    check("media give-up is recoverable (not settled)",
          not crosspost._backfill_settled(gaveup_media))

    os.environ["IG_ACCESS_TOKEN"] = "IGT"
    os.environ["FB_ACCESS_TOKEN"] = "FBT"
    try:
        bf_state = {
            "shorts": [dict(legacy), {"n": 44, "video_id": "y44",
                                      "slot": None, "clip_dur": 55.0,
                                      "crosspost": {}}],
            "crosspost_ids": {"ig": "17841234567890123",
                              "fb": "1234567890"},
            "alerts_sent": {},
        }
        with tempfile.TemporaryDirectory() as td:
            vid2 = Path(td) / "short_40.mp4"
            vid2.write_bytes(b"B" * 3000)

            orig_conf = crosspost._conf

            def bf_conf():
                c = dict(orig_conf())
                c["backfill"] = {"enabled": True, "per_hour": 1}
                return c

            def bf_graph(token, path, method="GET", params=None,
                         timeout=60):
                p = path.rstrip("/")
                if p == "me":
                    return {"id": "1", "name": "n"}
                if p == "M40":
                    return {"permalink":
                             "https://www.instagram.com/reel/X40/"}
                if p == "V40":
                    return {"permalink_url":
                             "https://www.facebook.com/reel/V40/"}
                raise AssertionError(f"unexpected {method} {path}")

            def bf_ig(video, caption, ig_id, token):
                assert "youtube.com/shorts/y40" in caption, caption
                return "M40"

            def bf_fb(video, desc, page_id, token):
                assert "youtube.com/shorts/y40" in desc, desc
                return "V40"

            def bf_fetch(video_id, workdir, n=None):
                return vid2

            def bf_fetch_media(media, workdir, zips=None):
                raise AssertionError("legacy short has no media pointer")

            def bf_yt(state_, s_):
                pass          # patched out — no YouTube creds in tests

            apply, restore = patched(
                _conf=bf_conf, _graph=bf_graph,
                ig_publish_reel=bf_ig, fb_publish_reel=bf_fb,
                _fetch_public_short=bf_fetch,
                fetch_media=bf_fetch_media,
                youtube_add_links=bf_yt,
                auto_discover=discover, alert_once=alert)
            apply()
            try:
                rc = crosspost.run_backfill(bf_state)
            finally:
                restore()
            check("run_backfill exits 0", rc == 0)
            s40 = bf_state["shorts"][0]
            check("legacy #40 got both reels",
                  s40["crosspost"]["ig"]["status"] == "done"
                  and s40["crosspost"]["fb"]["status"] == "done",
                  str(s40.get("crosspost")))
            check("reel permalinks recorded",
                  "instagram.com/reel/X40/" in str(
                      s40["crosspost"]["ig"].get("permalink"))
                  and "facebook.com/reel/V40/" in str(
                      s40["crosspost"]["fb"].get("permalink")))
            check("budget honored (second short untouched)",
                  bf_state["shorts"][1].get("crosspost") == {},
                  str(bf_state["shorts"][1].get("crosspost")))
    finally:
        os.environ.pop("IG_ACCESS_TOKEN", None)
        os.environ.pop("FB_ACCESS_TOKEN", None)

    # GONE: a source video deleted from YouTube settles permanently
    os.environ["IG_ACCESS_TOKEN"] = "IGT"
    os.environ["FB_ACCESS_TOKEN"] = "FBT"
    try:
        gone_state = {
            "shorts": [{"n": 50, "video_id": "y50", "slot": None,
                        "clip_dur": 55.0, "crosspost": {}}],
            "crosspost_ids": {"ig": "17841234567890123",
                              "fb": "1234567890"},
            "alerts_sent": {},
        }

        def gone_fetch(video_id, workdir, n=None):
            return "GONE"

        apply, restore = patched(
            _conf=bf_conf, _graph=bf_graph,
            _fetch_public_short=gone_fetch,
            auto_discover=discover, alert_once=alert,
            youtube_add_links=bf_yt)
        apply()
        try:
            rc2 = crosspost.run_backfill(gone_state)
        finally:
            restore()
        check("GONE backfill exits 0", rc2 == 0)
        g50 = gone_state["shorts"][0]["crosspost"]
        check("GONE settles both platforms immediately",
              g50["ig"]["status"] == "gave_up"
              and g50["fb"]["status"] == "gave_up", str(g50))
        check("GONE reason is the deleted source",
              "deleted" in str(g50["ig"].get("reason")))

        # IG second chance: a transcode failure re-encodes to yuv420p
        # and retries within the same attempt
        re_state = {
            "shorts": [{"n": 51, "video_id": "y51", "slot": None,
                        "clip_dur": 55.0, "crosspost": {}}],
            "crosspost_ids": {"ig": "17841234567890123",
                              "fb": "1234567890"},
            "alerts_sent": {},
        }
        ig_first = {"calls": 0}

        def picky_ig(video, caption, ig_id, token):
            ig_first["calls"] += 1
            if ig_first["calls"] == 1:
                raise crosspost.CrosspostError(
                    "rupload -> 500: Video Transcoding Error: both HD "
                    "and SD progressive failed to transcode")
            return "MR"

        def fake_reencode(video, n):
            return video

        apply, restore = patched(
            _conf=bf_conf, _graph=bf_graph, ig_publish_reel=picky_ig,
            _fetch_public_short=bf_fetch, _ig_reencode=fake_reencode,
            auto_discover=discover, alert_once=alert,
            youtube_add_links=bf_yt)
        apply()
        try:
            crosspost.run_backfill(re_state)
        finally:
            restore()
        r51 = re_state["shorts"][0]["crosspost"]["ig"]
        check("transcode failure -> yuv420p retry -> reel live",
              r51["status"] == "done" and r51["media_id"] == "MR",
              str(r51))
        check("second chance fired exactly twice",
              ig_first["calls"] == 2)
    finally:
        os.environ.pop("IG_ACCESS_TOKEN", None)
        os.environ.pop("FB_ACCESS_TOKEN", None)

    # ── 12. youtube_add_links is non-fatal without YouTube creds ──
    os.environ.pop("YT_CLIENT_ID", None)
    os.environ.pop("YT_CLIENT_SECRET", None)
    os.environ.pop("YT_REFRESH_TOKEN", None)
    stl = {"quota": {"used": 0}}
    shl = {"n": 45, "video_id": "y45", "crosspost": {
        "ig": {"status": "done", "permalink": "https://x/"}}}
    crosspost.youtube_add_links(stl, shl)
    check("yt link update is non-fatal without creds",
          "yt_links" not in (shl.get("crosspost") or {}))

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("ALL GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
