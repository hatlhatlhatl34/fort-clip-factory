#!/usr/bin/env python3
"""Set the COOKIES_TXT repo secret from an exported cookies.txt file.

Validates BEFORE touching the secret:
  * Netscape format — 7 tab-separated fields per cookie line;
  * at least one youtube.com-domain cookie;
  * repairs a missing '# Netscape HTTP Cookie File' header (a common
    export quirk).
Never prints cookie CONTENT — only counts and sizes.

Why not just the GitHub web UI paste? That works too (Settings →
Secrets and variables → Actions → COOKIES_TXT), but this adds
validation up front and makes ROTATION a one-liner: YouTube cookies
quietly expire after some months, and when a run log says "the
cookies may be expired", you re-export and re-run this command.

Usage:
    GH_TOKEN=<github token> python tools/set_cookies.py cookies.txt \
        [-R owner/repo] [-S COOKIES_TXT] [--dry-run]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.request

DEFAULT_REPO = "hatlhatlhatl34/fort-clip-factory"
API = "https://api.github.com"


def validate(raw: str) -> tuple[bool, str]:
    """Same rules the machine itself enforces at run time."""
    lines = raw.splitlines()
    header = any(l.strip().startswith("# Netscape HTTP Cookie File")
                 for l in lines[:5])
    body = [l for l in lines
            if l.strip() and not l.lstrip().startswith("#")]
    if not body:
        return False, "no cookie lines at all — wrong export"
    malformed = [l for l in body[:80] if len(l.split("\t")) < 7]
    if malformed:
        return False, (f"{len(malformed)}/{min(len(body), 80)} lines "
                       "are not 7-field Netscape rows — wrong export "
                       "format (JSON?). Re-export with 'Get cookies.txt "
                       "LOCALLY' in Netscape format.")
    yt = [l for l in body if "youtube.com" in l.split("\t")[0]]
    if not yt:
        return False, ("no youtube.com-domain cookies found — export "
                       "while logged into youtube.com")
    note = "" if header else " (header was missing — will be repaired)"
    return True, (f"valid: {len(body)} cookie lines, {len(yt)} for "
                  f"youtube.com{note}")


def _gh(method: str, url: str, token: str, data: dict | None = None) -> dict:
    req = urllib.request.Request(url, method=method, headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    }, data=json.dumps(data).encode() if data is not None else None)
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = r.read()
    return json.loads(payload) if payload else {}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cookies_file",
                    help="the exported cookies.txt (Netscape format)")
    ap.add_argument("-R", "--repo", default=DEFAULT_REPO,
                    help=f"owner/repo (default {DEFAULT_REPO})")
    ap.add_argument("-S", "--secret", default="COOKIES_TXT",
                    help="secret name (default COOKIES_TXT)")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate only; do not touch the secret")
    args = ap.parse_args()

    raw = open(args.cookies_file, encoding="utf-8").read()
    ok, msg = validate(raw)
    print(f"[validate] {msg}")
    if not ok:
        return 1

    text = raw if raw.endswith("\n") else raw + "\n"
    if not any(l.strip().startswith("# Netscape HTTP Cookie File")
               for l in text.splitlines()[:5]):
        text = "# Netscape HTTP Cookie File\n" + text
    if args.dry_run:
        print("[dry-run] cookies are valid — secret would be set now")
        return 0

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("[error] GH_TOKEN (or GITHUB_TOKEN) env var required — "
              "a classic PAT with repo scope.")
        return 2

    try:
        from nacl import public  # noqa: F401
    except ImportError:
        print("[deps] installing pynacl (GitHub secret encryption)...")
        subprocess.run([sys.executable, "-m", "pip", "install",
                        "--quiet", "pynacl"], check=True)
        from nacl import public

    key = _gh("GET",
              f"{API}/repos/{args.repo}/actions/secrets/public-key",
              token)
    sealed = public.SealedBox(
        public.PublicKey(base64.b64decode(key["key"])))
    enc = base64.b64encode(sealed.encrypt(text.encode())).decode()
    _gh("PUT",
        f"{API}/repos/{args.repo}/actions/secrets/{args.secret}",
        token, {"encrypted_value": enc, "key_id": key["key_id"]})
    print(f"[set] secret {args.secret} updated on {args.repo} "
          f"({len(text)/1024:.1f} KB) — the next scheduled run picks "
          "it up automatically")
    print("[next] trigger a confirmation run now:")
    print(f"       curl -X POST -H 'Authorization: token $GH_TOKEN' "
          f"-H 'Accept: application/vnd.github+json' "
          f"{API}/repos/{args.repo}/actions/workflows/"
          "daily-factory.yml/dispatches "
          "-d '{\"ref\":\"main\"}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
