# fort-clip-factory

An autonomous YouTube Shorts machine that clips **real Fortnite
creative-map gameplay videos** (found via the YouTube Data API) into
vertical Shorts carrying your map code and creator code — publishing
3 per day on weekdays and 4 on Fri/Sat/Sun, forever, for $0/month on
GitHub Actions.

**Map code `1789-8528-6429`** — big top bar, first frame to last.
**Creator code `H15`** — footer bar, full duration.
Both also live in every title and description.

## The daily act

1. **SEARCH** — YouTube Data API, rotating 16 query phrasings —
   XP-map heavy ("fortnite xp map gameplay", "fortnite xp farm
   map", ... — the approved sample-1 niche) plus creative-map,
   deathrun, edit-course, horror, parkour variety — with
   `publishedAfter` = now minus 11 months (owner rule:
   footage older than 11 months can never appear).
2. **DOWNLOAD** — yt-dlp pulls the actual video (cookies secret
   handles the bot-check; `ingest/` folder is the manual backup).
3. **CLIP** — scene-change + motion analysis finds the best 45-120 s
   window in the real footage.
4. **TRANSFORM** — 1080×1920 vertical zoom-crop; original audio is
   discarded; a seeded numpy-synthesized gaming track becomes the
   soundtrack (zero music copyright).
5. **PUBLISH** — resumable upload with `publishAt` on the
   00:00 / 08:00 / 16:00 Cairo grid (+ 20:00 on Fri/Sat/Sun);
   description credits the source.

## Layout

```
channel.yaml            machine DNA (codes, queries, freshness, grid)
main.py                 entry point (--dry-run, --sample, --verify-only)
src/sources.py          YouTube search + 11-month rule + no-repeat
                       (API key or OAuth token — either works)
src/downloader.py       yt-dlp ladder: cookies -> ingest/ -> alert
src/clipper.py          scene-change + motion -> best window
src/transform.py        one-pass ffmpeg render (crop/zoom/overlay/music)
src/overlay.py          PIL overlay bars (map code + H15)
src/music.py            numpy synth soundtrack (energetic bed)
src/metadata.py         titles/descriptions with codes + credit
src/youtube.py          OAuth + resumable upload + publishAt grid
src/ledger.py           state.json memory (bak ladder, quota ledger)
src/verify.py           zero-quota oEmbed verification
tools/gate.py           RUN/SKIP gate (runs before pip install)
tools/auth.py           one-time OAuth refresh-token helper
.github/workflows/      3 crons + hourly heal + watchdog
ingest/                 manual MP4 drops (ladder rung 2)
```

Setup: **RUNBOOK.md** (30 minutes, one time).
