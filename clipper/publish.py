"""Post finished clips to YouTube Shorts and Instagram Reels, also while a live stream is running.

    python -m clipper publish setup youtube --client-secret client_secret.json   # once, opens a browser
    python -m clipper publish setup instagram                                     # once, asks for app id/secret/token
    python -m clipper publish status                                             # queue, limits, token expiry
    python -m clipper publish post output/<id>/01_clip.mp4 [...]                  # post specific clips now
    python -m clipper publish approve <name|all>                                  # review mode: release clips
    python -m clipper publish run                                                # post whatever is queued

Live / run / render take --publish to queue every finished clip automatically.

Safety rails (config.yaml -> publish): spacing between posts, per-hour and per-day caps, a review mode,
and a check of Instagram's own publishing limit before each post. Sign-in tokens live in secrets/
(git-ignored) and are never printed.
"""
from __future__ import annotations

import json
import shutil
import os
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .util import ROOT, log, read_json, write_json

SECRETS = ROOT / "secrets"
YT_TOKEN = SECRETS / "youtube_token.json"
IG_FILE = SECRETS / "instagram.json"
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
PLATFORMS = ("youtube", "instagram")


# ----------------------------------------------------------------------------------- queue file
def _queue_path(cfg: dict) -> Path:
    return Path(cfg["paths"]["work"]) / "publish" / "queue.json"


_QLOCK = threading.Lock()


def _load(cfg: dict) -> dict:
    p = _queue_path(cfg)
    if p.exists():
        try:
            return read_json(p)
        except (json.JSONDecodeError, OSError):
            pass
    return {"items": []}


def _save(cfg: dict, q: dict) -> None:
    p = _queue_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(q, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)  # atomic: a crash never leaves half a queue


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def enabled_platforms(cfg: dict) -> list[str]:
    pc = cfg.get("publish") or {}
    return [p for p in PLATFORMS if (pc.get(p) or {}).get("enabled")]


def clip_info(clip: Path) -> dict:
    """Title, music credit, stream link and reason for a rendered clip (from its .json / .txt)."""
    info = {"title": "", "credit": "", "stream_url": "", "reason": ""}
    j = clip.with_suffix(".json")
    if j.exists():
        try:
            info.update(read_json(j))
        except (json.JSONDecodeError, OSError):
            pass
    t = clip.with_suffix(".txt")
    if t.exists() and not j.exists():  # older clips without the .json sidecar
        lines = t.read_text(encoding="utf-8").splitlines()
        for i, ln in enumerate(lines):
            if ln.startswith("Title:") and not info["title"]:
                info["title"] = ln[6:].strip()
            elif ln.startswith("Stream:") and not info["stream_url"]:
                url = ln[ln.rfind("(") + 1: ln.rfind(")")] if "(" in ln else ""
                info["stream_url"] = url.strip()
            elif ln.strip() == "Music:" and not info["credit"]:
                info["credit"] = "\n".join(lines[i + 1:]).strip()
    import re
    if not info["title"] or re.fullmatch(r"c\d+", info["title"].strip(), re.I):
        stem = clip.stem.split("_", 1)[-1].replace("-", " ").strip()
        info["title"] = (stem.upper() if not re.fullmatch(r"c\d+", stem, re.I)
                         else (info.get("stream_title") or "Stream highlight"))
    return info


def enqueue(clip: Path, cfg: dict, approved: bool | None = None, extra: dict | None = None) -> None:
    plats = enabled_platforms(cfg)
    if not plats:
        log("   publish: no platform enabled in config.yaml -> publish (nothing queued)")
        return
    mode = (cfg.get("publish") or {}).get("mode", "auto")
    with _QLOCK:
        q = _load(cfg)
        key = str(clip.resolve())
        if any(it["file"] == key for it in q["items"]):
            return
        item = {"file": key, "added": _now(), "approved": (mode != "review") if approved is None else approved,
                "info": {**clip_info(clip), **(extra or {})},
                "targets": {p: {"status": "pending", "tries": 0} for p in plats}}
        q["items"].append(item)
        _save(cfg, q)
    state = "queued" if item["approved"] else "waiting for approval (publish approve)"
    log(f"   publish: {clip.name} {state} for {', '.join(plats)}")


# ------------------------------------------------------------------------------------- captions
def _hashtags(cfg: dict, platform: str) -> str:
    pc = cfg.get("publish") or {}
    tags = list(pc.get("hashtags") or []) + list((pc.get(platform) or {}).get("hashtags") or [])
    seen, out = set(), []
    for t in tags:
        t = t if t.startswith("#") else "#" + t
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return " ".join(out)


def _text(cfg: dict, platform: str, info: dict) -> tuple[str, str]:
    """(title, description/caption) for a platform."""
    from .english import clean_text, clean_title
    pc = cfg.get("publish") or {}
    title = clean_title(info.get("title") or "", info.get("music_mood", "none"))
    parts = [title]
    line = (pc.get("description") or "").strip()
    if line and ("{stream_url}" not in line or info.get("stream_url")):
        parts.append(line.format(stream_url=info.get("stream_url", ""), title=title))
    tags = _hashtags(cfg, platform)
    if tags:
        parts.append(tags)
    if info.get("credit"):
        parts.append("Music: " + info["credit"].replace("\n", " "))
    body = "\n\n".join(clean_text(p) for p in parts if p)
    if platform == "youtube":
        yt_title = title.title() if title.isupper() else title
        if "#shorts" not in yt_title.lower() and len(yt_title) <= 90:
            yt_title += " #shorts"
        return yt_title[:100], body[:4900]
    return title, body[:2150]


# -------------------------------------------------------------------------------------- YouTube
def youtube_setup(client_secret: str) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(client_secret, YT_SCOPES)
    log("A browser window opens: sign in with the Google account that owns your YouTube channel.")
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    SECRETS.mkdir(exist_ok=True)
    YT_TOKEN.write_text(creds.to_json(), encoding="utf-8")
    log(f"YouTube connected. Token saved in {YT_TOKEN} (keep this folder private).")


def _youtube():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    if not YT_TOKEN.exists():
        raise RuntimeError("YouTube is not connected: run  python -m clipper publish setup youtube")
    creds = Credentials.from_authorized_user_file(str(YT_TOKEN), YT_SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            YT_TOKEN.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise RuntimeError("YouTube sign-in expired: run  python -m clipper publish setup youtube")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def post_youtube(clip: Path, info: dict, cfg: dict) -> dict:
    from googleapiclient.http import MediaFileUpload
    yc = (cfg.get("publish") or {}).get("youtube") or {}
    title, desc = _text(cfg, "youtube", info)
    body = {
        "snippet": {"title": title, "description": desc, "categoryId": str(yc.get("category_id", 20)),
                    "tags": [t.lstrip("#") for t in _hashtags(cfg, "youtube").split()][:15]},
        "status": {"privacyStatus": yc.get("privacy", "public"), "selfDeclaredMadeForKids": False,
                   "containsSyntheticMedia": False},
    }
    media = MediaFileUpload(str(clip), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    req = _youtube().videos().insert(part="snippet,status", body=body, media_body=media,
                                     notifySubscribers=bool(yc.get("notify_subscribers", True)))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    vid = resp["id"]
    return {"id": vid, "url": f"https://youtube.com/shorts/{vid}"}


# ------------------------------------------------------------------------------------ Instagram
def _ig() -> dict:
    if not IG_FILE.exists():
        raise RuntimeError("Instagram is not connected: run  python -m clipper publish setup instagram")
    return read_json(IG_FILE)


def _graph(cfg: dict) -> str:
    ver = ((cfg.get("publish") or {}).get("instagram") or {}).get("api_version", "v25.0")
    return f"https://graph.facebook.com/{ver}"


def instagram_setup(cfg: dict) -> None:
    """Exchange a short-lived user token (Graph API Explorer) for a 60-day one and find the IG account."""
    import getpass

    import requests
    print("You need a Meta app (developers.facebook.com, type Business, with Facebook Login for Business)\n"
          "and a user token from Graph API Explorer with: instagram_basic, instagram_content_publish,\n"
          "pages_show_list, pages_read_engagement. See README -> 'Posting to Instagram'.\n")
    app_id = input("Meta App ID: ").strip()
    secret = getpass.getpass("Meta App Secret (hidden): ").strip()
    short = getpass.getpass("User access token from Graph API Explorer (hidden): ").strip()
    g = _graph(cfg)
    r = requests.get(f"{g}/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": secret,
        "fb_exchange_token": short}, timeout=30)
    if r.status_code != 200:
        raise SystemExit(f"[clipper] Token exchange failed: {r.json().get('error', {}).get('message', r.text[:200])}")
    data = r.json()
    token, expires = data["access_token"], int(data.get("expires_in", 60 * 86400))
    pages = requests.get(f"{g}/me/accounts", params={
        "fields": "name,instagram_business_account{id,username}", "access_token": token}, timeout=30).json()
    accounts = [(p["name"], p["instagram_business_account"]) for p in pages.get("data", [])
                if p.get("instagram_business_account")]
    if not accounts:
        raise SystemExit("[clipper] No Instagram professional account linked to your Facebook Pages was found.\n"
                         "Switch Instagram to a Creator/Business account and link it to a Facebook Page.")
    for i, (page, ig) in enumerate(accounts, 1):
        print(f"  {i}. @{ig.get('username')}  (Page: {page})")
    pick = accounts[0] if len(accounts) == 1 else accounts[int(input("Which account? ") or 1) - 1]
    SECRETS.mkdir(exist_ok=True)
    write_json(IG_FILE, {"ig_user_id": pick[1]["id"], "username": pick[1].get("username"),
                         "token": token, "expires_at": (datetime.now() + timedelta(seconds=expires)).isoformat()})
    log(f"Instagram connected: @{pick[1].get('username')}. Token valid ~{expires // 86400} days; "
        "re-run this setup before it expires (publish status shows the date).")


def _ig_ready_copy(clip: Path) -> Path:
    """Reels accept AAC up to 128 kbps / 48 kHz: re-mux the audio, keep the video untouched."""
    tmp = Path(tempfile.mkdtemp()) / clip.name
    proc = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(clip), "-c:v", "copy", "-c:a", "aac",
                           "-b:a", "128k", "-ar", "48000", "-movflags", "+faststart", str(tmp)])
    return tmp if proc.returncode == 0 and tmp.exists() else clip


def instagram_quota(cfg: dict) -> tuple[int, int] | None:
    import requests
    ig = _ig()
    r = requests.get(f"{_graph(cfg)}/{ig['ig_user_id']}/content_publishing_limit",
                     params={"fields": "quota_usage,config", "access_token": ig["token"]}, timeout=30)
    if r.status_code != 200:
        return None
    d = (r.json().get("data") or [{}])[0]
    return int(d.get("quota_usage", 0)), int((d.get("config") or {}).get("quota_total", 100))


def _ig_safe_copy(clip: Path) -> Path:
    """Conservative Reels encode (30 fps, H.264 High 4.1, ~8 Mbps, AAC 128k): smaller and plainer,
    for when Meta refuses the full-quality file."""
    tmp = Path(tempfile.mkdtemp()) / clip.name
    proc = subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-i", str(clip),
        "-vf", "scale=1080:1920:flags=lanczos,fps=30,format=yuv420p",
        "-c:v", "libx264", "-preset", "medium", "-profile:v", "high", "-level", "4.1",
        "-crf", "20", "-maxrate", "8M", "-bufsize", "16M",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(tmp)])
    return tmp if proc.returncode == 0 and tmp.exists() else clip


def _ig_container_info(g: str, cid: str, tok: str) -> str:
    try:
        import requests
        d = requests.get(f"{g}/{cid}", params={"fields": "status_code,status,video_status",
                                                "access_token": tok}, timeout=30).json()
        return json.dumps({k: d[k] for k in ("status_code", "status", "video_status") if k in d})[:400]
    except Exception:
        return ""


def _ig_upload_once(clip_file: Path, caption: str, g: str, uid: str, tok: str) -> str:
    """Create a REELS container, send the bytes, wait for processing. Returns the container id."""
    import requests
    r = requests.post(f"{g}/{uid}/media", data={
        "media_type": "REELS", "upload_type": "resumable", "caption": caption, "access_token": tok}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError("container: " + r.json().get("error", {}).get("message", r.text[:200]))
    cid = r.json()["id"]
    upload_uri = r.json().get("uri") or f"https://rupload.facebook.com/ig-api-upload/{g.rsplit('/', 1)[1]}/{cid}"
    size = clip_file.stat().st_size
    with open(clip_file, "rb") as fh:
        up = requests.post(upload_uri, data=fh, timeout=900, headers={
            "Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(size),
            "Content-Type": "application/octet-stream", "Content-Length": str(size)})
    if up.status_code != 200:
        raise RuntimeError(f"upload: HTTP {up.status_code} {up.text[:200]} | container {_ig_container_info(g, cid, tok)}")
    for _ in range(40):  # up to ~10 minutes of processing
        time.sleep(15)
        st = requests.get(f"{g}/{cid}", params={"fields": "status_code,status", "access_token": tok},
                          timeout=30).json()
        code = st.get("status_code")
        if code == "FINISHED":
            return cid
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"processing {code}: {st.get('status', '')}")
    raise RuntimeError("processing took longer than 10 minutes")


def post_instagram(clip: Path, info: dict, cfg: dict) -> dict:
    import requests
    ig, g = _ig(), _graph(cfg)
    tok, uid = ig["token"], ig["ig_user_id"]
    quota = instagram_quota(cfg)
    if quota and quota[0] >= quota[1]:
        raise RuntimeError(f"Instagram's limit reached ({quota[0]}/{quota[1]} posts in 24 h)")
    _, caption = _text(cfg, "instagram", info)
    cid, errors = None, []
    # 1st try: the full-quality clip. 2nd try: a plainer, smaller encode (Meta's upload sometimes
    # refuses a file with "ProcessingFailedError" and gives no reason; a conservative encode often passes).
    for label, make in (("full quality", _ig_ready_copy), ("safe encode", _ig_safe_copy)):
        copy = None
        try:
            copy = make(clip)
            cid = _ig_upload_once(copy, caption, g, uid, tok)
            if errors:
                log(f"   publish: instagram accepted the {label} version")
            break
        except RuntimeError as e:
            errors.append(f"{label}: {e}")
            log(f"   publish: instagram refused the {label} version ({str(e)[:300]})")
        finally:
            if copy is not None and copy != clip:   # temporary re-encodes are not kept
                shutil.rmtree(copy.parent, ignore_errors=True)
    if cid is None:
        raise RuntimeError(" || ".join(errors)[:600])
    pub = requests.post(f"{g}/{uid}/media_publish", data={"creation_id": cid, "access_token": tok}, timeout=60)
    if pub.status_code != 200:
        raise RuntimeError("publish: " + pub.json().get("error", {}).get("message", pub.text[:200]))
    mid = pub.json()["id"]
    link = requests.get(f"{g}/{mid}", params={"fields": "permalink", "access_token": tok}, timeout=30).json()
    return {"id": mid, "url": link.get("permalink", "")}


POSTERS = {"youtube": post_youtube, "instagram": post_instagram}


# ------------------------------------------------------------------------------------- the worker
def _recent(q: dict, platform: str, within: timedelta) -> list[datetime]:
    cut = datetime.now() - within
    out = []
    for it in q["items"]:
        t = it["targets"].get(platform, {})
        if t.get("status") == "posted" and t.get("at"):
            at = datetime.fromisoformat(t["at"])
            if at >= cut:
                out.append(at)
    return out


def _next_due(cfg: dict, q: dict, platform: str) -> tuple[dict | None, str]:
    pc = cfg.get("publish") or {}
    pl = pc.get(platform) or {}
    day = _recent(q, platform, timedelta(days=1))
    hour = [t for t in day if t >= datetime.now() - timedelta(hours=1)]
    if len(day) >= int(pl.get("max_per_day", 20)):
        return None, "daily cap reached"
    if len(hour) >= int(pc.get("max_per_hour", 4)):
        return None, "hourly cap reached"
    gap = float(pc.get("min_minutes_between_posts", 10))
    if day and datetime.now() - max(day) < timedelta(minutes=gap):
        return None, "spacing"
    for it in q["items"]:
        t = it["targets"].get(platform) or {}
        if t.get("status") == "posting" and t.get("started") and \
                datetime.now() - datetime.fromisoformat(t["started"]) > timedelta(minutes=30):
            t.update(status="retry", error="interrupted while posting")  # e.g. the PC was closed
    for it in q["items"]:
        t = it["targets"].get(platform)
        if not it.get("approved") or not t or t.get("status") not in ("pending", "retry"):
            continue
        if t.get("next_try") and datetime.fromisoformat(t["next_try"]) > datetime.now():
            continue
        if not Path(it["file"]).exists():
            t.update(status="failed", error="file missing")
            continue
        return it, ""
    return None, "nothing queued"


def _poster_lock(cfg: dict) -> bool:
    """Only one process posts at a time (e.g. a live run and a `publish run` window), so nothing is
    posted twice and the 30-minute gap holds. The lock is refreshed every loop; stale after 2 minutes."""
    p = _queue_path(cfg).with_name("poster.lock")
    p.parent.mkdir(parents=True, exist_ok=True)
    me = str(os.getpid())
    try:
        if p.exists() and time.time() - p.stat().st_mtime < 120 and p.read_text().strip() != me:
            return False
        p.write_text(me)
        return True
    except OSError:
        return False


def other_poster_running(cfg: dict) -> bool:
    p = _queue_path(cfg).with_name("poster.lock")
    try:
        return p.exists() and time.time() - p.stat().st_mtime < 120 and p.read_text().strip() != str(os.getpid())
    except OSError:
        return False


def next_post_times(cfg: dict) -> dict[str, str]:
    """When each platform may post next (the 30-minute gap), for `publish status`."""
    q, out = _load(cfg), {}
    gap = timedelta(minutes=float((cfg.get("publish") or {}).get("min_minutes_between_posts", 30)))
    for pl in enabled_platforms(cfg):
        recent = _recent(q, pl, timedelta(days=1))
        nxt = max(recent) + gap if recent else datetime.now()
        out[pl] = "now" if nxt <= datetime.now() else nxt.strftime("%H:%M")
    return out


def post_once(cfg: dict) -> int:
    """Post at most one clip per platform if it's due. Returns how many posts were made."""
    made = 0
    if not _poster_lock(cfg):
        return 0
    for platform in enabled_platforms(cfg):
        with _QLOCK:
            q = _load(cfg)
            it, _ = _next_due(cfg, q, platform)
            if it is None:
                _save(cfg, q)
                continue
            it["targets"][platform].update(status="posting", started=_now())
            _save(cfg, q)
        clip = Path(it["file"])
        try:
            res = POSTERS[platform](clip, it["info"], cfg)
            upd = {"status": "posted", "at": _now(), **res}
            log(f"   publish: {clip.name} posted to {platform}: {res.get('url', '')}")
            made += 1
        except Exception as e:  # never let one failed upload stop the stream
            tries = it["targets"][platform].get("tries", 0) + 1
            msg = f"{type(e).__name__}: {str(e)[:300]}"
            # Every highlight must get posted: keep retrying (5, 10, 15 ... up to 60 min apart) instead of
            # giving up; a sign-in problem fixed later (publish setup) is picked up on the next try.
            wait = min(60, 5 * tries)
            upd = {"status": "retry", "tries": tries, "error": msg,
                   "next_try": (datetime.now() + timedelta(minutes=wait)).isoformat(timespec="seconds")}
            log(f"   publish: {clip.name} -> {platform} failed ({msg}); retrying in {wait} min")
            if tries == 6:
                log(f"   publish: {platform} keeps failing; check `publish status` (sign-in may need renewing)")
        with _QLOCK:
            q = _load(cfg)
            done_item = None
            for x in q["items"]:
                if x["file"] == it["file"]:
                    x["targets"][platform].update(upd)
                    if all(t.get("status") == "posted" for t in x["targets"].values()) and not x.get("deleted"):
                        done_item = x
            if done_item is not None and (cfg.get("publish") or {}).get("delete_after_post", True):
                done_item["deleted"] = _delete_clip(Path(done_item["file"]), cfg, done_item)
            _save(cfg, q)
    return made


def _delete_clip(clip: Path, cfg: dict, q_item: dict) -> str:
    """After a clip is live on every platform: delete the video, its .txt/.json notes and its working
    folder (source cut, captions). The post links stay in the queue file and in posted.csv."""
    import csv
    log_path = _queue_path(cfg).with_name("posted.csv")
    try:
        new = not log_path.exists()
        with open(log_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["posted_at", "clip", "title", "youtube", "instagram"])
            t = (q_item or {}).get("targets", {})
            w.writerow([_now(), clip.name, (q_item or {}).get("info", {}).get("title", ""),
                        t.get("youtube", {}).get("url", ""), t.get("instagram", {}).get("url", "")])
    except OSError:
        pass
    removed = []
    for f in (clip, clip.with_suffix(".txt"), clip.with_suffix(".json")):
        try:
            f.unlink()
            removed.append(f.name)
        except FileNotFoundError:
            pass
        except OSError as e:
            log(f"   publish: could not delete {f.name} ({e}); will leave it")
    work_clip = Path(cfg["paths"]["work"]) / clip.parent.name / "clips" / clip.stem
    if work_clip.is_dir():
        shutil.rmtree(work_clip, ignore_errors=True)
    out_dir = clip.parent
    try:
        if out_dir.is_dir() and not any(out_dir.iterdir()):
            out_dir.rmdir()
    except OSError:
        pass
    log(f"   publish: {clip.name} is live everywhere; deleted the local clip ({', '.join(removed) or 'already gone'})")
    return _now()


def pending_count(cfg: dict) -> int:
    q = _load(cfg)
    return sum(1 for it in q["items"] if it.get("approved")
               for t in it["targets"].values() if t.get("status") in ("pending", "retry", "posting"))


class Publisher:
    """Background poster used by live mode: checks the queue every 20 s."""

    def __init__(self, cfg: dict):
        self.cfg, self.stop = cfg, threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> "Publisher":
        self._preflight()
        self.thread.start()
        return self

    def _preflight(self) -> None:
        for p in enabled_platforms(self.cfg):
            try:
                _youtube() if p == "youtube" else _ig()
            except Exception as e:
                log(f"publish: {p} is not ready ({e}). Clips will wait in the queue.")

    def _loop(self) -> None:
        while not self.stop.is_set():
            try:
                post_once(self.cfg)
            except Exception as e:
                log(f"publish: {type(e).__name__}: {e}")
            self.stop.wait(20)

    def finish(self, wait: bool) -> None:
        """End of the stream. Posting never stops just because the stream did:
        - background poster installed (`publish install`): it carries on; this window can close;
        - otherwise this window keeps posting the queue at the same interval until it is empty."""
        n = pending_count(self.cfg)
        if n and (other_poster_running(self.cfg) or task_installed()):
            if not other_poster_running(self.cfg):
                start_task()
            log(f"publish: {n} post(s) queued; the background poster keeps posting them every "
                f"{self.cfg['publish'].get('min_minutes_between_posts', 30)} min. You can close this window.")
        elif n and wait:
            log(f"publish: {n} post(s) still queued; posting them every "
                f"{self.cfg['publish'].get('min_minutes_between_posts', 30)} min. Keep this window open "
                "(or run `publish install` once so posting continues in the background).")
            try:
                while pending_count(self.cfg) and self.thread.is_alive():
                    time.sleep(20)
            except KeyboardInterrupt:
                pass
        self.stop.set()


# ------------------------------------------------------------------------------ background poster
TASK = "stream-clipper-poster"


def _ps(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                          capture_output=True, text=True)


def task_installed() -> bool:
    if os.name != "nt":
        return False
    return _ps(f"Get-ScheduledTask -TaskName '{TASK}' -ErrorAction SilentlyContinue").stdout.strip() != ""


def start_task() -> None:
    if os.name == "nt":
        _ps(f"Start-ScheduledTask -TaskName '{TASK}'")


def install_task_script() -> str:
    import sys
    pyw = Path(sys.executable).with_name("pythonw.exe")
    launcher = ROOT / "tools" / "poster.pyw"
    return (
        f"$a = New-ScheduledTaskAction -Execute '{pyw}' -Argument '\"{launcher}\"' -WorkingDirectory '{ROOT}'\n"
        "$t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME\n"
        "$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 999 "
        "-RestartInterval (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew\n"
        f"Register-ScheduledTask -TaskName '{TASK}' -Action $a -Trigger $t -Settings $s "
        "-Description 'stream-clipper: posts queued clips to YouTube/Instagram at the configured interval' -Force | Out-Null\n"
        f"Start-ScheduledTask -TaskName '{TASK}'\n"
    )


def daemon(cfg: dict) -> None:
    """Run forever: post whatever is queued, one post per platform per interval. Used by the
    background task; also fine in a window (`publish daemon`)."""
    log("Background poster started: posting queued clips at the configured interval.")
    while True:
        try:
            from .util import load_config
            cfg = load_config()          # pick up config changes without a restart
            post_once(cfg)
        except Exception as e:
            log(f"publish: {type(e).__name__}: {e}")
        time.sleep(60)


# ------------------------------------------------------------------------------------------ CLI
def cli(args, cfg: dict) -> None:
    act = args.action
    args.files = list(getattr(args, "files", None) or [])
    if act == "setup":
        platform = args.files[0] if args.files else ""
        if platform not in PLATFORMS:
            raise SystemExit("[clipper] Usage: publish setup youtube --client-secret <file>  |  publish setup instagram")
        if platform == "youtube":
            if not args.client_secret:
                raise SystemExit("[clipper] Pass --client-secret path/to/client_secret.json (see README)")
            youtube_setup(args.client_secret)
        else:
            instagram_setup(cfg)
        return
    if act == "post":
        # Accept clip files, folders (every .mp4 inside) and wildcards (PowerShell doesn't expand them).
        import glob
        files: list[Path] = []
        for a in args.files:
            pa = Path(a)
            if pa.is_dir():
                files += sorted(pa.glob("*.mp4"))
            elif any(ch in a for ch in "*?["):
                files += [Path(x) for x in sorted(glob.glob(a))]
            else:
                files.append(pa)
        if not files:
            raise SystemExit("[clipper] No clips found to post.")
        for f in files:
            enqueue(f, cfg, approved=True)
        if other_poster_running(cfg) or task_installed():
            if not other_poster_running(cfg):
                start_task()
            log(f"Queued {len(files)} clip(s); the background poster posts them "
                f"(next: {', '.join(f'{k} {v}' for k, v in next_post_times(cfg).items())}).")
            return
        act = "run"
    if act == "approve":
        with _QLOCK:
            q = _load(cfg)
            n = 0
            for it in q["items"]:
                if not it.get("approved") and (args.files == ["all"] or any(s in it["file"] for s in args.files)):
                    it["approved"] = True
                    n += 1
            _save(cfg, q)
        log(f"Approved {n} clip(s).")
        return
    if act == "install":
        if os.name != "nt":
            raise SystemExit("[clipper] `publish install` sets up a Windows background task.")
        r = _ps(install_task_script())
        if r.returncode != 0:
            raise SystemExit("[clipper] Could not create the background task:\n" + r.stderr[-800:] +
                             "\nTry again from PowerShell opened with 'Run as administrator'.")
        log(f"Background poster installed and started (task '{TASK}'). It starts with Windows when you "
            "log in and posts queued clips every interval, stream or no stream. Log: work/publish/poster.log")
        return
    if act == "uninstall":
        _ps(f"Stop-ScheduledTask -TaskName '{TASK}' -ErrorAction SilentlyContinue; "
            f"Unregister-ScheduledTask -TaskName '{TASK}' -Confirm:$false -ErrorAction SilentlyContinue")
        log("Background poster removed.")
        return
    if act == "daemon":
        daemon(cfg)
        return
    if act == "run":
        log("Posting queued clips (Ctrl+C to stop)...")
        try:
            while pending_count(cfg):
                post_once(cfg)
                if pending_count(cfg):
                    time.sleep(20)
        except KeyboardInterrupt:
            pass
        act = "status"
    if act == "status":
        q = _load(cfg)
        plats = enabled_platforms(cfg)
        print(f"Platforms enabled: {', '.join(plats) or 'none (config.yaml -> publish)'}  "
              f"mode: {(cfg.get('publish') or {}).get('mode', 'auto')}")
        if "instagram" in plats and IG_FILE.exists():
            exp = _ig().get("expires_at", "")[:10]
            quota = instagram_quota(cfg)
            print(f"Instagram @{_ig().get('username')}: token valid until {exp}"
                  + (f", {quota[0]}/{quota[1]} API posts used in 24 h" if quota else ""))
        print(f"YouTube: {'connected' if YT_TOKEN.exists() else 'not connected'}")
        bg = "running" if other_poster_running(cfg) else ("installed, not running" if task_installed() else "not installed")
        print(f"Background poster: {bg}.  Queued: {pending_count(cfg)}.  Next post: "
              + ", ".join(f"{k} {v}" for k, v in next_post_times(cfg).items()))
        for it in q["items"][-30:]:
            st = "  ".join(f"{p}:{t.get('status')}" + (f" {t.get('url', '')}" if t.get("url") else "")
                           for p, t in it["targets"].items())
            flag = "" if it.get("approved") else "  [needs approval]"
            print(f"- {Path(it['file']).name}: {st}{flag}")
