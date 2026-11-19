"""Karaoke-style ASS subtitles (one highlighted word at a time) plus a hook title."""
from __future__ import annotations

from pathlib import Path


def _ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _esc(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def _chunks(words: list[dict], per_line: int) -> list[list[dict]]:
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        brk = (len(cur) >= per_line or w["w"][-1:] in ".!?,"
               or (nxt is not None and nxt["s"] - w["e"] > 0.5))
        if brk:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def build_ass(words: list[dict], clip_start: float, clip_len: float, title: str,
              cfg: dict, out: Path, title_y: int | None = None) -> Path:
    r = cfg["render"]
    # Subtitles are laid out on a 1080-wide canvas; libass scales them to the real output size,
    # so captions look the same at 1080x1920 or 1440x2560.
    k = 1080 / int(r["width"])
    W, H = 1080, round(int(r["height"]) * k)
    if title_y is not None:
        title_y = round(title_y * k)
    cc, tc = r["captions"], r["title"]
    margin_v = int(H * (1 - float(cc["position"])))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{cc['font']},{cc['size']},{cc['color']},&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{cc['outline']},2,2,70,70,{margin_v},1
Style: Title,{cc['font']},{tc['size']},{tc['color']},&H000000FF,{tc['box']},&H00000000,-1,0,0,0,100,100,0,0,3,18,0,8,80,80,{title_y if title_y is not None else int(H * 0.11)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if tc.get("enabled") and title:
        t = _esc(title.upper() if cc.get("uppercase") else title)
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(min(float(tc['seconds']), clip_len))},Title,,0,0,0,,"
                     f"{{\\fad(150,300)}}{t}")

    if cc.get("enabled"):
        from .english import caption_words
        inside = [w for w in words if w["e"] > clip_start and w["s"] < clip_start + clip_len]
        rel = [{"w": w["w"], "s": w["s"] - clip_start, "e": w["e"] - clip_start}
               for w in caption_words(inside, cfg)]
        hi = cc["highlight"]
        groups = _chunks(rel, int(cc["words_per_line"]))
        for gi, g in enumerate(groups):
            g_end = g[-1]["e"]
            nxt = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else None
            if nxt is not None and nxt - g_end < 0.35:
                g_end = nxt  # no flicker between close phrases
            texts = [_esc(w["w"].upper() if cc.get("uppercase") else w["w"]) for w in g]
            for wi, w in enumerate(g):
                s = max(0.0, w["s"])
                e = g[wi + 1]["s"] if wi + 1 < len(g) else g_end
                e = min(max(e, s + 0.08), clip_len)
                if e <= s:
                    continue
                parts = [(f"{{\\c{hi}\\fscx112\\fscy112}}{t}{{\\r}}" if j == wi else t)
                         for j, t in enumerate(texts)]
                pop = "{\\t(0,80,\\fscx104\\fscy104)}" if wi == 0 else ""
                lines.append(f"Dialogue: 0,{_ts(s)},{_ts(e)},Cap,,0,0,0,,{pop}{' '.join(parts)}")

    out.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return out
