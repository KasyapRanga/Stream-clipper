# stream-clipper

Gaming stream link → vertical (1080x1920) highlight clips with burned-in captions and background music.

- Package: `clipper/` (run as `.venv/Scripts/python -m clipper ...`). Settings: `config.yaml`.
- Pipeline: `analyze <url>` → `work/<id>/candidates.md` → you write `work/<id>/selection.json` → `render <id>` → `output/<id>/*.mp4`.
- When the user gives a stream link and wants clips, follow `/make-clips` (source: `claude-setup/make-clips.md`; `setup.ps1` copies it to `.claude/commands/`). You do the ranking yourself; never add LLM API keys or external LLM API calls.
- Live streams: `/live-clips <channel>` (source `claude-setup/live-clips.md`) starts `python -m clipper live <url> --agent claude` in the background; status in `work/<session>/live/status.json`.
- Fully automatic, headless: `.venv/Scripts/python -m clipper run "<url>" --agent claude` (calls `claude -p` to write selection.json).
- Use forward-slash paths (`.venv/Scripts/python`) so commands work in both Git Bash and PowerShell.
- Don't commit or delete `work/`, `output/` or `music/` contents unless asked.
- Downloaded music is Kevin MacLeod, CC BY 4.0: the credit in each clip's `.txt` goes in the post description.
- Antigravity gets the same commands in `.agent/workflows/` (headless agent there: `--agent gemini`, Gemini CLI). They are generated from `claude-setup/`: after changing a command, run `.venv/Scripts/python tools/sync_antigravity.py`.
- Posting: `--publish` on live/run/render posts clips to YouTube Shorts / Instagram Reels (config.yaml → publish). Connecting accounts (`publish setup youtube|instagram`) is done by the user in their own terminal; never ask for, read or print secrets/tokens or files in `secrets/`.
- Agent order: `--agent auto` (default) tries `agents.priority` (Gemini CLI / Antigravity first, then Claude Code); every agent's selection is normalised by `clipper/agents.py:tidy_selection`. Posting: one post per platform every 30 min (`publish.min_minutes_between_posts`), all clips queued and retried until posted.
- Background poster: `publish install` (Windows task `stream-clipper-poster`, runs `tools/poster.pyw`) keeps posting the queue every 30 min after streams end; `publish status` shows it.
- Posted clips are deleted locally once live on every enabled platform (`publish.delete_after_post`); links in `work/publish/posted.csv`.
- English only: captions only for clearly English speech (no translation); titles/descriptions always English (`clipper/english.py`). Facecam: face-sized crop that tracks the face (`facecam.track_face`).
