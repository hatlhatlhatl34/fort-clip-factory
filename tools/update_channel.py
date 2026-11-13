#!/usr/bin/env python3
"""Apply channel.yaml's branding block to the YouTube channel.

Channels the owner can't reach from a sandbox (the OAuth secrets live
in the repo, never on a laptop): run this inside GitHub Actions with
the YT_* secrets in the environment — the channel-branding.yml
workflow does exactly that, once, on demand.

  python tools/update_channel.py            # apply branding
  python tools/update_channel.py --show     # print current + planned

Uses channels.update (part=brandingSettings, ~50 quota units). Only
description and keywords are touched — title, avatar and banners stay
exactly as they are (avatar/banner need YouTube Studio, not the API).

The refresh token's OAuth scope must allow channel branding writes
(youtube / youtube.force-ssl). If it doesn't, the run fails loudly
with the API's 403 message and nothing changes.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import youtube                     # noqa: E402
from src.config import cfg                  # noqa: E402

API = "https://www.googleapis.com/youtube/v3"


def _get(token: str, url: str) -> dict:
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def current_branding(token: str) -> dict:
    data = _get(token,
                f"{API}/channels?part=brandingSettings,snippet&mine=true")
    items = data.get("items", [])
    if not items:
        raise RuntimeError("token works but no channel found (mine=true)")
    it = items[0]
    return {"id": it["id"],
            "title": it["snippet"].get("title", ""),
            "branding": it.get("brandingSettings", {})}


def update_branding(token: str, channel_id: str,
                    description: str, keywords: str) -> None:
    body = json.dumps({
        "id": channel_id,
        "brandingSettings": {
            "description": description,
            "keywords": keywords,
        },
    }).encode()
    req = urllib.request.Request(
        f"{API}/channels?part=brandingSettings", data=body, method="PUT",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def main() -> int:
    show_only = "--show" in sys.argv
    brand = (cfg().get("branding") or {})
    desc = (brand.get("description") or "").strip()
    kws = " ".join(brand.get("keywords") or [])
    if not desc:
        print("[branding] channel.yaml has no branding.description — "
              "nothing to apply")
        return 1

    token = youtube.get_access_token()
    cur = current_branding(token)
    print(f"[branding] channel: {cur['title']!r} ({cur['id']})")
    print("[branding] current description "
          f"({len(cur['branding'].get('description', ''))} chars):")
    print("\n".join("    " + ln for ln in
                    (cur["branding"].get("description", "")
                     or "(empty)").splitlines()[:8]))
    print("[branding] planned description:")
    print("\n".join("    " + ln for ln in desc.splitlines()))
    print(f"[branding] planned keywords: {kws[:120]}...")

    if show_only:
        print("[branding] --show: not applying")
        return 0

    try:
        update_branding(token, cur["id"], desc, kws)
    except urllib.error.HTTPError as e:
        print(f"BRANDING-ALERT: channels.update failed "
              f"({e.code}): {e.read().decode()[:400]}")
        return 1
    print("[branding] DONE — description + keywords applied "
          "(avatar/banner untouched; those need YouTube Studio)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
