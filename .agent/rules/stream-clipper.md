---
trigger: always_on
---

# stream-clipper project rules

- This project turns gaming stream VODs, live streams and local recordings into vertical (1080x1920) highlight clips with burned-in English captions and mood-matched background music. Python package: `clipper/`. Settings: `config.yaml`.
- VOD / local file: `analyze <url or path>` → `work/<id>/candidates.md` → you write `work/<id>/selection.json` → `render <id>` → `output/<id>/*.mp4`. Workflow: `/make-clips`.
- Live stream: `/live-clips <channel>` runs `python -m clipper live <url> --agent gemini` as a background command (never in the foreground; it runs for hours). Progress: `work/<session>/live/status.json`.
- When the user pastes a stream link or a video path and asks for clips or highlights, follow `.agent/workflows/make-clips.md`. You (the agent) do the clip ranking; do not add LLM API keys or calls to external LLM APIs.
- Headless agents: `--agent gemini` (Gemini CLI, Google sign-in) or `--agent claude` (Claude Code). `--agent none` = signals only.
- Run Python through `.venv/Scripts/python` (forward slashes work in PowerShell and Git Bash).
- The workflows in `.agent/workflows/` are generated from `claude-setup/` by `tools/sync_antigravity.py`: edit the Claude file, then re-run the script.
- Never commit or delete `work/`, `output/` or `music/` contents unless asked; they are large and user data.
- Downloaded music is Kevin MacLeod, CC BY 4.0: the credit in each clip's `.txt` must go in the post description.
- Posting: `--publish` on live/run/render posts clips to YouTube Shorts / Instagram Reels (config.yaml → publish). The user connects accounts with `publish setup youtube|instagram` in their own terminal; never ask for, read or print secrets, tokens or anything in `secrets/`.
- Agent order: `--agent auto` (default) tries `agents.priority` (Gemini CLI / Antigravity first, then Claude Code); every agent's selection is normalised by `clipper/agents.py:tidy_selection`. Posting: one post per platform every 30 min (`publish.min_minutes_between_posts`), all clips queued and retried until posted.
- Background poster: `publish install` (Windows task `stream-clipper-poster`, runs `tools/poster.pyw`) keeps posting the queue every 30 min after streams end; `publish status` shows it.
- Posted clips are deleted locally once live on every enabled platform (`publish.delete_after_post`); links in `work/publish/posted.csv`.
- English only: captions only for clearly English speech (no translation); titles/descriptions always English (`clipper/english.py`). Facecam: face-sized crop that tracks the face (`facecam.track_face`).
