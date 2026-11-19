"""stream-clipper CLI.

  python -m clipper analyze <url-or-file> [--chat chat.json] [--auto]
  python -m clipper select  <id> --auto
  python -m clipper render  <id> [--only c01,c04] [--layout blur|facecam|crop] [--no-music]
  python -m clipper run     <url-or-file>          # analyze + auto select + render
  python -m clipper music fetch|list
  python -m clipper doctor
"""
from __future__ import annotations

import argparse
import shutil
import sys

from .util import load_config, log, work_dir, write_json


def cmd_analyze(args, cfg) -> str:
    from . import chat as chatmod
    from .candidates import chat_sample, snap_to_speech, write_candidates
    from .download import fetch_audio, fetch_meta
    from .signals import audio_loudness, build_scores, pick_peaks
    from .transcribe import transcribe_window

    meta = fetch_meta(args.source, cfg)
    wdir = work_dir(cfg, meta["id"])
    log(f"Stream: {meta['title']}  [{meta['id']}]  {meta['duration'] / 60:.0f} min")
    audio = fetch_audio(meta, wdir, cfg)
    meta["audio"] = str(audio.resolve())
    write_json(wdir / "meta.json", meta)

    msgs = chatmod.get_chat(meta, wdir, args.chat)
    log("Measuring loudness...")
    loud = audio_loudness(audio)
    meta["duration"] = meta["duration"] or float(len(loud))
    comps = build_scores(meta["duration"], loud, msgs, meta.get("heatmap") or [], cfg)
    peaks = pick_peaks(comps["score"], meta["duration"], cfg)

    pad = float(cfg["whisper"]["pad_seconds"])
    cands = []
    for i, p in enumerate(peaks, 1):
        cid = f"c{i:02d}"
        tx_s, tx_e = max(0.0, p["start"] - pad), p["end"] + pad + 6
        log(f"Transcribing {cid} ({i}/{len(peaks)})...")
        words = transcribe_window(audio, tx_s, tx_e, cfg)
        pk = int(p["peak"])
        c = {"id": cid, **p, "tx_start": tx_s, "tx_end": tx_e, "words": words,
             "signals": {k: float(v[pk]) for k, v in comps.items() if k != "score" and pk < len(v)}}
        snap_to_speech(c, words, cfg)
        c["chat"] = chat_sample(msgs, c["start"], c["end"], float(cfg["highlights"]["chat_delay"])) if msgs else {}
        cands.append(c)
    write_json(wdir / "meta.json", meta)
    write_candidates(wdir, meta, cands)
    log(f"Candidates written: {wdir / 'candidates.md'}")
    if args.auto:
        from .rank import auto_select
        auto_select(wdir, cfg)
    else:
        log("Next: run /make-clips in Claude Code or Antigravity (or ask the agent to pick the "
            f"highlights for {meta['id']}), then:  python -m clipper render {meta['id']}")
    print(f"STREAM_ID={meta['id']}")
    return meta["id"]


def cmd_select(args, cfg) -> None:
    from .rank import auto_select
    auto_select(work_dir(cfg, args.id), cfg)


def cmd_render(args, cfg) -> None:
    from .render import render_all
    only = set(args.only.split(",")) if args.only else None
    outs = render_all(args.id, cfg, only=only, layout=args.layout, no_music=args.no_music)
    log(f"Done: {len(outs)} clip(s) in {outs[0].parent if outs else cfg['paths']['output']}")
    if should_publish(args, cfg) and outs:
        from . import publish
        for o in outs:
            publish.enqueue(o, cfg)
        # Hand the queue to the background poster when it exists, so this command returns at once
        # (posts go out one per platform every interval). Without it, post here in the foreground.
        if publish.other_poster_running(cfg) or publish.task_installed():
            if not publish.other_poster_running(cfg):
                publish.start_task()
            nxt = ", ".join(f"{k} {v}" for k, v in publish.next_post_times(cfg).items())
            log(f"Queued {len(outs)} clip(s) for posting; the background poster posts them "
                f"(next: {nxt}). Check with: publish status")
        else:
            log("No background poster installed (run `publish install` once); posting from this window.")
            args.action = "run"
            publish.cli(args, cfg)


def _sync_agent_commands() -> None:
    """Keep Claude Code's /make-clips and /live-clips (.claude/commands) identical to claude-setup/,
    so an update takes effect without re-running setup.ps1."""
    import shutil
    from .util import ROOT
    src, dst = ROOT / "claude-setup", ROOT / ".claude" / "commands"
    try:
        dst.mkdir(parents=True, exist_ok=True)
        for f in src.glob("*.md"):
            d = dst / f.name
            if not d.exists() or d.read_bytes() != f.read_bytes():
                shutil.copyfile(f, d)
    except OSError:
        pass


def should_publish(args, cfg) -> bool:
    """Posting is on by default once a platform is enabled (config.yaml -> publish -> youtube/instagram
    enabled: true), so /make-clips and /live-clips post even if the command doesn't say --publish.
    --no-publish turns it off for one run; publish.auto_post: false makes --publish required again."""
    flag = getattr(args, "publish", None)
    if flag is not None:
        return bool(flag)
    from .publish import enabled_platforms
    return bool(enabled_platforms(cfg)) and bool((cfg.get("publish") or {}).get("auto_post", True))


def cmd_publish(args, cfg) -> None:
    from . import publish
    publish.cli(args, cfg)


def agent_select(agent: str, vid: str, cfg) -> bool:
    """Let a headless agent (Claude Code or Gemini CLI) pick and trim the clips, with your own sign-in."""
    from .agents import agent_select_dir
    return agent_select_dir(agent, f"work/{vid}", work_dir(cfg, vid), cfg)


def cmd_run(args, cfg) -> None:
    use_agent = args.agent != "none"
    args.auto = not use_agent
    vid = cmd_analyze(args, cfg)
    if use_agent and not agent_select(args.agent, vid, cfg):
        from .rank import auto_select
        auto_select(work_dir(cfg, vid), cfg)
    args.id, args.only = vid, None
    cmd_render(args, cfg)


def cmd_music(args, cfg) -> None:
    from . import music
    if args.action == "fetch":
        n = music.fetch(cfg)
        log(f"Downloaded {n} new track(s).")
    for t in music.library(cfg):
        print(f"  [{t['mood']:>5}] {t['title']}")


def cmd_facecam(args, cfg) -> None:
    from pathlib import Path
    from . import facecam
    video = Path(args.video)
    out = video.with_name(video.stem + "_facecam.png")
    res = facecam.preview(video, cfg, out)
    if not res:
        print("No webcam detected (layout 'auto' will use blur).")
    else:
        sw, sh = res["src"]
        x, y, w, h = res["cam"]
        print(f"{res['kind']}: x={x} y={y} w={w} h={h}  (face in {res['presence']:.0%} of frames)")
        print(f"As manual config fractions: x: {x / sw:.3f}  y: {y / sh:.3f}  w: {w / sw:.3f}  h: {h / sh:.3f}")
    print(f"Preview: {out}")


def cmd_doctor(args, cfg) -> None:
    from .util import add_nvidia_dll_dirs, run
    ok = True
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        print(f"{tool:<12} {'OK  ' + path if path else 'MISSING  (winget install Gyan.FFmpeg)'}")
        ok &= bool(path)
    if shutil.which("ffmpeg"):
        enc = run(["ffmpeg", "-hide_banner", "-encoders"]).stdout
        print(f"{'nvenc':<12} {'available' if 'h264_nvenc' in enc else 'not available (x264 will be used)'}")
        filt = run(["ffmpeg", "-hide_banner", "-filters"]).stdout
        print(f"{'libass':<12} {'OK' if ' subtitles ' in filt else 'MISSING: use a full ffmpeg build'}")
    for mod in ("yt_dlp", "faster_whisper", "chat_downloader", "numpy", "yaml", "requests", "cv2"):
        try:
            __import__(mod)
            print(f"{mod:<12} OK")
        except ImportError:
            print(f"{mod:<12} MISSING  (pip install -r requirements.txt)")
            ok = False
    print(f"{'Twitch chat':<12} {'TwitchDownloaderCLI found' if shutil.which('TwitchDownloaderCLI') else 'chat-downloader (TwitchDownloaderCLI optional)'}")
    try:
        add_nvidia_dll_dirs()
        import ctranslate2
        n = ctranslate2.get_cuda_device_count()
        print(f"{'CUDA':<12} {n} device(s)" + ("" if n else "  -> Whisper runs on CPU"))
    except Exception as e:
        print(f"{'CUDA':<12} check failed: {e}")
    sys.exit(0 if ok else 1)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="clipper", description="Gaming stream -> captioned vertical highlight clips")
    p.add_argument("--config", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="find candidate highlights")
    a.add_argument("source")
    a.add_argument("--chat", help="chat file (TwitchDownloader JSON / chat-downloader JSON / jsonl)")
    a.add_argument("--auto", action="store_true", help="also auto-select clips from signals")
    a.set_defaults(fn=cmd_analyze)

    s = sub.add_parser("select", help="write selection.json")
    s.add_argument("id")
    s.add_argument("--auto", action="store_true", required=True)
    s.set_defaults(fn=cmd_select)

    r = sub.add_parser("render", help="render clips from selection.json")
    r.add_argument("id")
    r.add_argument("--only")
    r.add_argument("--layout", choices=["auto", "split", "facecam", "blur", "crop"])
    r.add_argument("--no-music", action="store_true")
    r.add_argument("--publish", dest="publish", action="store_true", default=None,
                   help="post the clips (default: on whenever a platform is enabled in config.yaml -> publish)")
    r.add_argument("--no-publish", dest="publish", action="store_false", help="only make the clips, don't post")
    r.set_defaults(fn=cmd_render)

    u = sub.add_parser("run", help="analyze + auto-select + render, no agent")
    u.add_argument("source")
    u.add_argument("--chat")
    u.add_argument("--layout", choices=["auto", "split", "facecam", "blur", "crop"])
    u.add_argument("--no-music", action="store_true")
    u.add_argument("--agent", choices=["auto", "gemini", "claude", "none"], default="auto",
                   help="auto = agents in config agents.priority order (Gemini/Antigravity first); "
                        "gemini, claude = only that one; none = signals only")
    u.add_argument("--publish", dest="publish", action="store_true", default=None,
                   help="post the clips (default: on whenever a platform is enabled in config.yaml -> publish)")
    u.add_argument("--no-publish", dest="publish", action="store_false", help="only make the clips, don't post")
    u.set_defaults(fn=cmd_run)

    lv = sub.add_parser("live", help="clip a stream while it is live")
    lv.add_argument("source", help="channel/stream URL (Twitch, YouTube, Kick), an .m3u8, or a video file to simulate")
    lv.add_argument("--agent", choices=["auto", "gemini", "claude", "none"], default="auto",
                    help="who judges each batch of moments: auto (agents.priority order, Gemini first), "
                         "gemini, claude, or none (signals only)")
    lv.add_argument("--layout", choices=["auto", "split", "facecam", "blur", "crop"])
    lv.add_argument("--no-music", action="store_true")
    lv.add_argument("--publish", dest="publish", action="store_true", default=None,
                    help="post each clip as it is made (default: on whenever a platform is enabled in config.yaml)")
    lv.add_argument("--no-publish", dest="publish", action="store_false", help="only make the clips, don't post")
    lv.add_argument("--speed", type=float, help="simulation only: play a video file N times faster")
    lv.add_argument("--chat", help="simulation only: chat file to replay")
    lv.set_defaults(fn=lambda a, c: __import__("clipper.live", fromlist=["main"]).main(a, c))

    pb = sub.add_parser("publish", help="post clips to YouTube Shorts / Instagram Reels")
    pb.add_argument("action", choices=["setup", "status", "post", "approve", "run", "install", "uninstall", "daemon"])
    pb.add_argument("files", nargs="*",
                    help="setup: youtube|instagram; post: clip files; approve: clip names or 'all'")
    pb.add_argument("--client-secret", help="setup youtube: path to the OAuth client JSON")
    pb.set_defaults(fn=cmd_publish)

    m = sub.add_parser("music", help="manage the music library")
    m.add_argument("action", choices=["fetch", "list"])
    m.set_defaults(fn=cmd_music)

    f = sub.add_parser("facecam", help="detect the webcam in a video file and save a preview image")
    f.add_argument("video")
    f.set_defaults(fn=cmd_facecam)

    d = sub.add_parser("doctor", help="check the environment")
    d.set_defaults(fn=cmd_doctor)

    args = p.parse_args(argv)
    _sync_agent_commands()
    cfg = load_config(args.config)
    args.fn(args, cfg)


if __name__ == "__main__":
    main()
