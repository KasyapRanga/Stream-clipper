"""Regenerate the Antigravity workflows from the Claude Code commands, so both harnesses behave the same.

    .venv/Scripts/python tools/sync_antigravity.py

claude-setup/<name>.md  ->  .agent/workflows/<name>.md
- frontmatter keeps only `description` (Antigravity workflows have no argument-hint / allowed-tools)
- "$ARGUMENTS" lines become "the link or path the user gave with the command"
- `--agent claude` becomes `--agent gemini` (Gemini CLI, Antigravity's headless counterpart)
- a `// turbo` line goes above each step that runs the project's own python, so Antigravity runs it
  without asking, like Claude Code's allowed-tools
Run it after every change to a file in claude-setup/.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC, DST = ROOT / "claude-setup", ROOT / ".agent" / "workflows"

NOTE = ("<!-- Generated from claude-setup/{name} by tools/sync_antigravity.py. "
        "Edit the Claude file and re-run the script instead of editing this one. -->\n\n")


def convert(name: str, text: str) -> str:
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    front, body = (m.group(1), text[m.end():]) if m else ("", text)
    desc = next((ln for ln in front.splitlines() if ln.startswith("description:")), "description: stream-clipper")
    body = re.sub(r"^.*\$ARGUMENTS.*\n\n?", "", body, flags=re.M)
    body = body.replace("given above", "given with the command")
    body = body.replace("--agent claude", "--agent gemini")
    body = body.replace("hands batches of moments to Claude Code (headless)",
                        "hands batches of moments to Gemini CLI (headless, your Google sign-in)")
    body = body.replace("(no Claude usage during the stream)", "(no agent usage during the stream)")
    body = body.replace("moments waiting for Claude", "moments waiting for the agent")
    body = body.replace("Do not run the live command in the foreground: it would block this conversation",
                        "Run the live command as a background terminal command (do not wait for it): it would "
                        "otherwise block the agent")
    body = body.replace("use a long timeout", "let it run until it finishes")
    # `// turbo` above numbered steps that run the project's python.
    out = []
    for block in re.split(r"(?m)^(?=\d+\. )", body):
        if re.match(r"\d+\. ", block) and ".venv/Scripts/python -m clipper" in block.split("\n\n")[0]:
            out.append("// turbo\n" + block)
        else:
            out.append(block)
    body = "".join(out)
    if "--agent gemini" in body or "--agent auto" in body:
        body += ("\nThe Gemini agent needs Gemini CLI (`npm install -g @google/gemini-cli`, then run `gemini` once "
                 "to sign in with Google). Check with `gemini --version`; if it is missing, `--agent auto` uses Claude Code instead.\n")
    return f"---\n{desc}\n---\n\n" + NOTE.format(name=name) + body.lstrip("\n")


def main() -> None:
    DST.mkdir(parents=True, exist_ok=True)
    for src in sorted(SRC.glob("*.md")):
        dst = DST / src.name
        dst.write_text(convert(src.name, src.read_text(encoding="utf-8")), encoding="utf-8")
        print(f"synced {src.relative_to(ROOT)} -> {dst.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
