"""Copyright-free gaming music, synthesized from scratch with numpy.

Ported from forever-factory's proven synth (60+ episodes, zero music
copyright strikes) with one new mood: ENERGENTIC — a driving 128 BPM
bed for Fortnite clips (the original's calm beds fit bedtime stories,
not gameplay).

Layers:
  * saw-ish detuned bass line — the engine, eighth-note pulses
  * bright arpeggios — the sparkle riding the chords
  * kick + hi-hat — a real beat, synthesized (sine-drop kick,
    filtered-noise hats)
  * minor progressions — gaming tension (Am F C G family)

Everything is seeded: an original track per clip, nothing licensed,
nothing to expire, nothing to claim.
"""
from __future__ import annotations

import wave

import numpy as np

SR = 44_100
BPM = 128

# energetic: minor-key progressions (Hz voicings, root ~110-220)
CHORDS: dict[str, list[tuple[float, ...]]] = {
    "energetic": [
        (110.00, 220.00, 261.63, 329.63),   # Am
        ( 87.31, 174.61, 220.00, 261.63),   # F
        (130.81, 261.63, 329.63, 392.00),   # C
        ( 98.00, 196.00, 246.94, 293.66),   # G
        (110.00, 220.00, 261.63, 349.23),   # Am(add)
        ( 87.31, 174.61, 261.63, 329.63),   # Fmaj
        (123.47, 246.94, 293.66, 369.99),   # Dm
        ( 98.00, 196.00, 293.66, 392.00),   # Gsus
    ],
    "warm": [
        (174.61, 220.00, 261.63, 329.63),
        (130.81, 196.00, 246.94, 293.66),
        (146.83, 220.00, 261.63, 349.23),
        (164.81, 207.65, 246.94, 311.13),
    ],
}


def _moving_average(x: np.ndarray, k: int) -> np.ndarray:
    if k <= 1:
        return x
    cs = np.cumsum(np.insert(x.astype(np.float32), 0, np.float32(0)),
                   dtype=np.float32)
    out = (cs[k:] - cs[:-k]) / k
    pad = np.full(k - 1, out[0], dtype=np.float32)
    return np.concatenate([pad, out])


def _saw(f: float, t: np.ndarray) -> np.ndarray:
    return (2.0 * ((f * t) % 1.0) - 1.0).astype(np.float32)


def _pluck(f: float, dur: float, sr: int) -> np.ndarray:
    t = np.arange(int(dur * sr), dtype=np.float32) / sr
    tone = np.sin(2 * np.pi * f * t) * np.exp(-t / 0.35)
    tone += 0.30 * np.sin(2 * np.pi * f * 2 * t) * np.exp(-t / 0.18)
    return tone.astype(np.float32)


def _kick(sr: int) -> np.ndarray:
    n = int(0.16 * sr)
    t = np.arange(n, dtype=np.float32) / sr
    f = 150.0 * np.exp(-t * 26.0) + 42.0
    return (np.sin(2 * np.pi * np.cumsum(f) / sr) * np.exp(-t / 0.055)
            ).astype(np.float32)


def _hat(sr: int, rng) -> np.ndarray:
    n = int(0.05 * sr)
    noise = rng.standard_normal(n).astype(np.float32)
    noise = _moving_average(noise, 2)          # brighten: mild LP only
    env = np.exp(-np.arange(n, dtype=np.float32) / (0.012 * sr))
    return (noise * env * 0.5).astype(np.float32)


def synth_music(seed: int, duration: float, sr: int = SR,
                mood: str = "energetic") -> np.ndarray:
    """Mono float32 mix, RMS-normalized then peak-capped at 0.6."""
    rng = np.random.default_rng(seed)
    n = int(duration * sr)
    beat = 60.0 / BPM
    chords = CHORDS.get(mood, CHORDS["energetic"])
    chord_len = 2 * beat                     # one chord per 2 beats

    # ── bass: eighth-note pulses on the chord root ─────────────────
    bass = np.zeros(n, dtype=np.float32)
    eighth = beat / 2
    i = 0
    t_pos = 0.0
    while t_pos < duration:
        root = chords[i % len(chords)][0]
        dur_n = min(int(eighth * 0.92 * sr), n - int(t_pos * sr))
        if dur_n > 0:
            t = np.arange(dur_n, dtype=np.float32) / sr
            seg = _saw(root, t) * 0.42
            seg *= (np.exp(-t / (eighth * 0.7))).astype(np.float32)
            # soft clipping for warmth
            seg = np.tanh(seg * 1.6) * 0.75
            s0 = int(t_pos * sr)
            bass[s0:s0 + dur_n] += seg
        t_pos += eighth
        i += 1
    bass = _moving_average(bass, int(0.004 * sr))

    # ── pads: chord beds under everything ───────────────────────────
    pad = np.zeros(n, dtype=np.float32)
    pos = 0
    ci = 0
    while pos < n:
        chord = chords[ci % len(chords)]
        seg_n = min(int(chord_len * sr), n - pos)
        seg_t = np.arange(seg_n, dtype=np.float32) / sr
        seg = np.zeros(seg_n, dtype=np.float32)
        for k, f in enumerate(chord[1:]):     # upper voices only
            det = 1.0 + 0.002 * np.sin(2 * np.pi * 0.07 * seg_t + k)
            w = np.sin(2 * np.pi * f * det * seg_t) * 0.30 / (k + 1)
            seg += w.astype(np.float32)
        env = np.ones(seg_n, dtype=np.float32)
        fade = min(seg_n, int(0.03 * sr))
        env[:fade] = np.linspace(0, 1, fade)
        pad[pos:pos + seg_n] += seg * env
        pos += int(chord_len * sr * 0.9)
        ci += 1

    # ── arpeggio: 16th-note plucks over the current chord ───────────
    arp = np.zeros(n, dtype=np.float32)
    sixteenth = beat / 4
    t_pos = 0.0
    i = 0
    while t_pos < duration:
        chord = chords[(i // 8) % len(chords)]
        f = float(chord[1 + (i % 3)]) * 2.0
        s0 = int(t_pos * sr)
        dur = rng.uniform(0.10, 0.22)
        seg_n = min(int(dur * sr), n - s0)
        if seg_n > 0:
            arp[s0:s0 + seg_n] += _pluck(f, dur, sr)[:seg_n] * 0.16
        t_pos += sixteenth
        i += 1

    # ── drums: kick on the beat, hats on the off-eighths ────────────
    drums = np.zeros(n, dtype=np.float32)
    kick = _kick(sr)
    hat = _hat(sr, rng)
    t_pos = 0.0
    bi = 0
    while t_pos < duration:
        s0 = int(t_pos * sr)
        # kick every beat
        kn = min(len(kick), n - s0)
        if kn > 0:
            drums[s0:s0 + kn] += kick[:kn] * 0.85
        # hat on the off-eighth
        h0 = int((t_pos + eighth) * sr)
        hn = min(len(hat), n - h0)
        if hn > 0:
            drums[h0:h0 + hn] += hat[:hn] * 0.45
        t_pos += beat
        bi += 1

    # ── master ──────────────────────────────────────────────────────
    master_env = np.ones(n, dtype=np.float32)
    fin = min(n, int(0.05 * sr))
    master_env[:fin] = np.linspace(0.9, 1.0, fin)
    fout = min(n, int(1.2 * sr))
    master_env[-fout:] = np.linspace(1, 0, fout)

    mono = bass * 0.95 + pad * 0.85 + arp + drums
    mono *= master_env
    rms = float(np.sqrt(np.mean(np.square(mono.astype(np.float64)))))
    if rms > 1e-6:
        mono *= np.float32(0.16 / rms)
    peak = float(np.max(np.abs(mono))) or 1.0
    if peak > 0.6:
        mono *= np.float32(0.6 / peak)
    return mono


def write_wav(path, mono: np.ndarray, sr: int = SR) -> None:
    """Float32 mono -> 16-bit stereo WAV (ffmpeg's favorite meal)."""
    stereo = np.repeat((np.clip(mono, -1, 1) * 32767)
                       .astype(np.int16)[:, None], 2, axis=1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(stereo.tobytes())


def make_track(path, duration: float, seed: int,
               mood: str = "energetic") -> None:
    """Synthesize + write the clip's soundtrack (duration + 1s pad)."""
    from .config import cfg
    mood = cfg()["music"].get("mood", mood)
    mono = synth_music(seed, duration + 1.0, mood=mood)
    write_wav(path, mono)
    print(f"[music] {mood} track seeded {seed % 10000:04d} "
          f"-> {path.name} ({duration:.0f}s)")
