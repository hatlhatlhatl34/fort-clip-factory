#!/usr/bin/env python3
"""Daily TikTok token refresh — the 24 h access token swap.

WHY: TikTok access tokens live 24 h; refresh tokens 365 days and
ROLL (a refresh response may carry a new refresh_token — the classic
"connection dies after a few days" bug is code that keeps the old
one). This job runs daily (03:00 UTC, comfortably before the 08:30
factory session), refreshes, VERIFIES the new access token against
/v2/user/info/, then swaps both tokens into the GitHub secrets via
tools/set_secret.py so every later runner snapshot reads fresh creds.

Secrets in (workflow env):
    TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET / TIKTOK_REFRESH_TOKEN
    PAT_SET_SECRETS   — a classic PAT with repo scope, used ONLY to
                        write secrets (the runner GITHUB_TOKEN cannot).

Secrets out:
    TIKTOK_ACCESS_TOKEN (+ TIKTOK_REFRESH_TOKEN when rotated)

NEVER run this concurrently with itself (the workflow pins a
concurrency group). It is safe to run DURING a crosspost job: the
running job keeps its env snapshot; the next run reads the new one.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

TT = "https://open.tiktokapis.com"


def _post_form(path: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        TT + path, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode() or "{}")
        except Exception:                       # noqa: BLE001
            d = {}
        sys.exit(f"refresh failed: HTTP {e.code} "
                 f"{d.get('error')}: {d.get('error_description')}")


def _verify(access_token: str) -> dict:
    req = urllib.request.Request(
        TT + "/v2/user/info/?fields=open_id,display_name",
        headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read() or "{}")
        return ((d.get("data") or {}).get("user") or {})
    except urllib.error.HTTPError as e:
        sys.exit(f"new access token failed verification: HTTP {e.code}")


def _set_secret(name: str, value: str) -> None:
    env = dict(os.environ)
    env["GH_TOKEN"] = env["PAT_SET_SECRETS"]
    r = subprocess.run(
        [sys.executable, "tools/set_secret.py", name, value],
        capture_output=True, text=True, env=env)
    print(r.stdout.strip() or r.stderr.strip())
    if r.returncode != 0:
        sys.exit(f"secret update failed for {name}")


def main() -> int:
    key = os.environ.get("TIKTOK_CLIENT_KEY", "").strip()
    secret = os.environ.get("TIKTOK_CLIENT_SECRET", "").strip()
    refresh = os.environ.get("TIKTOK_REFRESH_TOKEN", "").strip()
    pat = os.environ.get("PAT_SET_SECRETS", "").strip()
    missing = [n for n, v in (("TIKTOK_CLIENT_KEY", key),
                              ("TIKTOK_CLIENT_SECRET", secret),
                              ("TIKTOK_REFRESH_TOKEN", refresh),
                              ("PAT_SET_SECRETS", pat)) if not v]
    if missing:
        print(f"[tiktok-refresh] not armed yet ({', '.join(missing)}) "
              "— nothing to do")
        return 0

    print("[tiktok-refresh] refreshing access token ...")
    d = _post_form("/v2/oauth/token/", {
        "client_key": key,
        "client_secret": secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh,
    })
    access = str(d.get("access_token") or "")
    new_refresh = str(d.get("refresh_token") or "")
    if not access or not new_refresh:
        sys.exit(f"refresh response missing tokens: {sorted(d)}")

    # D2 (audit 2026-11-18): TikTok rotates the refresh token on EVERY
    # successful call — the OLD root is dead the moment this POST
    # returned. Persist the NEW ROOT FIRST (with retries); only then
    # verify and swap the access token. A crash between the two
    # writes now leaves a valid root + an old-but-still-valid-enough
    # access token (tomorrow's refresh re-derives it) instead of a
    # DEAD root and an unfixable mirror.
    def _persist(name: str, value: str, tries: int = 3) -> None:
        for i in range(1, tries + 1):
            try:
                _set_secret(name, value)
                return
            except SystemExit as e:
                if i == tries:
                    raise
                print(f"[tiktok-refresh] secret {name} write {i} failed "
                      f"({e}) — retrying")

    if new_refresh != refresh:
        _persist("TIKTOK_REFRESH_TOKEN", new_refresh)

    user = _verify(access)
    print(f"[tiktok-refresh] verified as "
          f"@{user.get('display_name', '?')} "
          f"(open_id {str(user.get('open_id', '?'))[:8]}...)")

    _persist("TIKTOK_ACCESS_TOKEN", access)
    if new_refresh != refresh:
        note = "rotated — the new root was persisted FIRST"
    else:
        note = "unchanged (365-day rolling window)"
    print(f"[tiktok-refresh] DONE: access token swapped in; "
          f"refresh token {note}; access expires in "
          f"{d.get('expires_in')} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
