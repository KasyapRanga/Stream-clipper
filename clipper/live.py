"""Live mode: clip a stream WHILE it is live.

  python -m clipper live https://www.twitch.tv/<channel> [--agent auto|gemini|claude|none]

How it works
  Recorder  ffmpeg copies the live stream into 30 s pieces (no re-encode) under work/<id>/live/segments.
  Chat      Twitch chat is read live (anonymous, no login); YouTube via chat-downloader; others: none.
  Watcher   every few seconds the new audio is measured and the chat counted; the same scoring as the
            VOD mode finds peaks once enough time has passed after them for the reaction to finish.
  Worker    each peak is cut from the pieces, transcribed, and then either rendered straight away
            (--agent none, score >= live.min_score) or batched for an agent to judge (--agent claude = Claude Code, --agent gemini = Gemini CLI),
            which picks, trims and titles the clips that are rendered right after.
  The run waits for the channel to go live, survives dropped connections, and finishes the last clips
  when the stream ends (or on Ctrl+C). Finished clips land in output/<id>/ as they are made.

Testing without a live stream: pass a local video file; it is played in real time (or faster with
--speed 4) and --chat chat.jsonl replays its chat on the same clock.
"""
from __future__ import annotations

import csv
import json
import queue
import random
import re
import socket
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from .candidates import chat_sample, snap_to_speech, write_candidates
from .download import CUT_ENC
from .agents import agent_select_dir, label
from .rank import _guess_mood, _guess_title, load_selection
from .render import best_stream_geo, detect_geo, render_one
from .signals import audio_loudness, build_scores
from .transcribe import transcribe_window
from .util import ROOT, log, require_tool, run, slugify, write_json


# ----------------------------------------------------------------------------- source resolution
class NotLive(Exception):
    pass


def resolve(source: str, cfg: dict) -> dict:
    """Return {'url', 'headers', 'title', 'id', 'kind'} for the live input."""
    p = Path(source)
    if p.exists():
        return {"url": str(p.resolve()), "headers": {}, "title": p.stem, "id": slugify(p.stem, 40),
                "kind": "file", "page": None}
    if source.split("?")[0].endswith(".m3u8"):
        if not hls_is_live(source):
            raise NotLive("playlist is not advancing")
        return {"url": source, "headers": {}, "title": "live", "id": "hls", "kind": "hls", "page": None}
    import yt_dlp
    from yt_dlp.utils import DownloadError
    h = int(cfg["download"].get("max_height", 1080))
    opts = {"quiet": True, "no_warnings": True, "format": f"best[height<={h}]/best"}
    browser = cfg["download"].get("cookies_from_browser")
    if browser:
        opts["cookiesfrombrowser"] = (browser,)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(source, download=False)
    except DownloadError as e:
        raise NotLive(str(e).splitlines()[0][:200])
    if not info.get("is_live"):
        raise NotLive("not live right now")
    ex = (info.get("extractor_key") or "").lower()
    name = info.get("uploader_id") or info.get("channel") or info.get("id") or "stream"
    return {"url": info["url"], "headers": info.get("http_headers") or {},
            "title": info.get("title") or "", "id": slugify(f"{ex}-{name}", 40), "kind": ex,
            "page": info.get("webpage_url") or source}


def hls_is_live(url: str, wait: float = 8.0) -> bool:
    """A raw .m3u8 is live if it has no end marker and its newest segment changes over a few seconds."""
    import requests
    from urllib.parse import urljoin

    def newest(u: str) -> tuple[str, bool]:
        text = requests.get(u, timeout=15).text
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if any(ln.startswith("#EXT-X-STREAM-INF") for ln in lines):  # master playlist: follow a variant
            variant = next(ln for ln in lines if not ln.startswith("#"))
            return newest(urljoin(u, variant))
        uris = [ln for ln in lines if not ln.startswith("#")]
        return (uris[-1] if uris else ""), any(ln.startswith("#EXT-X-ENDLIST") for ln in lines)

    try:
        first, ended = newest(url)
        if ended:
            return False
        time.sleep(wait)
        second, ended = newest(url)
        return not ended and second != first
    except Exception:
        return False


# ----------------------------------------------------------------------------- recorder
class Recorder:
    """ffmpeg copying the live input into short MPEG-TS pieces. Restartable; times are kept continuous."""

    def __init__(self, seg_root: Path, cfg: dict, speed: float | None):
        self.root, self.cfg, self.speed = seg_root, cfg, speed
        self.runs: list[dict] = []  # each: dir, offset (session seconds), proc
        self.wall0: float | None = None

    def start(self, src: dict) -> None:
        require_tool("ffmpeg")
        if self.wall0 is None:
            self.wall0 = time.time()
        offset = self.end_time()
        if self.runs:  # a reconnect: the gap is real time that was not recorded
            offset = max(offset, self.clock())
        d = self.root / f"run{len(self.runs) + 1:02d}"
        d.mkdir(parents=True, exist_ok=True)
        seg = int(self.cfg["live"].get("segment_seconds", 30))
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
        if src["kind"] == "file":
            cmd += ["-readrate", str(self.speed or 1)]
        else:
            hdrs = "".join(f"{k}: {v}\r\n" for k, v in src["headers"].items())
            if hdrs:
                cmd += ["-headers", hdrs]
        cmd += ["-i", src["url"], "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                "-f", "segment", "-segment_time", str(seg), "-segment_format", "mpegts",
                "-segment_list", str(d / "list.csv"), "-segment_list_type", "csv",
                "-reset_timestamps", "1", str(d / "seg_%05d.ts")]
        logf = open(d / "ffmpeg.log", "w", encoding="utf-8")
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=logf)
        self.runs.append({"dir": d, "offset": offset, "proc": proc})
        log(f"Recording started (piece length {seg}s){' after a reconnect' if len(self.runs) > 1 else ''}.")

    def clock(self) -> float:
        """Session seconds elapsed (scaled for --speed simulations)."""
        return (time.time() - (self.wall0 or time.time())) * (self.speed or 1)

    def alive(self) -> bool:
        return bool(self.runs) and self.runs[-1]["proc"].poll() is None

    def stop(self) -> None:
        for r in self.runs:
            if r["proc"].poll() is None:
                r["proc"].terminate()
                try:
                    r["proc"].wait(10)
                except subprocess.TimeoutExpired:
                    r["proc"].kill()

    def segments(self) -> list[dict]:
        """Finished pieces: [{path, start, end}] in session seconds."""
        out = []
        for r in self.runs:
            lst = r["dir"] / "list.csv"
            if not lst.exists():
                continue
            rows = list(csv.reader(open(lst, encoding="utf-8", newline="")))
            base = None
            for row in rows:
                if len(row) < 3:
                    continue
                s, e = float(row[1]), float(row[2])
                base = s if base is None else base
                out.append({"path": r["dir"] / row[0], "start": r["offset"] + s - base,
                            "end": r["offset"] + e - base})
        return out

    def end_time(self) -> float:
        segs = self.segments()
        return segs[-1]["end"] if segs else 0.0


def cut_from_segments(segs: list[dict], start: float, end: float, out: Path) -> bool:
    """Accurate cut of [start, end] (session seconds) out of the recorded pieces."""
    need = [s for s in segs if s["end"] > start and s["start"] < end]
    if not need:
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{s['path'].resolve().as_posix()}'\n" for s in need), encoding="utf-8")
    off = max(0.0, start - need[0]["start"])
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
           "-ss", f"{off:.3f}", "-t", f"{end - start:.3f}", "-map", "0:v:0", "-map", "0:a:0?"] + CUT_ENC + [str(out)]
    ok = run(cmd).returncode == 0 and out.exists() and out.stat().st_size > 10_000
    lst.unlink(missing_ok=True)
    return ok


# ----------------------------------------------------------------------------- chat
class Chat(threading.Thread):
    """Collects chat messages as {'t': session seconds, 'user', 'text'}."""

    def __init__(self, src: dict, page: str | None, recorder: Recorder, replay: str | None):
        super().__init__(daemon=True)
        self.src, self.page, self.rec, self.replay = src, page or "", recorder, replay
        self.msgs: list[dict] = []
        self.lock = threading.Lock()
        self.stop_flag = False

    def upto(self, t: float) -> list[dict]:
        with self.lock:
            return [m for m in self.msgs if m["t"] <= t]

    def _add(self, user: str, text: str, t: float | None = None) -> None:
        with self.lock:
            self.msgs.append({"t": self.rec.clock() if t is None else t, "user": user, "text": text})

    def run(self) -> None:
        try:
            if self.replay:
                self._replay()
            elif "twitch.tv/" in self.page:
                self._twitch(self.page.rstrip("/").split("twitch.tv/")[1].split("/")[0].split("?")[0].lower())
            elif "youtube.com" in self.page or "youtu.be" in self.page:
                self._chat_downloader()
            else:
                log("Live chat not available for this site: using audio signals only.")
        except Exception as e:  # chat must never stop the recording
            log(f"Live chat stopped ({type(e).__name__}: {e}); continuing with audio signals.")

    def _replay(self) -> None:
        from .chat import parse_chat_file
        msgs = parse_chat_file(Path(self.replay))
        log(f"Replaying {len(msgs)} chat messages on the recording clock.")
        for m in msgs:
            while not self.stop_flag and self.rec.clock() < m["t"]:
                time.sleep(0.2)
            if self.stop_flag:
                return
            self._add(m.get("user", ""), m.get("text", ""), m["t"])

    def _twitch(self, channel: str) -> None:
        """Anonymous read-only Twitch IRC (justinfan login: no account, cannot post)."""
        log(f"Reading Twitch chat for #{channel} (read-only).")
        rx = re.compile(r"^(?:@\S+ )?:(\w+)!\S+ PRIVMSG #\w+ :(.*)$")
        while not self.stop_flag:
            try:
                s = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=30)
                s.sendall(f"NICK justinfan{random.randint(10000, 99999)}\r\nJOIN #{channel}\r\n".encode())
                buf = b""
                while not self.stop_flag:
                    data = s.recv(4096)
                    if not data:
                        break
                    buf += data
                    *lines, buf = buf.split(b"\r\n")
                    for raw in lines:
                        line = raw.decode("utf-8", "replace")
                        if line.startswith("PING"):
                            s.sendall(line.replace("PING", "PONG", 1).encode() + b"\r\n")
                            continue
                        m = rx.match(line)
                        if m:
                            self._add(m.group(1), m.group(2))
            except (OSError, socket.timeout) as e:
                log(f"Chat connection dropped ({e}); reconnecting in 5s.")
                time.sleep(5)

    def _chat_downloader(self) -> None:
        from chat_downloader import ChatDownloader
        log("Reading YouTube live chat.")
        for m in ChatDownloader().get_chat(self.page, message_groups=["messages"]):
            if self.stop_flag:
                return
            self._add((m.get("author") or {}).get("name", ""), m.get("message", "") or "")


# ----------------------------------------------------------------------------- session
class LiveSession:
    def __init__(self, source: str, cfg: dict, agent: str = "none", layout: str | None = None,
                 no_music: bool = False, speed: float | None = None, chat_file: str | None = None,
                 publish: bool = False):
        self.source, self.cfg, self.agent = source, cfg, agent
        self.publish = publish
        self.publisher = None
        self.layout = layout or cfg["render"]["layout"]
        self.no_music, self.speed, self.chat_file = no_music, speed, chat_file
        self.lc = cfg["live"]
        self.jobs: queue.Queue = queue.Queue()
        self.pending: list[dict] = []          # candidates waiting for Claude
        self.pending_since: float | None = None
        self.peaks: list[float] = []           # accepted peak times (session seconds)
        self.scanned = 0                       # scores before this second have been examined
        self.loud = np.zeros(0, dtype=np.float32)
        self.measured: set[str] = set()
        self.n_cand = 0
        self.n_clip = 0
        self.batch_no = 0
        self.lock = threading.Lock()
        self.made: list[Path] = []

    # -- setup ---------------------------------------------------------------------------------
    def _wait_live(self) -> dict:
        every = float(self.lc.get("check_every_minutes", 2)) * 60
        while True:
            try:
                return resolve(self.source, self.cfg)
            except NotLive as e:
                if not self.lc.get("wait_for_live", True):
                    raise SystemExit(f"[clipper] {self.source} is not live: {e}")
                log(f"Not live yet ({e}). Checking again in {every / 60:.0f} min (Ctrl+C to stop).")
                time.sleep(every)

    def run(self) -> list[Path]:
        src = self._wait_live()
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        self.vid = f"live-{src['id']}-{stamp}"
        self.wdir = Path(self.cfg["paths"]["work"]) / self.vid
        self.ldir = self.wdir / "live"
        self.out_dir = Path(self.cfg["paths"]["output"]) / self.vid
        self.meta = {"id": self.vid, "title": src["title"], "url": src["page"], "local": False,
                     "source": self.source, "duration": 0}
        write_json(self.wdir / "meta.json", self.meta)
        log(f"Live session {self.vid}: clips will appear in {self.out_dir}")

        self.rec = Recorder(self.ldir / "segments", self.cfg, self.speed if src["kind"] == "file" else None)
        self.rec.start(src)
        self.chat = Chat(src, src["page"], self.rec, self.chat_file)
        self.chat.start()
        worker = threading.Thread(target=self._worker, daemon=True)
        worker.start()
        if self.publish:
            from .publish import Publisher, enabled_platforms
            plats = enabled_platforms(self.cfg)
            if plats:
                log(f"Auto-posting to {', '.join(plats)} as clips are made "
                    f"(mode: {self.cfg['publish'].get('mode', 'auto')}).")
                self.publisher = Publisher(self.cfg).start()
            else:
                log("--publish given but no platform is enabled in config.yaml -> publish.")

        last_end, last_progress = 0.0, time.time()
        limit = float(self.lc.get("end_after_offline_minutes", 5)) * 60
        try:
            while True:
                time.sleep(float(self.lc.get("poll_seconds", 10)) / (self.speed or 1))
                self._measure()
                self._scan(final=False)
                self._maybe_flush_batch(force=False)
                self._status()
                end_now = self.rec.end_time()
                if end_now > last_end:
                    last_end, last_progress = end_now, time.time()
                if self.rec.alive() and time.time() - last_progress < limit:
                    continue
                if src["kind"] == "file":
                    log("Recording finished.")
                    break
                if time.time() - last_progress > limit:
                    log("No new video for a while: the stream has ended.")
                    break
                if not self.rec.alive():
                    try:
                        src = resolve(self.source, self.cfg)
                        log("Connection dropped but the stream is still live: reconnecting.")
                        self.rec.start(src)
                    except NotLive:
                        log("Stream looks offline; waiting in case it comes back.")
                        time.sleep(20)
        except KeyboardInterrupt:
            log("Stopping: finishing the clips already found (Ctrl+C again to quit immediately).")
        finally:
            self.rec.stop()
            self.chat.stop_flag = True
            time.sleep(1)
            self._measure()
            self._scan(final=True)
            self.jobs.join()                     # let queued moments finish transcribing first
            self._maybe_flush_batch(force=True)  # then Claude judges whatever is still waiting
            self.jobs.put(None)
            worker.join()
            self._finish_recording()
            self._status(done=True)
            if self.publisher:
                self.publisher.finish(wait=bool(self.cfg["publish"].get("finish_queue_after_stream", True)))
        log(f"Live session done: {len(self.made)} clip(s) in {self.out_dir}")
        return self.made

    # -- watching ------------------------------------------------------------------------------
    def _measure(self) -> None:
        for s in self.rec.segments():
            key = str(s["path"])
            if key in self.measured or not s["path"].exists():
                continue
            self.measured.add(key)
            db = audio_loudness(s["path"])
            i0 = int(round(s["start"]))
            need = i0 + len(db)
            if need > len(self.loud):
                grown = np.full(need, np.nan, dtype=np.float32)
                grown[: len(self.loud)] = self.loud
                self.loud = grown
            self.loud[i0: i0 + len(db)] = db
        self.meta["duration"] = self.rec.end_time()

    def _scan(self, final: bool) -> None:
        end = self.rec.end_time()
        warm = float(self.lc.get("warmup_minutes", 4)) * 60
        if end < min(warm, 120) or len(self.loud) < 60:
            return
        loud = self.loud.copy()
        if np.isnan(loud).all():
            return
        loud[np.isnan(loud)] = np.nanmin(loud)
        hc = self.cfg["highlights"]
        comps = build_scores(end, loud, self.chat.upto(end), [], self.cfg)
        score = comps["score"]
        settle = end if final else end - float(hc["post_roll"]) - float(hc["chat_delay"]) - 5
        start_scan = max(self.scanned, 30, int(warm * 0.5))
        thr = float(self.lc.get("candidate_score", 0.8))
        half = int(hc.get("min_gap", 45)) // 2
        for i in range(start_scan, int(min(settle, len(score) - 1))):
            v = score[i]
            if v < thr or v < score[max(0, i - half): i + half + 1].max():
                continue
            if any(abs(i - p) < float(hc["min_gap"]) for p in self.peaks):
                continue
            self.peaks.append(float(i))
            self.n_cand += 1
            cand = {"id": f"c{self.n_cand:03d}", "peak": float(i), "score": float(v),
                    "start": max(0.0, i - float(hc["pre_roll"])), "end": min(end, i + float(hc["post_roll"])),
                    "signals": {k: float(a[i]) for k, a in comps.items() if k != "score" and i < len(a)}}
            log(f"Highlight candidate {cand['id']} at {self._hms(i)} (score {v:.2f})")
            self.jobs.put(("candidate", cand))
        self.scanned = max(self.scanned, int(settle))

    def _maybe_flush_batch(self, force: bool) -> None:
        if self.agent == "none":
            return
        with self.lock:
            if not self.pending:
                return
            wait = float(self.lc.get("agent_max_wait_minutes", 5)) * 60 / (self.speed or 1)
            due = (len(self.pending) >= int(self.lc.get("agent_batch_size", 3))
                   or time.time() - (self.pending_since or time.time()) >= wait)
            if not (force or due):
                return
            batch, self.pending, self.pending_since = self.pending, [], None
        self.jobs.put(("batch", batch))

    # -- worker --------------------------------------------------------------------------------
    def _worker(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                self.jobs.task_done()
                return
            try:
                kind, data = job
                if kind == "candidate":
                    self._prepare(data)
                else:
                    self._judge(data)
            except Exception as e:  # one bad clip must not end the session
                log(f"   clip failed: {type(e).__name__}: {e}")
            finally:
                self.jobs.task_done()

    def _prepare(self, c: dict) -> None:
        """Cut a padded window, transcribe it, tidy the clip edges; then render or queue for the agent."""
        segs = self.rec.segments()
        pad = float(self.cfg["whisper"]["pad_seconds"])
        w0, w1 = max(0.0, c["start"] - pad), min(self.rec.end_time(), c["end"] + pad + 6)
        win = self.ldir / "windows" / f"{c['id']}.mp4"
        if not cut_from_segments(segs, w0, w1, win):
            log(f"   {c['id']}: could not cut the recording, skipped")
            return
        words = [{**w, "s": round(w["s"] + w0, 2), "e": round(w["e"] + w0, 2)}
                 for w in transcribe_window(win, 0, w1 - w0, self.cfg)]
        c.update(tx_start=w0, tx_end=w1, words=words)
        snap_to_speech(c, words, self.cfg)
        msgs = self.chat.upto(self.rec.end_time())
        c["chat"] = chat_sample(msgs, c["start"], c["end"], float(self.cfg["highlights"]["chat_delay"])) if msgs else {}
        c["window"] = str(win)

        if self.agent != "none":
            with self.lock:
                self.pending.append(c)
                self.pending_since = self.pending_since or time.time()
            log(f"   {c['id']} ready for {label(self.agent)} ({len(self.pending)} waiting)")
            return
        if c["score"] < float(self.lc.get("min_score", 1.2)):
            log(f"   {c['id']}: score {c['score']:.2f} below live.min_score, not clipped")
            return
        self._render({"candidate_id": c["id"], "start": c["start"], "end": c["end"],
                      "title": _guess_title(c), "music_mood": _guess_mood(c),
                      "reason": f"live auto: score {c['score']:.2f}"}, c)

    def _judge(self, batch: list[dict]) -> None:
        self.batch_no += 1
        bdir = self.ldir / f"batch_{self.batch_no:03d}"
        write_json(bdir / "meta.json", self.meta)
        write_candidates(bdir, self.meta, batch)
        rel = bdir.relative_to(ROOT).as_posix() if bdir.is_relative_to(ROOT) else bdir.as_posix()
        extra = ("This is a LIVE stream: these candidates are the newest moments; keep every real "
                 "highlight, skip weak ones. ")
        by_id = {c["id"]: c for c in batch}
        if agent_select_dir(self.agent, rel, bdir, self.cfg, extra=extra):
            picks = load_selection(bdir, self.cfg)
        else:
            picks = [{"candidate_id": c["id"], "start": c["start"], "end": c["end"], "title": _guess_title(c),
                      "music_mood": _guess_mood(c), "reason": "fallback: score"}
                     for c in batch if c["score"] >= float(self.lc.get("min_score", 1.2))]
        log(f"Batch {self.batch_no}: {len(picks)} of {len(batch)} candidates become clips.")
        for p in picks:
            self._render(p, by_id.get(p.get("candidate_id"), batch[0]))

    def _render(self, pick: dict, cand: dict) -> None:
        self.n_clip += 1
        c = {**pick, "n": self.n_clip}
        from .english import clean_title
        c["title"] = clean_title(c.get("title") or "", c.get("music_mood", "none"), seed=self.n_clip)
        c["music_mood"] = c.get("music_mood", "none")
        end = self.rec.end_time()
        c["start"], c["end"] = max(0.0, float(c["start"])), min(end, float(c["end"]))
        name = f"{self.n_clip:02d}_{slugify(c['title'] or c['candidate_id'])}"
        clip_dir = self.wdir / "clips" / name
        src = clip_dir / "source.mp4"
        if not cut_from_segments(self.rec.segments(), c["start"], c["end"], src):
            log(f"   {name}: cut failed, skipped")
            return
        log(f"Clip {self.n_clip}: {name}  ({self._hms(c['start'])} - {self._hms(c['end'])})")
        geo = detect_geo(src, clip_dir, self.layout, self.cfg)
        stream_geo = best_stream_geo(self.wdir, [geo])
        if geo is None and stream_geo is not None and self.layout != "blur":
            geo = stream_geo
        words = cand.get("words") or []
        if not (cand.get("tx_start", 1e18) <= c["start"] and c["end"] <= cand.get("tx_end", -1)):
            words = [{**w, "s": w["s"] + c["start"], "e": w["e"] + c["start"]}
                     for w in transcribe_window(src, 0, c["end"] - c["start"], self.cfg)]
        out = render_one(c, name, clip_dir, src, geo, words, self.cfg, self.layout, self.no_music,
                         self.meta, self.out_dir)
        self.made.append(out)
        if self.publisher:
            from .publish import enqueue
            enqueue(out, self.cfg, extra={"score": round(float(cand.get("score", 0)), 2)})
        lag = self.rec.clock() - c["end"]
        log(f"   clip ready {lag / 60:.1f} min after the moment")

    # -- bookkeeping ---------------------------------------------------------------------------
    @staticmethod
    def _hms(t: float) -> str:
        t = int(t)
        return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"

    def _status(self, done: bool = False) -> None:
        write_json(self.ldir / "status.json", {
            "session": self.vid, "recorded_seconds": round(self.rec.end_time(), 1),
            "candidates": self.n_cand, "clips": [str(p) for p in self.made],
            "waiting_for_agent": len(self.pending), "waiting_for_claude": len(self.pending), "done": done,
            "updated": datetime.now().isoformat(timespec="seconds")})

    def _finish_recording(self) -> None:
        segs = self.rec.segments()
        if not segs:
            return
        if not self.lc.get("keep_recording", True):
            for s in segs:
                s["path"].unlink(missing_ok=True)
            return
        lst = self.ldir / "all_segments.txt"
        lst.write_text("".join(f"file '{s['path'].resolve().as_posix()}'\n" for s in segs), encoding="utf-8")
        full = self.wdir / "recording.mp4"
        proc = run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c", "copy", "-movflags", "+faststart", str(full)])
        if proc.returncode == 0 and full.exists():
            for s in segs:
                s["path"].unlink(missing_ok=True)
            log(f"Full recording saved: {full}  (re-run `analyze` on it any time for a full VOD pass)")
        lst.unlink(missing_ok=True)


def main(args, cfg) -> None:
    sess = LiveSession(args.source, cfg, agent=args.agent, layout=args.layout, no_music=args.no_music,
                       speed=args.speed, chat_file=args.chat, publish=__import__("clipper.__main__", fromlist=["should_publish"]).should_publish(args, cfg))
    sess.run()
    with open(Path(cfg["paths"]["work"]) / "last_live_session.json", "w", encoding="utf-8") as f:
        json.dump({"session": sess.vid, "clips": [str(p) for p in sess.made]}, f, indent=2)
