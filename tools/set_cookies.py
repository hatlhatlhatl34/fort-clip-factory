#!/usr/bin/env python3
"""Set the COOKIES_TXT repo secret from exported cookies.txt file(s).

MULTI-JAR aware (owner pool, 2026-11-13): the input may contain ONE
account's export or MANY, separated by '===== JAR N =====' marker
lines — exactly the format src/downloader.py's _JAR_SEP parses at run
time (and the format produced by scripts that merge several
accounts). Every jar is validated SEPARATELY:
  * Netscape format — 7 tab-separated fields per cookie line;
  * at least one youtube.com-domain cookie;
  * repairs a missing '# Netscape HTTP Cookie File' header (a common
    export quirk) on each jar.
Never prints cookie CONTENT — only counts and sizes.

Why not just the GitHub web UI paste? That works too (Settings →
Secrets and variables → Actions → COOKIES_TXT), but this adds
validation up front and makes ROTATION a one-liner: YouTube cookies
quietly expire after some months, and when a run log says "the
cookies may be expired", you re-export and re-run this command.

Usage:
    GH_TOKEN=<github token> python tools/set_cookies.py cookies.txt \
        [-R owner/repo] [-S COOKIES_TXT] [--dry-run]

Merging several single-account exports into one file? Insert a line
exactly like '===== JAR 2 =====' between them (or send them one by
one — the LAST run wins, so merge first).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.request

DEFAULT_REPO = "hatlhatlhatl34/fort-clip-factory"
API = "https://api.github.com"

JAR_SEP = re.compile(r"^=+\s*jar\s*#?\s*\d+\s*=*$", re.IGNORECASE)


def _split_jars(raw: str) -> list[str]:
    blocks, cur = [], []
    for line in raw.splitlines():
        if JAR_SEP.match(line.strip()):
            blocks.append("\n".join(cur))
            cur = []
        else:
            cur.append(line)
    blocks.append("\n".join(cur))
    return [b for b in blocks if b.strip()]


def _validate_jar(block: str, idx: int) -> tuple[bool, str]:
    """Same rules the machine itself enforces at run time."""
    lines = block.splitlines()
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
    return True, (f"{len(body)} cookie lines, {len(yt)} for "
                  f"youtube.com{note}")


def validate(raw: str) -> tuple[bool, str]:
    """Validate one jar or a whole multi-account pool."""
    jars = _split_jars(raw)
    if not jars:
        return False, "no cookie lines at all — wrong export"
    details = []
    for i, block in enumerate(jars, 1):
        ok, msg = _validate_jar(block, i)
        label = f"jar {i}" if len(jars) > 1 else "the paste"
        if not ok:
            return False, f"{label}: {msg}"
        details.append(f"{label}: {msg}")
    head = (f"valid: {len(jars)} jar(s) — "
            if len(jars) > 1 else "valid: ")
    return True, head + "; ".join(details)


def _normalize_blocks(raw: str) -> list[str]:
    """Per-jar header repair; returns the jar blocks (separators
    stripped) so the caller can bucket them across secrets."""
    blocks = _split_jars(raw)
    fixed = []
    for block in blocks:
        lines = block.splitlines()
        if not any(l.strip().startswith("# Netscape HTTP Cookie File")
                   for l in lines[:5]):
            block = "# Netscape HTTP Cookie File\n" + "\n".join(lines)
        fixed.append(block.strip() + "\n")
    return fixed


BUCKET_LIMIT = 40_000          # bytes per secret; GitHub caps at 48 KB
SECRET_NAMES = ("COOKIES_TXT", "COOKIES_TXT2", "COOKIES_TXT3")


def _bucket(blocks: list[str]) -> list[list[str]]:
    """Pack jar blocks into buckets of <= BUCKET_LIMIT bytes."""
    buckets, cur, size = [], [], 0
    for b in blocks:
        if cur and size + len(b) > BUCKET_LIMIT:
            buckets.append(cur)
            cur, size = [], 0
        cur.append(b)
        size += len(b)
    if cur:
        buckets.append(cur)
    return buckets


def _bucket_text(bucket: list[str], start_idx: int) -> str:
    """Blocks -> one secret value, machine-parseable '===== JAR N ====='
    separators inside (numbering continues across buckets)."""
    parts = [bucket[0]]
    for k, b in enumerate(bucket[1:], start_idx + 2):
        parts.append(f"===== JAR {k} =====")
        parts.append(b)
    return "\n".join(parts)


def _gh(method: str, url: str, token: str,
        data: dict | None = None, expect: tuple[int, ...] = ()) -> dict:
    req = urllib.request.Request(url, method=method, headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    }, data=json.dumps(data).encode() if data is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = r.read()
        return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as e:
        if e.code in expect:        # e.g. 404 on DELETE of a missing secret
            return {}
        raise


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cookies_file",
                    help="the exported cookies.txt (Netscape format; "
                         "may contain several jars)")
    ap.add_argument("-R", "--repo", default=DEFAULT_REPO,
                    help=f"owner/repo (default {DEFAULT_REPO})")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate only; do not touch the secret")
    args = ap.parse_args()

    raw = open(args.cookies_file, encoding="utf-8").read()
    ok, msg = validate(raw)
    print(f"[validate] {msg}")
    if not ok:
        return 1

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

    blocks = _normalize_blocks(raw)
    buckets = _bucket(blocks)
    if len(buckets) > len(SECRET_NAMES):
        print(f"[error] pool too large: {len(buckets)} buckets needed, "
              f"only {len(SECRET_NAMES)} secret names available — "
              "split the accounts across runs")
        return 1

    key = _gh("GET",
              f"{API}/repos/{args.repo}/actions/secrets/public-key",
              token)
    sealed = public.SealedBox(
        public.PublicKey(base64.b64decode(key["key"])))

    idx = 0
    for name, bucket in zip(SECRET_NAMES, buckets):
        text = _bucket_text(bucket, idx)
        idx += len(bucket)
        enc = base64.b64encode(sealed.encrypt(text.encode())).decode()
        _gh("PUT", f"{API}/repos/{args.repo}/actions/secrets/{name}",
            token, {"encrypted_value": enc, "key_id": key["key_id"]})
        print(f"[set] {name}: {len(bucket)} jar(s), "
              f"{len(text)/1024:.1f} KB")

    # retire stale extra secrets (e.g. shrinking 5 accounts -> 1)
    for name in SECRET_NAMES[len(buckets):]:
        _gh("DELETE", f"{API}/repos/{args.repo}/actions/secrets/{name}",
            token, expect=(404,))
        print(f"[retired] {name} (no longer needed)")

    total = len(blocks)
    print(f"[done] {total} jar(s) live across "
          f"{len(buckets)} secret(s) on {args.repo} — the next "
          "scheduled run picks them up automatically")
    print("[next] trigger a confirmation run now:")
    print(f"       curl -X POST -H 'Authorization: token $GH_TOKEN' "
          f"-H 'Accept: application/vnd.github+json' "
          f"{API}/repos/{args.repo}/actions/workflows/"
          "daily-factory.yml/dispatches "
          "-d '{\"ref\":\"main\"}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
