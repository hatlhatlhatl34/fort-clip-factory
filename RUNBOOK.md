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

1. Create a new GitHub repository `fort-clip-factory` (it can be
   public — public repos get **unlimited free Actions minutes**, which
   is what lets this machine run for years at zero cost; the workflow
   keeps the repo active by committing memory every run, so GitHub
   never auto-disables the schedule).
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

GitHub runner IPs are datacenter IPs; YouTube blocks anonymous
downloads from them ("Sign in to confirm you're not a bot" — the
01:20 UTC 2026-11-11 run hit it on all six player clients). A
logged-in cookies file walks past the check:

1. In YOUR browser, log into youtube.com with a spare/throwaway
   Google account (never your main one).
2. Install a cookie-export extension (e.g. "Get cookies.txt
   LOCALLY" — local only, nothing leaves the browser).
3. Visit youtube.com, then export cookies for `youtube.com` →
   `cookies.txt` (Netscape format).
4. Set the secret one of two ways:
   * **Web UI:** repo Settings → Secrets and variables → Actions →
     New repository secret → name `COOKIES_TXT` → paste the whole
     file.
   * **One command** (validates first, then encrypts and PUTs the
     secret via the API):
     ```
     GH_TOKEN=<github token> python tools/set_cookies.py cookies.txt
     ```

### MANY ACCOUNTS = MANY JARS (owner idea 2026-11-11)

6-7 spare accounts is even better: paste ALL their exports into
the ONE `COOKIES_TXT` secret, one after another, separated by a
marker line between them:

```
# Netscape HTTP Cookie File
.youtube.com    TRUE    /...   (account 1's cookies)
===== JAR 2 =====
# Netscape HTTP Cookie File
.youtube.com    TRUE    /...   (account 2's cookies)
===== JAR 3 =====
...
```

The machine validates every jar separately, rotates across them,
and combined with the monthly harvest each account is touched only
a handful of times a YEAR — the smallest footprint possible.

The machine validates the paste itself (Netscape format, youtube.com
domain, repairs a missing header) and logs
`[cookies] jar N armed: ...` on every run — if the log instead says
the cookies "may be expired", re-export and set the secret again
(rotation: rare; usually only when YOU log out of that account).

### Exporting cookies ON A PHONE (Android)

1. Install **Firefox** from the Play Store (it officially supports
   extensions on Android).
2. Menu → Add-ons → search "**Get cookies.txt LOCALLY**" → Add.
3. Log into youtube.com with the spare account (or a second spare
   account for jar 2, etc.).
4. Open youtube.com → menu → Add-ons → Get cookies.txt LOCALLY →
   **Export** → the file saves to Downloads.
5. Send that file here in chat (or paste it into the secret) —
   done. Repeat per account, one export each.

(On iPhone there is no clean way — use any computer, or tell me and
we find another path.)

If even cookies fail someday, the machine falls through to its other
food rungs — the **archive.org mirror** of the picked video (items
named `youtube-<id>`) and the **archive.org open pool** (a direct
search for fresh Fortnite gameplay mirrors, each mined once) — and
only then files a DOWNLOAD-ALERT issue and waits for MP4s in
`ingest/`. It never starves silently.

## 5b. THE BANK — monthly harvest, daily publish (owner idea 2026-11-11)

The IP-flag problem, solved structurally: the risky work (YouTube
downloads) happens **once a month** in ONE long session from ONE
IP with the cookie jars; the daily runs do ONLY the API upload
(OAuth is identity-based, never IP-flagged).

* `monthly-bank-harvest.yml` fires on the 1st (06:00 UTC) and its
  guard retries daily at 07:00 UTC until the bank is full — "try
  again and again until the gate opens".
* The harvest downloads only a ~5-minute **slice** of each source
  (`--download-sections`), clips + renders ~93 Shorts, and banks
  them into ONE GitHub **artifact** (free on public repos,
  auto-expires after 45 days — no repo bloat).
* Daily runs fetch the month's artifact, pop the next banked clip,
  and upload it into its grid slot — no yt-dlp, no cookies, no bot
  wall on daily IPs, ever.
* **Never starves:** bank empty / artifact gone / download failed →
  the daily run falls back to the live ladder exactly as before.
  The bank is a cache, never a single point of failure.
* Quota stays legal: uploads stay spread daily (an upload is 1600
  API units of the 10,000/day budget — max ~6/day anyway).

No setup needed — everything above is automatic once the secrets
are set.

## 6. First run

Actions tab → **daily-clip-factory** → Run workflow. Watch the log:
it searches, downloads, clips, renders, uploads, and books the next
grid slot. From then on the crons run it forever.

Quota math (worst case, a 4-Short weekend day): 4 × (search 100 +
videos.list 1 + upload 1600 + verify fallback ≤2) ≈ **6,812 units**
of the 10,000 daily quota — comfortable margin. Weekdays ≈ 5,100.

## 7. What the machine remembers (data/state.json)

* `used_sources` — every source video ever mined (never re-clipped)
* `used_drops` — every ingest/ drop ever consumed (never re-mined,
  even if a commit fails)
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
* `sources.queries` — 16 rotating phrasings, XP-map heavy (the
  approved sample-1 niche): "fortnite xp map gameplay", "fortnite xp
  farm map", "fortnite afk xp map gameplay", ... plus deathruns, edit
  courses, horror maps...
* codes: `1789-8528-6429` top bar + `H15` footer, first frame to
  last, plus title + description.
* original audio is discarded; the soundtrack is synthesized by
  numpy per clip (zero music copyright).
* descriptions NEVER mention the source video or channel (owner
  rule 2026-11-10: the "clipped from ..." sentence is permanently
  banned) — only the map code, creator code and hashtags appear.
