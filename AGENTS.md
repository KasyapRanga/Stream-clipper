# Agent guide: stream-clipper

Gaming stream link or local recording → captioned 9:16 highlight clips with music.

- VOD or local video → follow `.agent/workflows/make-clips.md` (`/make-clips <link or path>`):
  1. `.venv/Scripts/python -m clipper analyze "<url or path>"` (prints `STREAM_ID=<id>`)
  2. Read `work/<id>/candidates.md`, make a clip for EVERY real highlight (no fixed number), write `work/<id>/selection.json` (rubric and schema in the workflow).
  3. `.venv/Scripts/python -m clipper render <id>`
- Live stream → `.agent/workflows/live-clips.md` (`/live-clips <channel>`): starts `clipper live <url> --agent gemini` in the background.
- You do the ranking yourself; no LLM API keys. Headless runs use Gemini CLI (`--agent gemini`) or Claude Code (`--agent claude`) with the user's own sign-in.
- The Claude Code commands in `claude-setup/` are the source of these workflows: change those, then run `tools/sync_antigravity.py`.
- Project rules: `.agent/rules/stream-clipper.md`.
- Posting: `--publish` on live/run/render posts clips to YouTube Shorts / Instagram Reels (config.yaml → publish). The user connects accounts with `publish setup youtube|instagram` in their own terminal; never ask for, read or print secrets, tokens or anything in `secrets/`.
- Agent order: `--agent auto` (default) tries `agents.priority` (Gemini CLI / Antigravity first, then Claude Code); every agent's selection is normalised by `clipper/agents.py:tidy_selection`. Posting: one post per platform every 30 min (`publish.min_minutes_between_posts`), all clips queued and retried until posted.
- Background poster: `publish install` (Windows task `stream-clipper-poster`, runs `tools/poster.pyw`) keeps posting the queue every 30 min after streams end; `publish status` shows it.
- Posted clips are deleted locally once live on every enabled platform (`publish.delete_after_post`); links in `work/publish/posted.csv`.
- English only: captions only for clearly English speech (no translation); titles/descriptions always English (`clipper/english.py`). Facecam: face-sized crop that tracks the face (`facecam.track_face`).
