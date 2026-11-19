"""Fetching stream metadata, audio for analysis, and only the video sections we clip."""
from __future__ import annotations

from pathlib import Path

from .util import log, probe_duration, require_tool, run, slugify


def is_local(source: str) -> bool:
    return Path(source).exists()


def _ydl_opts(cfg: dict, **extra) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noprogress": True}
    browser = cfg["download"].get("cookies_from_browser")
    if browser:
        opts["cookiesfrombrowser"] = (browser,)
    opts.update(extra)
    return opts


def fetch_meta(source: str, cfg: dict) -> dict:
    """Return a small metadata dict for a URL or a local file."""
    if is_local(source):
        p = Path(source).resolve()
        return {
            "id": slugify(p.stem, 60), "source": str(p), "local": True, "title": p.stem,
            "uploader": "", "extractor": "local", "duration": probe_duration(p),
            "heatmap": [], "url": None,
        }
    import yt_dlp
    with yt_dlp.YoutubeDL(_ydl_opts(cfg)) as ydl:
        info = ydl.extract_info(source, download=False)
    if info.get("is_live"):
        raise SystemExit(
            "[clipper] This stream is still live. Run the tool on the VOD once the stream ends "
            "(Twitch: the /videos/<id> link; YouTube: the same link after it finishes)."
        )
    extractor = (info.get("extractor_key") or info.get("extractor") or "").lower()
    vid = slugify(f"{extractor}-{info.get('id')}", 60)
    return {
        "id": vid, "source": source, "local": False, "title": info.get("title") or "",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "extractor": extractor, "duration": float(info.get("duration") or 0),
        "heatmap": info.get("heatmap") or [], "url": info.get("webpage_url") or source,
        "native_id": info.get("id"),
    }


def fetch_audio(meta: dict, wdir: Path, cfg: dict) -> Path:
    """Audio track used for loudness analysis and transcription."""
    if meta["local"]:
        return Path(meta["source"])
    existing = sorted(wdir.glob("audio.*"))
    existing = [p for p in existing if p.suffix not in (".part", ".ytdl")]
    if existing:
        return existing[0]
    require_tool("ffmpeg")
    import yt_dlp
    log("Downloading audio track for analysis (video is fetched later, only for the clips)...")
    opts = _ydl_opts(cfg, format="bestaudio/worstaudio/worst",
                     outtmpl=str(wdir / "audio.%(ext)s"))
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([meta["url"]])
    files = [p for p in wdir.glob("audio.*") if p.suffix not in (".part", ".ytdl")]
    if not files:
        raise SystemExit("[clipper] Audio download failed.")
    return files[0]


# Near-lossless intermediate: the clip is encoded once more at render, so keep this one clean.
CUT_ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "10", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "256k"]


def _pcm(path: str, start: float, dur: float, sr: int = 4000):
    import numpy as np
    proc = __import__("subprocess").run(
        ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-i", path, "-t", f"{dur:.3f}",
         "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"], capture_output=True)
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32)


def _envelope(x, sr: int = 4000, rate: int = 200):
    import numpy as np
    n = len(x) // (sr // rate)
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    e = np.abs(x[: n * (sr // rate)]).reshape(n, sr // rate).mean(1)
    return e - e.mean()


def align_start(chunk: Path, full_audio: str, guess: float, search: float = 25.0) -> float | None:
    """Where (in stream seconds) does `chunk` start? Matches its audio against the analysis audio,
    so stream seeking that lands on a keyframe a few seconds off can't desync the captions."""
    import numpy as np
    rate = 200
    a = _envelope(_pcm(str(chunk), 0, 30))
    t0 = max(0.0, guess - search)
    b = _envelope(_pcm(full_audio, t0, 30 + 2 * search))
    if len(a) < rate * 5 or len(b) < len(a):
        return None
    n = len(a) + len(b)
    corr = np.fft.irfft(np.fft.rfft(b, n) * np.conj(np.fft.rfft(a, n)), n)[: len(b) - len(a) + 1]
    i = int(np.argmax(corr))
    rest = corr.copy()
    rest[max(0, i - rate // 5): i + rate // 5] = -np.inf   # ignore the peak's own shoulders
    conf = corr[i] / max(float(rest.max()), 1e-9)       # how clearly the best match beats the next
    if conf < 1.3:
        log(f"   audio alignment unsure (confidence {conf:.1f}); using the seek position")
        return None
    return t0 + i / rate


def _ffmpeg_section(inputs: list[dict], start: float, end: float, out: Path,
                    full_audio: str | None = None) -> bool:
    """High-quality cut. Local files: accurate seek, one near-lossless encode.
    Streams: copy a padded chunk without re-encoding, find its true start from the audio, then cut."""
    def hdr_args(f):
        hdrs = "".join(f"{k}: {v}\r\n" for k, v in (f.get("http_headers") or {}).items())
        return ["-headers", hdrs] if hdrs else []

    a_in = len(inputs) - 1  # with separate video+audio formats, audio is the last input
    if full_audio is None:  # local file
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", inputs[0]["url"],
               "-t", f"{end - start:.3f}", "-map", "0:v:0", "-map", "0:a:0?"] + CUT_ENC + [str(out)]
        return run(cmd).returncode == 0 and out.exists()

    pad = 15.0
    c0 = max(0.0, start - pad)
    raw = out.with_name("raw_chunk.mkv")
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for f in inputs:
        cmd += hdr_args(f) + (["-ss", f"{c0:.3f}"] if c0 > 0 else []) + ["-i", f["url"]]
    cmd += ["-t", f"{end - c0 + pad:.3f}", "-map", "0:v:0", "-map", f"{a_in}:a:0?", "-c", "copy", str(raw)]
    proc = run(cmd)
    if proc.returncode != 0 or not raw.exists() or raw.stat().st_size < 10_000:
        log(f"   direct download failed ({proc.stderr.strip()[-200:]}); trying yt-dlp sections")
        raw.unlink(missing_ok=True)
        return False
    real0 = align_start(raw, full_audio, c0)
    off = start - (real0 if real0 is not None else c0)
    # Some streams keep a non-zero first timestamp in the copied chunk; seeking in it is absolute.
    if c0 > 0:
        st = run(["ffprobe", "-v", "error", "-show_entries", "format=start_time", "-of", "csv=p=0", str(raw)])
        try:
            off += max(0.0, float(st.stdout.strip()))
        except ValueError:
            pass
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, off):.3f}", "-i", str(raw),
           "-t", f"{end - start:.3f}", "-map", "0:v:0", "-map", "0:a:0?"] + CUT_ENC + [str(out)]
    ok = run(cmd).returncode == 0 and out.exists() and out.stat().st_size > 10_000
    raw.unlink(missing_ok=True)
    return ok


def fetch_section(meta: dict, start: float, end: float, out: Path, cfg: dict) -> Path:
    """Get just [start, end] of the video as an mp4 whose t=0 is `start`, at the best quality available."""
    if out.exists() and out.stat().st_size > 0:
        return out
    require_tool("ffmpeg")
    out.parent.mkdir(parents=True, exist_ok=True)
    if meta["local"]:
        if not _ffmpeg_section([{"url": meta["source"]}], start, end, out):
            raise RuntimeError("ffmpeg cut failed")
        return out
    import yt_dlp
    h = int(cfg["download"].get("max_height", 1080))
    fmt = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
    with yt_dlp.YoutubeDL(_ydl_opts(cfg, format=fmt)) as ydl:
        info = ydl.extract_info(meta["url"], download=False)
    inputs = info.get("requested_formats") or [info]
    v = inputs[0]
    log(f"   source quality: {v.get('format_id')} {v.get('width')}x{v.get('height')}"
        f"@{v.get('fps') or '?'}fps")
    if v.get("height") and v["height"] < 1080:
        log("   note: this VOD is only available below 1080p, so clips will look softer")
    if _ffmpeg_section(inputs, start, end, out, full_audio=meta.get("audio")):
        return out

    # Fallback: yt-dlp's own section download (re-encodes the cut with x264 at high quality).
    from yt_dlp.utils import download_range_func
    tmpl = str(out.with_suffix("")) + ".%(ext)s"
    opts = _ydl_opts(
        cfg, format=fmt, merge_output_format="mp4",
        download_ranges=download_range_func(None, [(start, end)]),
        force_keyframes_at_cuts=True, outtmpl=tmpl,
        postprocessor_args={"ffmpeg": ["-crf", "12", "-preset", "veryfast"]},
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([meta["url"]])
    if not out.exists():
        cands = [p for p in out.parent.glob(out.stem + ".*") if p.suffix not in (".part", ".ytdl")]
        if not cands:
            raise RuntimeError(f"section download failed for {start}-{end}")
        cands[0].rename(out)
    return out
