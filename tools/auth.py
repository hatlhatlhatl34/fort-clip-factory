#!/usr/bin/env python3
"""One-time OAuth helper — get your YT_REFRESH_TOKEN.

Run locally (NOT in Actions):

  1. Google Cloud console -> create project
  2. Enable "YouTube Data API v3"
  3. OAuth consent screen: External, add YOUR Google account as a
     test user
  4. Credentials -> Create OAuth client -> Web application
     - Authorized redirect URI:
       http://localhost:8080  (this helper runs a local catcher)
  5. Download the client secret JSON, then:

     python tools/auth.py --client-json client_secret_XXXX.json

  6. Authorize the account that OWNS the target YouTube channel.
  7. Copy the printed refresh token into the GitHub secret
     YT_REFRESH_TOKEN (client id + secret go to YT_CLIENT_ID /
     YT_CLIENT_SECRET).

Scopes requested: youtube.upload (publish), youtube (read channel).
"""
from __future__ import annotations

import argparse
import http.server
import json
import threading
import urllib.parse
import urllib.request

SCOPES = ("https://www.googleapis.com/auth/youtube.upload "
          "https://www.googleapis.com/auth/youtube")
PORT = 8080
TOKEN_URL = "https://oauth2.googleapis.com/token"


class _Catcher(http.server.BaseHTTPRequestHandler):
    code: str | None = None

    def do_GET(self):  # noqa: N802
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Catcher.code = (qs.get("code") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h2>Got it - return to the terminal.</h2>")

    def log_message(self, *a):  # silence
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-json", required=True,
                    help="the downloaded client_secret_*.json")
    args = ap.parse_args()

    doc = json.load(open(args.client_json))
    inst = doc.get("installed") or doc.get("web")
    if not inst:
        print("That does not look like an OAuth client JSON.")
        return 1
    client_id, client_secret = inst["client_id"], inst["client_secret"]
    redirect = f"http://localhost:{PORT}"

    auth_url = ("https://accounts.google.com/o/oauth2/v2/auth?"
                + urllib.parse.urlencode({
                    "client_id": client_id,
                    "redirect_uri": redirect,
                    "response_type": "code",
                    "scope": SCOPES,
                    "access_type": "offline",
                    "prompt": "consent",
                }))
    print("\nOpen this URL in your browser and authorize the channel "
          "owner account:\n\n" + auth_url + "\n")

    server = http.server.HTTPServer(("localhost", PORT), _Catcher)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Waiting on http://localhost:{PORT} ...")
    while _Catcher.code is None:
        pass
    server.shutdown()

    body = urllib.parse.urlencode({
        "code": _Catcher.code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        tok = json.loads(resp.read())

    print("\n================ REFRESH TOKEN ================")
    print(tok.get("refresh_token", "(none returned — re-run with "
          "prompt=consent)"))
    print("================================================")
    print("\nGitHub secrets to set:")
    print(f"  YT_CLIENT_ID     = {client_id[:20]}...")
    print(f"  YT_CLIENT_SECRET = (the client secret from the JSON)")
    print(f"  YT_REFRESH_TOKEN = (the token above)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
