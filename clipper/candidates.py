"""Assemble candidate moments (signals + transcript + chat) for the ranker."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from .util import hms, write_json


def snap_to_speech(c: dict, words: list[dict], cfg: dict) -> None:
    """Avoid cutting mid-sentence: pull start back to a pause, push end to the end of a phrase."""
    hc = cfg["highlights"]
    if not words:
        return
    start, end = c["start"], c["end"]
    before = [w for w in words if w["s"] < start <= w["e"] + 1.2]
    if before:
        start = min(start, before[0]["s"] - 0.2)
    after = [w for w in words if w["s"] <= end + 6 and w["e"] >= end]
    for w in after:
        end = max(end, w["e"] + 0.4)
        nxt = next((x for x in words if x["s"] > w["e"]), None)
        if nxt is None or nxt["s"] - w["e"] > 0.6 or w["w"][-1:] in ".!?":
            break
    end = min(end, start + float(hc["max_clip"]))
    if end - start < float(hc["min_clip"]):
        end = start + float(hc["min_clip"])
    c["start"], c["end"] = round(max(0.0, start), 2), round(end, 2)


def chat_sample(chat: list[dict], start: float, end: float, delay: float, limit: int = 18) -> dict:
    inside = [m for m in chat if start + delay <= m["t"] <= end + delay]
    tokens = Counter()
    for m in inside:
        for tok in set(m.get("text", "").split()):
            if len(tok) > 1:
                tokens[tok] += 1
    step = max(1, len(inside) // limit)
    sample = [f"{hms(m['t'])} {m.get('user', '')}: {m.get('text', '')}"[:140] for m in inside[::step][:limit]]
    return {"count": len(inside), "top_tokens": [t for t, _ in tokens.most_common(8)], "sample": sample}


def write_candidates(wdir: Path, meta: dict, cands: list[dict]) -> None:
    write_json(wdir / "candidates.json", {"video": meta, "candidates": cands})
    lines = [
        f"# Highlight candidates: {meta['title']}",
        "",
        f"- Stream id: `{meta['id']}`  ·  Source: {meta.get('url') or meta['source']}",
        f"- Duration: {hms(meta['duration'])}  ·  Candidates: {len(cands)}",
        "",
        "Pick the best moments and write `selection.json` in this folder "
        "(see `.agent/workflows/make-clips.md` for the rubric and schema). "
        "All times are seconds from the start of the stream.",
        "",
    ]
    for c in cands:
        sig = ", ".join(f"{k} {v:+.1f}" for k, v in c["signals"].items())
        lines += [
            f"## {c['id']}  ·  {hms(c['start'])} → {hms(c['end'])}  "
            f"({c['start']:.1f}s–{c['end']:.1f}s, peak {c['peak']:.0f}s)",
            f"Score **{c['score']:.2f}**  ·  {sig}",
            "",
        ]
        ch = c.get("chat") or {}
        if ch.get("count"):
            lines += [f"Chat: {ch['count']} msgs · top: {' '.join(ch['top_tokens'])}", ""]
            lines += ["```", *ch["sample"], "```", ""]
        from .english import english_transcript
        all_words = c.get("words") or []
        words = english_transcript(all_words)
        if all_words and not words:
            # Not English (usually Telugu): show what was said so the agent can judge the moment.
            # These are often the best clips; they just get no captions. Titles must still be English.
            lines += ["Speech is NOT English (likely Telugu); shown as heard. Use it to understand and judge the "
                      "moment like any other: it can be a great clip. It will get no captions. Write the title "
                      "and reason in plain English; never copy these words.", ""]
            buf, t0 = [], None
            for w in all_words:
                t0 = w["s"] if t0 is None else t0
                buf.append(w["w"])
                if len(buf) >= 14:
                    lines.append(f"- [{t0:.1f}] {' '.join(buf)}")
                    buf, t0 = [], None
            if buf:
                lines.append(f"- [{t0:.1f}] {' '.join(buf)}")
            lines.append("")
        elif words:
            lines.append("Transcript (word start times):")
            lines.append("")
            buf, t0 = [], None
            for w in words:
                if t0 is None:
                    t0 = w["s"]
                buf.append(w["w"])
                if w["w"][-1:] in ".!?" or len(buf) >= 18:
                    lines.append(f"- [{t0:.1f}] {' '.join(buf)}")
                    buf, t0 = [], None
            if buf:
                lines.append(f"- [{t0:.1f}] {' '.join(buf)}")
            lines.append("")
        elif not all_words:
            lines += ["_(no speech detected: likely gameplay audio / reaction noise)_", ""]
    (wdir / "candidates.md").write_text("\n".join(lines), encoding="utf-8")
