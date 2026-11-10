# SAMPLE SHORT — what the machine publishes

**File:** `sample-short.mp4` — rendered by the exact production
pipeline (`main.py --sample`): the clip engine picks the best 20-35 s
motion window, transforms it to 1080×1920 vertical (1.08× zoom
crop), burns the map-code top bar + creator-code footer into EVERY
frame, discards original audio, and lays a numpy-synthesized
soundtrack. NOTE: the shipped sample carries TEST-pattern footage
(this sandbox's IP was rate-limited by every video source on
delivery day — see the delivery README); in production the same
pipeline runs on real downloaded Fortnite gameplay, and you can
regenerate the sample from real footage anytime via
`python main.py --sample out.mp4` with a video in `ingest/`.

| Property | Value |
|---|---|
| Resolution | 1080 × 1920 (9:16) |
| Frame rate | 30 fps |
| Length | 20-35 s (clip-engine window) |
| Video | H.264 (yuv420p, CRF 20, faststart) |
| Audio | AAC 160 kbps — synthesized, zero copyright |
| Map code | 1789-8528-6429 — top bar, first frame → last |
| Creator code | H15 — footer, full duration |
| Title (example) | `1789-8528-6429 🔥 CRAZY Fortnite Map Clips #1` |
| Disclosure | none (real gameplay, not AI-generated) |

The published title/description (see `sample-metadata.txt`) carries
both codes plus honest credit to the source video.
