"""Turn audio, chat and replay data into a per-second "hype" score and pick peaks."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import numpy as np

from .util import log, require_tool

SR = 8000  # analysis sample rate; plenty for loudness


def audio_loudness(audio: Path) -> np.ndarray:
    """Per-second RMS loudness in dB, streamed through ffmpeg so long VODs stay light."""
    require_tool("ffmpeg")
    cmd = ["ffmpeg", "-v", "error", "-i", str(audio), "-vn", "-ac", "1", "-ar", str(SR),
           "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    out, chunk = [], SR * 2  # bytes per second (int16)
    buf = b""
    while True:
        data = proc.stdout.read(chunk * 60)
        if not data:
            break
        buf += data
        n = len(buf) // chunk
        if n:
            arr = np.frombuffer(buf[: n * chunk], dtype=np.int16).astype(np.float32) / 32768
            rms = np.sqrt(np.mean(arr.reshape(n, SR) ** 2, axis=1) + 1e-10)
            out.append(20 * np.log10(rms))
            buf = buf[n * chunk:]
    proc.wait()
    return np.concatenate(out) if out else np.zeros(0, dtype=np.float32)


def _smooth(x: np.ndarray, win: int) -> np.ndarray:
    if win <= 1 or len(x) == 0:
        return x
    k = np.ones(win) / win
    return np.convolve(x, k, mode="same")


def _rolling_median(x: np.ndarray, win: int) -> np.ndarray:
    """Cheap rolling median using block medians (fine for multi-hour series)."""
    if len(x) == 0:
        return x
    step = max(1, win // 4)
    idx = np.arange(0, len(x), step)
    meds = np.array([np.median(x[max(0, i - win // 2): i + win // 2 + 1]) for i in idx])
    return np.interp(np.arange(len(x)), idx, meds)


def _z(x: np.ndarray) -> np.ndarray:
    if len(x) == 0:
        return x
    sd = x.std()
    return (x - x.mean()) / sd if sd > 1e-9 else np.zeros_like(x)


def _term_regex(terms: list[str]) -> re.Pattern:
    parts = []
    for t in terms:
        esc = re.escape(t)
        parts.append(rf"(?<!\w){esc}(?!\w)" if t[:1].isalnum() else esc)
    return re.compile("|".join(parts), re.IGNORECASE)


def build_scores(duration: float, loud: np.ndarray, chat: list[dict], heatmap: list[dict],
                 cfg: dict) -> dict[str, np.ndarray]:
    hc = cfg["highlights"]
    w = hc["weights"]
    n = int(max(duration, len(loud))) + 1
    comps: dict[str, np.ndarray] = {}

    # Audio: how much louder than the surrounding minute.
    a = np.full(n, np.nan, dtype=np.float32)
    a[: len(loud)] = loud
    a = np.nan_to_num(a, nan=float(np.nanmin(loud)) if len(loud) else -60.0)
    rel = _smooth(a, 3) - _rolling_median(a, 60)
    comps["audio"] = np.clip(_z(rel), -2, None)

    # Chat: message rate vs. the stream's own rolling baseline, shifted back by the reaction delay.
    if chat:
        delay = int(hc["chat_delay"])
        rate = np.zeros(n, dtype=np.float32)
        kw = np.zeros(n, dtype=np.float32)
        rx = _term_regex(hc["hype_terms"])
        for m in chat:
            i = int(m["t"]) - delay
            if 0 <= i < n:
                rate[i] += 1
                kw[i] += len(rx.findall(m.get("text", "")))
        r = _smooth(rate, 8)
        base = _rolling_median(r, 300) + 0.5
        comps["chat"] = np.clip(_z(np.log((r + 0.5) / base)), -2, None)
        comps["keywords"] = np.clip(_z(_smooth(kw, 8)), -2, None)

    # YouTube "most replayed" heatmap.
    if heatmap:
        hm = np.zeros(n, dtype=np.float32)
        for seg in heatmap:
            s, e = int(seg.get("start_time", 0)), int(seg.get("end_time", 0))
            hm[max(0, s): min(n, e + 1)] = float(seg.get("value", 0))
        comps["replay"] = np.clip(_z(hm), -2, None)

    total_w = sum(w.get(k, 0) for k in comps) or 1.0
    score = sum(comps[k] * w.get(k, 0) for k in comps) / total_w
    comps["score"] = _smooth(score, 5)
    return comps


def pick_peaks(score: np.ndarray, duration: float, cfg: dict) -> list[dict]:
    hc = cfg["highlights"]
    s = score.copy()
    gap = int(hc["min_gap"])
    s[: min(len(s), 30)] = -np.inf  # skip the stream intro / starting-soon screen
    picks = []
    mc = hc.get("max_candidates", "auto")
    if mc == "auto":  # longer streams get more candidates: about one per `minutes_per_candidate`
        mc = int(min(hc.get("candidate_cap", 60),
                     max(12, (duration or len(s)) / 60 / float(hc.get("minutes_per_candidate", 4)))))
    while len(picks) < int(mc):
        i = int(np.argmax(s))
        if not np.isfinite(s[i]) or s[i] < float(hc.get("min_score", 0.3)):
            break
        start = max(0.0, i - float(hc["pre_roll"]))
        end = min(duration or i + 60, i + float(hc["post_roll"]))
        picks.append({"peak": float(i), "start": start, "end": end, "score": float(score[i])})
        s[max(0, i - gap): i + gap + 1] = -np.inf
    log(f"Found {len(picks)} candidate moments.")
    return sorted(picks, key=lambda p: p["peak"])
