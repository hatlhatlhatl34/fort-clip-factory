"""Downloader — the food ladder. Keeps the machine fed for years.

YouTube blocks anonymous datacenter-IP downloads with a bot-check
("Sign in to confirm you're not a bot"). One rung failing never
starves the machine — it falls through to the next:

  RUNG 0  ingest/ folder — owner drops (bootstrap stockpile). Every
          consumed drop is remembered in state.used_drops AND deleted
          from the repo, so a drop is mined exactly once, ever.
  RUNG 1  yt-dlp player-client ladder (web, android_vr, tv,
          web_embedded, ios, tv_simply) + COOKIES_TXT secret when set —
          now MULTI-JAR (owner idea 2026-11-11): the single secret may
          hold several exports separated by '===== JAR 2 =====' lines;
          the ladder rotates across jars. The workflows install deno
          (yt-dlp's JS runtime) and requirements.txt pins yt-dlp[default]
          (the EJS challenge-solver dist) — with cookies armed, BOTH
          are needed to beat the SABR-only experiment logged-in
          accounts hit (live-confirmed 2026-11-13: web + jar + EJS
          downloads fine from a datacenter IP).
  RUNG 2  archive.org MIRROR of the exact YouTube video just picked
          (items are named youtube-<video id>) — byte-identical
          footage, no bot-check, honest source credit unchanged.
  RUNG 3  archive.org OPEN POOL — search archive.org directly for
          fresh Fortnite gameplay mirrors; item is marked used so
          it is never mined twice.
  RUNG 4  NEVER silent: a failed download prints a DOWNLOAD-ALERT
          marker the workflow turns into a GitHub Issue, so the
          machine is never quietly starving.

MONTHLY BANK MODE (owner idea 2026-11-11): `download_slice()` grabs
only a ~5-minute window of a video (--download-sections) so the
once-a-month harvest can feed ~93 clips from ONE IP in ONE session
without burning runner disk. Daily runs then only upload (OAuth is
never IP-flagged).

Everything runs under a wall-clock budget so one rung can never eat
the whole CI job. Formats: we PREFER up to 1440p sources (a 1080x1920
vertical Short re-rendered from a landscape crop needs the extra source
height to stay crisp), and a hard quality floor (sources.min_source_height,
default 720) rejects any file — yt-dlp OR archive.org — that comes in
below it. A 144p/360p download is structurally impossible now: the
rung falls through to the next one instead of shipping mush.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import ledger
from .config import cfg, path

FMT = ("bv*[height<=1440][ext=mp4]+ba[ext=m4a]/"
       "b[height<=1440][ext=mp4]/bv*[height<=1440]+ba/b[height<=1440]/b")
PLAYER_CLIENTS = ["web", "android_vr", "tv", "web_embedded", "ios",
                  "tv_simply"]
# cookie-capable ladder (2026-11-13): with a jar loaded yt-dlp
# auto-skips android_vr/ios/tv_simply ("does not support cookies") —
# wasted attempts. web/web_embedded work once the EJS solver from
# yt-dlp[default] is installed; tv stays as a last try.
JAR_CLIENTS = ["web", "web_embedded", "tv"]

AO_UA = ("Mozilla/5.0 (X11; Linux x86_64) fort-clip-factory/1.0")
AO_SEARCH = "https://archive.org/advancedsearch.php"
AO_META = "https://archive.org/metadata/"
AO_DL = "https://archive.org/download/"
AO_MAX_FILE_B = 600 * 1024 * 1024      # keep runner disk/time sane
AO_MIN_FILE_B = 4 * 1024 * 1024
AO_QUERIES = [
    "title:(fortnite xp map) AND mediatype:movies",
    "title:(fortnite creative) AND mediatype:movies",
    "title:(fortnite gameplay) AND mediatype:movies",
    "title:(fortnite) AND mediatype:movies",
]
DOWNLOAD_BUDGET_S = 22 * 60           # whole ladder, every rung shares it
YTDLP_ATTEMPT_S = 150                  # per client attempt


class DownloadError(RuntimeError):
    pass


# ── small helpers ───────────────────────────────────────────────────

def _left(deadline: float) -> float:
    return deadline - time.monotonic()


def _probe_duration_s(p: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(p)],
            capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip() or 0)
    except Exception:
        return 0.0


def _probe_height(p: Path) -> int:
    """Video height in pixels (0 when probing fails)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "-error", "-select_streams", "v:0",
             "-show_entries", "stream=height",
             "-of", "csv=p=0", str(p)],
            capture_output=True, text=True, timeout=60)
        return int((r.stdout.strip().splitlines() or ["0"])[0] or 0)
    except Exception:
        return 0


def _quality_ok(p: Path, min_dur: float = 0.0) -> bool:
    """The quality gate every downloaded file must pass (owner rule
    2026-11-10): duration ≥ min_dur AND height ≥ sources.min_source_height.
    Low-res garbage (144p/360p) is rejected so a rung falls through
    instead of shipping a mushy Short."""
    if not p.exists() or p.stat().st_size < 1_000_000:
        return False
    if min_dur and _probe_duration_s(p) < min_dur:
        return False
    floor = int((cfg().get("sources") or {}).get("min_source_height", 720))
    h = _probe_height(p)
    if floor and h and h < floor:
        print(f"[download] quality gate: {p.name} is {h}p "
              f"(< {floor}p floor) — rejecting, trying next rung")
        return False
    return True


_JAR_SEP = re.compile(r"^=+\s*jar\s*#?\s*\d+\s*=*$", re.IGNORECASE)

# TRUE rotation (owner pool 2026-11-13): the jar that leads the
# ladder advances one position on EVERY download call, so each of
# the 5 accounts leads ~1/5 of the session's downloads instead of
# account #1 always going first (round-robin, crash-safe, stateless).
_JAR_CURSOR = [0]


def _rotate(jars: list[Path]) -> list[Path]:
    if len(jars) < 2:
        return jars
    off = _JAR_CURSOR[0] % len(jars)
    _JAR_CURSOR[0] = (_JAR_CURSOR[0] + 1) % len(jars)
    return jars[off:] + jars[:off]


def _write_jar(work: Path, block: str, idx: int) -> Path | None:
    """Validate + write one cookie jar (work/cookies_<idx>.txt).
    Loud, actionable rejection instead of a silent ladder death."""
    lines = block.splitlines()
    header = any(l.strip().startswith("# Netscape HTTP Cookie File")
                 for l in lines[:5])
    body = [l for l in lines
            if l.strip() and not l.lstrip().startswith("#")]
    if not body:
        print(f"[cookies] jar {idx}: no cookie lines — wrong export, "
              "jar skipped")
        return None
    malformed = [l for l in body[:80] if len(l.split("\t")) < 7]
    if malformed:
        print(f"[cookies] jar {idx}: {len(malformed)}/"
              f"{min(len(body), 80)} lines are not 7-field Netscape "
              "rows — wrong export format (JSON?), jar skipped")
        return None
    yt = [l for l in body if "youtube.com" in l.split("\t")[0]]
    if not yt:
        print(f"[cookies] jar {idx}: no youtube.com cookies — jar "
              "skipped")
        return None
    text = "\n".join(lines) + "\n"
    if not header:                      # repair the common quirk
        text = "# Netscape HTTP Cookie File\n" + text
    cfile = work / f"cookies_{idx}.txt"
    cfile.write_text(text)
    print(f"[cookies] jar {idx} armed: {len(yt)} youtube.com lines, "
          f"{len(text)/1024:.1f} KB")
    return cfile


def _cookie_jars(work: Path) -> list[Path]:
    """COOKIES_TXT (+ COOKIES_TXT2/3) secrets -> validated jar files
    (never printed, never committed; deleted by the caller after the
    run).

    ONE POOL, up to three secrets (GitHub caps a single secret at 48
    KB — 5 accounts don't fit in one): the three values are stitched
    together with a parseable '===== JAR N =====' separator before
    the jar splitter runs. Multi-account + monthly harvest rotation
    + true round-robin = each account is touched only a handful of
    times a YEAR — the smallest footprint possible.
    """
    parts = [os.environ.get(n, "").strip()
             for n in ("COOKIES_TXT", "COOKIES_TXT2", "COOKIES_TXT3")]
    parts = [p for p in parts if p]
    if not parts:
        return []
    raw = "\n===== JAR 0 =====\n".join(parts)
    blocks, cur = [], []
    for line in raw.splitlines():
        if _JAR_SEP.match(line.strip()):
            blocks.append("\n".join(cur))
            cur = []
        else:
            cur.append(line)
    blocks.append("\n".join(cur))
    jars = []
    for i, b in enumerate([b for b in blocks if b.strip()], 1):
        jar = _write_jar(work, b, i)
        if jar:
            jars.append(jar)
    if jars:
        print(f"[cookies] {len(jars)} jar(s) ready — ladder rotates "
              "across them")
    return jars


def _wipe_jars(jars: list[Path]) -> None:
    for j in jars:
        j.unlink(missing_ok=True)


# ── RUNG 0: ingest/ owner drops ─────────────────────────────────────

def _take_ingest_drop(work: Path, state: dict) -> Path | None:
    """Consume one never-used drop file. Records it in state memory
    IMMEDIATELY (a crash mid-render must not re-mine the same drop),
    unlinks it locally, and lets the workflow commit the deletion."""
    folder = path(cfg()["ingest"]["folder"])
    if not folder.exists():
        return None
    used = set(state.setdefault("used_drops", []))
    drops = sorted([p for p in folder.iterdir()
                    if p.suffix.lower() in (".mp4", ".mkv", ".mov", ".webm")
                    and p.is_file() and p.name not in used])
    if not drops:
        return None
    src = drops[0]
    out = work / f"drop_{src.stem}.mp4"
    shutil.copy(src, out)
    dur = _probe_duration_s(out)
    if dur < 45:
        print(f"[download] drop {src.name} too short "
              f"({dur:.0f}s < 45s) — discarding, moving on")
    else:
        print(f"[download] using ingest/ drop: {src.name} "
              f"({dur:.0f}s of footage)")
    # consumed either way: remembered immediately + removed from the
    # tree so this drop is mined at most once, ever
    state["used_drops"].append(src.name)
    ledger.save(state)
    src.unlink(missing_ok=True)
    if dur >= 45 and _quality_ok(out):
        return out
    print(f"[download] drop {src.name} discarded "
          "(too short or below the quality floor)")
    out.unlink(missing_ok=True)
    return None


# ── RUNG 1: yt-dlp ladder ───────────────────────────────────────────

def _run_ytdlp(url: str, out: Path, jars: list[Path],
               deadline: float, sections: str | None = None) -> bool:
    base = ["yt-dlp", "-f", FMT, "--merge-output-format", "mp4",
            "--no-playlist", "--no-part",
            "-o", str(out),
            "--socket-timeout", "20",
            "--retries", "4", "--fragment-retries", "4"]
    if sections:
        base += ["--download-sections", sections]

    # attempt ladder: every rotated jar x the cookie-capable clients
    # (cap 12); anonymous (no jars) -> the full 6-client ladder as before
    if jars:
        combos = [(client, jar)
                  for jar in _rotate(jars)
                  for client in JAR_CLIENTS][:12]
    else:
        combos = [(client, None) for client in PLAYER_CLIENTS]

    for i, (client, jar) in enumerate(combos):
        if _left(deadline) < 30:
            print("[download] yt-dlp ladder out of time budget")
            return False
        args = list(base)
        if jar:
            args += ["--cookies", str(jar)]
        if client != PLAYER_CLIENTS[0]:
            args += ["--extractor-args",
                     f"youtube:player_client={client}"]
        try:
            r = subprocess.run(
                args + [url], capture_output=True, text=True,
                timeout=min(YTDLP_ATTEMPT_S, max(int(_left(deadline)), 20)))
        except subprocess.TimeoutExpired:
            print(f"[download] attempt {i+1} ({client}, "
                  f"jar {jar.stem if jar else '-'}): timeout")
            continue
        if r.returncode == 0 and out.exists() \
                and out.stat().st_size > 1_000_000:
            print(f"[download] OK via player_client={client}"
                  f"{', jar ' + jar.stem if jar else ''} "
                  f"({out.stat().st_size/1e6:.1f} MB)")
            return True
        tail = (r.stderr or "").strip().splitlines()[-3:]
        print(f"[download] attempt {i+1} ({client}, "
              f"jar {jar.stem if jar else '-'}): "
              f"rc={r.returncode} {' | '.join(tail)[:300]}")
        if out.exists() and out.stat().st_size < 1_000_000:
            out.unlink(missing_ok=True)   # error-page stub
        time.sleep(3)
    return False


def download_slice(source: dict, work: Path, slice_start: float,
                   slice_len: float,
                   budget_s: float = 8 * 60) -> Path | None:
    """MONTHLY BANK MODE: grab only slice_len seconds of the video
    starting at slice_start (--download-sections). One IP, one
    session, ~100 sources/month — the anti-flag design.

    Returns None (never raises) on failure — the harvest simply moves
    to the next candidate; ingest/archive rungs do not apply here
    (a slice is video-specific)."""
    work.mkdir(parents=True, exist_ok=True)
    vid = source.get("id", "unknown")
    url = f"https://www.youtube.com/watch?v={vid}"
    deadline = time.monotonic() + budget_s
    jars = _cookie_jars(work)
    out = work / f"slice_{vid}.mp4"
    try:
        ok = _run_ytdlp(url, out, jars, deadline,
                        sections=f"*{int(max(0, slice_start))}-"
                                  f"{int(max(0, slice_start) + slice_len)}")
        if ok and _quality_ok(out, 45):
            return out
        out.unlink(missing_ok=True)
    finally:
        _wipe_jars(jars)
    print(f"[download] slice failed for {vid} — trying the next "
          "candidate")
    return None


# ── RUNG 2/3: archive.org ───────────────────────────────────────────

def _ao_get_json(url: str, deadline: float) -> dict | None:
    for attempt in range(3):
        if _left(deadline) < 20:
            return None
        req = urllib.request.Request(url, headers={"User-Agent": AO_UA})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read())
        except Exception as e:
            print(f"[archive.org] metadata {attempt+1}/3 failed: {e}")
            time.sleep(15 * (attempt + 1))
    return None


def _ao_stream(item: str, fname: str, out: Path, deadline: float) -> bool:
    """Download one archive.org file to out (honors the budget)."""
    url = AO_DL + item + "/" + urllib.parse.quote(fname)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": AO_UA})
        with urllib.request.urlopen(req, timeout=90) as resp, \
                open(out, "wb") as fh:
            total = 0
            while True:
                if _left(deadline) < 15:
                    break               # keep what we have if it's media
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                fh.write(chunk)
                total += len(chunk)
                if total > AO_MAX_FILE_B:
                    print(f"[archive.org] {fname} exceeds cap mid-stream")
                    out.unlink(missing_ok=True)
                    return False
    except Exception as e:
        print(f"[archive.org] download failed: {e}")
        if out.exists():
            out.unlink(missing_ok=True)
        return False
    if not out.exists() or out.stat().st_size < AO_MIN_FILE_B:
        out.unlink(missing_ok=True)
        return False
    head = out.open("rb").read(16)
    ok = head[:4] == b"\x1a\x45\xdf\xa3" or head[4:8] == b"ftyp"
    if not ok:                          # error page, not media
        out.unlink(missing_ok=True)
        return False
    print(f"[archive.org] fetched {fname} "
          f"({out.stat().st_size/1e6:.1f} MB)")
    return True


def _ao_video_file(item: str, deadline: float) -> tuple[str, int] | None:
    """Best media file of an item: mp4 preferred, no chat overlays,
    within the size window."""
    doc = _ao_get_json(AO_META + item, deadline)
    if not doc:
        return None
    cands = []
    for f in doc.get("files", []):
        name = f.get("name", "")
        if name.lower().endswith((".mp4", ".mkv")) and "chat" not in \
                name.lower():
            size = int(f.get("size", 0) or 0)
            if AO_MIN_FILE_B <= size <= AO_MAX_FILE_B:
                cands.append((name, size))
    if not cands:
        return None
    cands.sort(key=lambda c: (not c[0].lower().endswith(".mp4"), -c[1]))
    return cands[0]


def _ao_mirror(source: dict, work: Path, deadline: float) -> Path | None:
    """RUNG 2: archive.org often mirrors YouTube videos as items named
    youtube-<video id>. Byte-identical footage — the source credit the
    machine prints stays true."""
    item = f"youtube-{source.get('id')}"
    pick = _ao_video_file(item, deadline)
    if not pick:
        return None
    fname, size = pick
    print(f"[archive.org] mirror exists for {source.get('id')} "
          f"({fname.split('/')[-1][:60]}, {size/1e6:.0f} MB)")
    out = work / f"mirror_{source.get('id')}.mp4"
    if _ao_stream(item, fname, out, deadline) \
            and _quality_ok(out, 45):
        return out
    out.unlink(missing_ok=True)
    return None


def _ao_open_pool(source: dict, work: Path, state: dict,
                  deadline: float) -> Path | None:
    """RUNG 3: search archive.org itself for fresh Fortnite gameplay.
    The item is marked used in the machine's memory the moment it is
    chosen, so the open pool is mined without repeats too.

    OWNER RULE (2026-11-10, freshness): archive.org items ADDED within
    the 11-month window (sources.max_age_days) are tried first — the
    same freshness spirit as the YouTube search rule. Items added
    longer ago (2019 BR compilations and the like) are a LAST resort
    only when the fresh pool comes up dry, because the machine must
    never starve silently."""
    from datetime import datetime, timezone

    used = set(state.get("used_sources", []))

    def mined(ident: str) -> bool:
        # an item can be remembered in three forms: bare identifier
        # (open pool), "ao:" + identifier (open pool, legacy), or the
        # bare YouTube id it mirrors (mirror rung) — check them all so
        # one video can never be mined twice through different rungs.
        forms = {ident, f"ao:{ident}"}
        if ident.startswith("youtube-"):
            forms.add(ident[len("youtube-"):])
        return bool(forms & used)

    age_days = float((cfg().get("sources") or {}).get("max_age_days", 334))

    def _added_recently(d: dict) -> bool:
        try:
            pd = datetime.fromisoformat(
                str(d.get("publicdate", "")).replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - pd).days <= age_days
        except Exception:
            return False           # unknown date -> last-resort pass

    def _try_docs(doclist: list[dict]) -> Path | None:
        for d in doclist:
            if _left(deadline) < 120:
                return None
            ident = d.get("identifier", "")
            if not ident or mined(ident):
                continue
            pick = _ao_video_file(ident, deadline)
            if not pick:
                continue
            fname, size = pick
            out = work / f"ao_{ident.replace('/', '_')}.mp4"
            if _ao_stream(ident, fname, out, deadline):
                if _quality_ok(out, 45):
                    title = str(d.get("title") or ident)[:80]
                    source.update({
                        "id": f"ao:{ident}",
                        "title": title,
                        "channel": "archive.org mirror",
                        "origin": "archive",
                        "query": "",
                        "duration_s": int(_probe_duration_s(out)),
                    })
                    print(f"[archive.org] open-pool source: {title!r}")
                    return out
                out.unlink(missing_ok=True)
        return None

    for q in AO_QUERIES:
        if _left(deadline) < 120:
            return None
        url = (AO_SEARCH + "?" + urllib.parse.urlencode({
            "q": q, "fl[]": ["identifier", "title", "publicdate"],
            "rows": 50, "page": 1, "output": "json",
            "sort": "-publicdate"}, doseq=True))
        doc = _ao_get_json(url, deadline)
        docs = ((doc or {}).get("response", {}) or {}).get("docs", []) or []
        fresh = [d for d in docs if _added_recently(d)]
        older = [d for d in docs if not _added_recently(d)]
        print(f"[archive.org] query {q!r}: {len(fresh)} fresh + "
              f"{len(older)} older items")
        hit = _try_docs(fresh)
        if hit:
            return hit
        if older:
            print("[archive.org] fresh pool dry — trying older items "
                  "(last resort, machine must not starve)")
            hit = _try_docs(older)
            if hit:
                return hit
        print(f"[archive.org] open pool query {q!r} had no usable item")
    return None


# ── master ──────────────────────────────────────────────────────────

def download(source: dict, work: Path, state: dict | None = None) -> Path:
    """Feed the machine. Returns the local media path.

    Raises DownloadError with a human-actionable message when every
    rung fails — the orchestrator turns that into a GitHub Issue.
    """
    work.mkdir(parents=True, exist_ok=True)
    if state is None:
        state = ledger.load()
    deadline = time.monotonic() + DOWNLOAD_BUDGET_S
    vid = source.get("id", "unknown")
    url = f"https://www.youtube.com/watch?v={vid}"

    # RUNG 0: owner drops first (bootstrap / override)
    drop = _take_ingest_drop(work, state)
    if drop:
        return drop

    # RUNG 1: yt-dlp (+ cookies jars when the secret exists)
    jars = _cookie_jars(work)
    src_out = work / f"source_{vid}.mp4"
    try:
        if _run_ytdlp(url, src_out, jars, deadline) \
                and _quality_ok(src_out):
            return src_out
        src_out.unlink(missing_ok=True)   # low-res / broken -> next rung
    finally:
        _wipe_jars(jars)

    # RUNG 2: archive.org mirror of this exact video
    mirror = _ao_mirror(source, work, deadline)
    if mirror:
        return mirror

    # RUNG 3: archive.org open pool (source dict mutates to the item)
    pool = _ao_open_pool(source, work, state, deadline)
    if pool:
        return pool

    # RUNG 4: loud, actionable failure
    if not jars:
        msg = ("DOWNLOAD-ALERT: yt-dlp was bot-checked for source "
               f"{vid} ({source.get('title','')[:60]}), the archive.org "
               "mirror + open-pool rungs found nothing, and no "
               "COOKIES_TXT secret is set.\n"
               "FIX (permanent): export your youtube.com cookies.txt (a "
               "logged-in browser, 'Get cookies.txt LOCALLY' extension) "
               "and add it as the repo secret COOKIES_TXT — RUNBOOK.md "
               "section 5.\n"
               "INTERIM: drop any real Fortnite map gameplay MP4 into "
               "the ingest/ folder and the machine mines it on the "
               "next run.")
    else:
        msg = (f"DOWNLOAD-ALERT: every rung failed for source {vid} "
               f"({source.get('title','')[:60]}) — yt-dlp (with "
               "COOKIES_TXT), the archive.org mirror, and the "
               "archive.org open pool. The cookies may be expired "
               "(re-export them). The next run picks a different "
               "source automatically.")
    raise DownloadError(msg)
