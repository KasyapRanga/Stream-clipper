"""Cut, reframe to 9:16, burn captions, and mix ducked music."""
from __future__ import annotations

import shutil
from pathlib import Path

from . import music
from .captions import build_ass
from .download import fetch_section
from .rank import load_selection
from .transcribe import needs_redo, transcribe_window
from . import facecam
from .util import write_json, has_stream, log, probe_duration, probe_fps, probe_size, read_json, require_tool, run, slugify

_NVENC_OK: bool | None = None


def _even(x: float) -> int:
    return int(round(x / 2)) * 2


def _fit_crop(box: list[float], aspect: float, zoom: float, focus: tuple[float, float] | None,
              bounds: tuple[int, int]) -> tuple[int, int, int, int]:
    """Largest crop of `aspect` (w/h) inside `box`, shrunk by `zoom`, centred on `focus`."""
    bx, by, bw, bh = box
    if bw / bh > aspect:
        h = bh / zoom
        w = h * aspect
    else:
        w = bw / zoom
        h = w / aspect
    fx, fy = focus if focus else (bx + bw / 2, by + bh / 2)
    x = min(max(fx - w / 2, bx), bx + bw - w)
    y = min(max(fy - h / 2, by), by + bh - h)
    W, H = bounds
    w, h = min(w, W), min(h, H)
    x, y = min(max(0, x), W - w), min(max(0, y), H - h)
    return _even(w), _even(h), _even(x), _even(y)


def resolve_layout(layout: str, geo: dict | None) -> str:
    if layout == "facecam":
        layout = "split"
    if layout == "auto":
        if geo and geo["kind"] == "overlay":
            return "split"
        if geo and geo["kind"] == "fullcam":
            return "crop"
        return "blur"
    if layout == "split" and not (geo and geo["kind"] == "overlay"):
        log("   no facecam found for split layout; using blur instead")
        return "blur"
    return layout


def _sharpen(cfg: dict, src_w: float, out_w: float) -> str:
    """Light unsharp after a big upscale (small webcam / 720p source) so faces and HUD stay crisp."""
    if not cfg["render"].get("sharpen", True):
        return ""
    f = out_w / max(1.0, src_w)
    if f < 1.3:
        return ""
    return f",unsharp=5:5:{0.7 if f > 2.2 else 0.45}:5:5:0"


def cam_box(geo: dict, cfg: dict) -> list[float]:
    """The webcam rectangle minus a small inset (trims the overlay's border / any gameplay sliver)."""
    bx, by, bw, bh = geo["cam"]
    ins = float(cfg["render"]["facecam"].get("inset", 0.03))
    return [bx + bw * ins, by + bh * ins, bw * (1 - 2 * ins), bh * (1 - 2 * ins)]


def video_filter(cfg: dict, layout: str, geo: dict | None = None,
                 src: tuple[int, int] = (1920, 1080), track: list[dict] | None = None) -> str:
    r = cfg["render"]
    W, H = int(r["width"]), int(r["height"])
    sw, sh = geo["src"] if geo else src
    face = geo.get("face") if geo else None
    face_c = (face[0] + face[2] / 2, face[1] + face[3] * 0.45) if face else None
    if layout == "crop":
        from .facecam import crop_window
        w, h, x, y = crop_window(track or [], [0, 0, sw, sh], W / H, W, cfg, face_c)
        chain = (f"[0:v]crop=w={w}:h={h}:x='{x}':y='{y}',scale={W}:{H}:flags=lanczos{_sharpen(cfg, w, W)},"
                 "setsar=1[base]")
    elif layout == "split":
        f = r["facecam"]
        ch = _even(H * float(f["height_share"]))
        gh = H - ch
        from .facecam import crop_window
        # Crop sized to the face and following it (see facecam.track_face / crop_window).
        cw_, chh_, cx_, cy_ = crop_window(track or [], cam_box(geo, cfg), W / ch, W, cfg, face_c)
        gzoom = float(f.get("game_zoom", 1.0))
        game_box = [0, 0, sw, sh]
        gw, ghh, gx, gy = _fit_crop(game_box, W / gh, gzoom, None, (sw, sh))
        # Keep the webcam out of the gameplay panel when the two overlap.
        cx0, cx1 = geo["cam"][0], geo["cam"][0] + geo["cam"][2]
        if cx0 < gx + gw and gx < cx1:
            gx = _even(max(0, cx0 - gw)) if cx0 + (cx1 - cx0) / 2 > sw / 2 else _even(min(sw - gw, cx1))
        top, bottom = ("cam", "game") if f.get("position", "top") == "top" else ("game", "cam")
        div = _even(int(f.get("divider", 6)) * W / 1080)
        seam = ch if top == "cam" else gh
        chain = (
            f"[0:v]split=2[a][b];"
            f"[a]crop=w={cw_}:h={chh_}:x='{cx_}':y='{cy_}',scale={W}:{ch}:flags=lanczos{_sharpen(cfg, cw_, W)}[cam];"
            f"[b]crop={gw}:{ghh}:{gx}:{gy},scale={W}:{gh}:flags=lanczos{_sharpen(cfg, gw, W)}[game];"
            f"[{top}][{bottom}]vstack=inputs=2,setsar=1"
            + (f",drawbox=x=0:y={seam - div // 2}:w={W}:h={div}:color={f.get('divider_color', 'white')}:t=fill"
               if div > 0 else "")
            + "[base]"
        )
    else:  # blur
        fw = _even(W * float(r.get("fg_zoom", 1.3)))
        chain = (
            f"[0:v]split=2[a][b];"
            f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            f"boxblur=24:2,eq=brightness=-0.10[bg];"
            f"[b]scale={fw}:-2:flags=lanczos{_sharpen(cfg, sw, fw)},crop=min(iw\\,{W}):ih[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2-{int(H * 0.04)},setsar=1[base]"
        )
    return chain + ";[base]subtitles=captions.ass[vout]"


def speech_spans(words: list[dict], clip_start: float, length: float, gap: float = 0.6) -> list[tuple[float, float]]:
    spans: list[list[float]] = []
    for w in words:
        s, e = w["s"] - clip_start, w["e"] - clip_start
        if e <= 0 or s >= length:
            continue
        if spans and s - spans[-1][1] < gap:
            spans[-1][1] = max(spans[-1][1], e)
        else:
            spans.append([max(0.0, s), e])
    return [(a, b) for a, b in spans]


def _duck_expr(spans: list[tuple[float, float]], duck: float, ramp: float = 0.25) -> str:
    """ffmpeg volume expression: 1.0 normally, `duck` while anyone talks, with short ramps."""
    if not spans:
        return "1"
    parts = [f"clip((t-{s - ramp:.2f})/{ramp},0,1)*clip(({e + ramp * 2:.2f}-t)/{ramp * 2},0,1)"
             for s, e in spans[:120]]
    return f"1-{1 - duck:.3f}*min(1,{'+'.join(parts)})"  # single-quoted in the graph, so commas are safe


def audio_filter(cfg: dict, has_music: bool, length: float, spans=None) -> str:
    """Voice first: lift quiet voices (Discord), normalise loudness, then tuck music underneath."""
    mc = cfg["music"]
    voice = ("[0:a]dynaudnorm=f=150:g=15:p=0.9:m=8,"
             "loudnorm=I=-15:TP=-1.5:LRA=11,aresample=48000")
    if not has_music:
        return voice + ",alimiter=limit=0.95[aout]"
    fade_st = max(0.0, length - 1.5)
    level = float(mc.get("level_lufs", -30))
    duck = float(mc.get("duck_gain", 0.3))
    mus = (f"[1:a]loudnorm=I={level}:TP=-8:LRA=7,aresample=48000,"
           f"volume='{_duck_expr(spans or [], duck)}':eval=frame,"
           f"afade=t=in:d=1,afade=t=out:st={fade_st:.2f}:d=1.5[m0]")
    if mc.get("duck", True):
        # Second safety net: also duck on any voice-band energy (untranscribed callouts, laughs).
        return (voice + ",asplit=2[voice][k0];"
                "[k0]highpass=f=200,lowpass=f=3500[key];" + mus + ";"
                "[m0][key]sidechaincompress=threshold=0.02:ratio=6:attack=20:release=500[md];"
                "[voice][md]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[aout]")
    return voice + "[voice];" + mus + ";[voice][m0]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[aout]"


def _encoders(cfg: dict) -> list[list[str]]:
    q = int(cfg["render"]["crf"])
    x264 = ["-c:v", "libx264", "-preset", cfg["render"].get("x264_preset", "slow"), "-crf", str(q),
            "-profile:v", "high", "-x264-params", "aq-mode=3", "-maxrate", "20M", "-bufsize", "40M"]
    nvenc = ["-c:v", "h264_nvenc", "-preset", "p7", "-tune", "hq", "-rc", "vbr", "-cq", str(q + 1),
             "-b:v", "0", "-maxrate", "20M", "-bufsize", "40M", "-spatial-aq", "1", "-temporal-aq", "1",
             "-rc-lookahead", "32", "-bf", "3", "-profile:v", "high"]
    mode = cfg["render"].get("encoder", "auto")
    if mode == "x264" or _NVENC_OK is False:
        return [x264]
    if mode == "nvenc":
        return [nvenc]
    return [nvenc, x264]


def render_clip(src: Path, clip_dir: Path, out: Path, length: float, track: dict | None,
                cfg: dict, layout: str, geo: dict | None = None, spans=None) -> None:
    global _NVENC_OK
    has_audio = has_stream(src, "audio")
    use_music = bool(track) and has_audio
    cmd_base = ["ffmpeg", "-y", "-v", "error", "-i", str(src.resolve())]
    if use_music:
        mlen = probe_duration(Path(track["path"]))
        offset = 0.0 if mlen < length + 20 else (hash(src.name) % int(mlen - length - 10))
        cmd_base += ["-stream_loop", "-1", "-ss", f"{offset:.1f}", "-i", str(Path(track["path"]).resolve())]
    face_track = None
    if layout in ("split", "crop") and geo and cfg["render"]["facecam"].get("track", True):
        from .facecam import track_face
        sw, sh = geo.get("src") or probe_size(src)
        region = cam_box(geo, cfg) if layout == "split" else [0, 0, sw, sh]
        try:
            face_track = track_face(src, region, cfg, clip_dir / "facetrack.json")
            if face_track:
                log(f"   face tracked ({len(face_track)} points)")
        except Exception as e:  # tracking is a nice-to-have: fall back to the static framing
            log(f"   face tracking skipped ({type(e).__name__}: {e})")
    fc = video_filter(cfg, layout, geo, probe_size(src), face_track)
    maps = ["-map", "[vout]"]
    if has_audio:
        fc += ";" + audio_filter(cfg, use_music, length, spans)
        maps += ["-map", "[aout]"]
    fps = min(probe_fps(src), float(cfg["render"].get("max_fps", 60)))
    tail = ["-t", f"{length:.3f}", "-pix_fmt", "yuv420p", "-r", f"{fps:g}",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out.resolve())]
    last = None
    for enc in _encoders(cfg):
        # cwd=clip_dir so the subtitles filter can use a plain relative path (avoids Windows C:\ escaping).
        last = run(cmd_base + ["-filter_complex", fc] + maps + enc + tail, cwd=clip_dir)
        if last.returncode == 0:
            if enc[1] == "h264_nvenc":
                _NVENC_OK = True
            return
        if enc[1] == "h264_nvenc":
            _NVENC_OK = False
            log("NVENC unavailable, using x264.")
    raise RuntimeError("ffmpeg render failed:\n" + (last.stderr[-2500:] if last else ""))


def render_all(vid: str, cfg: dict, only: set[str] | None = None, layout: str | None = None,
               no_music: bool = False) -> list[Path]:
    require_tool("ffmpeg")
    wdir = Path(cfg["paths"]["work"]) / vid
    if not (wdir / "meta.json").exists():
        raise SystemExit(f"[clipper] Unknown stream id '{vid}'. Run analyze first.")
    meta = read_json(wdir / "meta.json")
    audio = Path(meta["audio"]) if meta.get("audio") else None
    cands = {c["id"]: c for c in read_json(wdir / "candidates.json")["candidates"]}
    clips = load_selection(wdir, cfg)
    if only:
        clips = [c for c in clips if c["candidate_id"] in only or str(c["n"]) in only]
    layout = layout or cfg["render"]["layout"]
    out_dir = Path(cfg["paths"]["output"]) / vid
    out_dir.mkdir(parents=True, exist_ok=True)

    # Pass 1: fetch every clip's video and look for the webcam in each.
    jobs = []
    for c in clips:
        name = f"{c['n']:02d}_{slugify(c['title'] or c['candidate_id'])}"
        clip_dir = wdir / "clips" / name
        clip_dir.mkdir(parents=True, exist_ok=True)
        log(f"[{c['n']}/{len(clips)}] fetching {name}")
        src = fetch_section(meta, c["start"], c["end"], clip_dir / "source.mp4", cfg)
        geo = detect_geo(src, clip_dir, layout, cfg)
        jobs.append((c, name, clip_dir, src, geo))
    stream_geo = best_stream_geo(wdir, [j[4] for j in jobs])

    # Pass 2: captions, music and render.
    results = []
    for c, name, clip_dir, src, geo in jobs:
        log(f"[{c['n']}/{len(clips)}] rendering {name}")
        if geo is None and stream_geo is not None and layout != "blur":
            geo = stream_geo
            log("   facecam: not found in this clip, using the webcam position found in other clips")
        cand = cands.get(c["candidate_id"], {})
        words = cand.get("words") or []
        covered = cand.get("tx_start", 1e18) <= c["start"] and c["end"] <= cand.get("tx_end", -1)
        if audio is not None and (not covered or needs_redo(words, cfg)):
            words = transcribe_window(audio, c["start"], c["end"], cfg)
        results.append(render_one(c, name, clip_dir, src, geo, words, cfg, layout, no_music, meta, out_dir))
    if track_credits_needed(results):
        log("Paste the music credit from each clip's .txt into the post description (CC BY licence).")
    return results


def render_one(c: dict, name: str, clip_dir: Path, src: Path, geo: dict | None, words: list[dict],
               cfg: dict, layout: str, no_music: bool, meta: dict, out_dir: Path) -> Path:
    """Captions, music, layout and encode for one clip whose source video is already cut.
    `words` use the same time base as c['start'] / c['end']."""
    length = min(c["end"] - c["start"], probe_duration(src) or 1e9)
    from .english import clean_title
    c["title"] = clean_title(c.get("title", ""), c.get("music_mood", "none"), seed=c.get("n", 0))
    clip_layout = resolve_layout(layout, geo)
    log(f"   layout: {clip_layout}  ({length:.0f}s)")
    title_y = None
    if clip_layout == "split" and cfg["render"]["facecam"].get("position", "top") == "top":
        title_y = _even(int(cfg["render"]["height"]) * float(cfg["render"]["facecam"]["height_share"])) + 40
    build_ass(words, c["start"], length, c["title"], cfg, clip_dir / "captions.ass", title_y=title_y)

    track = None if no_music else music.choose(cfg, c["music_mood"], seed=c["n"])
    if track:
        log(f"   music: {track['title']} ({track['mood']})")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{name}.mp4"
    render_clip(src, clip_dir, out, length, track, cfg, clip_layout, geo,
                speech_spans(words, c["start"], length))

    notes = [f"Title: {c['title']}", f"Stream: {meta.get('title', '')} ({meta.get('url') or ''})",
             f"Source time: {c['start']:.1f}s - {c['end']:.1f}s", f"Why: {c.get('reason', '')}"]
    if track:
        notes += ["", "Music:", track.get("credit") or f"{track['title']} (your own track)"]
    out.with_suffix(".txt").write_text("\n".join(notes) + "\n", encoding="utf-8")
    write_json(out.with_suffix(".json"), {
        "title": c["title"], "stream_title": meta.get("title", ""), "stream_url": meta.get("url") or "",
        "start": c["start"], "end": c["end"], "reason": c.get("reason", ""), "music_mood": c["music_mood"],
        "credit": (track.get("credit") or "") if track else ""})
    log(f"   -> {out}")
    return out


def detect_geo(src: Path, clip_dir: Path, layout: str, cfg: dict) -> dict | None:
    """Webcam box for one clip: auto-detected, manual from config, or none."""
    if layout == "blur":
        return None
    mode = cfg["render"]["facecam"].get("mode", "auto")
    if mode == "manual":
        return facecam.manual(cfg, *probe_size(src))
    if mode == "off":
        return None
    return facecam.detect_cached(src, clip_dir / "facecam.json", cfg)


def best_stream_geo(wdir: Path, geos: list[dict | None]) -> dict | None:
    """The webcam rarely moves during a stream: remember the most confident overlay box."""
    path = wdir / "facecam_stream.json"
    found = [g for g in geos if g and g.get("kind") == "overlay"]
    if path.exists():
        prev = read_json(path)
        from .facecam import CACHE_VERSION
        if prev and prev.get("cache_v") == CACHE_VERSION:
            found.append(prev)
    if not found:
        return None
    best = max(found, key=lambda g: (g.get("border_found", False), g.get("presence", 0)))
    from .util import write_json
    write_json(path, best)
    return best


def track_credits_needed(paths: list[Path]) -> bool:
    return any("Kevin MacLeod" in p.with_suffix(".txt").read_text(encoding="utf-8") for p in paths)


def clean(vid: str, cfg: dict) -> None:
    d = Path(cfg["paths"]["work"]) / vid / "clips"
    if d.exists():
        shutil.rmtree(d)
