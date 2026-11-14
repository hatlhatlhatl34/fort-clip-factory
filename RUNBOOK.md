# RUNBOOK — fort-clip-factory setup (30 minutes, one time)

The machine clips REAL Fortnite map gameplay videos from YouTube into
vertical Shorts that carry **map code 1789-8528-6429** (top bar, every
frame) and **creator code H15** (footer, every frame), publishing
**4 Shorts every day** (owner rule 2026-11-13) at 00:00 / 08:00 /
16:00 / 20:00 Cairo — ONE upload session per day (08:30 UTC) that
uploads all four at once, each scheduled with publishAt.

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
   * **One command** (validates first, then buckets the jars across
     up to three secrets — GitHub caps a single secret at 48 KB, and
     5 accounts don't fit in one):
     ```
     GH_TOKEN=<github token> python tools/set_cookies.py cookies.txt
     ```
   * **Web UI:** paste into `COOKIES_TXT` (and `COOKIES_TXT2`,
     `COOKIES_TXT3` for the rest) — separate multiple accounts with
     `===== JAR 2 =====` marker lines.

### MANY ACCOUNTS = ONE ROTATING POOL (owner idea; 5 armed 2026-11-13)

6-7 spare accounts is even better: paste ALL their exports into ONE
file (separated by `===== JAR N =====` marker lines) and run
set_cookies.py once — it packs them into `COOKIES_TXT` +
`COOKIES_TXT2` + `COOKIES_TXT3` and the machine stitches the three
back into one pool at run time. The ladder TRULY rotates (round
robin): the account leading each download advances one position per
call, so with 5 accounts each one leads ~1/5 of the session's
downloads. Combined with the monthly harvest, each account is
touched only a handful of times a YEAR — the smallest footprint
possible.

The machine validates the paste itself (Netscape format, youtube.com
domain, repairs a missing header) and logs
`[cookies] jar N armed: ...` on every run — if the log instead says
the cookies "may be expired", re-export and set the secret again
(rotation: rare; usually only when YOU log out of that account).

### The other half of the fix: yt-dlp[default] (EJS solver)

Cookies beat the bot-check, but logged-in accounts also hit YouTube's
**SABR-only streaming experiment** — the web client then sees "Only
images are available" even with perfect cookies (2026-11-13 run).
The cure is the EJS challenge-solver dist: requirements.txt pins
`yt-dlp[default]` (pulls `yt-dlp-ejs`), and the workflows install
deno (its JS runtime). Live-confirmed 2026-11-13: web client +
cookies + EJS = download OK from a datacenter IP. One jar note:
`android_vr`/`ios`/`tv_simply` are auto-skipped by yt-dlp when a
jar is loaded ("does not support cookies") — the jar ladder only
tries the cookie-capable clients (web, web_embedded, tv).

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
  (`--download-sections`), clips + renders ~124 Shorts (4/day x 31
  days), and banks them into ONE GitHub **artifact** (a single
  artifact may hold up to 5 GB — the month fits; free on public
  repos, auto-expires after 45 days — no repo bloat). The 350-minute
  job cap means ~60 clips per session; 2-3 guard sessions fill a
  whole month.
* The ONE daily session (08:30 UTC) fetches the month's artifact,
  pops the next banked clips, and uploads all 4 day-Shorts with
  publishAt — no yt-dlp, no cookies, no bot wall on daily IPs, ever.
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

## Daily shape (owner rule 2026-11-13: 4/day, ONE session)

```
08:30 UTC cron (the ONLY daily wake-up) -> gate RUN -> ONE session:
    pops up to 4 banked clips (or live-ladder fallback) ->
    uploads all 4 at once -> publishAt 16:00+20:00 Cairo today,
    00:00+08:00 Cairo tomorrow
hourly heal cron -> SKIP unless a slot would expire before the next
    08:30 UTC session can claim it -> then heals every hole at once
watchdog 6x/day -> oEmbed verify; Issue if a Short is late
monthly harvest (1st + daily guard) -> banks ~124 clips for the month
```

Quota math: one session = 4 x upload 1600 + search/verify crumbs
= **~6,500 units** of the 10,000 daily budget — comfortable margin
(the session fires right AFTER the Pacific-midnight reset).

## Owner rules encoded in channel.yaml

* `sources.max_age_days: 334` — **sources ≤ 11 months old**, enforced
  by `publishedAfter` in the search itself (old footage cannot enter
  the candidate pool).
* `sources.queries` — 16 rotating phrasings, XP-map heavy (the
  approved sample-1 niche): "fortnite xp map gameplay", "fortnite xp
  farm map", "fortnite afk xp map gameplay", ... plus deathruns, edit
  courses, horror maps...
* codes: `1789-8528-6429` top bar + `H15` footer, first frame to
  last. Codes live in the DESCRIPTION only — **NEVER in titles**
  (owner rule 2026-11-13: titles are pure XP/free-skins hooks like
  "How to get FREE SKINS in Fortnite 🤑 #N"; the map code still
  burns into every frame's top bar).
* **the word "Fortnite" is in EVERY title** (owner rule 2026-11-14):
  titles talk about XP maps + free skins FOR FORTNITE, so every
  template in channel.yaml carries the game's name, and
  src/metadata.py auto-repairs any title that loses it — a
  Fortnite-less title can never ship.
* original audio is discarded; the soundtrack is synthesized by
  numpy per clip (zero music copyright).
* descriptions NEVER mention the source video or channel (owner
  rule 2026-11-10: the "clipped from ..." sentence is permanently
  banned) — only the map code, creator code and hashtags appear.
* channel description + keywords live in channel.yaml `branding:`;
  apply changes with the **channel-branding** workflow (manual
  dispatch — runs tools/update_channel.py with the OAuth secrets;
  avatar/banner changes need YouTube Studio, not the API).

## Phase 2 (parked, owner 2026-11-13)

After the machine is proven: post the same Shorts to OTHER
platforms. NOT before everything above is green and boring. The
Shorts render pipeline already produces platform-neutral
1080x1920 MP4s — the multi-platform step will reuse the bank
artifact, not re-download anything.
