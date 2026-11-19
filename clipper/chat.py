"""Chat replay download. Chat is optional: without it the tool falls back to audio signals."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .util import log, run


def _load_jsonl(path: Path) -> list[dict]:
    msgs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                msgs.append(json.loads(line))
    return msgs


def _save_jsonl(path: Path, msgs: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for m in msgs:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")


def parse_chat_file(path: Path) -> list[dict]:
    """Accepts our chat.jsonl, a TwitchDownloader JSON, or a chat-downloader JSON list."""
    if path.suffix == ".jsonl":
        return _load_jsonl(path)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    msgs = []
    if isinstance(data, dict) and "comments" in data:  # TwitchDownloaderCLI format
        for c in data["comments"]:
            body = (c.get("message") or {}).get("body", "")
            msgs.append({"t": float(c.get("content_offset_seconds", 0)),
                         "user": (c.get("commenter") or {}).get("display_name", ""), "text": body})
    elif isinstance(data, list):  # chat-downloader --output x.json
        for c in data:
            if "time_in_seconds" in c:
                msgs.append({"t": float(c["time_in_seconds"]),
                             "user": (c.get("author") or {}).get("name", ""),
                             "text": c.get("message", "")})
    return sorted(msgs, key=lambda m: m["t"])


def _via_twitchdownloader(meta: dict, wdir: Path) -> list[dict] | None:
    exe = shutil.which("TwitchDownloaderCLI")
    if not exe or "twitch" not in meta["extractor"]:
        return None
    vod_id = str(meta.get("native_id", "")).lstrip("v")
    out = wdir / "chat_twitchdownloader.json"
    log("Downloading chat with TwitchDownloaderCLI...")
    proc = run([exe, "chatdownload", "--id", vod_id, "-o", str(out), "--collision", "Overwrite"])
    if proc.returncode != 0 or not out.exists():
        return None
    return parse_chat_file(out)


def _via_chat_downloader(meta: dict) -> list[dict] | None:
    try:
        from chat_downloader import ChatDownloader
    except ImportError:
        return None
    log("Downloading chat replay (this can take a few minutes on long VODs)...")
    msgs = []
    try:
        chat = ChatDownloader().get_chat(meta["url"], message_groups=["messages"])
        for i, m in enumerate(chat):
            t = m.get("time_in_seconds")
            if t is None:
                continue
            msgs.append({"t": float(t), "user": (m.get("author") or {}).get("name", ""),
                         "text": m.get("message", "") or ""})
            if i and i % 20000 == 0:
                log(f"  ...{i} messages")
    except Exception as e:  # chat APIs change often; never fail the whole run on chat
        log(f"Chat download stopped: {type(e).__name__}: {e}")
    return msgs or None


def get_chat(meta: dict, wdir: Path, chat_file: str | None = None) -> list[dict]:
    cache = wdir / "chat.jsonl"
    if chat_file:
        msgs = parse_chat_file(Path(chat_file))
        _save_jsonl(cache, msgs)
        return msgs
    if cache.exists():
        return _load_jsonl(cache)
    if meta["local"]:
        log("Local file and no --chat given: using audio signals only.")
        return []
    msgs = _via_twitchdownloader(meta, wdir) or _via_chat_downloader(meta) or []
    if msgs:
        log(f"Chat: {len(msgs)} messages.")
        _save_jsonl(cache, msgs)
    else:
        log("No chat replay available: using audio (and replay heatmap) signals only.")
    return msgs
