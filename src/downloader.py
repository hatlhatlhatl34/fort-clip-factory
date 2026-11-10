"""Downloader — the 3-rung ingestion ladder.

YouTube blocks anonymous datacenter-IP downloads with a bot-check
("Sign in to confirm you're not a bot"). The ladder:

  RUNG 1  yt-dlp with COOKIES_TXT (a GitHub secret: your exported
          youtube.com cookies.txt). Logged-in session = no bot check.
          Player-client fallbacks are tried in order (web, android_vr,
          tv, web_embedded, ios) before giving up on a rung.
  RUNG 2  the ingest/ folder — drop real MP4s there manually anytime;
          the machine mines clips from them and clears the folder.
          (This is also how you bootstrap the channel day one.)
  RUNG 3  NEVER silent: a failed download prints a DOWNLOAD-ALERT
          marker the workflow turns into a GitHub Issue, so the
          machine is never quietly starving.

Formats: we need <=720p (we re-encode to 1080x1920 vertical anyway —
anything above 720 source is wasted bytes on a free runner).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from .config import cfg, path

FMT = ("bv*[height<=720][ext=mp4]+ba[ext=m4a]/"
       "b[height<=720][ext=mp4]/bv*[height<=720]+ba/b[height<=720]/b")
PLAYER_CLIENTS = ["web", "android_vr", "tv", "web_embedded", "ios"]


class DownloadError(RuntimeError):
    pass


def _cookies_file(work: Path) -> Path | None:
    """COOKIES_TXT secret -> work/cookies.txt (never printed, never
    committed; deleted after the run)."""
    raw = os.environ.get("COOKIES_TXT", "")
    if not raw.strip():
        return None
    cfile = work / "cookies.txt"
    cfile.write_text(raw if raw.endswith("\n") else raw + "\n")
    return cfile


def _run_ytdlp(url: str, out: Path, cookies: Path | None) -> bool:
    base = ["yt-dlp", "-f", FMT, "--merge-output-format", "mp4",
            "--no-playlist", "--no-part",
            "-o", str(out),
            "--socket-timeout", "20",
            "--retries", "4", "--fragment-retries", "4"]
    if cookies:
        base += ["--cookies", str(cookies)]

    attempts = [list(base)]           # rung 1a: default client (+cookies)
    for client in PLAYER_CLIENTS[1:]:
        a = list(base)
        a += ["--extractor-args", f"youtube:player_client={client}"]
        attempts.append(a)

    for i, args in enumerate(attempts):
        client = PLAYER_CLIENTS[min(i, len(PLAYER_CLIENTS) - 1)]
        try:
            r = subprocess.run(
                args + [url], capture_output=True, text=True,
                timeout=600)
        except subprocess.TimeoutExpired:
            print(f"[download] attempt {i+1} ({client}): timeout")
            continue
        if r.returncode == 0 and out.exists() and out.stat().st_size > 1_000_000:
            print(f"[download] OK via player_client={client} "
                  f"({out.stat().st_size/1e6:.1f} MB)")
            return True
        tail = (r.stderr or "").strip().splitlines()[-3:]
        print(f"[download] attempt {i+1} ({client}): "
              f"rc={r.returncode} {' | '.join(tail)[:300]}")
        if out.exists() and out.stat().st_size < 1_000_000:
            out.unlink(missing_ok=True)   # error-page stub
        time.sleep(3)
    return False


def download(source: dict, work: Path) -> Path:
    """Download the chosen source video. Returns the local MP4 path.

    Raises DownloadError with a human-actionable message when every
    rung fails — the orchestrator turns that into a GitHub Issue.
    """
    work.mkdir(parents=True, exist_ok=True)
    vid = source["id"]
    url = f"https://www.youtube.com/watch?v={vid}"
    out = work / f"source_{vid}.mp4"

    # rung 2 first if a manual drop is waiting (owner override)
    ingest_dir = path(cfg()["ingest"]["folder"])
    if ingest_dir.exists():
        drops = sorted(ingest_dir.glob("*.mp4"))
        if drops:
            src = drops[0]
            print(f"[download] using ingest/ drop: {src.name}")
            shutil.copy(src, out)
            src.unlink()        # consumed — the folder self-cleans
            return out

    # rung 1: yt-dlp (+ cookies when the secret exists)
    cookies = _cookies_file(work)
    if _run_ytdlp(url, out, cookies):
        return out

    # rung 3: loud, actionable failure
    if cookies is None:
        msg = ("DOWNLOAD-ALERT: yt-dlp was blocked by YouTube's bot-check "
               f"for source {vid} ({source.get('title','')[:60]}), and no "
               "COOKIES_TXT secret is set.\n"
               "FIX: export your youtube.com cookies.txt (a logged-in "
               "browser, 'Get cookies.txt LOCALLY' extension) and add it "
               "as the repo secret COOKIES_TXT — see RUNBOOK.md.\n"
               "INTERIM: drop any real Fortnite map gameplay MP4 into the "
               "ingest/ folder and the machine will mine clips from it.")
    else:
        msg = (f"DOWNLOAD-ALERT: yt-dlp failed for source {vid} "
               f"({source.get('title','')[:60]}) even with COOKIES_TXT. "
               "The cookies may be expired (re-export them) or the video "
               "is private/age-restricted. The next run picks a different "
               "source automatically.")
    raise DownloadError(msg)
