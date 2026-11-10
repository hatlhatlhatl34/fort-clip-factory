# RUNBOOK — fort-clip-factory setup (30 minutes, one time)

The machine clips REAL Fortnite map gameplay videos from YouTube into
vertical Shorts that carry **map code 1789-8528-6429** (top bar, every
frame) and **creator code H15** (footer, every frame), publishing 3
Shorts/day at 00:00 / 08:00 / 16:00 Cairo — plus a 4th at 20:00 on
Fri/Sat/Sun.

**Services used (by owner decree): YouTube Data API + GitHub only.**
No render farms, no paid APIs, no bots — the whole machine runs on
free GitHub Actions compute, exactly like the forever-factory
doctrine it was ported from.

---

## 1. Create the GitHub repo

1. Create a new **private** GitHub repository (e.g. `fort-clip-factory`).
2. Upload this folder's contents (all files, keeping the directory
   structure). Include `.github/workflows/`.
3. Repo → Settings → Actions → General → Workflow permissions →
   **Read and write permissions** (the machine commits its memory
   back every run), and allow GitHub Actions to create issues.

## 2. YouTube Data API key (for SEARCH) — OPTIONAL

The machine PREFERS a plain API key for search, but when
`YOUTUBE_API_KEY` is not set it automatically searches with the OAuth
token instead (the `youtube` scope granted by tools/auth.py covers
search.list). If you skip this step, everything still works with just
the three OAuth secrets.

1. https://console.cloud.google.com → create a project.
2. APIs & Services → Library → enable **YouTube Data API v3**.
3. APIs & Services → Credentials → **Create credentials → API key**.
4. Copy the key.

## 3. OAuth refresh token (for UPLOAD)

1. Same project → **OAuth consent screen**: External, add YOUR
   Google account (the one owning the target channel) as a test user.
2. Credentials → **Create OAuth client ID** → Web application →
   Authorized redirect URI: `http://localhost:8080`.
3. Download the client JSON.
4. **CRITICAL for the forever plan:** on the OAuth consent screen press
   **PUBLISH APP** (push to production). While the app stays in
   "Testing", Google expires refresh tokens after **7 days** and the
   machine would need re-authorization weekly. Published (even
   unverified) apps keep refresh tokens alive indefinitely.
5. On your own machine:
   ```
   python tools/auth.py --client-json client_secret_XXXX.json
   ```
   Authorize with the channel-owner account; copy the printed
   **refresh token**.

## 4. Set the GitHub secrets

Repo → Settings → Secrets and variables → Actions:

| Secret             | What                              |
|--------------------|-----------------------------------|
| `YT_CLIENT_ID`     | OAuth client id from step 3       |
| `YT_CLIENT_SECRET` | OAuth client secret from step 3   |
| `YT_REFRESH_TOKEN` | refresh token from step 3         |
| `YOUTUBE_API_KEY`  | (optional) the API key from step 2 — search falls back to the OAuth token when unset |
| `COOKIES_TXT`      | (recommended) see step 5          |

## 5. COOKIES_TXT — the anti-bot-check rung (recommended)

GitHub runner IPs are datacenter IPs; YouTube sometimes blocks
anonymous downloads from them ("Sign in to confirm you're not a
bot"). A logged-in cookies file walks past the check:

1. In YOUR browser, log into youtube.com with a spare/throwaway
   Google account (never your main one).
2. Install a cookie-export extension (e.g. "Get cookies.txt
   LOCALLY" — local only, nothing leaves the browser).
3. Export cookies for `youtube.com` → `cookies.txt`.
4. Paste the whole file into the `COOKIES_TXT` secret.

If even cookies fail someday, the machine files a DOWNLOAD-ALERT
issue and you drop MP4s into `ingest/` — it never starves silently.

## 6. First run

Actions tab → **daily-clip-factory** → Run workflow. Watch the log:
it searches, downloads, clips, renders, uploads, and books the next
grid slot. From then on the crons run it forever.

Quota math (worst case, a 4-Short weekend day): 4 × (search 100 +
videos.list 1 + upload 1600 + verify fallback ≤2) ≈ **6,812 units**
of the 10,000 daily quota — comfortable margin. Weekdays ≈ 5,100.

## 7. What the machine remembers (data/state.json)

* `used_sources` — every source video ever mined (never re-clipped)
* `shorts` — every published Short (source, window, video id, title)
* `slots` — booked publishAt grid slots
* `quota` — daily API units (rolls at midnight Pacific, like YouTube)
* memory ladder: `state.json` → `state.json.bak` → fresh default

## Daily shape

```
21:30 UTC cron  ->  gate RUN  ->  clip machine  ->  publishAt 00:00 Cairo
05:30 UTC cron  ->  gate RUN  ->  clip machine  ->  publishAt 08:00 Cairo
13:30 UTC cron  ->  gate RUN  ->  clip machine  ->  publishAt 16:00 Cairo
16:00 UTC cron (Fri/Sat/Sun) -> publishAt 20:00 Cairo   # the 4th slot
hourly heal cron -> gate SKIP (99% of the time) or RUN when a grid hole exists
watchdog 6x/day -> oEmbed verify; Issue if a Short is late
```

## Owner rules encoded in channel.yaml

* `sources.max_age_days: 334` — **sources ≤ 11 months old**, enforced
  by `publishedAfter` in the search itself (old footage cannot enter
  the candidate pool).
* `sources.queries` — 12 rotating phrasings: "fortnite creative map",
  "fortnite map", "fortnite map gameplay", deathruns, edit courses,
  horror maps...
* codes: `1789-8528-6429` top bar + `H15` footer, first frame to
  last, plus title + description.
* original audio is discarded; the soundtrack is synthesized by
  numpy per clip (zero music copyright).
* every description credits the source video and channel.
