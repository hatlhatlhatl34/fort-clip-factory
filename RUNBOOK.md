# RUNBOOK — fort-clip-factory setup (30 minutes, one time)

The machine clips REAL Fortnite map gameplay videos from YouTube into
vertical Shorts that carry **map code 7992-0068-2838** (top bar, every
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

* `monthly-bank-harvest.yml` fires on the 1st (**00:29 UTC**, owner
  rule 2026-11-18 — BEFORE the day-1 08:33 upload session, so the
  month's very first slots are served by the fresh harvest) and its
  stacking guards retry daily at **12:43 + 22:43 UTC** until the
  month is provisioned — "try again and again until the gate opens".
  The 22:43 second session halves the provisioning time: the whole
  124-clip month is ready within the first ~3 days (GitHub's 6-hour
  job cap makes a literally-one-day harvest impossible — "beginning
  of the month" = the first days, every month).
* **OWNER RULE (2026-11-18) — no old bank clips, ever:** a leftover
  bank from a PREVIOUS month is POISON for the algorithm. At month
  rollover the daily publish path **retires** it
  (`src/bank.py` `stale()`/`retire()`: clips discarded, pointer
  unlinked) and the live ladder feeds the grid until the fresh
  month's harvest provisions the new bank. The `daily-factory.yml`
  bank check also refuses to even fetch a previous-month artifact,
  and the harvest never plans clips past its own month boundary.
* **OWNER RULE (2026-11-18) — harvest ALL videos:** the monthly
  candidate pool is a FULL sweep over every query phrasing (16
  queries x 50 results, ~1,600 quota units once a month), cached in
  `state.bank.pool` — the stacking sessions reuse the SAME pool at
  zero quota cost, and the pool dies with the bank at month
  rollover, so every month starts from a fully fresh sweep. The
  harvest downloads only a ~5-minute **slice** of each source
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
08:33 UTC cron (the ONLY daily wake-up; :33 = off-minute — GitHub's
    :00/:30 crons are congested and skipped; the old 08:30 fired as
    late as 17:46 UTC on 2026-11-17) -> gate RUN -> ONE session:
    pops up to 4 banked clips (or live-ladder fallback) ->
    restamps each with the CURRENT map/creator code bars +
    rebuilds each description from the CURRENT channel.yaml
    (publish-time truth — a code switch applies to the whole
    remaining bank) -> uploads all 4 at once -> publishAt
    12:00+18:00 Cairo today, 00:00+06:00 Cairo tomorrow (one Short
    every 6 hours) -> caches each final MP4 for the cross-poster
crosspost-hour cron (:23 past every hour, OWN concurrency group —
    2026-11-18 split) -> --due reels/TikToks minutes after each grid
    slot + --backfill at 4/run; NEVER queues behind the bank harvest
hourly-heal cron (:17, group clip-factory) -> the factory part only
    RUNs when a slot would expire before the next 08:33 UTC session
    -> then heals every hole at once
watchdog 6x/day -> oEmbed verify; Issue if a Short is late
monthly harvest (1st at 11:29 + daily guard at 12:43 UTC, off-minutes)
    -> banks ~124 clips for the month
```

**2026-11-18 post-mortem — why crosspost is its own workflow**: the
harvest guard (old cron 13:00, delayed by GitHub to 21:38 UTC) held
the shared `clip-factory` group through the 22:00 UTC grid slot;
every hourly heal queued behind the 5-hour job; short #30 sat live
on YouTube with ZERO reels for hours. Reel posting is time-critical:
it now runs in `crosspost-hour.yml` (group `crosspost`) and can never
serialize behind a harvest or factory session. Because the crosspost
job can now run BESIDE a harvest, the harvest's memory commit unions
through `tools/merge_state.py` like every other writer (its old
simple commit would have silently lost the bank state on a push
race).

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
* codes: `7992-0068-2838` top bar + `H15` footer, first frame to
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

**Backfill — the feeds must look alike (owner rule 2026-11-15).**
Live quota probe (2026-11-15): Instagram `content_publishing_limit`
returns an EMPTY config for this account and Facebook documents no
Reels publishing cap — so the whole already-public YouTube catalogue
is mirrored to both platforms, oldest first, at
`crosspost.backfill.per_hour` shorts per hourly heal run (default 4 —
~20 legacy Shorts spread over ~5 hours). Legacy Shorts that predate
the media cache are downloaded back from their own public
`youtube.com/shorts/<id>` URL with yt-dlp (no cookies — our own
public videos). A download that keeps failing burns the normal
attempt budget (8), then gives up + alerts once.

**Cross-links everywhere (owner rule 2026-11-15).**
* YouTube descriptions: a "WATCH EVERYWHERE" block (IG + FB profile
  links) is baked into every new upload; once a Short's reels are
  live the heal APPENDS "SEE THIS SHORT ON" with the two per-video
  reel permalinks (videos.update, 51 quota units, idempotent,
  retried hourly until it sticks).
* Instagram reel captions: the YouTube twin link + the Facebook page.
* Facebook reel descriptions: the YouTube twin link + the Instagram
  profile.
Links are configured in `channel.yaml` under `crosspost.links`.

**Four live-diagnosed traps (2026-11-15, worth remembering):**
* GitHub artifact downloads 303 to a signed Azure blob URL; urllib
  auto-follows redirects AND forwards the Authorization header — the
  blob host answers 401. `src/crosspost.py` now follows redirects by
  hand and drops the auth header when leaving api.github.com.
* The factory job and the crosspost job run on separate runners; a
  plain `git push` from crosspost can lose the race to the factory's
  memory commit (the armed ids were silently reverted once, 13:20
  UTC). Worse (17:42 UTC): `git pull --rebase --autostash` popped
  into a state.json conflict, `|| true` swallowed the dead commit,
  and the run's reels vanished from state — the next run re-posted
  two Shorts as DUPLICATE reels on both platforms. Memory commits now
  NEVER line-merge state.json: they take origin's freshest state and
  union this job's fields through `tools/merge_state.py`
  (status-rank merge, direction-aware), and the crosspost job pulls
  origin/main before reading state. Post-mortem tested in
  tests/test_merge_state.py.
* Instagram's transcoders REJECT 4:4:4 chroma — the render used to
  emit yuv444p, which YouTube takes happily but Meta answers with
  "Video Transcoding Error: both HD and SD progressive failed".
  The render now pins `-pix_fmt yuv420p`, and any pre-fix file is
  re-encoded to yuv420p and retried once inside the same attempt.
* YouTube bot-gates anonymous datacenter IPs ("Sign in to confirm
  you're not a bot") — the backfill downloads ride the SAME cookie
  pool (COOKIES_TXT/2/3) as the factory. Shorts deleted from
  YouTube are settled as GONE immediately instead of burning
  retries.
* After ~15 backfill downloads in 2 h YouTube starts serving
  STORYBOARD-ONLY formats even with cookies ("Requested format is
  not available", live-diagnosed 2026-11-15 18:06) — account-level
  throttling. The ladder's tail now also tries the cookie-less
  clients (android_vr / tv_simply / ios — different throttle pools),
  and a full wall trips a 3 h cooldown (`state.backfill_cooldown_until`,
  `crosspost.backfill.cooldown_s`) that preserves the retry budget.
  The daily grid Shorts are unaffected: their reels use the session
  media cache, no YouTube download involved.

**Dedupe gate (owner rule 2026-11-15, post-dupe):** before ANY publish
attempt `_attempt_reel` scans the platform's newest reels for the
`shorts/<video_id>` link every caption carries (`_existing_reel`). A
hit means ADOPT — status flips to done with the live media id, no
re-publish. This makes duplicate reels structurally impossible, even
when state races, rupload 500s still publish, or ids get re-armed.
Live cleanup note: 3 orphan duplicates from the 2026-11-15 races
(#69/#11/#19 twins) stay visible on Instagram until the system-user
token gains `instagram_manage_content` (neither the publish nor page
token can DELETE IG media — graph answers `#10 Insufficient
permissions`); deleting them by hand in the app takes ~10 s each.

**crosspost-now.yml — the stateless one-shot finisher (2026-11-15):**
manual `workflow_dispatch` that publishes due + backfill reels on a
free runner while a long harvest holds the `clip-factory` group. It
carries NO concurrency group and NO memory commit — the ephemeral
state dies with the runner, so it can never race another writer; the
next grouped heal ADOPTS whatever went live (dedupe gate + backfill
fast-path) into the ledger without re-posting. This finished the
full 17-video mirror the same evening the throttled-IP sandbox could
not.

## 9. TIKTOK MIRROR — the fourth platform (owner rule 2026-11-18)

The exact MP4 YouTube publishes now also becomes a TikTok video on
**@h15master** — same file, same title (cross-links per the owner
rule; the TikTok title carries the YouTube twin + IG + FB links,
never its own). The mirror rides the SAME machinery as the reels:
armed by secrets, attempted by the hourly heal + backfill, retried
up to `crosspost.max_attempts`, failures isolated from YouTube
forever.

### The one-time setup (DONE 2026-11-18 — keep for re-auth)

The TikTok developer app is **sandboxed + unaudited**. Two hard
rules came out of the live authorization battle:

1. **The authorize page only accepts the SANDBOX client key**
   (the `sb...` one — the production key gets rejected with a
   misleading "client_key" error until the app passes review).
2. **Authorization works only for the sandbox target user** —
   @h15master (added under Sandbox -> Target users).

The login chain: **https://hatlhatlhatl34.github.io/go-sb.html**
(GitHub Pages, the app's verified domain) while logged into TikTok
as @h15master -> approve -> paste the full callback URL to the setup
assistant -> the code is exchanged server-side (codes expire in
~5 minutes — exchange immediately, never stockpile).

### Secrets on this repo

| secret | what |
|---|---|
| `TIKTOK_CLIENT_KEY` | sandbox client key (`sb...`) |
| `TIKTOK_CLIENT_SECRET` | sandbox client secret |
| `TIKTOK_REFRESH_TOKEN` | the rolling 365-day refresh token |
| `TIKTOK_ACCESS_TOKEN` | the 24 h access token (auto-swapped daily) |
| `PAT_SET_SECRETS` | classic PAT with repo scope — used ONLY by the refresh workflow to write the token secrets (the runner GITHUB_TOKEN cannot) |

`tiktok-token-refresh.yml` runs daily 03:07 UTC (off-minute): refresh ->
verify against /v2/user/info/ -> swap both secrets (a rotated refresh
token is written back the same run). A refresh failure files an issue;
the mirror keeps running on the previous token for up to 24 h, then
pauses itself cleanly (never breaks IG/FB/YouTube).

### Unaudited reality (until the Content Posting API audit passes)

- every post is **SELF_ONLY (private)** — TikTok enforces it; the
  flip to `PUBLIC_TO_EVERYONE` is one line in channel.yaml
  (`crosspost.tiktok.privacy`) AFTER approval, nothing else changes;
- **@h15master must stay a private account** in the app (TikTok
  couples unaudited direct-post to private accounts). LIVE PROOF
  2026-11-18: the account was switched PUBLIC and every init
  flipped to HTTP 403
  `unaudited_client_can_only_post_to_private_accounts` (shorts
  #7-#10 burned failures on it). The mirror now treats that exact
  error as a **PAUSE, not a failure**: no attempt burned, no alert,
  no give_up — the short stays pending with `tt_paused: true` and
  every hourly pass re-probes; the hour the account is private
  again (or the audit passes) posting auto-resumes. To see views
  on already-posted SELF_ONLY videos in the meantime, flip them
  one-by-one in the app AND keep the account public — but accept
  that the mirror pauses while it is. The permanent fix is the
  audit (below);
- the re-review description (prepared, 1000-char, full-scope
  explanation) goes in with the sandbox's Products: Login Kit +
  Content Posting API — remove **Share Kit** and **Data Portability
  API** before submitting (unused products slow reviews);
- when review passes, import the sandbox config into a Production
  draft, THEN rotate the secrets to the production key/secret +
  re-authorize once (the production key starts working at that
  point, and only then).

### Operational cautions

- **No adoption**: the app does not hold the `video.list` scope, so
  an already-live TikTok twin cannot be discovered from TikTok's
  side. State marks are the ONLY dedupe gate — never run the
  stateless `crosspost-now` twice in a row for TikTok without a
  grouped heal committing state in between (IG/FB adopt; TikTok
  cannot). Poll-timeouts are SAFE: the publish_id is stored and
  re-checked before any re-post (no duplicates by construction).
- **6 publish-API requests/min per token** — posts are paced and
  status polls run at 20 s cadence; a 4-Short slot mirrors in ~2-3
  minutes.
- **FILE_UPLOAD is single-chunk only** (live init-matrix 2026-11-18):
  the API rejects `total_chunk_count > 1` ("The total chunk count is
  invalid") AND single chunks over 64 MB ("The chunk size is
  invalid"); it also REQUIRES `total_chunk_count` even though the
  docs mark it optional (omitting it -> "The video info is empty").
  `src/tiktok.py` therefore always declares exactly one chunk and
  re-encodes a >64 MB file down to fit (rare — 720p Shorts run
  8-40 MB).
- **merge_state must know every platform** (post-mortem 2026-11-18
  17:47): `tools/merge_state.py`'s PLATFORMS lacked tiktok, so every
  crosspost commit silently dropped the freshly-written tiktok marks
  (the extra-keys branch lets BASE win) — 4 videos went live while
  state kept saying 'failed', and the next hourly heal would have
  re-posted them as duplicates. PLATFORMS now covers tiktok with the
  same status-rank merge; regression lives in
  `tests/test_merge_state.py`; the state itself was repaired by
  hand the same hour (publish_ids restored from the run log).
- **Publish-time code truth** (2026-11-18): banked clips are
  finished renders — clips harvested before the map-code switch
  would have shipped the OLD code burned-in + old description for
  weeks. Every banked publish now (a) re-burns the code bars with
  the CURRENT channel.yaml codes (`transform.restamp_current`,
  fully-opaque cover, idempotent, never blocks a publish) and
  (b) rebuilds the description from the CURRENT config
  (`metadata.refresh_description`). Changing `codes.map_code` in
  channel.yaml therefore applies to the whole remaining bank the
  very next day. Tests: `tests/test_daily_session.py`.
- Egypt is not a TikTok Creator Rewards region — TikTok stays a
  pure traffic funnel to the YouTube/FB/IG feeds; the money engine
  is unchanged.

## Phase 2 — DONE (owner rule 2026-11-15)

Multi-platform distribution as originally parked (2026-11-13):
post the same Shorts to OTHER platforms once the machine is proven.
**Shipped 2026-11-15** — see section 8 above: the reels cross-poster
reuses the cached session media (no re-downloads, no re-renders),
keeps YouTube untouched, and self-arms the moment the Meta-side ids
land.

## Phase 3 — PARKED (owner assets pending, 2026-11-15)

The owner named the next two upgrades; both wait on assets only the
owner can supply. No code exists for them yet by design.

1. **TikTok as a fourth platform** — **SHIPPED 2026-11-18**, see
   section 9 above: the same MP4 mirrors to @h15master through the
   sandbox (SELF_ONLY until the audit), tokens auto-refresh daily.
2. **Multiple map codes** — the owner will hand over NEW map codes
   to rotate through videos (not only 7992-0068-2838). Design when
   they arrive: a `codes.pool` list in channel.yaml; each render
   picks one (round-robin with no-repeat), the overlay burns it into
   every frame, and the description/caption carries the same code as
   the overlay. History per code is tracked in state so analytics
   stay attributable.


---

## 2026-11-18 — the old bank purge + the full-repo audit

### OWNER RULES (2026-11-18)
- **#2 SURPLUS = EMERGENCY BUFFER** — the monthly harvest may stack past
  the month's open slots (124 banked vs ~120 consumed). The surplus stays
  banked as the month's emergency buffer, consumed by heal/topups; it is
  NEVER carried across months (the rollover retirement wipes it —
  old-month clips are poison).
- **#3 THE FIRST OLD BANK IS DEAD** — the pre-overhaul bank (random
  Fortnite footage, cross-month slots) was completely deleted
  2026-11-18: all bank-2026-11 artifacts (~13.3 GB),
  data/bank_pointer.json and state.bank. A bank only ever holds
  same-month fresh-sweep niche clips; the harvest sweeps unreferenced
  bank/ files before re-uploading parts, so dead weight can never ride
  again.

### The audit (every file, from scratch) — 2026-11-18
Four parallel auditors + coordinator verification; 30+ real bugs found
and fixed (full detail lives in code comments marked `audit 2026-11-18`):
- **Duplicate-post killers**: crosspost-now no longer arms TikTok
  (stateless + no adoption scope = guaranteed duplicate); TikTok
  publish_id is persisted BEFORE the upload; the adoption re-check
  fails CLOSED; the media-zip cache is keyed by (run_id, artifact).
- **Memory-race killers**: daily-factory / hourly-heal /
  monthly-harvest / crosspost-hour commits are rebuilt on fresh
  origin/main (reset --soft) with 3 push retries and a LOUD final
  failure — a session's whole memory can never be silently swallowed
  by `git push || true` again (the pointer-stranding race was observed
  live).
- **Grid protectors**: the daily render block is guarded per-source; a
  missing bank clip is EVICTED (no month-long bank jam); a drained
  bank keeps its provision meter (topup, not full re-provision); the
  month-rollover poison check runs per target; the bookable window is
  ONE rule in gate.py + open_targets (channel.yaml
  bookable_margin_minutes); quota/slots/used_sources union
  monotonically in merge_state.
- **TikTok token arm**: the rotated 365-day refresh root is persisted
  FIRST (a crash can no longer strand a dead root); mid-run token
  expiry is a wait-state (no burned attempts).
- **Quality**: motion-floor picker fixed (the first window was
  auto-accepted); post-render duration gate; HttpOnly cookie rows
  kept; jar rotation seeded per session; scene analysis downscaled;
  bass locked to the chord clock; ingest drops survive --dry-run; the
  download quality gate fails CLOSED.

### Machine state after the purge
- bank: month=None, clips=[], harvested=0 — the next guard re-provisions
  November from a FRESH sweep (niche queries, this month's slots only).
- 4 old-bank clips were uploaded by the 08:33 session minutes before
  the deletion (n=31/32/33/51, publishAt Nov 18-19 slots); they can be
  deleted on request via videos.delete.
