"""Background music library: free CC-BY tracks plus anything you drop into music/."""
from __future__ import annotations

import random
from pathlib import Path
from urllib.parse import quote

from .util import log, read_json, write_json

AUDIO_EXT = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus"}
BASE = "https://incompetech.com/music/royalty-free/mp3-royaltyfree/"
LICENSE = "Creative Commons: By Attribution 4.0 License (http://creativecommons.org/licenses/by/4.0/)"

# Kevin MacLeod (incompetech.com), CC BY 4.0. Missing tracks are skipped.
# Moods: hype = upbeat/energetic (wins, clutches), funny = goofy/bouncy (fails, banter),
# chill = relaxed (vibes, chatting), tense = suspense only (horror, stealth, close calls).
CATALOG = [
    ("Hyperfun", "hype"), ("Blip Stream", "hype"), ("Funkorama", "hype"), ("Galactic Rap", "hype"),
    ("Monkeys Spinning Monkeys", "funny"), ("Fluffing a Duck", "funny"), ("Sneaky Snitch", "funny"),
    ("Pixel Peeker Polka - faster", "funny"), ("Itty Bitty 8 Bit", "funny"), ("Run Amok", "funny"),
    ("Local Forecast - Elevator", "chill"), ("Wallpaper", "chill"), ("Cool Vibes", "chill"),
    ("Carefree", "chill"),
    ("Investigations", "tense"), ("Cipher", "tense"),
]
MOOD_OF = {t: m for t, m in CATALOG}
RETIRED = {"Volatile Reaction", "Hitman", "Five Armies", "Chill Wave"}  # too intense / off-mood


def _lib_path(cfg: dict) -> Path:
    return Path(cfg["paths"]["music"]) / "library.json"


_FETCHED = False


def fetch(cfg: dict) -> int:
    global _FETCHED
    _FETCHED = True
    import requests
    mdir = Path(cfg["paths"]["music"])
    mdir.mkdir(parents=True, exist_ok=True)
    lib = read_json(_lib_path(cfg)) if _lib_path(cfg).exists() else {"tracks": []}
    have = {t["file"] for t in lib["tracks"]}
    got = 0
    for title, mood in CATALOG:
        fname = f"{title}.mp3"
        if fname in have and (mdir / fname).exists():
            continue
        try:
            r = requests.get(BASE + quote(fname), timeout=60,
                             headers={"User-Agent": "stream-clipper/1.0"})
            if r.status_code != 200 or len(r.content) < 50_000:
                log(f"  music: '{title}' unavailable (HTTP {r.status_code}), skipped")
                continue
            (mdir / fname).write_bytes(r.content)
        except Exception as e:
            log(f"  music: '{title}' failed ({type(e).__name__}), skipped")
            continue
        lib["tracks"].append({
            "file": fname, "title": title, "mood": mood, "artist": "Kevin MacLeod",
            "credit": f'"{title}" Kevin MacLeod (incompetech.com)\nLicensed under {LICENSE}',
        })
        got += 1
        log(f"  music: downloaded '{title}' ({mood})")
    write_json(_lib_path(cfg), lib)
    return got


def library(cfg: dict) -> list[dict]:
    mdir = Path(cfg["paths"]["music"])
    mdir.mkdir(parents=True, exist_ok=True)
    known = read_json(_lib_path(cfg))["tracks"] if _lib_path(cfg).exists() else []
    known = [{**t, "mood": MOOD_OF.get(t["title"], t["mood"])} for t in known
             if (mdir / t["file"]).exists() and t["title"] not in RETIRED]
    names = {t["file"] for t in known}
    # Your own tracks: put them in music/<mood>/ (hype, funny, tense, chill) or directly in music/.
    for p in mdir.rglob("*"):
        if p.suffix.lower() in AUDIO_EXT:
            rel = p.relative_to(mdir).as_posix()
            if rel not in names and p.stem not in RETIRED:
                mood = p.parent.name.lower() if p.parent != mdir else "any"
                known.append({"file": rel, "title": p.stem, "mood": mood, "credit": ""})
    return known


def choose(cfg: dict, mood: str, seed: int) -> dict | None:
    """Pick a track of exactly this mood. No match -> no music (never a random intense track)."""
    if mood == "none" or not cfg["music"].get("enabled"):
        return None
    tracks = library(cfg)
    if not [t for t in tracks if t["mood"] == mood] and cfg["music"].get("auto_fetch") and not _FETCHED:
        fetch(cfg)
        tracks = library(cfg)
    pool = [t for t in tracks if t["mood"] == mood] or [t for t in tracks if t["mood"] == "any"]
    if not pool:
        log(f"   music: no '{mood}' track available (add some to music/{mood}/); no music for this clip")
        return None
    t = random.Random(seed).choice(pool)
    return {**t, "path": str(Path(cfg["paths"]["music"]) / t["file"])}
