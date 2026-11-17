"""Architecture tests for the TikTok mirror (owner rule 2026-11-18):

 1. the SAME Short ships to YouTube + IG Reel + FB Reel + TikTok video
 2. TikTok title = build_caption(for_platform="tiktok"): YouTube twin
    + IG + FB links, own link skipped, codes carried
 3. unaudited reality: privacy SELF_ONLY by default and taken from
    channel.yaml (PUBLIC_TO_EVERYONE only after the audit)
 4. flow shape: FILE_UPLOAD init (video_size+chunk_size) -> PUT
    upload_url with Content-Range -> status/fetch PUBLISH_COMPLETE
 5. failure isolation: a TikTok failure NEVER touches YouTube or the
    IG/FB reels (recorded in crosspost.tiktok, retried hourly)
 6. poll timeout is NOT a definite failure: publish_id is stored and
    re-checked before any re-post (no duplicates by construction)
 7. retry budget: gave_up + one alert after max_attempts (same
    semantics as the reels)
 8. unarmed is cleanly OFF (no token secret -> armed() False, the
    reels keep flowing)

Run:  python3 tests/test_tiktok.py
(no pytest dependency; plain asserts + prints)
"""
from __future__ import annotations

import os
import sys
import types
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.pop("TIKTOK_ACCESS_TOKEN", None)

from src import tiktok                       # noqa: E402
from src.crosspost import build_caption      # noqa: E402

PASS = []


def check(label: str, ok: bool) -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    PASS.append(ok)
    assert ok, label


# 8. unarmed is cleanly OFF
check("unarmed without TIKTOK_ACCESS_TOKEN", tiktok.armed() is False)
os.environ["TIKTOK_ACCESS_TOKEN"] = "act.fake"
check("armed once the token secret exists", tiktok.armed() is True)
os.environ.pop("TIKTOK_ACCESS_TOKEN", None)

# 2/3. the TikTok title: same content, cross-links, own link skipped
cap = build_caption("RED vs BLUE Breakout 7992-0068-2838 #12",
                    "abc123xyz", "tiktok")
check("tiktok title carries the YouTube twin",
      "youtube.com/shorts/abc123xyz" in cap)
check("tiktok title carries the IG profile",
      "instagram.com/h15master" in cap)
check("tiktok title carries the FB page",
      "facebook.com/131825308398327" in cap)
check("tiktok title skips its OWN profile link",
      "@h15master" not in cap and "TIKTOK:" not in cap)
check("creator code carried", "H15" in cap)
check("map code carried", "7992-0068-2838" in cap)
check("#{n} bookkeeping dropped", "#12" not in cap.splitlines()[0])
check("<= TikTok 2200 limit", len(cap) <= tiktok.TITLE_MAX)

# a reel caption DOES carry the TikTok link (cross-link rule)
cap_ig = build_caption("RED vs BLUE Breakout #1", "abc", "ig")
check("IG captions gain the TIKTOK profile link", "TIKTOK:" in cap_ig)

# 3. privacy from config; SELF_ONLY default
check("default privacy is SELF_ONLY (unaudited)",
      tiktok._conf().get("privacy", "SELF_ONLY") == "SELF_ONLY")

# 4. flow shape — fake the API + upload endpoints, watch the calls
calls = {"init": None, "put": [], "status": []}


def fake_api(method, path, *, token, payload=None, params=None):
    if path == "/v2/post/publish/video/init/":
        calls["init"] = payload
        assert payload["source_info"]["source"] == "FILE_UPLOAD"
        assert payload["source_info"]["video_size"] == SZ
        assert payload["post_info"]["privacy_level"] == "SELF_ONLY"
        assert payload["post_info"]["title"].endswith("title")
        return {"publish_id": "v_pub_file~v2-1.TEST",
                "upload_url": "https://open-upload.test/video?tok=1"}
    if path == "/v2/post/publish/status/fetch/":
        calls["status"].append(payload)
        return {"status": "PUBLISH_COMPLETE"}
    raise AssertionError(f"unexpected call {method} {path}")


class FakeResp:
    def __init__(self, body=b""):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


SZ = 5 * 1024 * 1024       # 5 MB -> one chunk


def fake_urlopen(req, timeout=None):
    if req.get_method() == "PUT":
        calls["put"].append((
            req.get_full_url(),
            req.headers.get("Content-range"),
            req.headers.get("Content-type"),
            len(req.data),
        ))
        assert req.headers.get("Content-type") == "video/mp4"
        return FakeResp()
    return FakeResp(b"{}")


tmp = Path("/tmp/factory_tiktok_test.mp4")
tmp.write_bytes(b"\x00" * SZ)

tiktok._api = fake_api
urllib.request.urlopen = fake_urlopen
tiktok.STATUS_POLL_S = 0                    # no sleeping in tests
pid = tiktok.publish(tmp, "title", "act.fake")
os.unlink(tmp)

check("publish_id returned on PUBLISH_COMPLETE",
      pid == "v_pub_file~v2-1.TEST")
check("single PUT for a <=32 MB file", len(calls["put"]) == 1)
url, rng, ctype, nbytes = calls["put"][0]
check("PUT went to the init's upload_url", url.endswith("tok=1"))
check(f"Content-Range covers the whole file ({rng})", rng == f"bytes 0-{SZ - 1}/{SZ}")
check("PUT shipped exactly video_size bytes", nbytes == SZ)
check("init declares exactly ONE chunk (live matrix 2026-11-18)",
      calls["init"]["source_info"]["total_chunk_count"] == 1
      and calls["init"]["source_info"]["chunk_size"] == SZ)

# 6. poll timeout keeps the publish_id for the re-check path
class _Never:
    pass


def timeout_api(method, path, *, token, payload=None, params=None):
    if path == "/v2/post/publish/video/init/":
        calls["init"] = payload
        return {"publish_id": "v_pub_file~v2-2.TIMEOUT",
                "upload_url": "https://open-upload.test/video?tok=2"}
    if path == "/v2/post/publish/status/fetch/":
        return {"status": "PROCESSING"}
    raise AssertionError(path)


tiktok._api = timeout_api
tiktok.STATUS_TIMEOUT_S = 0.2
tmp.write_bytes(b"\x00" * 1024)      # file again for the 2nd run
try:
    tiktok.publish(tmp, "title", "act.fake")
    timed_out = False
except tiktok._PollTimeout as e:
    timed_out = True
    check("timeout carries the publish_id for adoption",
          e.publish_id == "v_pub_file~v2-2.TIMEOUT")
check("a poll timeout raises (not a silent success)", timed_out)

# 5/7. state machine parity — settled/settled transitions
check("settled() recognizes done",
      tiktok.settled({"crosspost": {"tiktok": {"status": "done"}}}))
check("settled() rejects failed (retry continues)",
      not tiktok.settled({"crosspost": {"tiktok": {"status": "failed"}}}))

# attempt() with a _PollTimeout records timeout+publish_id (adoption
# input) rather than a plain failure
fake_state = {"shorts": [{"n": 99}]}
short = fake_state["shorts"][0]
fake_ledger = types.SimpleNamespace(save=lambda st: None)
sys.modules["src.ledger"] = fake_ledger
import src.crosspost as cp_mod                     # noqa: E402
saved_alerts = []
cp_mod.alert_once = lambda *a, **k: saved_alerts.append(a[2])

captured = {}


def attempt_publish(video, title, tok):
    raise tiktok._PollTimeout("v_pub_file~v2-3.KEEP", "PROCESSING")


tiktok.publish = attempt_publish
tiktok.token = lambda: "act.fake"
ok = tiktok.attempt(fake_state, short, tmp, "t", max_attempts=8)
st = short["crosspost"]["tiktok"]
check("timeout attempt returns False (not live yet)", ok is False)
check("state kept publish_id + timeout flag",
      st.get("publish_id") == "v_pub_file~v2-3.KEEP"
      and st.get("timeout") is True)
check("attempt counter advanced", st.get("attempts") == 1)

# the NEXT attempt re-checks that publish_id BEFORE posting anything
rechecked = {}
tmp.write_bytes(b"\x00" * 1024)      # present for the adoption call


def recheck_api(method, path, *, token, payload=None, params=None):
    if path == "/v2/post/publish/status/fetch/":
        rechecked["prior"] = payload.get("publish_id")
        return {"status": "PUBLISH_COMPLETE"}
    raise AssertionError(path)


tiktok._api = recheck_api
ok = tiktok.attempt(fake_state, short, tmp, "t", max_attempts=8)
check("re-check adopts the completed post", ok is True)
check("adoption queried the STORED publish_id",
      rechecked.get("prior") == "v_pub_file~v2-3.KEEP")
check("adopted into done", short["crosspost"]["tiktok"]["status"] == "done")

# 7. gave_up at the budget, one alert
short2_state = {"shorts": [{"n": 100}]}
s2 = short2_state["shorts"][0]
s2["crosspost"] = {"tiktok": {"status": "failed", "attempts": 8,
                              "error": "boom"}}
tiktok._api = lambda *a, **k: (_ for _ in ()).throw(AssertionError(
    "must not post past the budget"))
ok = tiktok.attempt(short2_state, s2, tmp, "t", max_attempts=8)
check("budget exhausted -> gave_up, no further posting",
      ok is False and s2["crosspost"]["tiktok"]["status"] == "gave_up")
check("exactly one alert fired", len(saved_alerts) == 1
      and "gave up" in saved_alerts[0])

# back-compat: marks for other platforms untouched
check("IG/FB marks are not touched by the tiktok module",
      "ig" not in s2["crosspost"] and "fb" not in s2["crosspost"])

print(f"\n{len(PASS)}/{len(PASS)} checks passed")
sys.exit(0 if all(PASS) else 1)
