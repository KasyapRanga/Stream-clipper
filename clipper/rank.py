"""Clip selection: automatic (signals only) or written by the Antigravity agent."""
from __future__ import annotations

from pathlib import Path

from .util import log, read_json, write_json

MOODS = {"hype", "funny", "tense", "chill", "none"}
FUNNY = {"lul", "lulw", "kekw", "omegalul", "lmao", "icant", "xdd", "haha", "lmfao"}
TENSE = {"monkas", "aware"}
HYPE = {"pog", "poggers", "pogchamp", "pogu", "w", "ww", "holy", "noway", "clip", "ez", "kreygasm"}


def _guess_mood(c: dict) -> str:
    # top_tokens is ordered by frequency: the first emote family that appears wins.
    for tok in (c.get("chat") or {}).get("top_tokens", []):
        t = tok.lower().strip("!?.")
        if t in FUNNY:
            return "funny"
        if t in TENSE:
            return "tense"
        if t in HYPE:
            return "hype"
    return "none" if len(c.get("words") or []) > 25 else "chill"  # talky clips: let the voices carry it


def _guess_title(c: dict) -> str:
    from .english import clean_title, english_transcript
    inside = [w for w in c.get("words") or [] if c["start"] <= w["s"] <= c["end"]]
    words = [w["w"] for w in english_transcript(inside)]
    if not words:
        return clean_title("", _guess_mood(c), seed=hash(c.get("id", "")) & 0xFFFF)
    title = []
    for w in words:
        title.append(w)
        if w[-1:] in ".!?" and len(title) >= 3:
            break
        if len(title) >= 9:
            break
    return clean_title(" ".join(title).strip(" ,"), _guess_mood(c), seed=hash(c.get("id", "")) & 0xFFFF)


def auto_select(wdir: Path, cfg: dict) -> dict:
    data = read_json(wdir / "candidates.json")
    cands = sorted(data["candidates"], key=lambda c: c["score"], reverse=True)
    n = cfg["highlights"].get("clips", "auto")
    if n == "auto":  # every strong moment becomes a clip, not a fixed number
        floor = float(cfg["highlights"].get("auto_clip_score", 0.8))
        chosen = [c for c in cands if c["score"] >= floor] or cands[:3]
    else:
        chosen = cands[: int(n)]
    chosen = sorted(chosen, key=lambda c: c["start"])
    sel = {"clips": [{
        "candidate_id": c["id"], "start": c["start"], "end": c["end"],
        "title": _guess_title(c), "music_mood": _guess_mood(c),
        "reason": f"auto: score {c['score']:.2f}",
    } for c in chosen]}
    write_json(wdir / "selection.json", sel)
    log(f"Auto-selected {len(chosen)} clips -> {wdir / 'selection.json'}")
    return sel


def load_selection(wdir: Path, cfg: dict) -> list[dict]:
    path = wdir / "selection.json"
    if not path.exists():
        raise SystemExit(
            f"[clipper] No selection.json in {wdir}.\n"
            "  Ask Antigravity to run the /make-clips workflow (it reads candidates.md and writes "
            "selection.json), or run:  python -m clipper select <id> --auto"
        )
    data = read_json(path)
    clips = data.get("clips", data if isinstance(data, list) else [])
    meta = read_json(wdir / "meta.json")
    dur = float(meta.get("duration") or 1e9)
    hc = cfg["highlights"]
    good = []
    for i, c in enumerate(clips, 1):
        try:
            s, e = float(c["start"]), float(c["end"])
        except (KeyError, TypeError, ValueError):
            log(f"selection clip #{i}: missing/invalid start or end, skipped")
            continue
        s, e = max(0.0, s), min(dur, e)
        if e - s < 3:
            log(f"selection clip #{i}: too short ({e - s:.1f}s), skipped")
            continue
        if e - s > float(hc["max_clip"]) * 1.5:
            log(f"selection clip #{i}: {e - s:.0f}s is long; trimming to {hc['max_clip']}s")
            e = s + float(hc["max_clip"])
        mood = str(c.get("music_mood", "hype")).lower()
        mood = mood if mood in MOODS else "none"
        from .english import clean_title
        good.append({
            "n": len(good) + 1, "candidate_id": c.get("candidate_id", f"x{i:02d}"),
            "start": s, "end": e, "title": clean_title(str(c.get("title", "")), mood, seed=i),
            "music_mood": mood, "reason": c.get("reason", ""),
        })
    if not good:
        raise SystemExit("[clipper] selection.json has no usable clips.")
    return good


def claude_select_dir(rel_dir: str, abs_dir: Path, cfg: dict, extra: str = "") -> bool:
    """Kept for compatibility; see clipper.agents.agent_select_dir."""
    from .agents import agent_select_dir
    return agent_select_dir("claude", rel_dir, abs_dir, cfg, extra)
