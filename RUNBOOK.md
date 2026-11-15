# RUNBOOK — fort-clip-factory setup (30 minutes, one time)

The machine clips REAL Fortnite map gameplay videos from YouTube into
vertical Shorts that carry **map code 1789-8528-6429** (top bar, every
frame) and **creator code H15** (footer, every frame), publishing
**4 Shorts every day, ONE every 6 hours** (owner rule 2026-11-14:
6+6+6+6 = 24h of even coverage) at 00:00 / 06:00 / 12:00 / 18:00
Cairo — ONE upload session per day (08:30 UTC) that uploads all
four at once, each scheduled with publishAt.

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

* `monthly-bank-harvest.yml` fires on the 1st (12:00 UTC) and its
  guard retries daily at 13:00 UTC (15:00 Cairo — after the day's
  08:30 session has finished, before tomorrow's) until the month is
  provisioned — "try again and again until the gate opens".
* The harvest downloads only a ~5-minute **slice** of each source
  (`--download-sections`), clips + renders Shorts, and banks them
  into GitHub **artifact parts** `bank-<ym>-p1/p2/...` (~4.2 GB each;
  free on public repos, auto-expire after 45 days — no repo bloat).
* **Month provision (owner rule 2026-11-14):** the month's harvest
  must total **>= 124 clips** ("at least 120 per month"). One session
  renders only ~25 in its ~5h budget, so guard sessions **APPEND**:
  the workflow fetches the existing artifact parts into `bank/`
  first, the harvest adds fresh clips on top, and the whole bank is
  re-uploaded as new parts. `bank.harvested_total` in state.json is
  the meter — it never drops when clips publish. Once it reaches
  124 the guard goes quiet for the rest of the month;
  `refill_threshold` (clips <= 8) is the drained-bank emergency.
* The ONE daily session (08:30 UTC) fetches the month's artifact
  parts by pattern (`bank-<ym>*`, merged), pops the next banked
  clips, and uploads all 4 day-Shorts with publishAt — no yt-dlp, no
  cookies, no bot wall on daily IPs, ever.
* **Never starves:** bank empty / artifact gone / download failed →
  the daily run falls back to the live ladder exactly as before.
  The bank is a cache, never a single point of failure.
* **Push-race recovery (2026-11-14 incident):** if a harvest's
  memory push loses a git race (`git push || true` swallows it),
  GitHub can end up with a full artifact but state.json saying
  bank.clips = 0. Recover with
  `python tools/restore_bank.py --manifest <manifest.json> --run-id <id> --apply`
  — it renumbers the clips past the published shorts, re-titles
  them with today's templates (Fortnite rule included), re-marks
  the used sources, and fixes bank_pointer.json. The manifest can
  be pulled out of the artifact zip with HTTP range requests (no
  1.4 GB download needed).
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
    uploads all 4 at once -> publishAt 12:00+18:00 Cairo today,
    00:00+06:00 Cairo tomorrow (one Short every 6 hours) ->
    caches each final MP4 for the reels cross-poster
hourly heal cron -> gate SKIPs the factory ~99% of the time, BUT the
    crosspost job runs EVERY hour: at each grid slot it pushes the
    same MP4 as an Instagram Reel + Facebook Reel (see section 8);
    the factory part only RUNs when a slot would expire before the
    next 08:30 UTC session -> then heals every hole at once
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

## 8. CROSS-POSTING — Instagram Reels + Facebook Reels (owner rule 2026-11-15)

**The SAME Short now goes everywhere**: the exact MP4 that YouTube
publishes as a Short becomes an Instagram Reel and a Facebook Reel.

```
08:30 UTC session:  uploads 4 Shorts (publishAt = grid slots) AND
                    caches each final into a media-cache-<date>
                    artifact (state.shorts[].media points at it)
hourly heal:        at each 6-hour grid slot (00/06/12/18 Cairo) the
                    crosspost job fetches the cached MP4 back and
                    pushes the SAME bytes to Meta's resumable-upload
                    endpoints — reels go live within minutes of their
                    YouTube twin. Failures retry next hour (8 tries),
                    never touch the YouTube pipeline, alert once
                    after the budget is spent.
```

Instagram flow: `POST /{ig_id}/media` (`media_type=REELS`,
`upload_type=resumable`, caption) → binary POST to
`rupload.facebook.com/ig-api-upload/{v}/{container}` → poll
`status_code` → `FINISHED` → `POST /{ig_id}/media_publish`.
Facebook flow: `POST /{page_id}/video_reels`
(`upload_phase=start`) → binary POST to the returned `upload_url` →
`upload_phase=finish` with `video_state=PUBLISHED` + description.
No public hosting is ever needed — pure resumable uploads.

Secrets (Settings → Secrets and variables → Actions):

* `IG_ACCESS_TOKEN` — **set 2026-11-15** (Business system-user
  "Fortnite System User"; live-checked 2026-11-15 ~13:40 Cairo:
  ALIVE, `instagram_basic` + `instagram_content_publish` granted).
* `FB_ACCESS_TOKEN` — **not set yet**: the Explorer token pasted on
  2026-11-15 was live-checked DEAD (expired 09:00 UTC, as Explorer
  tokens do after ~1 h). The replacement must be a system-user
  token — see the checklist below.

Arming each platform (channel.yaml `crosspost:`):

* `ig_user_id` — the Instagram professional account id (1784...).
  Empty 2026-11-15: the system-user token carries the right
  permissions but sees no pages and lacks `business_management`, so
  no account id is visible to it yet (live-probed). The owner
  confirmed the account is **@h15master** (2026-11-15) — recorded as
  `crosspost.ig_username`, the hint discovery matches on; a numeric
  id paste also arms it instantly.
* `fb_page_id` — a Facebook **Page** id. Empty: the personal profile
  "Hfifteen Master" has NO Page — reels NEED a Page + a Page-capable
  token.

**The 3-minute Meta-side checklist to finish arming** (one time):

1. **Facebook Page** — https://www.facebook.com/pages/create →
   e.g. "H15 Fortnite", category "Gaming video creator" → Create.
   If Facebook offers to **connect an Instagram account during
   creation, connect @h15master** — that link is discovery gold.
   (Personal profiles cannot carry reels via API.)
2. **Assign assets** — https://business.facebook.com/settings →
   Users → System users → "Fortnite System User" → Add assets →
   * Pages: the new page, full control;
   * Instagram accounts: @h15master, full control (if not already).
3. **Generate the permanent token** — still on the system user →
   Generate token, permissions: `instagram_basic`,
   `instagram_content_publish`, `pages_show_list`,
   `pages_manage_posts`, `pages_read_engagement`,
   `business_management` → copy (starts with EAAG…) → **paste it to
   the bot in chat** (or set secret `FB_ACCESS_TOKEN` yourself:
   repo Settings → Secrets and variables → Actions). System-user
   tokens NEVER expire.

With `business_management` on the pasted token, ONE paste arms BOTH
platforms: discovery walks the business's Instagram accounts,
matches `@h15master`, verifies the id queryable, and records the
page — reels start the same hour, no code changes, no numeric ids
to hunt.

Auto-discovery (upgraded 2026-11-15): every hourly run re-probes
`/me/accounts` AND `me/businesses → owned_instagram_accounts` with
whatever tokens exist — the moment a token can see a Page (+ its
linked IG account, or the business-owned @h15master), the ids are
recorded in `state.crosspost_ids` and reels start. Candidates the
token cannot query are never armed; until then each unarmed
platform is cleanly OFF (YouTube keeps publishing alone — never an
error).

Limits & rules baked in:

* Instagram Content Publishing API: 100 posts / 24 h moving window
  (we use 4/day) — the run logs the quota config each pass.
* Facebook reels: **3–90 s hard cap** → `clip.target_len_s` is now
  [45, 90] so every Short fits ALL platforms; the 4 legacy banked
  clips over 90 s still ship to YouTube + Instagram and skip only
  the FB reel (logged in state, no alert spam).
* Reel captions = the YouTube hook (#{n} bookkeeping tag dropped,
  Fortnite rule enforced) + CREATOR CODE H15 + MAP CODE + the
  crosspost.hashtags set.
* Token health is checked every run; a dead token alerts once per
  day and pauses that platform instead of burning retries.

## Phase 2 — DONE (owner rule 2026-11-15)

Multi-platform distribution as originally parked (2026-11-13):
post the same Shorts to OTHER platforms once the machine is proven.
**Shipped 2026-11-15** — see section 8 above: the reels cross-poster
reuses the cached session media (no re-downloads, no re-renders),
keeps YouTube untouched, and self-arms the moment the Meta-side ids
land.
