"""Shared helpers: config, paths, ffmpeg, logging."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def log(msg: str) -> None:
    print(f"[clipper] {msg}", flush=True)


def load_config(path: str | os.PathLike | None = None) -> dict:
    p = Path(path) if path else ROOT / "config.yaml"
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for key in ("work", "output", "music"):
        v = Path(cfg["paths"][key])
        cfg["paths"][key] = v if v.is_absolute() else ROOT / v
    return cfg


def work_dir(cfg: dict, vid: str) -> Path:
    d = Path(cfg["paths"]["work"]) / vid
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def slugify(text: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-").lower()
    return s[:maxlen].strip("-") or "clip"


def hms(t: float) -> str:
    t = max(0, int(round(t)))
    return f"{t // 3600:d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def require_tool(name: str) -> str:
    exe = shutil.which(name)
    if not exe:
        hint = {
            "ffmpeg": "Install it with:  winget install Gyan.FFmpeg   (then reopen the terminal)",
            "ffprobe": "It ships with ffmpeg:  winget install Gyan.FFmpeg",
        }.get(name, "")
        sys.exit(f"[clipper] '{name}' was not found on PATH. {hint}")
    return exe


def run(cmd: list[str], cwd: Path | None = None, quiet: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0 and not quiet:
        log("command failed: " + " ".join(cmd))
        log(proc.stderr[-3000:])
    return proc


def probe_duration(path: Path) -> float:
    require_tool("ffprobe")
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", str(path)])
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def probe_size(path: Path) -> tuple[int, int]:
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
               "stream=width,height", "-of", "csv=p=0", str(path)])
    try:
        w, h = out.stdout.strip().split(",")[:2]
        return int(w), int(h)
    except ValueError:
        return 1920, 1080


def probe_fps(path: Path) -> float:
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
               "stream=avg_frame_rate", "-of", "csv=p=0", str(path)])
    try:
        n, d = out.stdout.strip().split("/")
        fps = float(n) / float(d)
        return round(fps) if fps > 1 else 30.0
    except (ValueError, ZeroDivisionError):
        return 30.0


def has_stream(path: Path, kind: str) -> bool:
    out = run(["ffprobe", "-v", "error", "-select_streams", kind[0],
               "-show_entries", "stream=index", "-of", "csv=p=0", str(path)])
    return bool(out.stdout.strip())


def add_nvidia_dll_dirs() -> None:
    """On Windows, pip-installed CUDA libs (nvidia-cublas-cu12, nvidia-cudnn-cu12) live in
    site-packages/nvidia/*/bin, which is not on the DLL search path by default."""
    if os.name != "nt":
        return
    import glob
    import site
    roots = list(site.getsitepackages()) + [site.getusersitepackages()]
    for sp in roots:
        for d in glob.glob(os.path.join(sp, "nvidia", "*", "bin")):
            try:
                os.add_dll_directory(d)
            except (OSError, AttributeError):
                pass
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
