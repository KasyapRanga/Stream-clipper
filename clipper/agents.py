"""Headless agents that pick, trim and title clips: Claude Code (`claude -p`) or Gemini CLI (`gemini -p`).

Both use your own sign-in (Claude account / Google account), never an API key. The judging rules are
the same for both: steps 4-6 of the /make-clips workflow (claude-setup/make-clips.md for Claude Code,
.agent/workflows/make-clips.md for Antigravity), so the two harnesses pick clips the same way.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from .util import ROOT, log, write_json

AGENTS = ("auto", "gemini", "claude", "none")
LABEL = {"claude": "Claude Code", "gemini": "Gemini CLI (Antigravity)", "auto": "the agent"}


def label(agent: str) -> str:
    return LABEL.get(agent, agent)


def _rubric(workflow: Path) -> str:
    """Steps 4-6 (choose, trim, selection.json schema) of a make-clips workflow file."""
    text = workflow.read_text(encoding="utf-8")
    text = "\n".join(ln for ln in text.splitlines() if not ln.startswith(("//", "<!--")))
    a, b = text.find("4. "), text.find("7. ")
    return text[a:b].strip() if a >= 0 and b > a else text


def _run_claude(rel_dir: str, abs_dir: Path, extra: str) -> bool:
    exe = shutil.which("claude")
    if not exe:
        log("Claude Code CLI ('claude') not found on PATH.")
        return False
    prompt = (
        f"Read {rel_dir}/candidates.md. Using the selection rubric, trimming rules and JSON schema in "
        f"claude-setup/make-clips.md (steps 4-6), make a clip for every real highlight (no fixed number) and "
        f"write {rel_dir}/selection.json. {extra}Do not run any commands; only read files and write that one file."
    )
    proc = subprocess.run([exe, "-p", prompt, "--allowedTools", "Read,Write"], cwd=ROOT)
    return proc.returncode == 0 and (abs_dir / "selection.json").exists()


def _extract_selection(text: str) -> dict | None:
    """The JSON object with a "clips" list in the agent's reply (fenced or bare)."""
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    first = text.find("{")
    if first >= 0:
        blocks.append(text[first: text.rfind("}") + 1])
    for blk in blocks:
        try:
            data = json.loads(blk)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("clips"), list):
            return data
    return None


def _run_gemini(abs_dir: Path, cfg: dict, extra: str) -> bool:
    """Gemini CLI headless. Everything goes in on stdin and the JSON comes back on stdout, so the
    agent needs no file or shell permissions; this program writes selection.json itself."""
    exe = shutil.which("gemini")
    if not exe:
        log("Gemini CLI ('gemini') not found on PATH. Install it with:  npm install -g @google/gemini-cli"
            "  then run 'gemini' once to sign in with your Google account.")
        return False
    rubric = _rubric(ROOT / ".agent" / "workflows" / "make-clips.md")
    candidates = (abs_dir / "candidates.md").read_text(encoding="utf-8")
    stdin = (
        "You are the highlight editor for stream-clipper. Follow these rules to choose, trim and title "
        "clips from the candidates below.\n\n" + rubric + "\n\n" + extra +
        "\nReply with ONLY the selection JSON object (the shape in step 6), no other text.\n\n"
        "=== candidates.md ===\n" + candidates
    )
    cmd = [exe]
    model = (cfg.get("agents") or {}).get("gemini_model")
    if model:
        cmd += ["-m", str(model)]
    # Keep the -p text free of quotes and shell symbols: on Windows 'gemini' is a .cmd shim.
    cmd += ["-p", "Follow the instructions in the input and reply with only the JSON object"]
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=ROOT, timeout=900)
    except subprocess.TimeoutExpired:
        log("Gemini CLI took longer than 15 minutes.")
        return False
    data = _extract_selection(proc.stdout or "")
    if data is None:
        tail = (proc.stderr or proc.stdout or "").strip()[-300:]
        log(f"Gemini CLI did not return a selection (exit {proc.returncode}). {tail}")
        return False
    write_json(abs_dir / "selection.json", data)
    return True


MOODS = {"hype", "funny", "tense", "chill", "none"}


def tidy_selection(abs_dir: Path, cfg: dict) -> int:
    """Hold every agent to the same output rules, so Gemini and Claude produce the same kind of clips:
    known candidate ids, 15-60 s (config), inside the transcribed window, a short hook title, a valid
    music mood. Fixes what it can, drops what it can't. Returns the number of clips kept."""
    from .util import read_json
    sel_p, cand_p = abs_dir / "selection.json", abs_dir / "candidates.json"
    data = read_json(sel_p)
    cands = {c["id"]: c for c in read_json(cand_p).get("candidates", [])} if cand_p.exists() else {}
    hc = cfg["highlights"]
    lo, hi = float(hc.get("min_clip", 12)), float(hc.get("max_clip", 60))
    kept, seen = [], set()
    for c in data.get("clips", []):
        cid = str(c.get("candidate_id", ""))
        cand = cands.get(cid)
        if cands and not cand:
            continue  # an id the agent made up
        try:
            s, e = float(c["start"]), float(c["end"])
        except (KeyError, TypeError, ValueError):
            if not cand:
                continue
            s, e = cand["start"], cand["end"]
        if cand:  # stay inside what was recorded/transcribed for this moment (with a little slack)
            s = max(s, float(cand.get("tx_start", s)) - 2)
            e = min(e, float(cand.get("tx_end", e)) + 2)
        if e - s > hi:
            e = s + hi
        if e - s < lo:
            e = s + lo
        if (cid, round(s)) in seen or e <= s:
            continue
        seen.add((cid, round(s)))
        mood = str(c.get("music_mood", "none")).lower().strip()
        from .english import clean_title
        title = clean_title(str(c.get("title", "")), mood if mood in MOODS else "none", seed=len(kept))
        kept.append({"candidate_id": cid, "start": round(s, 2), "end": round(e, 2), "title": title.upper(),
                     "music_mood": mood if mood in MOODS else "none", "reason": str(c.get("reason", ""))[:300]})
    write_json(sel_p, {"clips": kept})
    return len(kept)


def _order(agent: str, cfg: dict) -> list[str]:
    if agent == "auto":
        pri = (cfg.get("agents") or {}).get("priority") or ["gemini", "claude"]
        return [a for a in pri if a in ("gemini", "claude")]
    return [agent] if agent in ("gemini", "claude") else []


def agent_select_dir(agent: str, rel_dir: str, abs_dir: Path, cfg: dict, extra: str = "") -> bool:
    """Have an agent write <dir>/selection.json from <dir>/candidates.md. With agent='auto' the agents
    are tried in config `agents.priority` order (Antigravity's Gemini first by default), each one
    falling back to the next. False -> caller falls back to signal-based picks."""
    for a in _order(agent, cfg):
        if not shutil.which("gemini" if a == "gemini" else "claude"):
            log(f"{label(a)} is not installed; trying the next agent.")
            continue
        (abs_dir / "selection.json").unlink(missing_ok=True)
        log(f"Asking {label(a)} to pick the highlights...")
        ok = _run_claude(rel_dir, abs_dir, extra) if a == "claude" else _run_gemini(abs_dir, cfg, extra)
        if ok:
            try:
                n = tidy_selection(abs_dir, cfg)
            except Exception as e:  # malformed output counts as a failure
                log(f"{label(a)} returned an unusable selection ({type(e).__name__}).")
                continue
            log(f"{label(a)} picked {n} clip(s).")
            return True
        log(f"{label(a)} did not pick clips.")
    if _order(agent, cfg):
        log("No agent could pick clips; using signal-based selection.")
    return False
