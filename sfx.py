"""Sound effects for animated tutorial slides, synthesized here (no sample files, nothing to license).

The slide's animation script lists its cues ([{t, k, d}] = time, kind, duration; see slides.MOTION_JS):
pop (a part appears), pop2 (a logo), type (code typing), whoosh (a dot travels an arrow), blip (it arrives),
tick / tick2 (quiz countdown), correct (quiz answer), focus (a code line is spotlighted).
They are mixed quietly under the narration of that scene.
"""

from __future__ import annotations

import numpy as np


def _t(sec: float, sr: int) -> np.ndarray:
    return np.arange(int(sec * sr)) / sr


def _band_noise(n: int, sr: int, lo: float, hi: float, rng: np.random.Generator) -> np.ndarray:
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / sr)
    spec[(f < lo) | (f > hi)] = 0
    x = np.fft.irfft(spec, n)
    return x / (np.max(np.abs(x)) + 1e-9)


def _pop(sr: int, f0: float = 720, f1: float = 340, amp: float = 0.2, dur: float = 0.09) -> np.ndarray:
    t = _t(dur, sr)
    freq = f1 + (f0 - f1) * np.exp(-t / 0.018)
    phase = 2 * np.pi * np.cumsum(freq) / sr
    env = np.minimum(1, t / 0.002) * np.exp(-t / 0.03)
    return amp * np.sin(phase) * env


def _bell(sr: int, f: float, amp: float, dur: float = 0.9, tau: float = 0.32) -> np.ndarray:
    t = _t(dur, sr)
    env = np.minimum(1, t / 0.003) * np.exp(-t / tau)
    tone = np.sin(2 * np.pi * f * t) + 0.35 * np.sin(2 * np.pi * 2.0 * f * t) + 0.12 * np.sin(2 * np.pi * 3.01 * f * t)
    return amp * tone * env / 1.47


def _tick(sr: int, f: float, amp: float) -> np.ndarray:
    t = _t(0.05, sr)
    env = np.exp(-t / 0.007)
    return amp * (np.sin(2 * np.pi * f * t) + 0.5 * np.sin(2 * np.pi * 2.02 * f * t)) * env / 1.5


def _effect(kind: str, dur: float, sr: int, rng: np.random.Generator) -> np.ndarray:
    if kind == "pop":
        return _pop(sr, 700 + rng.uniform(-60, 60), 330, 0.16)
    if kind == "pop2":  # logo: a deeper pop plus a little sparkle
        out = np.zeros(int(0.45 * sr))
        p = _pop(sr, 520, 230, 0.24, 0.12)
        out[:len(p)] += p
        for k, f in enumerate((2637, 3520)):
            b = _bell(sr, f, 0.045, 0.35, 0.09)
            s = int((0.06 + 0.05 * k) * sr)
            out[s:s + len(b)] += b[:len(out) - s]
        return out
    if kind == "type":  # keyboard clicks, irregular like real typing
        out = np.zeros(int(max(0.05, dur) * sr) + int(0.02 * sr))
        pos = 0.0
        while pos < dur:
            n = int(0.006 * sr)
            click = _band_noise(n, sr, 1800, 7000, rng) * np.exp(-_t(0.006, sr) / 0.0018)
            click += 0.4 * np.sin(2 * np.pi * rng.uniform(2600, 3600) * _t(0.006, sr)) * np.exp(-_t(0.006, sr) / 0.002)
            s = int(pos * sr)
            out[s:s + n] += rng.uniform(0.035, 0.065) * click[:len(out) - s]
            pos += rng.uniform(0.055, 0.1)
        return out
    if kind == "whoosh":  # filtered noise swelling and fading while the dot travels
        d = max(0.3, dur)
        n = int(d * sr)
        x = _band_noise(n, sr, 350, 2600, rng)
        u = np.linspace(0, 1, n)
        return 0.075 * x * np.sin(np.pi * u) ** 2
    if kind == "blip":
        t = _t(0.08, sr)
        env = np.minimum(1, t / 0.002) * np.exp(-t / 0.025)
        return 0.09 * (np.sin(2 * np.pi * 1320 * t) + 0.3 * np.sin(2 * np.pi * 1980 * t)) * env
    if kind == "tick":
        return _tick(sr, 1900, 0.2)
    if kind == "tick2":
        return _tick(sr, 2500, 0.24)
    if kind == "correct":  # two-note chime
        out = np.zeros(int(1.2 * sr))
        for k, f in enumerate((1318.5, 1760.0)):
            b = _bell(sr, f, 0.2)
            s = int(0.11 * k * sr)
            out[s:s + len(b)] += b[:len(out) - s]
        return out
    if kind == "focus":  # soft rising swish
        n = int(0.22 * sr)
        u = np.linspace(0, 1, n)
        t = _t(0.22, sr)
        tone = np.sin(2 * np.pi * np.cumsum(420 + 260 * u) / sr)
        return (0.035 * _band_noise(n, sr, 900, 4000, rng) + 0.04 * tone) * np.sin(np.pi * u) ** 1.5 * (t >= 0)
    return np.zeros(0)


def render(events: list[dict], sr: int, length: int, seed: int = 7) -> np.ndarray:
    """A mono float32 track of `length` samples with every cue placed at its time."""
    out = np.zeros(length, dtype=np.float64)
    seen = set()
    for k, ev in enumerate(sorted(events, key=lambda e: e.get("t", 0))):
        kind, t = str(ev.get("k", "")), float(ev.get("t", 0))
        key = (kind, round(t, 2))
        if key in seen or t < 0:  # the same cue twice at the same moment would just be louder
            continue
        seen.add(key)
        x = _effect(kind, float(ev.get("d") or 0), sr, np.random.default_rng(seed + k))
        s = int(t * sr)
        if s >= length or not len(x):
            continue
        out[s:s + len(x)] += x[:length - s]
    return out.astype(np.float32)
