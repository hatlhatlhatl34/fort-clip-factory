#!/usr/bin/env python3
"""Set a GitHub Actions secret on this repo (client-side sealed box).

WHY: the cross-poster (owner rule 2026-11-15) needs
IG_ACCESS_TOKEN / FB_ACCESS_TOKEN as repo secrets, and the owner
will eventually rotate the Facebook token themselves after creating
the Page (RUNBOOK.md section 8). This tool does it without the gh
CLI or the web UI:

    pip install pynacl
    python tools/set_secret.py IG_ACCESS_TOKEN "$(cat token.txt)"

Secrets are encrypted with the repo's public key (libsodium sealed
box) BEFORE they ever touch the network — exactly like the web UI
does it. Needs a classic PAT with repo scope in the origin remote
(or the GH_TOKEN environment variable).
"""
from __future__ import annotations

import json
import os
import re
import sys
import subprocess
import urllib.request

API = "https://api.github.com"


def _token() -> str:
    tok = os.environ.get("GH_TOKEN", "")
    if tok:
        return tok
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True, text=True).stdout.strip()
    m = re.search(r"(gh[pousr]_[A-Za-z0-9]{20,})", url)
    if not m:
        sys.exit("no GH_TOKEN env and no token in the origin remote")
    return m.group(1)


def _repo() -> str:
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True, text=True).stdout.strip()
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    if not m:
        sys.exit("cannot parse owner/repo from the origin remote")
    return m.group(1)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    name, value = sys.argv[1], sys.argv[2]
    repo, tok = _repo(), _token()

    def call(path: str, data: bytes | None = None, method: str = "GET"):
        req = urllib.request.Request(
            f"{API}/repos/{repo}/{path}",
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {tok}",
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "fort-clip-factory"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}

    key = call("actions/secrets/public-key")
    from nacl import encoding, public
    pk = public.PublicKey(key["key"], encoding.Base64Encoder())
    sealed = public.SealedBox(pk).encrypt(value.encode())

    call(f"actions/secrets/{name}",
         data=json.dumps({
             "encrypted_value":
                 encoding.Base64Encoder.encode(sealed).decode(),
             "key_id": key["key_id"],
         }).encode(), method="PUT")
    print(f"secret {name} set on {repo} "
          f"(encrypted with key {key['key_id'][:12]}...)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
